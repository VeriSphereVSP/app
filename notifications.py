# app/notifications.py
"""
Notifications API: surfaces tx_log entries to the frontend so it can show
"your stake was confirmed" / "your claim creation reverted" toasts and a
notifications panel, independent of which page the user is on.
"""

import logging
from datetime import datetime
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session

from db import get_db
from tx_log import get_user_notifications

logger = logging.getLogger(__name__)
router = APIRouter()


def _serialize_dt(obj):
    """Convert datetime values in a dict to ISO strings, recursively for lists."""
    if isinstance(obj, list):
        return [_serialize_dt(x) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, datetime):
                out[k] = v.isoformat()
            elif isinstance(v, (list, dict)):
                out[k] = _serialize_dt(v)
            else:
                out[k] = v
        return out
    return obj


@router.get("/api/notifications/{address}")  # patch_bundle04_5_p2_notifications_route
async def list_notifications(
    address: str,
    pending_limit: int = 50,
    recent_limit: int = 50,
    before_cursor: str | None = None,
    db: Session = Depends(get_db),
):
    """Return pending and recently-resolved tx_log entries for the user.

    Response shape:
        {
          "address": "0x...",
          "pending": [
            {"id":..., "tx_hash":..., "action_type":"stake",
             "action_value": 1.5, "to_address":..., "post_id":...,
             "submitted_at": "ISO8601"}
          ],
          "recent": [
            {"id":..., "tx_hash":..., "action_type":...,
             "status": "confirmed"|"reverted"|"dropped",
             "block_number":..., "gas_used":...,
             "error_message": null|"...", "post_id":...,
             "submitted_at":..., "resolved_at":...}
          ]
        }

    The frontend computes "unread" client-side from `submitted_at` /
    `resolved_at` timestamps and the last time the user viewed the
    notifications panel. No server-side read state in this bundle.
    """
    if not address or not address.startswith("0x") or len(address) != 42:
        raise HTTPException(400, "address must be a 0x-prefixed hex address")

    # Clamp limits to avoid abuse.
    pending_limit = max(1, min(pending_limit, 200))
    recent_limit  = max(1, min(recent_limit,  200))

    data = get_user_notifications(
        db, address,
        pending_limit=pending_limit,
        recent_limit=recent_limit,
        before_cursor=before_cursor,
    )
    return _serialize_dt(data)
