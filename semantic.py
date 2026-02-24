# app/semantic.py
from __future__ import annotations
import re
import unicodedata
from typing import Any, Dict, List, Literal, Optional
from sqlalchemy.orm import Session
from sqlalchemy import text
from hashing import content_hash
from embedding import embed
from similarity import cosine_similarity
from db import decode_embedding
from config import EMBEDDINGS_MODEL, DUPLICATE_THRESHOLD, NEAR_DUPLICATE_THRESHOLD

Classification = Literal["duplicate", "near_duplicate", "new"]


def classify(sim):
    if sim >= DUPLICATE_THRESHOLD:
        return "duplicate"
    if sim >= NEAR_DUPLICATE_THRESHOLD:
        return "near_duplicate"
    return "new"


def normalize_claim_text(t):
    t = unicodedata.normalize("NFC", t)
    t = t.strip().lower()
    t = re.sub(r"\s+", " ", t)
    return t


def ensure_claim(db, claim_text):
    """Insert claim if not present. Returns claim_id."""
    h = content_hash(claim_text)
    row = db.execute(
        text("SELECT claim_id FROM claim WHERE content_hash=:h"), {"h": h}
    ).fetchone()
    if row:
        return int(row[0])

    h_norm = content_hash(normalize_claim_text(claim_text))
    if h_norm != h:
        row = db.execute(
            text("SELECT claim_id FROM claim WHERE content_hash=:h"),
            {"h": h_norm},
        ).fetchone()
        if row:
            return int(row[0])

    row = db.execute(
        text(
            "INSERT INTO claim (claim_text, content_hash) "
            "VALUES (:t,:h) RETURNING claim_id"
        ),
        {"t": claim_text, "h": h},
    ).fetchone()
    cid = int(row[0])
    vec = embed(claim_text)
    db.execute(
        text(
            "INSERT INTO claim_embedding "
            "(claim_id, embedding_model, embedding) VALUES (:id,:m,:v)"
        ),
        {"id": cid, "m": EMBEDDINGS_MODEL, "v": vec},
    )
    db.commit()
    return cid


def get_post_id(db, claim_id):
    """Get on-chain post_id for a claim, or None."""
    row = db.execute(
        text("SELECT post_id FROM claim WHERE claim_id = :id"),
        {"id": claim_id},
    ).fetchone()
    if row and row[0] is not None:
        return int(row[0])
    return None


def compute_one(db, claim_text, top_k=5):
    cid = ensure_claim(db, claim_text)
    post_id = get_post_id(db, cid)

    similar = []
    try:
        rows = db.execute(
            text(
                "WITH q AS ("
                "  SELECT embedding FROM claim_embedding WHERE claim_id=:id"
                ") "
                "SELECT c.claim_id, c.claim_text, "
                "  (1.0 - (e.embedding <=> q.embedding)) AS similarity "
                "FROM claim c "
                "JOIN claim_embedding e USING (claim_id) "
                "CROSS JOIN q "
                "WHERE c.claim_id != :id "
                "ORDER BY (e.embedding <=> q.embedding) ASC "
                "LIMIT :k"
            ),
            {"id": cid, "k": top_k},
        ).fetchall()
        similar = [
            {"claim_id": int(r[0]), "text": str(r[1]), "similarity": float(r[2])}
            for r in rows
        ]
    except Exception:
        q = db.execute(
            text("SELECT embedding FROM claim_embedding WHERE claim_id=:id"),
            {"id": cid},
        ).fetchone()
        qvec = decode_embedding(db, q[0]) or []
        rows = db.execute(
            text(
                "SELECT c.claim_id, c.claim_text, e.embedding "
                "FROM claim c "
                "JOIN claim_embedding e USING (claim_id) "
                "WHERE c.claim_id != :id"
            ),
            {"id": cid},
        ).fetchall()
        for ocid, txt, emb_val in rows:
            avec = decode_embedding(db, emb_val) or []
            sim = cosine_similarity(qvec, avec) if qvec and avec else 0.0
            similar.append(
                {"claim_id": int(ocid), "text": str(txt), "similarity": float(sim)}
            )
        similar.sort(key=lambda x: x["similarity"], reverse=True)
        similar = similar[:top_k]

    max_sim = float(similar[0]["similarity"]) if similar else 0.0

    return {
        "hash": content_hash(claim_text),
        "claim_id": cid,
        "post_id": post_id,
        "classification": classify(max_sim),
        "max_similarity": max_sim,
        "similar": similar,
    }