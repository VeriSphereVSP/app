# app/tx_log.py
"""
Tx-log helper: record, look up, and update entries in the tx_log table.

Used by:
  - /api/relay/async (relay.py) to record pending transactions on submission
  - chain_indexer.resolve_pending_txs to mark rows confirmed/reverted/dropped
  - /api/notifications/{address} (notifications.py) to query user history

All functions take an SQLAlchemy Session and assume the caller manages
the transaction. None of these helpers call db.commit() — the caller
decides when to commit.

Status transitions:
   inserted as → pending
   pending     → confirmed   (receipt.status == 1)
   pending     → reverted    (receipt.status == 0)
   pending     → dropped     (receipt not found after TX_PENDING_TIMEOUT)
"""

import logging
from typing import Optional
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def record_pending(
    db: Session,
    tx_hash: str,
    user_address: str,
    to_address: str,
    calldata: str,
    action_type: str,
    action_value: Optional[float] = None,
) -> int:
    """Insert a tx_log row with status='pending'. Returns the new row id.

    tx_hash is stored as a lowercase hex string (with 0x prefix preserved
    if present). Idempotent via the unique constraint on tx_hash: if the
    same hash is recorded twice, returns the existing id.
    """
    txh = tx_hash.lower() if tx_hash.startswith("0x") else "0x" + tx_hash.lower()
    user = user_address.lower()
    to_addr = to_address.lower()
    calld = calldata if calldata.startswith("0x") else "0x" + calldata

    result = db.execute(sql_text("""
        INSERT INTO tx_log
            (tx_hash, user_address, to_address, calldata,
             action_type, action_value, status)
        VALUES
            (:txh, :user, :to_addr, :calld, :atype, :aval, 'pending')
        ON CONFLICT (tx_hash) DO UPDATE
            SET tx_hash = EXCLUDED.tx_hash
        RETURNING id
    """), {
        "txh": txh, "user": user, "to_addr": to_addr, "calld": calld,
        "atype": action_type, "aval": action_value,
    })
    row_id = result.scalar()
    return row_id


def mark_confirmed(
    db: Session,
    tx_log_id: int,
    block_number: int,
    gas_used: int,
    post_id: Optional[int] = None,
) -> bool:
    """Mark a tx_log row as confirmed. Only updates if currently pending,
    so re-running is safe (idempotent — second call is a no-op).

    Returns True if the row was updated, False otherwise.
    """
    result = db.execute(sql_text("""
        UPDATE tx_log
        SET status = 'confirmed',
            block_number = :bn,
            gas_used = :gu,
            post_id = COALESCE(:pid, post_id),
            resolved_at = CURRENT_TIMESTAMP
        WHERE id = :id
          AND status = 'pending'
    """), {"id": tx_log_id, "bn": block_number, "gu": gas_used, "pid": post_id})
    return result.rowcount > 0


def mark_reverted(
    db: Session,
    tx_log_id: int,
    error_message: str,
    block_number: Optional[int] = None,
    gas_used: Optional[int] = None,
) -> bool:
    """Mark a tx_log row as reverted with an error message. Only updates if
    currently pending. Returns True if updated."""
    result = db.execute(sql_text("""
        UPDATE tx_log
        SET status = 'reverted',
            block_number = :bn,
            gas_used = :gu,
            error_message = :err,
            resolved_at = CURRENT_TIMESTAMP
        WHERE id = :id
          AND status = 'pending'
    """), {"id": tx_log_id, "bn": block_number, "gu": gas_used,
           "err": (error_message or "")[:1000]})
    return result.rowcount > 0


def mark_dropped(db: Session, tx_log_id: int) -> bool:
    """Mark a tx_log row as dropped (never mined within TX_PENDING_TIMEOUT).
    Only updates if currently pending. Returns True if updated."""
    result = db.execute(sql_text("""
        UPDATE tx_log
        SET status = 'dropped',
            error_message = 'Transaction not found on chain within timeout',
            resolved_at = CURRENT_TIMESTAMP
        WHERE id = :id
          AND status = 'pending'
    """), {"id": tx_log_id})
    return result.rowcount > 0


def get_pending_for_watcher(db: Session, min_age_seconds: int = 5,
                            limit: int = 50) -> list:
    """Return pending tx_log rows older than min_age_seconds, oldest first.

    The age filter avoids races with /api/relay/async's INSERT — we don't
    want to fetch a receipt before the tx has had time to propagate to
    the RPC's view of the mempool.

    Uses postgres-specific interval arithmetic for production. Tests
    that exercise this path may need to use a postgres test DB or
    bypass this function and call the marker helpers directly.
    """
    rows = db.execute(sql_text("""
        SELECT id, tx_hash, user_address, to_address, calldata,
               action_type, submitted_at,
               EXTRACT(EPOCH FROM (now() - submitted_at))::INT AS age_sec
        FROM tx_log
        WHERE status = 'pending'
          AND submitted_at < now() - (:age || ' seconds')::interval
        ORDER BY submitted_at
        LIMIT :lim
    """), {"age": min_age_seconds, "lim": limit}).fetchall()
    return rows


def get_user_notifications(
    db: Session,
    user_address: str,
    pending_limit: int = 50,
    recent_limit: int = 50,
) -> dict:
    """Return pending + recent tx_log rows for a user, ordered for UI display.

    pending: oldest first (FIFO display)
    recent:  newest resolved first (reverse chrono)
    """
    user = user_address.lower()

    pending = db.execute(sql_text("""
        SELECT id, tx_hash, action_type, action_value, to_address,
               post_id, submitted_at
        FROM tx_log
        WHERE user_address = :user
          AND status = 'pending'
        ORDER BY submitted_at
        LIMIT :lim
    """), {"user": user, "lim": pending_limit}).fetchall()

    recent = db.execute(sql_text("""
        SELECT id, tx_hash, action_type, action_value, to_address,
               status, post_id, block_number, gas_used,
               error_message, submitted_at, resolved_at
        FROM tx_log
        WHERE user_address = :user
          AND status IN ('confirmed','reverted','dropped')
        ORDER BY resolved_at DESC NULLS LAST
        LIMIT :lim
    """), {"user": user, "lim": recent_limit}).fetchall()

    def serialize(row, fields):
        return {f: getattr(row, f, None) for f in fields}

    return {
        "address": user,
        "pending": [serialize(r, ["id","tx_hash","action_type","action_value",
                                  "to_address","post_id","submitted_at"]) for r in pending],
        "recent":  [serialize(r, ["id","tx_hash","action_type","action_value",
                                  "to_address","status","post_id","block_number",
                                  "gas_used","error_message","submitted_at",
                                  "resolved_at"]) for r in recent],
    }
