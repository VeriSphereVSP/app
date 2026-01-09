from __future__ import annotations
from typing import Any, Dict, List, Literal
from sqlalchemy.orm import Session
from sqlalchemy import text

from .hashing import content_hash
from .embedding import embed
from .similarity import cosine_similarity
from .db import decode_embedding
from .config import EMBEDDINGS_MODEL, DUPLICATE_THRESHOLD, NEAR_DUPLICATE_THRESHOLD

Classification = Literal["duplicate","near_duplicate","new"]

def classify(sim: float) -> Classification:
    if sim >= DUPLICATE_THRESHOLD: return "duplicate"
    if sim >= NEAR_DUPLICATE_THRESHOLD: return "near_duplicate"
    return "new"

def ensure_claim(db: Session, claim_text: str) -> int:
    h = content_hash(claim_text)
    row = db.execute(text("SELECT claim_id FROM claim WHERE content_hash=:h"), {"h": h}).fetchone()
    if row:
        return int(row[0])
    row = db.execute(
        text("INSERT INTO claim (claim_text, content_hash) VALUES (:t,:h) RETURNING claim_id"),
        {"t": claim_text, "h": h},
    ).fetchone()
    cid = int(row[0])
    vec = embed(claim_text)
    val = vec  # postgres vector accepts python list in SQLAlchemy driver
    db.execute(
        text("INSERT INTO claim_embedding (claim_id, embedding_model, embedding) VALUES (:id,:m,:v)"),
        {"id": cid, "m": EMBEDDINGS_MODEL, "v": val},
    )
    db.commit()
    return cid

def compute_one(db: Session, claim_text: str, top_k: int) -> Dict[str,Any]:
    cid = ensure_claim(db, claim_text)

    # If pgvector is available, use it
    similar: List[Dict[str,Any]] = []
    try:
        rows = db.execute(text("""
          WITH q AS (SELECT embedding FROM claim_embedding WHERE claim_id=:id)
          SELECT c.claim_id, c.claim_text, (1.0 - (e.embedding <=> q.embedding)) AS similarity
          FROM claim c JOIN claim_embedding e USING (claim_id) CROSS JOIN q
          WHERE c.claim_id != :id
          ORDER BY (e.embedding <=> q.embedding) ASC
          LIMIT :k
        """), {"id": cid, "k": top_k}).fetchall()
        similar = [{"claim_id": int(r[0]), "text": str(r[1]), "similarity": float(r[2])} for r in rows]
    except Exception:
        # Fallback: pull embeddings and score in python
        q = db.execute(text("SELECT embedding FROM claim_embedding WHERE claim_id=:id"), {"id": cid}).fetchone()
        qvec = decode_embedding(db, q[0]) or []
        rows = db.execute(text("SELECT c.claim_id, c.claim_text, e.embedding FROM claim c JOIN claim_embedding e USING (claim_id) WHERE c.claim_id != :id"), {"id": cid}).fetchall()
        for ocid, txt, emb in rows:
            avec = decode_embedding(db, emb) or []
            sim = cosine_similarity(qvec, avec) if qvec and avec else 0.0
            similar.append({"claim_id": int(ocid), "text": str(txt), "similarity": float(sim)})
        similar.sort(key=lambda x: x["similarity"], reverse=True)
        similar = similar[:top_k]

    max_sim = float(similar[0]["similarity"]) if similar else 0.0
    return {
        "hash": content_hash(claim_text),
        "claim_id": cid,
        "classification": classify(max_sim),
        "max_similarity": max_sim,
        "similar": similar
    }
