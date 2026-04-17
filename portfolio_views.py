# app/portfolio_views.py
"""
User portfolio API.

StakeEngine v2 view functions already project epoch gains/losses lazily,
so getUserStake() always returns the current virtual balance.

P&L = getUserStake() − cost_basis (from stake_history)
"""
from __future__ import annotations

import logging
from typing import List, Dict, Any

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy import text as sql_text
from web3 import Web3

from db import get_db
from sqlalchemy.orm import Session
from mm_wallet import w3
from config import PROTOCOL_VIEWS_ADDRESS, POST_REGISTRY_ADDRESS
from chain.abi import PROTOCOL_VIEWS_ABI, POST_REGISTRY_ABI
from chain.chain_reader import get_user_stake, get_stake_totals, get_verity_score

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])

_RAY = 10**18


def _views():
    if not PROTOCOL_VIEWS_ADDRESS:
        raise HTTPException(503, "ProtocolViews not configured")
    return w3.eth.contract(
        address=Web3.to_checksum_address(PROTOCOL_VIEWS_ADDRESS),
        abi=PROTOCOL_VIEWS_ABI,
    )


def _registry():
    if not POST_REGISTRY_ADDRESS:
        raise HTTPException(503, "PostRegistry not configured")
    return w3.eth.contract(
        address=Web3.to_checksum_address(POST_REGISTRY_ADDRESS),
        abi=POST_REGISTRY_ABI,
    )


def _ray_to_pct(ray_value: int) -> float:
    return round(ray_value / _RAY * 100, 2)


