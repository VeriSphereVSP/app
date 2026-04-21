# app/dupe_groups.py
"""
Semantic dupe group management.

Groups claims that make substantively the same assertion (cosine similarity >= 0.90).
Each group has a canonical claim (highest stake × VS effect).
All members must be similar to the canonical — if canonical changes, members
that don't match the new canonical are ejected.

Flow:
  1. New claim indexed → embed → compare against all group canonicals
  2. If similar to a canonical (>= DUPE_THRESHOLD), join that group
  3. If not similar to any, create a new singleton group
  4. Periodically re-evaluate canonicals (highest effect may change)
"""

import logging
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)

DUPE_THRESHOLD = 0.90  # cosine similarity threshold for grouping


def _fmt_vec(v: list) -> str:
    """Format a Python list as pgvector literal."""
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def _parse_vec(s) -> Optional[list]:
    """Parse pgvector text to list[float]."""
    if s is None:
        return None
    if isinstance(s, list):
        return s
    s = str(s).strip()
    if not s.startswith("["):
        return None
    return [float(x) for x in s[1:-1].split(",")]


def embed_claim(db: Session, post_id: int, claim_text: str) -> Optional[list]:
    """Embed a claim and store in chain_claim_text. Returns the embedding."""
    # Check if already embedded
    row = db.execute(sql_text(
        "SELECT embedding::text FROM chain_claim_text WHERE post_id = :pid"
    ), {"pid": post_id}).fetchone()

    if row and row[0]:
        vec = _parse_vec(row[0])
        if vec:
            return vec

    # Compute embedding
    try:
        from embedding import embed
        vec = embed(claim_text)
    except Exception as e:
        logger.warning("Embedding failed for post %d: %s", post_id, e)
        return None

    # Store
    try:
        db.execute(sql_text(
            "UPDATE chain_claim_text SET embedding = (:v)::vector "
            "WHERE post_id = :pid"
        ), {"v": _fmt_vec(vec), "pid": post_id})
        db.commit()
    except Exception as e:
        logger.warning("Failed to store embedding for post %d: %s", post_id, e)
        db.rollback()

    return vec


def assign_to_group(db: Session, post_id: int) -> Optional[int]:
    """Assign a claim to its dupe group. Returns group_id."""
    # Get this claim's embedding
    row = db.execute(sql_text(
        "SELECT embedding::text, claim_text FROM chain_claim_text WHERE post_id = :pid"
    ), {"pid": post_id}).fetchone()

    if not row or not row[0]:
        return None

    claim_vec = _parse_vec(row[0])
    claim_text = row[1]
    if not claim_vec:
        return None

    # Check if already in a group
    existing = db.execute(sql_text(
        "SELECT dupe_group_id FROM chain_claim_text WHERE post_id = :pid"
    ), {"pid": post_id}).fetchone()
    if existing and existing[0]:
        return existing[0]

    # Compare against all group canonicals using pgvector
    # cosine_distance = 1 - cosine_similarity
    # We want similarity >= 0.90, so distance <= 0.10
    max_distance = 1.0 - DUPE_THRESHOLD

    matches = db.execute(sql_text(
        "SELECT g.group_id, g.canonical_post_id, "
        "       (c.embedding <=> (SELECT embedding FROM chain_claim_text WHERE post_id = :pid)) as dist "
        "FROM claim_dupe_group g "
        "JOIN chain_claim_text c ON c.post_id = g.canonical_post_id "
        "WHERE c.embedding IS NOT NULL "
        "ORDER BY dist ASC "
        "LIMIT 1"
    ), {"pid": post_id}).fetchone()

    if matches and matches[2] is not None and matches[2] <= max_distance:
        group_id = matches[0]
        # Join this group
        db.execute(sql_text(
            "UPDATE chain_claim_text SET dupe_group_id = :gid WHERE post_id = :pid"
        ), {"gid": group_id, "pid": post_id})
        # Update group stats
        _refresh_group_stats(db, group_id)
        db.commit()
        logger.info("Claim post_id=%d joined dupe group %d (dist=%.3f)",
                     post_id, group_id, matches[2])
        return group_id

    # No match — create new singleton group
    row = db.execute(sql_text(
        "INSERT INTO claim_dupe_group (canonical_post_id, canonical_text, member_count) "
        "VALUES (:pid, :txt, 1) RETURNING group_id"
    ), {"pid": post_id, "txt": claim_text}).fetchone()
    group_id = row[0]

    db.execute(sql_text(
        "UPDATE chain_claim_text SET dupe_group_id = :gid WHERE post_id = :pid"
    ), {"gid": group_id, "pid": post_id})
    _refresh_group_stats(db, group_id)
    db.commit()
    logger.info("Claim post_id=%d created new dupe group %d", post_id, group_id)
    return group_id


