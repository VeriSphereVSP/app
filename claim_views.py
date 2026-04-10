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

def _score():
    import json as _json
    from config import SCORE_ENGINE_ADDRESS
    if not SCORE_ENGINE_ADDRESS:
        raise HTTPException(503, "ScoreEngine not configured")
    return w3.eth.contract(
        address=Web3.to_checksum_address(SCORE_ENGINE_ADDRESS),
        abi=_json.loads(open("/core/out/ScoreEngine.sol/ScoreEngine.json").read())["abi"],
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
    """Deprecated: delegates to /fast/all (indexed DB, no RPC calls).
    Kept for backward compatibility with any external callers."""
    return claims_fast(limit=limit, db=db)



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
        score_engine = _score()

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
            try:
                vs_ray = score_engine.functions.effectiveVSRay(link_post_id).call()
                result["link_vs"] = (vs_ray / 1e18) * 100
            except Exception:
                result["link_vs"] = 0
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



@router.get("/search")
def search_claims(q: str = "", limit: int = 50, include_links: bool = False, db: Session = Depends(get_db)):
    """Search all on-chain claims by text. Returns claims with metrics from indexed DB."""
    from chain.chain_db import get_all_posts, get_edges

    posts = get_all_posts(db, limit=500, include_links=include_links)

    # Filter by search query
    if q.strip():
        ql = q.lower()
        posts = [p for p in posts if ql in p["text"].lower() or ql in str(p["post_id"])]

    claims = []
    for p in posts[:limit]:
        incoming = get_edges(db, p["post_id"], "incoming")
        outgoing = get_edges(db, p["post_id"], "outgoing")

        total = p["support_total"] + p.get("challenge_total", 0)
        controversy = 0
        if total > 0:
            minority = min(p["support_total"], p.get("challenge_total", 0))
            controversy = minority / total

        topic_row = db.execute(sql_text(
            "SELECT ta.topic_key FROM article_sentence s "
            "JOIN article_section sec ON s.section_id = sec.section_id "
            "JOIN topic_article ta ON sec.article_id = ta.article_id "
            "WHERE s.post_id = :pid LIMIT 1"
        ), {"pid": p["post_id"]}).fetchone()
        if not topic_row:
            topic_row = db.execute(sql_text(
                "SELECT topic FROM claim WHERE post_id = :pid AND topic IS NOT NULL LIMIT 1"
            ), {"pid": p["post_id"]}).fetchone()

        claims.append({
            "post_id": p["post_id"],
            "text": _moderate_text(p["text"]),
            "creator": p.get("creator", ""),
            "verity_score": round(p["verity_score"], 2),
            "stake_support": round(p["support_total"], 4),
            "stake_challenge": round(p.get("challenge_total", 0), 4),
            "total_stake": round(total, 4),
            "controversy": round(controversy, 4),
            "incoming_links": len(incoming),
            "outgoing_links": len(outgoing),
            "topic": topic_row[0] if topic_row else None,
        })

    return {"claims": claims, "total": len(claims)}

@router.get("/fast/all")
def claims_fast(limit: int = 500, include_links: bool = True, db: Session = Depends(get_db)):
    """Fast claims explorer using indexed DB data (no RPC calls).
    Returns both claims (content_type=0) and links (content_type=1)."""
    from chain.chain_db import get_all_posts, get_edges
    
    posts = get_all_posts(db, limit=limit, include_links=include_links)
    
    # Build a lookup of link metadata: link_post_id -> {from_post_id, to_post_id, is_challenge, from_text, to_text}
    link_meta = {}
    if include_links:
        link_rows = db.execute(sql_text(
            "SELECT l.link_post_id, l.from_post_id, l.to_post_id, l.is_challenge, "
            "       cf.claim_text, ct.claim_text "
            "FROM chain_link l "
            "LEFT JOIN chain_claim_text cf ON cf.post_id = l.from_post_id "
            "LEFT JOIN chain_claim_text ct ON ct.post_id = l.to_post_id"
        )).fetchall()
        for r in link_rows:
            link_meta[r[0]] = {
                "from_post_id": r[1], "to_post_id": r[2], "is_challenge": r[3],
                "from_text": r[4] or "", "to_text": r[5] or "",
            }
    
    claims = []
    for p in posts:
        # Count links
        incoming = get_edges(db, p["post_id"], "incoming")
        outgoing = get_edges(db, p["post_id"], "outgoing")
        
        total = p["support_total"] + p["challenge_total"]
        # New controversy: total stake × (1 - |VS|/100)
        # Captures both scale (more stake = more meaningful) and dispute level
        vs_abs = abs(p.get("verity_score", 0)) / 100.0
        controversy = total * (1 - vs_abs)
        
        # Find topic
        topic_row = db.execute(sql_text(
            "SELECT ta.topic_key FROM article_sentence s "
            "JOIN article_section sec ON s.section_id = sec.section_id "
            "JOIN topic_article ta ON sec.article_id = ta.article_id "
            "WHERE s.post_id = :pid LIMIT 1"
        ), {"pid": p["post_id"]}).fetchone()
        if not topic_row:
            topic_row = db.execute(sql_text(
                "SELECT topic FROM claim WHERE post_id = :pid AND topic IS NOT NULL LIMIT 1"
            ), {"pid": p["post_id"]}).fetchone()
        
        is_link = p.get("content_type", 0) == 1
        entry = {
            "post_id": p["post_id"],
            "content_type": p.get("content_type", 0),
            "is_link": is_link,
            "text": _moderate_text(p["text"]) if not is_link else "",
            "creator": p.get("creator", ""),
            "verity_score": round(p["verity_score"], 2),
            "base_vs": round(p.get("base_vs", 0), 2),
            "stake_support": round(p["support_total"], 4),
            "stake_challenge": round(p.get("challenge_total", 0), 4),
            "total_stake": round(total, 4),
            "controversy": round(controversy, 4),
            "incoming_links": len(incoming),
            "outgoing_links": len(outgoing),
            "topic": topic_row[0] if topic_row else None,
            "created_at": None,
            "created_epoch": p.get("created_epoch"),
        }
        
        # Enrich link entries
        if is_link and p["post_id"] in link_meta:
            m = link_meta[p["post_id"]]
            entry["from_post_id"] = m["from_post_id"]
            entry["to_post_id"] = m["to_post_id"]
            entry["is_challenge"] = m["is_challenge"]
            entry["from_text"] = _moderate_text(m["from_text"])
            entry["to_text"] = _moderate_text(m["to_text"])
            # For sorting purposes, use the target claim text
            entry["text"] = entry["to_text"]
        
        claims.append(entry)
    
    total_stake = sum(c["total_stake"] for c in claims)
    avg_vs = sum(c["verity_score"] for c in claims) / len(claims) if claims else 0
    
    return {
        "claims": claims,
        "total": len(claims),
        "total_stake": round(total_stake, 2),
        "avg_vs": round(avg_vs, 2),
    }



@router.get("/{post_id}/queue")
def claim_queue(post_id: int, db: Session = Depends(get_db)):
    """Get all stakers and their positions for a post."""
    rows = db.execute(sql_text(
        "SELECT user_address, side, amount, weighted_position, entry_epoch, tranche, position_weight "
        "FROM chain_user_stake WHERE post_id = :pid AND amount > 0 ORDER BY side, weighted_position"
    ), {"pid": post_id}).fetchall()
    
    support = []
    challenge = []
    for r in rows:
        entry = {
            "address": r[0],
            "amount": round(r[2], 4),
            "position": round(r[3], 4),
            "entry_epoch": r[4],
            "tranche": r[5],
            "position_weight": r[6],
        }
        if r[1] == 0:
            support.append(entry)
        else:
            challenge.append(entry)
    
    return {
        "post_id": post_id,
        "support": support,
        "challenge": challenge,
        "support_total": sum(s["amount"] for s in support),
        "challenge_total": sum(c["amount"] for c in challenge),
    }
