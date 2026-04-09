# app/chain/chain_db.py
"""
DB-backed chain reads. These replace direct RPC calls for API responses.
Data is populated by chain_indexer.py.

All functions accept a SQLAlchemy Session and return the same types
as the corresponding chain_reader.py functions.
"""

import logging
from sqlalchemy.orm import Session
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)


def get_stake_totals(db: Session, post_id: int) -> tuple[float, float]:
    """Returns (support, challenge) from indexed DB."""
    row = db.execute(sql_text(
        "SELECT support_total, challenge_total FROM chain_post WHERE post_id = :pid"
    ), {"pid": post_id}).fetchone()
    if row:
        return row[0], row[1]
    return 0.0, 0.0


def get_verity_score(db: Session, post_id: int) -> float:
    """Returns effective VS from indexed DB."""
    row = db.execute(sql_text(
        "SELECT effective_vs FROM chain_post WHERE post_id = :pid"
    ), {"pid": post_id}).fetchone()
    if row:
        return row[0]
    return 0.0


def get_user_stake(db: Session, user_address: str, post_id: int, side: int) -> float:
    """Returns user's stake amount from indexed DB."""
    row = db.execute(sql_text(
        "SELECT amount FROM chain_user_stake "
        "WHERE user_address = :addr AND post_id = :pid AND side = :side"
    ), {"addr": user_address.lower(), "pid": post_id, "side": side}).fetchone()
    if row:
        return row[0]
    return 0.0


def get_user_lot_info(db: Session, user_address: str, post_id: int, side: int) -> dict | None:
    """Returns lot info from indexed DB."""
    row = db.execute(sql_text(
        "SELECT amount, weighted_position, entry_epoch, tranche, position_weight "
        "FROM chain_user_stake "
        "WHERE user_address = :addr AND post_id = :pid AND side = :side"
    ), {"addr": user_address.lower(), "pid": post_id, "side": side}).fetchone()
    if row and row[0] > 0:
        return {
            "amount": row[0],
            "weighted_position": row[1],
            "entry_epoch": row[2],
            "tranche": row[3],
            "position_weight": row[4],
        }
    return None


def get_post_info(db: Session, post_id: int) -> dict | None:
    """Returns full post info from indexed DB."""
    row = db.execute(sql_text(
        "SELECT post_id, content_type, creator, support_total, challenge_total, "
        "base_vs, effective_vs, is_active, created_epoch "
        "FROM chain_post WHERE post_id = :pid"
    ), {"pid": post_id}).fetchone()
    if row:
        return {
            "post_id": row[0], "content_type": row[1], "creator": row[2],
            "support_total": row[3], "challenge_total": row[4],
            "base_vs": row[5], "effective_vs": row[6],
            "is_active": row[7], "created_epoch": row[8],
        }
    return None


def get_claim_text(db: Session, post_id: int) -> str | None:
    """Returns claim text from indexed DB."""
    row = db.execute(sql_text(
        "SELECT claim_text FROM chain_claim_text WHERE post_id = :pid"
    ), {"pid": post_id}).fetchone()
    return row[0] if row else None


def get_global(db: Session, key: str) -> float | None:
    """Returns a global stat from indexed DB."""
    row = db.execute(sql_text(
        "SELECT value_num FROM chain_global WHERE key = :k"
    ), {"k": key}).fetchone()
    return row[0] if row else None


def get_all_posts(db: Session, limit: int = 500, include_links: bool = True) -> list[dict]:
    """Returns all indexed posts for Claims Explorer."""
    rows = db.execute(sql_text(
        "SELECT p.post_id, p.content_type, p.creator, "
        "p.support_total, p.challenge_total, p.base_vs, p.effective_vs, p.is_active, "
        "t.claim_text "
        "FROM chain_post p "
        "LEFT JOIN chain_claim_text t ON p.post_id = t.post_id "
        + ("WHERE p.content_type = 0 " if not include_links else "")
        + "ORDER BY (p.support_total + p.challenge_total) DESC "
        "LIMIT :lim"
    ), {"lim": limit}).fetchall()

    return [{
        "post_id": r[0], "content_type": r[1], "creator": r[2],
        "support_total": r[3], "challenge_total": r[4],
        "base_vs": r[5], "verity_score": r[6], "is_active": r[7],
        "text": r[8] or "",
    } for r in rows]