def _refresh_group_stats(db: Session, group_id: int):
    """Recompute group stats: canonical (highest effect), aggregates."""
    # Get all members with their stake/VS data
    members = db.execute(sql_text(
        "SELECT c.post_id, c.claim_text, "
        "       COALESCE(p.support_total, 0) as support, "
        "       COALESCE(p.challenge_total, 0) as challenge, "
        "       COALESCE(p.effective_vs, 0) as vs "
        "FROM chain_claim_text c "
        "LEFT JOIN chain_post p ON c.post_id = p.post_id "
        "WHERE c.dupe_group_id = :gid"
    ), {"gid": group_id}).fetchall()

    if not members:
        return

    # Canonical = highest effect (stake × abs(VS))
    best_pid = members[0][0]
    best_text = members[0][1]
    best_effect = 0.0

    total_support = 0.0
    total_challenge = 0.0

    for m in members:
        pid, text, support, challenge, vs = m
        total_support += support
        total_challenge += challenge
        effect = (support + challenge) * abs(vs) / 100.0 if vs != 0 else 0
        if effect > best_effect:
            best_effect = effect
            best_pid = pid
            best_text = text

    # If no one has effect, pick highest total stake
    if best_effect == 0:
        best_stake = 0.0
        for m in members:
            s = m[2] + m[3]
            if s > best_stake:
                best_stake = s
                best_pid = m[0]
                best_text = m[1]

    # Compute aggregate VS (weighted by stake)
    total_stake = total_support + total_challenge
    if total_stake > 0:
        if total_support > total_challenge:
            agg_vs = (total_support / total_stake) * 100
        elif total_challenge > total_support:
            agg_vs = -(total_challenge / total_stake) * 100
        else:
            agg_vs = 0.0
    else:
        agg_vs = 0.0

    db.execute(sql_text(
        "UPDATE claim_dupe_group SET "
        "canonical_post_id = :cpid, canonical_text = :ctxt, "
        "member_count = :mc, total_support = :ts, total_challenge = :tc, "
        "aggregate_vs = :avs, updated_at = NOW() "
        "WHERE group_id = :gid"
    ), {
        "cpid": best_pid, "ctxt": best_text,
        "mc": len(members), "ts": total_support, "tc": total_challenge,
        "avs": round(agg_vs, 2), "gid": group_id,
    })

    # Check if canonical changed — if so, eject members not similar to new canonical
    old_canonical = db.execute(sql_text(
        "SELECT canonical_post_id FROM claim_dupe_group WHERE group_id = :gid"
    ), {"gid": group_id}).fetchone()

    # Ejection check: verify all members are similar to canonical
    canonical_emb = db.execute(sql_text(
        "SELECT embedding FROM chain_claim_text WHERE post_id = :pid"
    ), {"pid": best_pid}).fetchone()

    if canonical_emb and canonical_emb[0] is not None:
        max_dist = 1.0 - DUPE_THRESHOLD
        to_eject = []
        for m in members:
            if m[0] == best_pid:
                continue
            dist_row = db.execute(sql_text(
                "SELECT (c1.embedding <=> c2.embedding) as dist "
                "FROM chain_claim_text c1, chain_claim_text c2 "
                "WHERE c1.post_id = :pid1 AND c2.post_id = :pid2 "
                "AND c1.embedding IS NOT NULL AND c2.embedding IS NOT NULL"
            ), {"pid1": m[0], "pid2": best_pid}).fetchone()
            if dist_row and dist_row[0] is not None and dist_row[0] > max_dist:
                to_eject.append(m[0])

        for ejected_pid in to_eject:
            db.execute(sql_text(
                "UPDATE chain_claim_text SET dupe_group_id = NULL WHERE post_id = :pid"
            ), {"pid": ejected_pid})
            logger.info("Ejected post_id=%d from group %d (too different from new canonical)",
                         ejected_pid, group_id)
            # Create new singleton group for ejected claim
            _create_singleton_group(db, ejected_pid)


