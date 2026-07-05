"""
unified_grouping_db.py — DB adapters that feed the pure `unified_grouping` module.

Provides:
  - load_items_for_topic(db, topic_key)      -> [Item]   (claims + sentences)
  - make_similar()                            -> similar callable (cosine)
  - make_equivalent(db)                       -> equivalent callable (2-tier cache)
  - group_topic(db, topic_key)                -> [Group]  (convenience: load+group)

Keeps ALL DB/network here; unified_grouping itself stays pure. The equivalence
verifier is two-tier:
  - claim<->claim (both have post_id): existing claim_dupe_verdict cache +
    the existing dupe_groups._llm_verify_equivalent (no behavior change).
  - any pair involving a sentence: content-hash cache (dupe_verdict_text,
    migration 280) keyed by sha256(normalized text).
Both call the same LLM (llm_provider.complete) on a cache miss.
"""

from __future__ import annotations

import hashlib
import logging
from typing import List, Sequence

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from unified_grouping import Item, Group, group_items, KIND_CLAIM, KIND_SENTENCE

logger = logging.getLogger(__name__)


# ── Item loading ─────────────────────────────────────────────────────────────
def load_items_for_topic(db: Session, topic_key: str) -> List[Item]:
    """Build Items for a topic: on-chain claims (with stake/VS) + article sentences."""
    items: List[Item] = []

    # Claims tagged with this topic. claim.post_id is NULL in practice, so the
    # bridge to on-chain identity is claim.claim_text = chain_claim_text.claim_text.
    # From chain_claim_text we get post_id (on-chain id) + the embedding; from
    # chain_post we get stake/VS. Topic values are stored title-cased, so match
    # case-insensitively.
    claim_rows = db.execute(sql_text(
        "SELECT ct.post_id, c.claim_text, ct.embedding::text, "
        "       p.support_total, p.challenge_total, p.effective_vs "
        "FROM claim c "
        "JOIN chain_claim_text ct ON ct.claim_text = c.claim_text "
        "JOIN chain_post p ON p.post_id = ct.post_id "
        "WHERE LOWER(c.topic) = :t"
    ), {"t": topic_key.lower()}).fetchall()

    for pid, ctext, emb_txt, support, challenge, vs in claim_rows:
        it = Item(
            kind=KIND_CLAIM, id=int(pid), text=ctext or "",
            embedding=_parse_pgvector(emb_txt),
            stake=float(support or 0.0) + float(challenge or 0.0),
            vs=float(vs or 0.0),
            post_id=int(pid),
        )
        # keep the support/challenge split for claim_dupe_group aggregation
        it._support = float(support or 0.0)
        it._challenge = float(challenge or 0.0)
        items.append(it)

    # Sentences for this topic's article (include hidden=FALSE only for display;
    # hidden rows are handled by the store's own dedup, not shown here).
    sent_rows = db.execute(sql_text(
        "SELECT s.sentence_id, s.text, s.post_id, s.embedding::text "
        "FROM article_sentence s "
        "JOIN article_section sec ON s.section_id = sec.section_id "
        "JOIN topic_article a ON sec.article_id = a.article_id "
        "WHERE a.topic_key = :t AND s.is_hidden = FALSE"
    ), {"t": topic_key}).fetchall()

    for sid, stext, spost, semb in sent_rows:
        items.append(Item(
            kind=KIND_SENTENCE, id=int(sid), text=stext or "",
            embedding=_parse_pgvector(semb),
            post_id=int(spost) if spost is not None else None,
        ))

    return items


def _parse_pgvector(v):
    """pgvector ::text looks like '[0.1,0.2,...]'. Return list[float] or None."""
    if not v:
        return None
    try:
        s = v.strip()
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1]
        return [float(x) for x in s.split(",")] if s else None
    except Exception:
        return None


# ── Similarity (injected as `similar`) ───────────────────────────────────────
def make_similar():
    from similarity import cosine_similarity

    def similar(a: Sequence[float], b: Sequence[float]) -> float:
        return cosine_similarity(a, b)

    return similar


# ── Equivalence (injected as `equivalent`, two-tier cached) ──────────────────
def _norm(t: str) -> str:
    return (t or "").lower().strip()