def get_user_positions(db: Session, user_address: str) -> list[dict]:
    """Returns all staked positions for a user (for Portfolio)."""
    rows = db.execute(sql_text(
        "SELECT us.post_id, us.side, us.amount, us.tranche, us.position_weight, "
        "p.content_type, p.support_total, p.challenge_total, "
        "p.base_vs, p.effective_vs, p.is_active, "
        "COALESCE(t.claim_text, '') as claim_text "
        "FROM chain_user_stake us "
        "JOIN chain_post p ON us.post_id = p.post_id "
        "LEFT JOIN chain_claim_text t ON us.post_id = t.post_id "
        "WHERE us.user_address = :addr AND us.amount > 0 "
        "ORDER BY us.amount DESC"
    ), {"addr": user_address.lower()}).fetchall()

    positions = []
    for r in rows:
        post_id = r[0]
        side = r[1]
        amount = r[2]
        tranche = r[3]
        pos_weight = r[4]
        content_type = r[5]
        support = r[6]
        challenge = r[7]
        base_vs = r[8]
        effective_vs = r[9]
        is_active = r[10]
        text = r[11]

        # For links, build descriptive text
        if content_type != 0 and not text:
            link_row = db.execute(sql_text(
                "SELECT l.from_post_id, l.to_post_id, l.is_challenge, "
                "ft.claim_text as from_text, tt.claim_text as to_text "
                "FROM chain_link l "
                "LEFT JOIN chain_claim_text ft ON l.from_post_id = ft.post_id "
                "LEFT JOIN chain_claim_text tt ON l.to_post_id = tt.post_id "
                "WHERE l.link_post_id = :pid"
            ), {"pid": post_id}).fetchone()
            if link_row:
                verb = "challenges" if link_row[2] else "supports"
                from_t = (link_row[3] or f"#{link_row[0]}")[:30]
                to_t = (link_row[4] or f"#{link_row[1]}")[:30]
                text = f'"{from_t}" {verb} "{to_t}"'

        # Determine winning/losing
        vs = effective_vs
        support_wins = vs > 0
        side_name = "support" if side == 0 else "challenge"
        is_winner = (side_name == "support" and support_wins) or \
                    (side_name == "challenge" and not support_wins)

        if vs == 0:
            status = "neutral"
        elif is_winner:
            status = "winning"
        else:
            status = "losing"

        # APR calculation (inline, no RPC)
        s_max = get_global(db, "s_max") or max(support + challenge, 1.0)
        num_tranches = int(get_global(db, "num_tranches") or 10)
        total = support + challenge
        R_MIN, R_MAX = 0.01, 1.00

        abs_vs = abs(vs)
        v = abs_vs / 100.0
        participation = min(total / s_max, 1.0) if s_max > 0 else 0
        r_base = R_MIN + (R_MAX - R_MIN) * v * participation
        r_eff = r_base * pos_weight if vs != 0 else 0
        apr = r_eff * 100 if is_winner else -r_eff * 100
        if vs == 0:
            apr = 0
            r_eff = 0
            r_base = 0

        positions.append({
            "post_id": post_id,
            "post_type": "link" if content_type != 0 else "claim",
            "text": text,
            "user_support": amount if side == 0 else 0,
            "user_challenge": amount if side == 1 else 0,
            "user_total": amount,
            "user_net_side": side_name,
            "pool_support": support,
            "pool_challenge": challenge,
            "pool_total": total,
            "verity_score": effective_vs,
            "is_active": is_active,
            "position_status": status,
            "estimated_apr": round(apr, 1),
            "apr_breakdown": {
                "apr": round(apr, 1),
                "r_min": R_MIN * 100, "r_max": R_MAX * 100,
                "vs": round(vs, 2), "abs_vs": round(abs_vs, 2), "v": round(v, 4),
                "total_stake": round(total, 4), "s_max": round(s_max, 4),
                "participation": round(participation, 4),
                "r_base": round(r_base * 100, 2), "r_eff": round(r_eff * 100, 2),
                "is_winner": is_winner,
                "num_tranches": num_tranches, "tranche": tranche,
                "position_weight": round(pos_weight, 3),
            },
        })

    # Merge support + challenge on same post
    merged = {}
    for p in positions:
        pid = p["post_id"]
        if pid in merged:
            existing = merged[pid]
            existing["user_support"] += p["user_support"]
            existing["user_challenge"] += p["user_challenge"]
            existing["user_total"] = existing["user_support"] + existing["user_challenge"]
            if existing["user_support"] > 0 and existing["user_challenge"] > 0:
                existing["user_net_side"] = "both"
                existing["position_status"] = "hedged"
        else:
            merged[pid] = p

    return list(merged.values())


def get_edges(db: Session, post_id: int, direction: str) -> list[dict]:
    """Returns incoming or outgoing edges for a post."""
    if direction == "incoming":
        rows = db.execute(sql_text(
            "SELECT l.link_post_id, l.from_post_id, l.is_challenge, "
            "p.effective_vs, p.support_total, p.challenge_total, "
            "t.claim_text "
            "FROM chain_link l "
            "JOIN chain_post p ON l.from_post_id = p.post_id "
            "LEFT JOIN chain_claim_text t ON l.from_post_id = t.post_id "
            "WHERE l.to_post_id = :pid"
        ), {"pid": post_id}).fetchall()
        return [{
            "link_post_id": r[0], "claim_post_id": r[1],
            "is_challenge": r[2], "claim_vs": r[3],
            "claim_support": r[4], "claim_challenge": r[5],
            "claim_text": r[6] or "",
        } for r in rows]
    else:
        rows = db.execute(sql_text(
            "SELECT l.link_post_id, l.to_post_id, l.is_challenge, "
            "p.effective_vs, p.support_total, p.challenge_total, "
            "t.claim_text "
            "FROM chain_link l "
            "JOIN chain_post p ON l.to_post_id = p.post_id "
            "LEFT JOIN chain_claim_text t ON l.to_post_id = t.post_id "
            "WHERE l.from_post_id = :pid"
        ), {"pid": post_id}).fetchall()
        return [{
            "link_post_id": r[0], "claim_post_id": r[1],
            "is_challenge": r[2], "claim_vs": r[3],
            "claim_support": r[4], "claim_challenge": r[5],
            "claim_text": r[6] or "",
        } for r in rows]