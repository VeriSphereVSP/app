# app/claim_views.py
"""
Read-only API endpoints for claim summaries and evidence graph edges.
Used by the TopicExplorer frontend component.
"""
from __future__ import annotations

import logging
from typing import List, Dict, Any

from fastapi import APIRouter, HTTPException
from web3 import Web3

from mm_wallet import w3
from config import PROTOCOL_VIEWS_ADDRESS, STAKE_ENGINE_ADDRESS
from chain.abi import PROTOCOL_VIEWS_ABI, STAKE_ENGINE_ABI

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


@router.get("/{post_id}/summary")
def claim_summary(post_id: int):
    """
    Full claim summary from ProtocolViews.getClaimSummary().
    Returns text, VS, stakes, link counts.
    """
    try:
        views = _views()
        s = views.functions.getClaimSummary(post_id).call()
        # ClaimSummary struct fields (by index):
        #   0: text, 1: supportStake, 2: challengeStake, 3: totalStake,
        #   4: postingFee, 5: isActive, 6: baseVSRay, 7: effectiveVSRay,
        #   8: incomingCount, 9: outgoingCount
        return {
            "post_id": post_id,
            "text": str(s[0]),
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
    """
    Fetch incoming and outgoing evidence edges for a claim.

    Returns:
        {
            "incoming": [ { claim_post_id, link_post_id, is_challenge, claim_text, claim_vs, ... } ],
            "outgoing": [ ... ]
        }

    Each edge includes the linked claim's text, VS, and stake info for both
    the claim itself and the link post.
    """
    try:
        views = _views()
        stake_engine = _stake()

        # Fetch raw edges from ProtocolViews
        # IncomingEdge: (fromClaimPostId, linkPostId, isChallenge)
        raw_incoming = views.functions.getIncomingEdges(post_id).call()
        # Edge: (toClaimPostId, linkPostId, isChallenge)
        raw_outgoing = views.functions.getOutgoingEdges(post_id).call()

        def enrich_edge(
            claim_post_id: int,
            link_post_id: int,
            is_challenge: bool,
        ) -> Dict[str, Any]:
            """Enrich an edge with claim text, VS, and link stakes."""
            result: Dict[str, Any] = {
                "claim_post_id": claim_post_id,
                "link_post_id": link_post_id,
                "is_challenge": is_challenge,
            }

            # Fetch linked claim summary (text + VS)
            try:
                cs = views.functions.getClaimSummary(claim_post_id).call()
                result["claim_text"] = str(cs[0])
                result["claim_vs"] = _ray_to_pct(int(cs[7]))
                result["claim_support"] = _wei_to_vsp(int(cs[1]))
                result["claim_challenge"] = _wei_to_vsp(int(cs[2]))
            except Exception:
                result["claim_text"] = None
                result["claim_vs"] = 0
                result["claim_support"] = 0
                result["claim_challenge"] = 0

            # Fetch link post stakes
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