@router.get("/{address}")
def user_portfolio(address: str, db: Session = Depends(get_db)):
    try:
        addr = Web3.to_checksum_address(address)
    except Exception:
        raise HTTPException(400, "Invalid Ethereum address")

    registry = _registry()
    views = _views()

    try:
        next_id = registry.functions.nextPostId().call()
    except Exception as e:
        logger.error(f"nextPostId() failed: {e}")
        raise HTTPException(500, f"Failed to read nextPostId: {e}")

    positions: List[Dict[str, Any]] = []
    summary = {
        "total_staked": 0.0,
        "total_support": 0.0,
        "total_challenge": 0.0,
        "winning_count": 0,
        "losing_count": 0,
        "neutral_count": 0,
    }

    for post_id in range(next_id):
        # getUserStake already returns projected (virtual) amounts
        sup = get_user_stake(address, post_id, 0)
        chal = get_user_stake(address, post_id, 1)

        if sup == 0 and chal == 0:
            continue
        # PATCH: skip ghost lots in RPC endpoint (dust from fully-burned positions)
        if (sup + chal) < 1e-4:
            continue

        pos: Dict[str, Any] = {
            "post_id": post_id,
            "user_support": round(sup, 6),
            "user_challenge": round(chal, 6),
            "user_total": round(sup + chal, 6),
            "user_net_side": "support" if sup > chal else "challenge" if chal > sup else "both",
        }

        # Pool totals (also projected)
        pool_s, pool_c = get_stake_totals(post_id)
        pos["pool_support"] = round(pool_s, 4)
        pos["pool_challenge"] = round(pool_c, 4)
        pos["pool_total"] = round(pool_s + pool_c, 4)

        # Post metadata
        try:
            post = registry.functions.getPost(post_id).call()
            content_type = int(post[2])
            pos["creator"] = post[0]
            pos["post_type"] = "claim" if content_type == 0 else "link"
        except Exception:
            pos["post_type"] = "unknown"
            pos["creator"] = None

        # VS and text
        # Look up topic for this claim
        try:
            topic_row = db.execute(sql_text(
                "SELECT ta.topic_key FROM article_sentence s "
                "JOIN article_section sec ON s.section_id = sec.section_id "
                "JOIN topic_article ta ON sec.article_id = ta.article_id "
                "WHERE s.post_id = :pid LIMIT 1"
            ), {"pid": post_id}).fetchone()
            pos["topic"] = topic_row[0] if topic_row else None
        except Exception:
            pos["topic"] = None

        if pos["post_type"] == "claim":
            try:
                cs = views.functions.getClaimSummary(post_id).call()
                pos["text"] = str(cs[0])
                pos["verity_score"] = get_verity_score(post_id)
                pos["is_active"] = bool(cs[5])
            except Exception:
                pos["text"] = f"Claim #{post_id}"
                pos["verity_score"] = 0
                pos["is_active"] = False
        elif pos["post_type"] == "link":
            try:
                lm = views.functions.getLinkMeta(post_id).call()
                pos["link_from"] = int(lm[0])
                pos["link_to"] = int(lm[1])
                pos["link_is_challenge"] = bool(lm[2])
                try:
                    cs = views.functions.getClaimSummary(int(lm[0])).call()
                    from_text = str(cs[0])
                except Exception:
                    from_text = f"Claim #{lm[0]}"
                try:
                    cs2 = views.functions.getClaimSummary(int(lm[1])).call()
                    to_text = str(cs2[0])
                except Exception:
                    to_text = f"Claim #{lm[1]}"
                action = "challenges" if pos["link_is_challenge"] else "supports"
                pos["text"] = f'"{_truncate(from_text, 40)}" {action} "{_truncate(to_text, 40)}"'
                pos["verity_score"] = get_verity_score(post_id)
                pos["is_active"] = True
            except Exception:
                pos["text"] = f"Link #{post_id}"
                pos["verity_score"] = 0
                pos["is_active"] = False
        else:
            pos["text"] = f"Post #{post_id}"
            pos["verity_score"] = 0
            pos["is_active"] = False

        # Position status — based on VS direction vs user's side
        vs = pos.get("verity_score", 0)
        if pos["post_type"] == "claim":
            if sup > 0 and chal == 0:
                pos["position_status"] = "winning" if vs > 0 else "losing" if vs < 0 else "neutral"
                pos["user_net_side"] = "support"
            elif chal > 0 and sup == 0:
                pos["position_status"] = "winning" if vs < 0 else "losing" if vs > 0 else "neutral"
                pos["user_net_side"] = "challenge"
            else:
                pos["position_status"] = "hedged"
                pos["user_net_side"] = "support" if sup >= chal else "challenge"
        else:
            if sup > 0 and chal == 0:
                pos["position_status"] = "winning" if vs > 0 else "losing" if vs < 0 else "neutral"
            elif chal > 0 and sup == 0:
                pos["position_status"] = "winning" if vs < 0 else "losing" if vs > 0 else "neutral"
            else:
                pos["position_status"] = "neutral"

        # Estimated APR from on-chain rate formula
        if pos["post_type"] == "claim":
            try:
                from chain.chain_reader import get_estimated_apr, get_apr_breakdown
                breakdown = get_apr_breakdown(post_id, pos["user_net_side"])
                # Enrich with user's queue position
                try:
                    from chain.chain_reader import get_user_lot_info
                    side_int = 0 if pos["user_net_side"] == "support" else 1
                    lot_info = get_user_lot_info(address, post_id, side_int)
                    if lot_info:
                        breakdown["tranche"] = lot_info["tranche"]
                        breakdown["position_weight"] = round(lot_info["position_weight"], 3)
                        breakdown["num_tranches"] = 10
                        # Adjust r_eff by position weight
                        breakdown["r_base"] = breakdown["r_eff"]
                        breakdown["r_eff"] = round(breakdown["r_eff"] * lot_info["position_weight"], 2)
                        # Recalculate APR with position weight
                        if breakdown.get("vs", 0) == 0:
                            r_actual = 0
                            breakdown["r_eff"] = 0
                            breakdown["r_base"] = 0
                        else:
                            r_actual = breakdown["r_eff"]
                        breakdown["apr"] = round(r_actual if breakdown["is_winner"] else -r_actual, 1)
                except Exception as e:
                    import logging; logging.getLogger(__name__).debug("Lot info failed: %s", e)
                pos["estimated_apr"] = breakdown["apr"]
                pos["apr_breakdown"] = breakdown
            except Exception:
                pos["estimated_apr"] = 0
        else:
            try:
                from chain.chain_reader import get_estimated_apr, get_apr_breakdown
                breakdown = get_apr_breakdown(post_id, pos["user_net_side"])
                # Enrich with user's queue position
                try:
                    from chain.chain_reader import get_user_lot_info
                    side_int = 0 if pos["user_net_side"] == "support" else 1
                    lot_info = get_user_lot_info(address, post_id, side_int)
                    if lot_info:
                        breakdown["tranche"] = lot_info["tranche"]
                        breakdown["position_weight"] = round(lot_info["position_weight"], 3)
                        breakdown["num_tranches"] = 10
                        # Adjust r_eff by position weight
                        breakdown["r_base"] = breakdown["r_eff"]
                        breakdown["r_eff"] = round(breakdown["r_eff"] * lot_info["position_weight"], 2)
                        # Recalculate APR with position weight
                        if breakdown.get("vs", 0) == 0:
                            r_actual = 0
                            breakdown["r_eff"] = 0
                            breakdown["r_base"] = 0
                        else:
                            r_actual = breakdown["r_eff"]
                        breakdown["apr"] = round(r_actual if breakdown["is_winner"] else -r_actual, 1)
                except Exception as e:
                    import logging; logging.getLogger(__name__).debug("Lot info failed: %s", e)
                pos["estimated_apr"] = breakdown["apr"]
                pos["apr_breakdown"] = breakdown
            except Exception:
                pos["estimated_apr"] = 0

        positions.append(pos)

        summary["total_staked"] += pos["user_total"]
        summary["total_support"] += pos["user_support"]
        summary["total_challenge"] += pos["user_challenge"]
        status = pos["position_status"]
        if status == "winning":
            summary["winning_count"] += 1
            summary["winning_stake"] = summary.get("winning_stake", 0.0) + pos["user_total"]
        elif status == "losing":
            summary["losing_count"] += 1
            summary["losing_stake"] = summary.get("losing_stake", 0.0) + pos["user_total"]
        else:
            summary["neutral_count"] += 1
            summary["neutral_stake"] = summary.get("neutral_stake", 0.0) + pos["user_total"]

    status_order = {"winning": 0, "losing": 2, "neutral": 1, "hedged": 1}
    positions.sort(key=lambda p: (status_order.get(p["position_status"], 3), -p["user_total"]))

    for k in ["total_staked", "total_support", "total_challenge", "winning_stake", "losing_stake", "neutral_stake"]:
        summary[k] = round(summary.get(k, 0.0), 6)

    # Compute weighted average APR
    weighted_apr = 0.0
    total_for_apr = 0.0
    for p in positions:
        apr = p.get("estimated_apr", 0)
        stake = p.get("user_total", 0)
        if stake > 0:
            weighted_apr += apr * stake
            total_for_apr += stake
    summary["weighted_apr"] = round(weighted_apr / total_for_apr, 1) if total_for_apr > 0 else 0.0

    return {
        "address": address,
        "position_count": len(positions),
        "summary": summary,
        "positions": positions,
    }


