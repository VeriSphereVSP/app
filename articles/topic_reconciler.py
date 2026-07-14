#!/usr/bin/env python3
"""topic_reconciler.py — heal #0 for the claim<->topic m2m model (in-DB relevance).

Scope (Phase 1 heal): derive associations over the CURRENT corpus, seeded by the
existing topic set. Populates `topic` and `claim_topic`. NO Phase 3 work — no
speciation, topic_edge/lineage, continuity mapping, or clustering discovery.

COST: relevance is computed IN-DB via pgvector (claim's stored embedding <=> topic
centroid). The ONLY embedding API calls are O(topics) article-summary embeds at
seed time (cached: skipped when a centroid already exists) — never O(claims*topics).
Claim vectors are read from chain_claim_text.embedding (already stored at ingest).

SAFETY: writes only to the dormant m2m tables (nothing reads them until Phase 2);
fully reversible (`--wipe`). Dry-run runs the real logic in a transaction and
ROLLS BACK — accurate counts, zero persistence.

Association sources at heal #0: detected (claim.topic label -> primary seed),
relevant (claim vec within threshold of a topic centroid), nearest (fallback so no
claim is topicless). snapped/inherited come online in Phase 3.

CLI:
  python -m articles.topic_reconciler heal --dry-run   # plan only (tx rollback)
  python -m articles.topic_reconciler heal             # apply
  python -m articles.topic_reconciler heal --wipe      # truncate + re-derive
  python -m articles.topic_reconciler check            # invariants only
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

RELEVANCE_THRESHOLD = float(os.getenv("VSP_TOPIC_RELEVANCE_THRESHOLD", "0.5"))


@dataclass
class HealReport:
    topics_seeded: int = 0
    summary_embeds: int = 0
    associations: int = 0
    primaries: int = 0
    nearest_fallbacks: int = 0
    claims_seen: int = 0
    invariant_failures: list[str] = field(default_factory=list)
    dry_run: bool = True

    def ok(self) -> bool:
        return not self.invariant_failures

    def summary(self) -> str:
        head = "DRY-RUN (tx rolled back, no writes)" if self.dry_run else "APPLIED"
        lines = [
            f"[reconciler] {head}",
            f"  claims seen         : {self.claims_seen}",
            f"  topics seeded       : {self.topics_seeded}",
            f"  summary embeds (API): {self.summary_embeds}   (O(topics), cached)",
            f"  associations        : {self.associations}",
            f"  primaries elected   : {self.primaries}",
            f"  nearest fallbacks   : {self.nearest_fallbacks}",
        ]
        lines.append("  INVARIANT FAILURES:" if self.invariant_failures else "  invariants          : all pass")
        lines += [f"    - {f}" for f in self.invariant_failures]
        return "\n".join(lines)


# ── pure decision logic (unit-tested, unchanged) ────────────────────────────
def elect_primary(assocs: list[dict]) -> Optional[int]:
    if not assocs:
        return None
    detected = [a for a in assocs if a["source"] == "detected"]
    if detected:
        return min(a["topic_id"] for a in detected)
    scored = [a for a in assocs if a.get("relevance") is not None]
    if scored:
        return max(scored, key=lambda a: (a["relevance"], -a["topic_id"]))["topic_id"]
    return min(a["topic_id"] for a in assocs)


def _norm(s: str) -> str:
    return (s or "").strip()


def _vec_literal(v) -> str:
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


# ── heal ────────────────────────────────────────────────────────────────────
def heal(db: Session, dry_run: bool = True, wipe: bool = False) -> HealReport:
    from articles.claim_indexer import _article_summary_embedding
    rep = HealReport(dry_run=dry_run)
    try:
        if wipe:
            db.execute(sql_text("TRUNCATE claim_topic;"))
            db.execute(sql_text("TRUNCATE topic_edge;"))
            db.execute(sql_text("TRUNCATE topic RESTART IDENTITY CASCADE;"))

        # 1. seed topics = topic_article labels ∪ distinct claim.topic
        seed = set()
        for (lbl,) in db.execute(sql_text(
            "SELECT DISTINCT COALESCE(NULLIF(btrim(title),''), topic_key) FROM topic_article")):
            if _norm(lbl):
                seed.add(_norm(lbl))
        for (lbl,) in db.execute(sql_text(
            "SELECT DISTINCT topic FROM claim WHERE topic IS NOT NULL AND btrim(topic) <> ''")):
            seed.add(_norm(lbl))

        label_to_id: dict[str, int] = {}
        for tid, lbl in db.execute(sql_text("SELECT topic_id, label FROM topic")):
            label_to_id[_norm(lbl).lower()] = tid
        for lbl in sorted(seed):
            if lbl.lower() in label_to_id:
                continue
            tid = db.execute(sql_text(
                "INSERT INTO topic(label) VALUES (:l) RETURNING topic_id"), {"l": lbl}).scalar()
            label_to_id[lbl.lower()] = tid
            rep.topics_seeded += 1

        # 2. seed centroids from article summaries — ONCE per topic (cached: skip if set).
        #    O(topics) API embeds, NOT O(claims*topics).
        art_rows = db.execute(sql_text(
            "SELECT article_id, topic_key, title FROM topic_article")).fetchall()
        for aid, tkey, title in art_rows:
            tid = None
            for cand in (title, tkey):
                if _norm(cand):
                    tid = label_to_id.get(_norm(cand).lower())
                    if tid:
                        break
            if not tid:
                continue
            has_centroid = db.execute(sql_text(
                "SELECT centroid IS NOT NULL FROM topic WHERE topic_id=:t"), {"t": tid}).scalar()
            if has_centroid:
                continue
            vec = _article_summary_embedding(db, aid)
            if vec is None:
                continue
            rep.summary_embeds += 1
            db.execute(sql_text(
                "UPDATE topic SET centroid = (:v)::vector, updated_at=now() WHERE topic_id=:t"),
                {"v": _vec_literal(vec), "t": tid})

        # 3. detected associations (from claim.topic label) — needed before member centroids
        claims = db.execute(sql_text(
            "SELECT c.claim_id, c.claim_text, c.topic FROM claim c "
            "WHERE EXISTS (SELECT 1 FROM chain_claim_text x WHERE x.claim_text=c.claim_text)"
        )).fetchall()
        rep.claims_seen = len(claims)
        detected_pairs = []
        for claim_id, claim_text, lbl in claims:
            if _norm(lbl) and _norm(lbl).lower() in label_to_id:
                tid = label_to_id[_norm(lbl).lower()]
                db.execute(sql_text(
                    "INSERT INTO claim_topic(claim_id,topic_id,is_primary,source) "
                    "VALUES (:c,:t,false,'detected') ON CONFLICT DO NOTHING"),
                    {"c": claim_id, "t": tid})
                detected_pairs.append((claim_id, tid))

        # 4. member-based centroids override summary seeds where members exist (in-DB, no API)
        db.execute(sql_text(
            "UPDATE topic t SET centroid = sub.c, updated_at=now() FROM ("
            "  SELECT ct.topic_id, AVG(x.embedding) AS c FROM claim_topic ct "
            "  JOIN claim cl ON cl.claim_id=ct.claim_id "
            "  JOIN chain_claim_text x ON x.claim_text=cl.claim_text "
            "  WHERE x.embedding IS NOT NULL GROUP BY ct.topic_id) sub "
            "WHERE t.topic_id=sub.topic_id"))

        # 5. relevant associations — SINGLE in-DB query, claim vec <=> topic centroid.
        #    Zero API calls. Cosine sim = 1 - (<=>). Excludes self (already detected).
        db.execute(sql_text(
            "INSERT INTO claim_topic(claim_id,topic_id,is_primary,source,relevance) "
            "SELECT c.claim_id, t.topic_id, false, 'relevant', "
            "       (1 - (t.centroid <=> x.embedding))::real "
            "FROM claim c "
            "JOIN chain_claim_text x ON x.claim_text=c.claim_text AND x.embedding IS NOT NULL "
            "JOIN topic t ON t.centroid IS NOT NULL "
            "WHERE (1 - (t.centroid <=> x.embedding)) >= :thr "
            "  AND EXISTS (SELECT 1 FROM chain_claim_text z WHERE z.claim_text=c.claim_text) "
            "ON CONFLICT (claim_id, topic_id) DO NOTHING"),
            {"thr": RELEVANCE_THRESHOLD})

        # 6. nearest-root fallback for any still-orphaned claim (in-DB, no API)
        orphans = db.execute(sql_text(
            "SELECT c.claim_id, c.claim_text FROM claim c "
            "WHERE EXISTS (SELECT 1 FROM chain_claim_text x WHERE x.claim_text=c.claim_text) "
            "  AND NOT EXISTS (SELECT 1 FROM claim_topic ct WHERE ct.claim_id=c.claim_id)"
        )).fetchall()
        for claim_id, claim_text in orphans:
            tid = db.execute(sql_text(
                "SELECT t.topic_id FROM topic t, chain_claim_text x "
                "JOIN claim c ON c.claim_text=x.claim_text "
                "WHERE c.claim_text=:ct AND t.centroid IS NOT NULL AND x.embedding IS NOT NULL "
                "ORDER BY (t.centroid <=> x.embedding) ASC LIMIT 1"), {"ct": claim_text}).scalar()
            if tid:
                db.execute(sql_text(
                    "INSERT INTO claim_topic(claim_id,topic_id,is_primary,source) "
                    "VALUES (:c,:t,false,'detected') ON CONFLICT DO NOTHING"),
                    {"c": claim_id, "t": tid})
                rep.nearest_fallbacks += 1

        # 7. elect exactly one primary per claim (detected > highest relevance > lowest id)
        rows = db.execute(sql_text(
            "SELECT claim_id, topic_id, source, relevance FROM claim_topic")).fetchall()
        by_claim: dict[int, list[dict]] = {}
        for cid, tid, src, rel in rows:
            by_claim.setdefault(cid, []).append(
                {"topic_id": tid, "source": src, "relevance": rel})
        for cid, assocs in by_claim.items():
            pid = elect_primary(assocs)
            db.execute(sql_text("UPDATE claim_topic SET is_primary=false WHERE claim_id=:c"), {"c": cid})
            db.execute(sql_text(
                "UPDATE claim_topic SET is_primary=true WHERE claim_id=:c AND topic_id=:t"),
                {"c": cid, "t": pid})
            rep.primaries += 1

        rep.associations = db.execute(sql_text("SELECT count(*) FROM claim_topic")).scalar()
        rep.invariant_failures = check_invariants(db)

        if dry_run:
            db.rollback()
        else:
            db.commit()
    except Exception:
        db.rollback()
        raise
    return rep


def check_invariants(db: Session) -> list[str]:
    fails: list[str] = []
    n = db.execute(sql_text(
        "SELECT count(*) FROM claim c "
        "WHERE EXISTS (SELECT 1 FROM chain_claim_text x WHERE x.claim_text=c.claim_text) "
        "  AND NOT EXISTS (SELECT 1 FROM claim_topic ct WHERE ct.claim_id=c.claim_id)")).scalar()
    if n:
        fails.append(f"I1: {n} on-chain claim(s) with zero associations")
    n = db.execute(sql_text(
        "SELECT count(*) FROM (SELECT claim_id FROM claim_topic GROUP BY claim_id "
        "HAVING count(*) FILTER (WHERE is_primary) <> 1) q")).scalar()
    if n:
        fails.append(f"I2: {n} claim(s) without exactly one primary")
    n = db.execute(sql_text(
        "SELECT count(*) FROM claim_topic "
        "WHERE source NOT IN ('detected','snapped','relevant','inherited')")).scalar()
    if n:
        fails.append(f"I5: {n} association(s) with invalid source")
    n = db.execute(sql_text("SELECT count(*) FROM topic_edge WHERE child_id=parent_id")).scalar()
    if n:
        fails.append(f"I4: {n} self-edge(s)")
    return fails


def _main(argv: list[str]) -> int:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from config import DATABASE_URL
    cmd = argv[1] if len(argv) > 1 else "heal"
    dry = "--dry-run" in argv or cmd == "check"
    wipe = "--wipe" in argv
    with sessionmaker(bind=create_engine(DATABASE_URL, future=True))() as db:
        if cmd == "check":
            fails = check_invariants(db)
            print("\n".join(fails) if fails else "[reconciler] invariants: all pass")
            return 1 if fails else 0
        rep = heal(db, dry_run=dry, wipe=wipe)
        print(rep.summary())
        return 0 if rep.ok() else 8


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(_main(sys.argv))


# ── per-claim association (Build A: called at ingest so every claim has >=1 topic) ──
def associate_one(db: Session, claim_id: int) -> None:
    """Ensure a single claim has >=1 claim_topic association, idempotently. Used by
    the derived-state worker at ingest so a new claim gets its topics immediately,
    not only after a manual heal. detected (claim.topic, seeded if new) + relevant
    (in-DB centroid <=>) + nearest fallback; exactly one primary. All in-DB except
    the topic seed which reuses the claim's already-stored embedding (no API)."""
    row = db.execute(sql_text(
        "SELECT claim_text, topic FROM claim WHERE claim_id=:c"), {"c": claim_id}).fetchone()
    if not row:
        return
    detected = _norm(row[1])
    if detected:
        tid = db.execute(sql_text(
            "SELECT topic_id FROM topic WHERE lower(label)=lower(:l) AND status='active' LIMIT 1"),
            {"l": detected}).scalar()
        if tid is None:
            tid = db.execute(sql_text(
                "INSERT INTO topic(label) VALUES (:l) RETURNING topic_id"), {"l": detected}).scalar()
        db.execute(sql_text(
            "INSERT INTO claim_topic(claim_id,topic_id,is_primary,source) "
            "VALUES (:c,:t,false,'detected') ON CONFLICT DO NOTHING"), {"c": claim_id, "t": tid})
        # seed a brand-new topic's centroid from this claim's stored embedding (no API)
        db.execute(sql_text(
            "UPDATE topic t SET centroid=x.embedding, updated_at=now() "
            "FROM chain_claim_text x JOIN claim c ON c.claim_text=x.claim_text "
            "WHERE t.topic_id=:t AND t.centroid IS NULL AND c.claim_id=:c AND x.embedding IS NOT NULL"),
            {"t": tid, "c": claim_id})
    # relevant via in-DB centroid
    db.execute(sql_text(
        "INSERT INTO claim_topic(claim_id,topic_id,is_primary,source,relevance) "
        "SELECT :c, t.topic_id, false, 'relevant', (1-(t.centroid<=>x.embedding))::real "
        "FROM chain_claim_text x JOIN claim c ON c.claim_text=x.claim_text "
        "JOIN topic t ON t.centroid IS NOT NULL AND t.status='active' "
        "WHERE c.claim_id=:c AND x.embedding IS NOT NULL "
        "  AND (1-(t.centroid<=>x.embedding)) >= :thr ON CONFLICT DO NOTHING"),
        {"c": claim_id, "thr": RELEVANCE_THRESHOLD})
    # nearest fallback -> guarantees >=1
    if not db.execute(sql_text("SELECT 1 FROM claim_topic WHERE claim_id=:c LIMIT 1"),
                      {"c": claim_id}).scalar():
        tid = db.execute(sql_text(
            "SELECT t.topic_id FROM topic t, chain_claim_text x "
            "JOIN claim c ON c.claim_text=x.claim_text "
            "WHERE c.claim_id=:c AND t.centroid IS NOT NULL AND x.embedding IS NOT NULL "
            "  AND t.status='active' ORDER BY (t.centroid<=>x.embedding) ASC LIMIT 1"),
            {"c": claim_id}).scalar()
        if tid:
            db.execute(sql_text(
                "INSERT INTO claim_topic(claim_id,topic_id,is_primary,source) "
                "VALUES (:c,:t,false,'detected') ON CONFLICT DO NOTHING"), {"c": claim_id, "t": tid})
    # elect exactly one primary
    rows = db.execute(sql_text(
        "SELECT topic_id, source, relevance FROM claim_topic WHERE claim_id=:c"), {"c": claim_id}).fetchall()
    if rows:
        pid = elect_primary([{"topic_id": t, "source": s, "relevance": r} for t, s, r in rows])
        db.execute(sql_text("UPDATE claim_topic SET is_primary=false WHERE claim_id=:c"), {"c": claim_id})
        db.execute(sql_text(
            "UPDATE claim_topic SET is_primary=true WHERE claim_id=:c AND topic_id=:t"),
            {"c": claim_id, "t": pid})
