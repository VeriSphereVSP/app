# app/claim_views.py
"""
Read-only API endpoints for claim summaries and evidence graph edges.
Used by the TopicExplorer frontend component.
"""
from __future__ import annotations

import logging
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session
from web3 import Web3

from mm_wallet import w3
from config import PROTOCOL_VIEWS_ADDRESS, STAKE_ENGINE_ADDRESS
from chain.abi import PROTOCOL_VIEWS_ABI, STAKE_ENGINE_ABI
from db import get_db
from moderation import check_content_fast

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/claims", tags=["claims"])

_RAY = 10**18


def _views():
    if not PROTOCOL_VIEWS_ADDRESS:
        raise HTTPException(503, "ProtocolViews not configured")
    return w3.eth.contract(
        address=Web3.to_checksum_address(PROTOCOL_VIEWS_ADDRESS),
        abi=PROTOCOL_VIEWS_ABI,
    )


def _stake():
    if not STAKE_ENGINE_ADDRESS:
        raise HTTPException(503, "StakeEngine not configured")
    return w3.eth.contract(
        address=Web3.to_checksum_address(STAKE_ENGINE_ADDRESS),
        abi=STAKE_ENGINE_ABI,
    )


def _ray_to_pct(ray_value: int) -> float:
    return round(ray_value / _RAY * 100, 2)


def _wei_to_vsp(wei: int) -> float:
    return wei / 1e18


def _moderate_text(text: str) -> str:
    """Return text if clean, or a placeholder if blocked."""
    mod = check_content_fast(text)
    if mod.allowed:
        return text
    return "[Content hidden — policy violation]"


# ── /all must be defined BEFORE /{post_id} routes ──

@router.get("/all")
def all_claims(limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    """List all on-chain claims with metrics."""
    rows = db.execute(sql_text("""
        SELECT c.claim_id, c.claim_text, c.post_id, c.created_tms,
               COALESCE(ta.topic_key, '') as topic
        FROM claim c
        LEFT JOIN article_sentence s ON s.post_id = c.post_id
        LEFT JOIN article_section sec ON s.section_id = sec.section_id
        LEFT JOIN topic_article ta ON sec.article_id = ta.article_id
        WHERE c.post_id IS NOT NULL
        GROUP BY c.claim_id, c.claim_text, c.post_id, c.created_tms, ta.topic_key
        HAVING ta.topic_key = MIN(ta.topic_key) OR ta.topic_key IS NULL
        ORDER BY c.post_id
        LIMIT :limit OFFSET :offset
    """), {"limit": limit, "offset": offset}).fetchall()

    if not rows:
        return {"claims": [], "total": 0}

    views = _views()
    results = []

    for row in rows:
        post_id = row[2]
        if post_id is None:
            continue
        try:
            s = views.functions.getClaimSummary(post_id).call()
            support = _wei_to_vsp(int(s[1]))
            challenge = _wei_to_vsp(int(s[2]))
            total = _wei_to_vsp(int(s[3]))
            vs = _ray_to_pct(int(s[7]))
            base_vs = _ray_to_pct(int(s[6]))
            incoming = int(s[8])
            outgoing = int(s[9])
            controversy = total * (100 - abs(vs)) / 100 if total > 0 else 0

            results.append({
                "post_id": post_id,
                "text": _moderate_text(str(s[0])),
                "verity_score": vs,
                "base_vs": base_vs,
                "stake_support": round(support, 4),
                "stake_challenge": round(challenge, 4),
                "total_stake": round(total, 4),
                "controversy": round(controversy, 4),
                "incoming_links": incoming,
                "outgoing_links": outgoing,
                "topic": row[4] or "",
                "created_at": row[3].isoformat() if row[3] else None,
            })
        except Exception as e:
            logger.warning(f"Failed to fetch summary for post {post_id}: {e}")
            continue

    results.sort(key=lambda x: -x["total_stake"])
    total_count = db.execute(sql_text(
        "SELECT COUNT(*) FROM claim WHERE post_id IS NOT NULL"
    )).scalar() or 0

    return {"claims": results, "total": total_count}


@router.get("/{post_id}/summary")
def claim_summary(post_id: int):
    """Full claim summary from ProtocolViews.getClaimSummary()."""
    try:
        views = _views()
        s = views.functions.getClaimSummary(post_id).call()
        return {
            "post_id": post_id,
            "text": _moderate_text(str(s[0])),
            "stake_support": _wei_to_vsp(int(s[1])),
            "stake_challenge": _wei_to_vsp(int(s[2])),
            "total_stake": _wei_to_vsp(int(s[3])),
            "posting_fee": _wei_to_vsp(int(s[4])),
            "is_active": bool(s[5]),
            "base_vs": _ray_to_pct(int(s[6])),
            "verity_score": _ray_to_pct(int(s[7])),
            "incoming_count": int(s[8]),
            "outgoing_count": int(s[9]),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"claim_summary({post_id}) failed: {e}")
        raise HTTPException(500, f"Failed to fetch claim summary: {e}")


@router.get("/{post_id}/edges")
def claim_edges(post_id: int):
    """Fetch incoming and outgoing evidence edges for a claim."""
    try:
        views = _views()
        stake_engine = _stake()

        raw_incoming = views.functions.getIncomingEdges(post_id).call()
        raw_outgoing = views.functions.getOutgoingEdges(post_id).call()

        def enrich_edge(
            claim_post_id: int,
            link_post_id: int,
            is_challenge: bool,
        ) -> Dict[str, Any]:
            result: Dict[str, Any] = {
                "claim_post_id": claim_post_id,
                "link_post_id": link_post_id,
                "is_challenge": is_challenge,
            }
            try:
                cs = views.functions.getClaimSummary(claim_post_id).call()
                result["claim_text"] = _moderate_text(str(cs[0]))
                result["claim_vs"] = _ray_to_pct(int(cs[7]))
                result["claim_support"] = _wei_to_vsp(int(cs[1]))
                result["claim_challenge"] = _wei_to_vsp(int(cs[2]))
            except Exception:
                result["claim_text"] = None
                result["claim_vs"] = 0
                result["claim_support"] = 0
                result["claim_challenge"] = 0
            try:
                ls, lc = stake_engine.functions.getPostTotals(link_post_id).call()
                result["link_support"] = _wei_to_vsp(int(ls))
                result["link_challenge"] = _wei_to_vsp(int(lc))
            except Exception:
                result["link_support"] = 0
                result["link_challenge"] = 0
            return result

        incoming: List[Dict[str, Any]] = []
        for edge in raw_incoming:
            incoming.append(enrich_edge(
                claim_post_id=int(edge[0]),
                link_post_id=int(edge[1]),
                is_challenge=bool(edge[2]),
            ))

        outgoing: List[Dict[str, Any]] = []
        for edge in raw_outgoing:
            outgoing.append(enrich_edge(
                claim_post_id=int(edge[0]),
                link_post_id=int(edge[1]),
                is_challenge=bool(edge[2]),
            ))

        return {
            "incoming": incoming,
            "outgoing": outgoing,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"claim_edges({post_id}) failed: {e}")
        raise HTTPException(500, f"Failed to fetch edges: {e}")