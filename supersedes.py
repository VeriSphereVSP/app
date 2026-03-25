# app/supersedes.py
"""
Supersedes relationships between claims.
When a user "edits" a claim, the new claim supersedes the old one.
Stakers on the old claim are notified and can accept (move stake) or reject (dismiss).
"""
import logging
from typing import Optional, List, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text as sql_text

from db import get_db

logger = logging.getLogger(__name__)
router = APIRouter()


class SupersedeRequest(BaseModel):
    old_post_id: int
    new_post_id: int
    created_by: str  # wallet address


class ResponseRequest(BaseModel):
    supersede_id: int
    user_address: str
    response: str  # "accept" or "reject"


def record_supersede(db: Session, old_post_id: int, new_post_id: int, created_by: str) -> Optional[int]:
    """Record that new_post_id supersedes old_post_id. Returns supersede id."""
    try:
        row = db.execute(sql_text(
            "INSERT INTO claim_supersedes (old_post_id, new_post_id, created_by) "
            "VALUES (:old, :new, :by) "
            "ON CONFLICT (old_post_id, new_post_id) DO NOTHING "
            "RETURNING id"
        ), {"old": old_post_id, "new": new_post_id, "by": created_by.lower()}).fetchone()
        db.commit()
        if row:
            logger.info("Recorded supersede: %d -> %d by %s", old_post_id, new_post_id, created_by[:10])
            return row[0]
        return None
    except Exception as e:
        logger.warning("Failed to record supersede: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return None


def get_pending_supersedes(db: Session, user_address: str) -> List[Dict]:
    """Get all pending supersede notifications for a user.
    Returns supersedes where the user has staked on the old claim
    and hasn't responded yet."""
    rows = db.execute(sql_text("""
        SELECT cs.id, cs.old_post_id, cs.new_post_id, cs.created_by, cs.created_at,
               COALESCE(sup.amount, 0) as support_amt,
               COALESCE(chal.amount, 0) as challenge_amt,
               c_old.claim_text as old_text,
               c_new.claim_text as new_text
        FROM claim_supersedes cs
        LEFT JOIN chain_user_stake sup ON sup.post_id = cs.old_post_id
            AND sup.user_address = :addr AND sup.side = 0
        LEFT JOIN chain_user_stake chal ON chal.post_id = cs.old_post_id
            AND chal.user_address = :addr AND chal.side = 1
        LEFT JOIN claim c_old ON c_old.post_id = cs.old_post_id
        LEFT JOIN claim c_new ON c_new.post_id = cs.new_post_id
        WHERE (COALESCE(sup.amount, 0) > 0 OR COALESCE(chal.amount, 0) > 0)
        AND NOT EXISTS (
            SELECT 1 FROM supersede_response sr
            WHERE sr.supersede_id = cs.id AND sr.user_address = :addr
        )
        ORDER BY cs.created_at DESC
    """), {"addr": user_address.lower()}).fetchall()

    return [{
        "supersede_id": r[0],
        "old_post_id": r[1],
        "new_post_id": r[2],
        "created_by": r[3],
        "created_at": r[4].isoformat() if r[4] else None,
        "your_support": float(r[5] or 0),
        "your_challenge": float(r[6] or 0),
        "old_text": r[7] or "",
        "new_text": r[8] or "",
    } for r in rows]


@router.post("/api/supersede")
def create_supersede(req: SupersedeRequest, db: Session = Depends(get_db)):
    """Record a supersede relationship between two claims."""
    if req.old_post_id == req.new_post_id:
        raise HTTPException(400, "Cannot supersede self")
    sid = record_supersede(db, req.old_post_id, req.new_post_id, req.created_by)
    return {"supersede_id": sid, "ok": sid is not None}


@router.get("/api/supersedes/pending/{address}")
def pending_supersedes(address: str, db: Session = Depends(get_db)):
    """Get pending supersede notifications for a wallet address."""
    return {"pending": get_pending_supersedes(db, address)}


@router.post("/api/supersedes/respond")
def respond_to_supersede(req: ResponseRequest, db: Session = Depends(get_db)):
    """Accept or reject a supersede notification."""
    if req.response not in ("accept", "reject"):
        raise HTTPException(400, "Response must be 'accept' or 'reject'")

    # Get the user address from the supersede's staker
    # (We trust the frontend to send the right address since wallet is connected)
    try:
        db.execute(sql_text(
            "INSERT INTO supersede_response (supersede_id, user_address, response) "
            "VALUES (:sid, :addr, :resp) "
            "ON CONFLICT (supersede_id, user_address) DO UPDATE SET response = :resp, responded_at = NOW()"
        ), {"sid": req.supersede_id, "addr": req.user_address.lower(), "resp": req.response})
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(500, f"Failed to record response: {e}")

    return {"ok": True}
