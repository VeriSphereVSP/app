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

from fastapi import APIRouter, HTTPException
from web3 import Web3

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
def user_portfolio(address: str):
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
                from chain.chain_reader import get_estimated_apr
                pos["estimated_apr"] = round(get_estimated_apr(post_id, pos["user_net_side"]), 1)
            except Exception:
                pos["estimated_apr"] = 0
        else:
            try:
                from chain.chain_reader import get_estimated_apr
                pos["estimated_apr"] = round(get_estimated_apr(post_id, pos["user_net_side"]), 1)
            except Exception:
                pos["estimated_apr"] = 0

        positions.append(pos)

        summary["total_staked"] += pos["user_total"]
        summary["total_support"] += pos["user_support"]
        summary["total_challenge"] += pos["user_challenge"]
        status = pos["position_status"]
        if status == "winning":
            summary["winning_count"] += 1
        elif status == "losing":
            summary["losing_count"] += 1
        else:
            summary["neutral_count"] += 1

    status_order = {"winning": 0, "losing": 2, "neutral": 1, "hedged": 1}
    positions.sort(key=lambda p: (status_order.get(p["position_status"], 3), -p["user_total"]))

    for k in ["total_staked", "total_support", "total_challenge"]:
        summary[k] = round(summary[k], 6)

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