def _truncate(text: str, length: int) -> str:
    if len(text) <= length:
        return text
    return text[:length - 1] + "…"

@router.get("/fast/{address}")
def portfolio_fast(address: str, db: Session = Depends(get_db)):
    """Fast portfolio using indexed DB data.
    If user has no indexed data, triggers on-demand indexing first."""
    from chain.chain_db import get_user_positions
    from sqlalchemy import text as sql_text

    # Check if user has any indexed stakes
    has_data = db.execute(sql_text(
        "SELECT 1 FROM chain_user_stake WHERE user_address = :addr LIMIT 1"
    ), {"addr": address.lower()}).fetchone()

    if not has_data:
        # On-demand: index all posts for this user
        try:
            from chain_indexer import index_post
            posts = db.execute(sql_text("SELECT post_id FROM chain_post")).fetchall()
            for row in posts:
                index_post(db, row[0], user_addresses=[address])
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("On-demand user index failed: %s", e)
    
    positions = get_user_positions(db, address)
    # PATCH: filter ghost lots (burned-to-zero positions that still exist in DB)
    positions = [p for p in positions if p.get("user_total", 0) >= 0.001]
    
    total_staked = sum(p["user_total"] for p in positions)
    total_support = sum(p["user_support"] for p in positions)
    total_challenge = sum(p["user_challenge"] for p in positions)
    winning = sum(1 for p in positions if p["position_status"] == "winning")
    losing = sum(1 for p in positions if p["position_status"] == "losing")
    neutral = sum(1 for p in positions if p["position_status"] == "neutral")
    winning_stake = sum(p["user_total"] for p in positions if p["position_status"] == "winning")
    losing_stake = sum(p["user_total"] for p in positions if p["position_status"] == "losing")
    neutral_stake = sum(p["user_total"] for p in positions if p["position_status"] == "neutral")
    
    # Weighted APR
    weighted_apr = 0.0
    total_for_apr = 0.0
    for p in positions:
        apr = p.get("estimated_apr", 0)
        stake = p.get("user_total", 0)
        if stake > 0:
            weighted_apr += apr * stake
            total_for_apr += stake
    
    # Look up topics for each position
    for p in positions:
        try:
            topic_row = db.execute(sql_text(
                "SELECT ta.topic_key FROM article_sentence s "
                "JOIN article_section sec ON s.section_id = sec.section_id "
                "JOIN topic_article ta ON sec.article_id = ta.article_id "
                "WHERE s.post_id = :pid LIMIT 1"
            ), {"pid": p["post_id"]}).fetchone()
            p["topic"] = topic_row[0] if topic_row else None
        except Exception:
            p["topic"] = None
    
    return {
        "address": address,
        "position_count": len(positions),
        "summary": {
            "total_staked": round(total_staked, 4),
            "total_support": round(total_support, 4),
            "total_challenge": round(total_challenge, 4),
            "winning_count": winning,
            "losing_count": losing,
            "neutral_count": neutral,
            "winning_stake": round(winning_stake, 4),
            "losing_stake": round(losing_stake, 4),
            "neutral_stake": round(neutral_stake, 4),
            "weighted_apr": round(weighted_apr / total_for_apr, 1) if total_for_apr > 0 else 0.0,
        },
        "positions": positions,
    }

