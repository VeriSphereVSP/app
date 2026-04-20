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
    """Portfolio — delegates to fast DB-backed implementation."""
    return portfolio_fast(address, db)


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

