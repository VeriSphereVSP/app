#!/usr/bin/env python3
"""topic_merge.py — Phase 3 (merge slice): collapse near-duplicate topics.

Auto-merges topics whose centroids are >= threshold cosine-similar (default 0.95),
e.g. 'Climate' and 'Climate Change'. Folds the losers' associations into the
survivor, deprecates the losers, re-elects primaries, and recomputes survivor
centroids. All in-DB (pgvector). Reversible via `topic_reconciler heal --wipe`
(re-derives topics from scratch). NOT the full split/speciation engine — merge only.

Survivor selection: most claim_topic members, tie-broken by lowest topic_id.

CLI:
  python -m articles.topic_merge merge --dry-run   # list planned merges, no writes
  python -m articles.topic_merge merge             # apply
"""
from __future__ import annotations

import logging
import os
import sys

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from articles.topic_reconciler import elect_primary, check_invariants

logger = logging.getLogger(__name__)
MERGE_THRESHOLD = float(os.getenv("VSP_TOPIC_MERGE_THRESHOLD", "0.95"))


def _find(uf, x):
    while uf[x] != x:
        uf[x] = uf[uf[x]]
        x = uf[x]
    return x


def _union(uf, a, b):
    ra, rb = _find(uf, a), _find(uf, b)
    if ra != rb:
        uf[max(ra, rb)] = min(ra, rb)  # deterministic: point to lower id