def _texthash(t: str) -> str:
    return hashlib.sha256(_norm(t).encode("utf-8")).hexdigest()


def make_equivalent(db: Session):
    """Return an `equivalent(a, b, sim) -> bool` that caches verdicts.

    claim<->claim: reuse dupe_groups._llm_verify_equivalent (post-keyed cache).
    sentence-involving: content-hash cache in dupe_verdict_text.
    """
    def equivalent(a: Item, b: Item, sim: float) -> bool:
        # both claims with post ids -> existing post-keyed path (no behavior change)
        if a.is_claim and b.is_claim and a.post_id is not None and b.post_id is not None:
            try:
                from dupe_groups import _llm_verify_equivalent
                return _llm_verify_equivalent(db, a.text, b.text, a.post_id, b.post_id, sim)
            except Exception as e:
                logger.warning("claim verify failed (%s,%s): %s", a.id, b.id, e)
                return False

        # otherwise content-hash cache
        ha, hb = _texthash(a.text), _texthash(b.text)
        if ha == hb:
            return True  # identical normalized text
        if hb < ha:
            ha, hb = hb, ha
        # cache read
        try:
            row = db.execute(sql_text(
                "SELECT verdict FROM dupe_verdict_text WHERE hash_a = :a AND hash_b = :b"
            ), {"a": ha, "b": hb}).fetchone()
            if row:
                return row[0] == "EQUIVALENT"
        except Exception as e:
            logger.debug("text-verdict cache read failed: %s", e)

        # miss -> LLM
        verdict = _llm_text_verdict(a.text, b.text, sim)
        if verdict is None:
            return False  # conservative; don't cache failures
        try:
            from llm_provider import MODEL as LLM_MODEL
            db.execute(sql_text(
                "INSERT INTO dupe_verdict_text (hash_a, hash_b, verdict, similarity, model) "
                "VALUES (:a, :b, :v, :s, :m) ON CONFLICT (hash_a, hash_b) DO NOTHING"
            ), {"a": ha, "b": hb, "v": verdict, "s": float(sim), "m": LLM_MODEL})
            db.commit()
        except Exception as e:
            logger.debug("text-verdict cache write failed: %s", e)
            try:
                db.rollback()
            except Exception:
                pass
        return verdict == "EQUIVALENT"

    return equivalent


def _llm_text_verdict(text_a: str, text_b: str, sim: float):
    """One LLM equivalence call for a sentence-involving pair. Returns
    'EQUIVALENT'|'DIFFERENT' or None on failure."""
    try:
        from llm_provider import complete
    except Exception as e:
        logger.warning("LLM provider unavailable for text verify: %s", e)
        return None
    system = (
        "You decide whether two short pieces of text express the same "
        "proposition. They are EQUIVALENT if they make the same factual "
        "assertion — same subject, predicate, truth conditions — even if worded "
        "differently. They are DIFFERENT if either could be true while the other "
        "is false. Answer with exactly one word: EQUIVALENT or DIFFERENT."
    )
    prompt = f"Text A: {text_a!r}\nText B: {text_b!r}\n\nAnswer (one word):"
    try:
        raw = complete(prompt, system=system, max_tokens=10, temperature=0.0)
        token = (raw or "").strip().upper().split()[0] if raw else ""
        if token.startswith("EQUIV"):
            return "EQUIVALENT"
        if token.startswith("DIFFER"):
            return "DIFFERENT"
        logger.warning("text verify unparseable: %r (sim=%.3f) -> DIFFERENT", raw, sim)
        return "DIFFERENT"
    except Exception as e:
        logger.warning("text verify call failed: %s", e)
        return None


# ── Convenience ──────────────────────────────────────────────────────────────
def group_topic(db: Session, topic_key: str) -> List[Group]:
    items = load_items_for_topic(db, topic_key)
    return group_items(items, similar=make_similar(), equivalent=make_equivalent(db))


def load_claims_for_topic(db: Session, topic_key: str) -> List[Item]:
    """Claims only (no sentences) for a topic — for claim_dupe_group grouping."""
    return [it for it in load_items_for_topic(db, topic_key) if it.kind == KIND_CLAIM]


