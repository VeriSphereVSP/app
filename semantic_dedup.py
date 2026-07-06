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
  >= 0.98  "high"   — near-verbatim duplicate, warn strongly
  >= 0.85  "medium" — similar claim, warn user, require confirmation
  <  0.85           — distinct enough, allow silently
"""

import logging
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, Depends, Query, Request, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text as sql_text

from db import get_db
from embedding import embed, embed_batch
from similarity import cosine_similarity
from config import EMBEDDINGS_PROVIDER
from rate_limit import public_endpoint  # patch_public_hardening

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/claims", tags=["claims"])

# Thresholds
HIGH_THRESHOLD = 0.98   # Warn strongly — only exact text match should block
MEDIUM_THRESHOLD = 0.85  # Warn — similar claim, user must confirm


@router.get("/check-similar")
@public_endpoint("/api/claims/check-similar", cost_tier="ai")
def check_similar_claims(
    request: Request,
    text: str = Query(..., min_length=3, description="Proposed claim text"),
    threshold: float = Query(MEDIUM_THRESHOLD, ge=0.0, le=1.0),
    top_k: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Check if similar claims already exist on-chain (single-text semantic match).

    patch_check_similar_reimpl: read-only reimplementation over the SAME pgvector path as
    /match-batch — no brute-force Python scan, no lazy embedding writes. Public (CORS + ai
    rate/cost tier). Returns matches sorted by similarity desc, each tagged level "high"
    (>= 0.98) or "medium". Stub-guarded. Response shape unchanged for existing clients
    (the add-claim flow reads matches[].{post_id, text, similarity, level} and provider).
    """
    provider = EMBEDDINGS_PROVIDER or "stub"
    if provider == "stub":
        return {"matches": [], "query": text, "threshold": threshold, "total_compared": 0,
                "provider": "stub",
                "note": "Semantic dedup disabled (stub embeddings). Only exact on-chain duplicate check is active."}
    try:
        qemb = embed(text)
    except Exception as e:
        logger.error("check_similar embed failed: %s", e)
        return {"matches": [], "error": "Embedding service unavailable", "provider": provider}

    qv = _mb_to_pgvector(qemb)
    maxdist = 1.0 - threshold
    rows = db.execute(sql_text(
        "SELECT ct.post_id, ct.claim_text, 1 - (ct.embedding <=> (:q)::vector) AS sim "
        "FROM chain_claim_text ct "
        "WHERE ct.embedding IS NOT NULL AND ct.claim_text IS NOT NULL "
        "AND (ct.embedding <=> (:q)::vector) <= :maxdist "
        "ORDER BY ct.embedding <=> (:q)::vector LIMIT :k"
    ), {"q": qv, "maxdist": maxdist, "k": top_k}).fetchall()
    matches = [{
        "post_id": r[0], "text": r[1], "similarity": round(float(r[2]), 4),
        "level": "high" if float(r[2]) >= HIGH_THRESHOLD else "medium",
    } for r in rows]
    return {
        "matches": matches,
        "query": text,
        "threshold": threshold,
        "total_compared": len(matches),
        "provider": provider,
    }


# ── patch_match_batch: public batch semantic-match endpoint ──────────────────────
import os as _mb_os


def _mb_limits():
    return (int(_mb_os.getenv("VSP_MATCH_MAX_SENTENCES", "100")),
            int(_mb_os.getenv("VSP_MATCH_MAX_CHARS", "2000")))


def _mb_authorized(request: Request) -> bool:
    """Open by default. If VSP_MATCH_API_KEYS is set (comma-separated), require a
    matching X-API-Key header. 'Open now, add a key later' == set that env var; no
    code change needed."""
    keys = [k.strip() for k in _mb_os.getenv("VSP_MATCH_API_KEYS", "").split(",") if k.strip()]
    if not keys:
        return True
    return (request.headers.get("X-API-Key") or "") in keys


def _mb_to_pgvector(emb) -> str:
    return "[" + ",".join(repr(float(x)) for x in emb) + "]"


def _mb_collapse(matches):
    """Collapse dupe-group members to ONE match per group (its canonical), keeping the
    best similarity in the group. Solo claims (no group) pass through. patch_public_hardening."""
    best = {}
    order = []
    for m in matches:
        gid = m.get("dupe_group_id")
        canon = m.get("dupe_canonical_post_id")
        key = ("g", gid) if gid is not None else ("s", m["post_id"])
        rep = dict(m)
        if gid is not None and canon is not None:
            rep["post_id"] = canon
        if key not in best:
            best[key] = rep
            order.append(key)
        elif m["similarity"] > best[key]["similarity"]:
            best[key] = rep
    return [best[k] for k in order]