def _create_singleton_group(db: Session, post_id: int):
    """Create a singleton group for an ejected claim."""
    text_row = db.execute(sql_text(
        "SELECT claim_text FROM chain_claim_text WHERE post_id = :pid"
    ), {"pid": post_id}).fetchone()
    text = text_row[0] if text_row else ""

    row = db.execute(sql_text(
        "INSERT INTO claim_dupe_group (canonical_post_id, canonical_text, member_count) "
        "VALUES (:pid, :txt, 1) RETURNING group_id"
    ), {"pid": post_id, "txt": text}).fetchone()

    db.execute(sql_text(
        "UPDATE chain_claim_text SET dupe_group_id = :gid WHERE post_id = :pid"
    ), {"gid": row[0], "pid": post_id})


def get_dupe_group(db: Session, post_id: int) -> Optional[Dict[str, Any]]:
    """Get the dupe group for a claim, with all members."""
    group_row = db.execute(sql_text(
        "SELECT g.group_id, g.canonical_post_id, g.canonical_text, "
        "       g.member_count, g.total_support, g.total_challenge, g.aggregate_vs "
        "FROM claim_dupe_group g "
        "JOIN chain_claim_text c ON c.dupe_group_id = g.group_id "
        "WHERE c.post_id = :pid"
    ), {"pid": post_id}).fetchone()

    if not group_row:
        return None

    group_id = group_row[0]

    # Get all members
    members = db.execute(sql_text(
        "SELECT c.post_id, c.claim_text, "
        "       COALESCE(p.support_total, 0), COALESCE(p.challenge_total, 0), "
        "       COALESCE(p.effective_vs, 0) "
        "FROM chain_claim_text c "
        "LEFT JOIN chain_post p ON c.post_id = p.post_id "
        "WHERE c.dupe_group_id = :gid "
        "ORDER BY (COALESCE(p.support_total, 0) + COALESCE(p.challenge_total, 0)) DESC"
    ), {"gid": group_id}).fetchall()

    return {
        "group_id": group_id,
        "canonical_post_id": group_row[1],
        "canonical_text": group_row[2],
        "member_count": group_row[3],
        "total_support": group_row[4],
        "total_challenge": group_row[5],
        "aggregate_vs": group_row[6],
        "members": [{
            "post_id": m[0],
            "text": m[1],
            "support": m[2],
            "challenge": m[3],
            "verity_score": m[4],
            "is_canonical": m[0] == group_row[1],
        } for m in members],
    }


def refresh_all_groups(db: Session):
    """Refresh stats for all groups + reassociate singletons. Run periodically."""
    groups = db.execute(sql_text(
        "SELECT group_id FROM claim_dupe_group"
    )).fetchall()
    for (gid,) in groups:
        try:
            _refresh_group_stats(db, gid)
        except Exception as e:
            logger.warning("Failed to refresh group %d: %s", gid, e)
    db.commit()

    # Reassociate singletons: check if any singleton now matches a non-singleton canonical
    max_dist = 1.0 - DUPE_THRESHOLD
    singletons = db.execute(sql_text(
        "SELECT g.group_id, g.canonical_post_id "
        "FROM claim_dupe_group g WHERE g.member_count = 1"
    )).fetchall()

    for sg_id, sg_pid in singletons:
        try:
            match = db.execute(sql_text(
                "SELECT g.group_id, "
                "       (c.embedding <=> (SELECT embedding FROM chain_claim_text WHERE post_id = :pid)) as dist "
                "FROM claim_dupe_group g "
                "JOIN chain_claim_text c ON c.post_id = g.canonical_post_id "
                "WHERE g.group_id != :sg AND g.member_count > 0 "
                "AND c.embedding IS NOT NULL "
                "ORDER BY dist ASC LIMIT 1"
            ), {"pid": sg_pid, "sg": sg_id}).fetchone()

            if match and match[1] is not None and match[1] <= max_dist:
                target_gid = match[0]
                # Move singleton into the target group
                db.execute(sql_text(
                    "UPDATE chain_claim_text SET dupe_group_id = :tgid WHERE post_id = :pid"
                ), {"tgid": target_gid, "pid": sg_pid})
                # Delete the empty singleton group
                db.execute(sql_text(
                    "DELETE FROM claim_dupe_group WHERE group_id = :sg"
                ), {"sg": sg_id})
                _refresh_group_stats(db, target_gid)
                logger.info("Singleton post_id=%d merged into group %d (dist=%.3f)",
                            sg_pid, target_gid, match[1])
        except Exception as e:
            logger.debug("Singleton reassociation failed for post %d: %s", sg_pid, e)
    db.commit()
