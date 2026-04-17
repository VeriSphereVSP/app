# app/semantic_dedup.py
"""
Semantic duplicate detection for claims.

Provides a /api/claims/check-similar endpoint that:
1. Embeds the proposed claim text
2. Compares against existing on-chain claim embeddings
3. Returns ranked matches above a similarity threshold

Uses pgvector for fast vector search if available, otherwise falls back
to brute-force cosine similarity (fine for <10k claims).

Embeddings are computed lazily: if a claim doesn't have an embedding yet,
it's embedded on first comparison and stored for future use.

Thresholds:
  >= 0.95  "high"   — almost certainly the same claim, block creation
  >= 0.85  "medium" — similar claim, warn user, require confirmation
  <  0.85           — distinct enough, allow silently
"""

import logging
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import text as sql_text

from db import get_db
from embedding import embed
from similarity import cosine_similarity
from config import EMBEDDINGS_PROVIDER

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/claims", tags=["claims"])

# Thresholds
HIGH_THRESHOLD = 0.95   # Block creation — almost certainly the same claim
MEDIUM_THRESHOLD = 0.85  # Warn — similar claim, user must confirm


def _ensure_embedding_column(db: Session) -> bool:
    """Check if chain_claim_text has an embedding column. Returns True if it does."""
    try:
        db.execute(sql_text(
            "SELECT embedding FROM chain_claim_text LIMIT 0"
        ))
        return True
    except Exception:
        db.rollback()
        return False


def _get_claim_embedding(db: Session, post_id: int, claim_text: str, has_col: bool) -> Optional[List[float]]:
    """Get or compute embedding for a claim. Caches in DB if column exists."""
    if has_col:
        try:
            row = db.execute(sql_text(
                "SELECT embedding FROM chain_claim_text WHERE post_id = :pid"
            ), {"pid": post_id}).fetchone()
            if row and row[0] is not None:
                emb = row[0]
                if isinstance(emb, str):
                    import json
                    return json.loads(emb)
                if isinstance(emb, list):
                    return emb
                return list(emb)
        except Exception:
            pass

    # Compute and cache
    try:
        emb = embed(claim_text)
    except Exception as e:
        logger.warning("Embedding failed for post %d: %s", post_id, e)
        return None

    if has_col and emb:
        try:
            import json
            db.execute(sql_text(
                "UPDATE chain_claim_text SET embedding = :emb WHERE post_id = :pid"
            ), {"pid": post_id, "emb": json.dumps(emb)})
            db.commit()
        except Exception as e:
            db.rollback()
            logger.debug("Failed to cache embedding for post %d: %s", post_id, e)

    return emb


@router.get("/check-similar")
def check_similar_claims(
    text: str = Query(..., min_length=3, description="Proposed claim text"),
    threshold: float = Query(MEDIUM_THRESHOLD, ge=0.0, le=1.0),
    top_k: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Check if similar claims already exist on-chain.

    Returns matches sorted by similarity descending.
    Clients should:
    - similarity >= 0.95: block creation ("this claim already exists")
    - 0.85 <= similarity < 0.95: warn, show similar claims, require confirm
    - < 0.85: allow creation

    When EMBEDDINGS_PROVIDER=stub, returns empty matches (stub embeddings
    are meaningless hashes, not semantic vectors). The exact on-chain
    duplicate check should be used instead.
    """
    provider = EMBEDDINGS_PROVIDER or "stub"

    # Stub embeddings produce random vectors — skip semantic comparison entirely
    if provider == "stub":
        return {
            "matches": [],
            "query": text,
            "threshold": threshold,
            "total_compared": 0,
            "provider": "stub",
            "note": "Semantic dedup disabled (stub embeddings). Only exact on-chain duplicate check is active.",
        }

    # Embed the proposed text
    try:
        query_emb = embed(text)
    except Exception as e:
        logger.error("Failed to embed query text: %s", e)
        # Fail open — if embedding fails, let the exact-match check handle it
        return {"matches": [], "error": "Embedding service unavailable", "provider": provider}

    has_col = _ensure_embedding_column(db)

    # Load all existing claims
    rows = db.execute(sql_text(
        "SELECT post_id, claim_text FROM chain_claim_text WHERE claim_text IS NOT NULL"
    )).fetchall()

    if not rows:
        return {"matches": [], "query": text, "threshold": threshold, "provider": provider}

    # Compare against each claim
    matches = []
    for row in rows:
        post_id = row[0]
        claim_text = row[1]
        if not claim_text or not claim_text.strip():
            continue

        claim_emb = _get_claim_embedding(db, post_id, claim_text, has_col)
        if claim_emb is None:
            continue

        sim = cosine_similarity(query_emb, claim_emb)
        if sim >= threshold:
            matches.append({
                "post_id": post_id,
                "text": claim_text,
                "similarity": round(sim, 4),
                "level": "high" if sim >= HIGH_THRESHOLD else "medium",
            })

    # Sort by similarity descending
    matches.sort(key=lambda m: m["similarity"], reverse=True)
    matches = matches[:top_k]

    return {
        "matches": matches,
        "query": text,
        "threshold": threshold,
        "total_compared": len(rows),
        "provider": provider,
    }