@router.post("/match-batch")
@public_endpoint("/api/claims/match-batch", cost_tier="ai_batch")
def match_batch(body: dict, request: Request, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Batch semantic match. Given sentences, return those with a matching on-chain claim
    (embedding cosine >= threshold), with matching post_ids + dupe-group canonical.
    Read-only. Public (CORS + ai_batch sentence budget). Dormant X-API-Key via
    VSP_MATCH_API_KEYS.

    Body: {"sentences": [str,...], "threshold": float=0.85, "top_k": int=3, "collapse": bool=true}
    collapse=true (default): one match per dupe group (the canonical). collapse=false: raw members.
    """
    if not _mb_authorized(request):
        raise HTTPException(401, "Invalid or missing X-API-Key")

    max_sentences, max_chars = _mb_limits()
    sentences = body.get("sentences")
    if not isinstance(sentences, list) or not sentences:
        raise HTTPException(400, "Body must include a non-empty 'sentences' array")
    if len(sentences) > max_sentences:
        raise HTTPException(413, "Too many sentences: %d > %d" % (len(sentences), max_sentences))

    clean = []
    for i, s in enumerate(sentences):
        if not isinstance(s, str):
            raise HTTPException(400, "sentences[%d] is not a string" % i)
        s2 = s.strip()
        if len(s2) > max_chars:
            raise HTTPException(413, "sentences[%d] exceeds %d chars" % (i, max_chars))
        clean.append(s2)

    try:
        threshold = min(max(float(body.get("threshold", 0.85)), 0.0), 1.0)
    except (TypeError, ValueError):
        raise HTTPException(400, "threshold must be a number")
    try:
        top_k = min(max(int(body.get("top_k", 3)), 1), 20)
    except (TypeError, ValueError):
        raise HTTPException(400, "top_k must be an integer")
    collapse = bool(body.get("collapse", True))

    provider = EMBEDDINGS_PROVIDER or "stub"
    if provider == "stub":
        return {"results": [], "by_sentence": {}, "threshold": threshold, "provider": "stub",
                "collapse": collapse, "matched": 0, "total_input": len(clean),
                "note": "Semantic match disabled (stub embeddings)."}

    idx_nonempty = [i for i, s in enumerate(clean) if s]
    try:
        embs = embed_batch([clean[i] for i in idx_nonempty])
    except Exception as e:
        logger.error("match_batch embed failed: %s", e)
        raise HTTPException(503, "Embedding service unavailable")

    maxdist = 1.0 - threshold
    fetch = min(50, max(top_k * 5, top_k))
    results = []
    by_sentence: Dict[str, List[int]] = {}
    for local_i, orig_i in enumerate(idx_nonempty):
        qv = _mb_to_pgvector(embs[local_i])
        rows = db.execute(sql_text(
            "SELECT ct.post_id, ct.claim_text, ct.dupe_group_id, g.canonical_post_id, "
            "       1 - (ct.embedding <=> (:q)::vector) AS sim "
            "FROM chain_claim_text ct "
            "LEFT JOIN claim_dupe_group g ON ct.dupe_group_id = g.group_id "
            "WHERE ct.embedding IS NOT NULL AND (ct.embedding <=> (:q)::vector) <= :maxdist "
            "ORDER BY ct.embedding <=> (:q)::vector LIMIT :k"
        ), {"q": qv, "maxdist": maxdist, "k": fetch}).fetchall()
        if not rows:
            continue
        matches = [{
            "post_id": r[0], "claim_text": r[1], "dupe_group_id": r[2],
            "dupe_canonical_post_id": r[3], "similarity": round(float(r[4]), 4),
        } for r in rows]
        if collapse:
            matches = _mb_collapse(matches)
        matches = matches[:top_k]
        results.append({"index": orig_i, "sentence": clean[orig_i], "matches": matches})
        by_sentence[clean[orig_i]] = [m["post_id"] for m in matches]

    return {"results": results, "by_sentence": by_sentence, "threshold": threshold,
            "provider": provider, "collapse": collapse, "matched": len(results),
            "total_input": len(clean)}