def merge_topics(db: Session, threshold: float = MERGE_THRESHOLD, dry_run: bool = True):
    plan = []  # (survivor_id, survivor_label, loser_id, loser_label, sim)
    try:
        # 1. candidate pairs by centroid cosine
        pairs = db.execute(sql_text(
            "SELECT a.topic_id, b.topic_id, (1-(a.centroid<=>b.centroid))::real AS sim "
            "FROM topic a JOIN topic b ON a.topic_id < b.topic_id "
            "WHERE a.status='active' AND b.status='active' "
            "  AND a.centroid IS NOT NULL AND b.centroid IS NOT NULL "
            "  AND (1-(a.centroid<=>b.centroid)) >= :thr "
            "ORDER BY sim DESC"), {"thr": threshold}).fetchall()
        if not pairs:
            print("[merge] no topic pairs >= %.2f — nothing to merge" % threshold)
            return plan

        # 2. cluster transitively
        ids = set()
        for a, b, _ in pairs:
            ids.add(a); ids.add(b)
        uf = {i: i for i in ids}
        sim_of = {}
        for a, b, s in pairs:
            _union(uf, a, b)
            sim_of[(a, b)] = s
        clusters: dict[int, list[int]] = {}
        for i in ids:
            clusters.setdefault(_find(uf, i), []).append(i)

        labels = dict(db.execute(sql_text(
            "SELECT topic_id, label FROM topic WHERE topic_id = ANY(:ids)"),
            {"ids": list(ids)}).fetchall())
        member_counts = dict(db.execute(sql_text(
            "SELECT topic_id, count(*) FROM claim_topic WHERE topic_id = ANY(:ids) "
            "GROUP BY topic_id"), {"ids": list(ids)}).fetchall())

        affected_claims: set[int] = set()
        for members in clusters.values():
            if len(members) < 2:
                continue
            # survivor = most members, then lowest id
            survivor = max(members, key=lambda t: (member_counts.get(t, 0), -t))
            for loser in members:
                if loser == survivor:
                    continue
                s = sim_of.get((min(survivor, loser), max(survivor, loser)))
                plan.append((survivor, labels.get(survivor), loser, labels.get(loser), s))
                # claims that will need re-election
                for (cid,) in db.execute(sql_text(
                    "SELECT claim_id FROM claim_topic WHERE topic_id=:l"), {"l": loser}):
                    affected_claims.add(cid)
                if not dry_run:
                    # fold loser assocs into survivor (keep existing survivor row on conflict)
                    db.execute(sql_text(
                        "INSERT INTO claim_topic(claim_id,topic_id,is_primary,source,relevance) "
                        "SELECT claim_id, :s, false, source, relevance FROM claim_topic "
                        "WHERE topic_id=:l ON CONFLICT (claim_id,topic_id) DO NOTHING"),
                        {"s": survivor, "l": loser})
                    db.execute(sql_text("DELETE FROM claim_topic WHERE topic_id=:l"), {"l": loser})
                    # rewrite lineage edges (empty at heal #0, but be safe), drop self/dup
                    db.execute(sql_text(
                        "UPDATE topic_edge SET parent_id=:s WHERE parent_id=:l "
                        "AND child_id<>:s AND NOT EXISTS (SELECT 1 FROM topic_edge e2 "
                        "WHERE e2.child_id=topic_edge.child_id AND e2.parent_id=:s)"),
                        {"s": survivor, "l": loser})
                    db.execute(sql_text(
                        "UPDATE topic_edge SET child_id=:s WHERE child_id=:l "
                        "AND parent_id<>:s AND NOT EXISTS (SELECT 1 FROM topic_edge e2 "
                        "WHERE e2.parent_id=topic_edge.parent_id AND e2.child_id=:s)"),
                        {"s": survivor, "l": loser})
                    db.execute(sql_text("DELETE FROM topic_edge WHERE child_id=:l OR parent_id=:l"),
                               {"l": loser})
                    db.execute(sql_text(
                        "UPDATE topic SET status='deprecated', updated_at=now() WHERE topic_id=:l"),
                        {"l": loser})

        if not dry_run:
            # re-elect exactly one primary per affected claim
            for cid in affected_claims:
                rows = db.execute(sql_text(
                    "SELECT topic_id, source, relevance FROM claim_topic WHERE claim_id=:c"),
                    {"c": cid}).fetchall()
                if not rows:
                    continue
                assocs = [{"topic_id": t, "source": s, "relevance": r} for t, s, r in rows]
                pid = elect_primary(assocs)
                db.execute(sql_text("UPDATE claim_topic SET is_primary=false WHERE claim_id=:c"), {"c": cid})
                db.execute(sql_text(
                    "UPDATE claim_topic SET is_primary=true WHERE claim_id=:c AND topic_id=:t"),
                    {"c": cid, "t": pid})
            # recompute survivor centroids
            db.execute(sql_text(
                "UPDATE topic t SET centroid=sub.c, updated_at=now() FROM ("
                "  SELECT ct.topic_id, AVG(x.embedding) c FROM claim_topic ct "
                "  JOIN claim cl ON cl.claim_id=ct.claim_id "
                "  JOIN chain_claim_text x ON x.claim_text=cl.claim_text "
                "  WHERE x.embedding IS NOT NULL GROUP BY ct.topic_id) sub "
                "WHERE t.topic_id=sub.topic_id"))
            fails = check_invariants(db)
            if fails:
                db.rollback()
                print("[merge] INVARIANT FAILURE after merge — rolled back:")
                print("\n".join("  - " + f for f in fails))
                return plan
            db.commit()
        else:
            db.rollback()
    except Exception:
        db.rollback()
        raise
    return plan


def _print_plan(plan, dry):
    head = "DRY-RUN — planned merges (no writes):" if dry else "MERGED:"
    print(f"[merge] {head}")
    if not plan:
        print("  (none)")
    for surv, slab, lose, llab, sim in plan:
        print(f"  {llab!r} (id {lose}) -> {slab!r} (id {surv})   sim={sim:.3f}")
    print(f"[merge] {len(plan)} topic(s) folded into survivors")


def _main(argv):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from config import DATABASE_URL
    dry = "--dry-run" in argv
    with sessionmaker(bind=create_engine(DATABASE_URL, future=True))() as db:
        plan = merge_topics(db, dry_run=dry)
        _print_plan(plan, dry)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(_main(sys.argv))
