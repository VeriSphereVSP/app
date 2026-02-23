# app/merge.py
from __future__ import annotations

import logging
from typing import Dict, Any, Optional
from sqlalchemy.orm import Session

from semantic import compute_one
from chain.claim_state import find_claim_by_text, fetch_claim_state

logger = logging.getLogger(__name__)


def merge_article_with_chain(
    result: Dict[str, Any],
    db: Session,
) -> Dict[str, Any]:
    """
    Merge LLM output with on-chain / semantic claim data.

    Guarantees a frontend-safe structure:
    - Articles always have sections[]
    - Sections always have claims[]
    - Claims always have text, on_chain, stake_support, stake_challenge
    """

    kind = result.get("kind")

    # --------------------------------------------------
    # Non-actionable: pass through unchanged
    # --------------------------------------------------
    if kind == "non_actionable":
        return result

    # --------------------------------------------------
    # Explicit claims → normalize into an article
    # --------------------------------------------------
    if kind == "claims":
        merged_claims = [
            _merge_claim(c, db) for c in result.get("claims", [])
        ]
        return {
            "kind": "article",
            "title": "User Claims",
            "sections": [
                {
                    "id": "claims",
                    "text": "",
                    "claims": merged_claims,
                }
            ],
        }

    # --------------------------------------------------
    # Article
    # --------------------------------------------------
    if kind == "article":
        sections = []
        for sec in result.get("sections", []):
            merged_claims = [
                _merge_claim(c, db)
                for c in (sec.get("claims") or [])
            ]
            sections.append({
                "id":     sec.get("id", "section"),
                "text":   sec.get("text", ""),
                "claims": merged_claims,
            })
        return {
            "kind":     "article",
            "title":    result.get("title", "Article"),
            "sections": sections,
        }

    # --------------------------------------------------
    # Defensive fallback
    # --------------------------------------------------
    return {
        "kind":    "non_actionable",
        "message": "Unrecognized interpretation result.",
    }


def _merge_claim(
    claim: Dict[str, Any],
    db: Session,
) -> Dict[str, Any]:
    """
    Merge a single claim with DB semantic metadata and blockchain state.

    Returns:
      on_chain – DB metadata (hash, similar claims) PLUS blockchain state if found
      stake_support / stake_challenge – top-level convenience fields
    """

    text = (claim.get("text") or "").strip()

    # --------------------------------------------------
    # Step 1: Semantic dedup — DB lookup
    # --------------------------------------------------
    db_meta = {}

    try:
        semantic_result = compute_one(db, text, top_k=5)
        if semantic_result:
            db_meta = semantic_result  # Contains: hash, claim_id, classification, similar
    except Exception as e:
        logger.warning(f"compute_one failed for '{text[:60]}': {e}")

    # --------------------------------------------------
    # Step 2: Blockchain lookup — augment db_meta with chain state
    # --------------------------------------------------
    try:
        post_id = find_claim_by_text(text)
        
        if post_id is not None:
            logger.info(f"Found on-chain post_id={post_id} for '{text[:40]}'")
            state = fetch_claim_state(post_id)
            
            # Merge blockchain state into db_meta
            db_meta.update({
                "eVS":   state["eVS"],
                "stake": state["stake"],
                "links": state["links"],
            })
        else:
            logger.debug(f"Claim not found on-chain: '{text[:40]}'")
    
    except Exception as e:
        logger.warning(f"Blockchain lookup failed for '{text[:60]}': {e}")

    # --------------------------------------------------
    # Step 3: Return merged claim
    # --------------------------------------------------
    # If db_meta is empty, on_chain will be None
    # If db_meta has data but no blockchain state, on_chain will have DB fields only
    # If blockchain state found, on_chain will have everything
    
    on_chain_data = db_meta if db_meta else None
    
    return {
        "text":           text,
        "confidence":     claim.get("confidence", 0.7),
        "actions":        claim.get("actions", []),
        "author":         claim.get("author", "AI Search"),
        
        "on_chain":       on_chain_data,
        
        # Convenience top-level fields
        "stake_support":  db_meta.get("stake", {}).get("support", 0),
        "stake_challenge": db_meta.get("stake", {}).get("challenge", 0),
        "verity_score":   db_meta.get("eVS", 0),
    }