def group_claims_for_topic(db: Session, topic_key: str) -> List[Group]:
    """Group ONLY the on-chain claims of a topic (claims-only), so the result
    maps 1:1 to claim_dupe_group. Uses the same similar/equivalent as everything
    else, guaranteeing the Claims view matches the Article view."""
    claims = load_claims_for_topic(db, topic_key)
    return group_items(claims, similar=make_similar(), equivalent=make_equivalent(db))


def load_all_claims(db: Session) -> List[Item]:
    """ALL on-chain claims (claims-only), TOPIC-INDEPENDENT — for global dupe grouping.

    patch_group_global: dupe identity is a pure function of the immutable claim set;
    topic (a fallible, LLM-assigned attribute) must NOT gate whether two claims are the
    same assertion. Bridges directly on chain_claim_text (post_id + text + embedding) so
    even claims lacking a `claim` row participate. Stake/VS from chain_post.
    """
    rows = db.execute(sql_text(
        "SELECT ct.post_id, ct.claim_text, ct.embedding::text, "
        "       p.support_total, p.challenge_total, p.effective_vs "
        "FROM chain_claim_text ct "
        "JOIN chain_post p ON p.post_id = ct.post_id "
        "WHERE ct.post_id IS NOT NULL AND ct.embedding IS NOT NULL"
    )).fetchall()
    items: List[Item] = []
    for pid, ctext, emb_txt, support, challenge, vs in rows:
        it = Item(
            kind=KIND_CLAIM, id=int(pid), text=ctext or "",
            embedding=_parse_pgvector(emb_txt),
            stake=float(support or 0.0) + float(challenge or 0.0),
            vs=float(vs or 0.0), post_id=int(pid),
        )
        it._support = float(support or 0.0)
        it._challenge = float(challenge or 0.0)
        items.append(it)
    return items


def rebuild_claim_dupe_groups(db: Session) -> int:
    """SINGLE SOURCE OF TRUTH: rebuild claim_dupe_group GLOBALLY (topic-independent).

    patch_group_global: grouping is ONE global pass over ALL claims — never scoped or
    sharded by topic — so it matches the incremental assign_to_group (also global) and
    the periodic path can no longer disagree with the per-claim path. Kept as an explicit
    repair/backfill tool; steady-state maintenance is the non-destructive straggler sweep.
    Idempotent.

    Returns the number of groups written.
    """
    import os
    group_threshold = float(os.getenv("VSP_GROUP_THRESHOLD", "0.95"))
    import unified_grouping as _ug
    _ug.HIGH_THRESHOLD = group_threshold

    # clear existing grouping (rebuild cleanly, idempotent)
    db.execute(sql_text("UPDATE chain_claim_text SET dupe_group_id = NULL"))
    db.execute(sql_text("DELETE FROM claim_dupe_group"))
    db.commit()

    # ONE global pass — no topic enumeration.
    claims = load_all_claims(db)
    groups = group_items(claims, similar=make_similar(), equivalent=make_equivalent(db))

    written = 0
    for g in groups:
        members = [m for m in g.members if m.kind == KIND_CLAIM]
        if not members:
            continue
        canonical = g.canonical if g.canonical.kind == KIND_CLAIM else members[0]
        total_support = sum(getattr(m, "_support", 0.0) for m in members)
        total_challenge = sum(getattr(m, "_challenge", 0.0) for m in members)
        agg_vs = g.agg_vs if g.claim_anchored else 0.0
        row = db.execute(sql_text(
            "INSERT INTO claim_dupe_group "
            "(canonical_post_id, canonical_text, member_count, "
            " total_support, total_challenge, aggregate_vs) "
            "VALUES (:pid, :txt, :mc, :sup, :chal, :vs) RETURNING group_id"
        ), {
            "pid": canonical.id, "txt": canonical.text, "mc": len(members),
            "sup": total_support, "chal": total_challenge, "vs": agg_vs,
        }).fetchone()
        gid = row[0]
        for m in members:
            db.execute(sql_text(
                "UPDATE chain_claim_text SET dupe_group_id = :gid WHERE post_id = :pid"
            ), {"gid": gid, "pid": m.id})
        written += 1
    db.commit()
    return written

