# app/app/merge.py
from __future__ import annotations

from typing import Dict, Any
from sqlalchemy.orm import Session

from semantic import compute_one


def merge_article_with_chain(
    result: Dict[str, Any],
    db: Session,
) -> Dict[str, Any]:
    """
    Merge LLM output with on-chain / semantic claim data.

    This function guarantees a frontend-safe structure:
    - Articles always have sections[]
    - Sections always have claims[]
    - Claims always have eVS
    - Off-chain claims are neutral
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
                "id": sec.get("id", "section"),
                "text": sec.get("text", ""),
                "claims": merged_claims,
            })

        return {
            "kind": "article",
            "title": result.get("title", "Article"),
            "sections": sections,
        }

    # --------------------------------------------------
    # Defensive fallback (should never happen)
    # --------------------------------------------------
    return {
        "kind": "non_actionable",
        "message": "Unrecognized interpretation result.",
    }


def _merge_claim(
    claim: Dict[str, Any],
    db: Session,
) -> Dict[str, Any]:
    """
    Merge a single claim with on-chain / semantic data.
    """

    text = (claim.get("text") or "").strip()

    try:
        on_chain = bool(compute_one(db, text, top_k=5))
    except Exception:
        on_chain = False

    return {
        "text": text,
        "on_chain": on_chain,
        # Placeholder until wired to ProtocolViews
        "eVS": 0,
        # Only present for on-chain claims
        "stake": {
            "support": 0,
            "challenge": 0,
            "total": 0,
        } if on_chain else None,
        "links": {
            "incoming": 0,
            "outgoing": 0,
        } if on_chain else None,
    }

