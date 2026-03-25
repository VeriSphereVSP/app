"""
Dynamic fee calculator. Reads costs from DB, computes per-txn fees.

Fee = max(base_fee, value × pct / 10000) × (1 + margin / 10000)

ADDITIVE model:
  Buy:    user pays (qty × price) + fee_usdc,  receives qty VSP
  Sell:   user sends qty VSP,                   receives (qty × price) - fee_usdc
  Stake:  wallet debited qty + fee_vsp,         qty goes on-chain
  Create: wallet debited posting_fee + fee_vsp, posting_fee goes on-chain
"""
import logging
from time import time
from typing import Dict, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import text as sql_text

logger = logging.getLogger(__name__)

_cache: dict = {"data": None, "ts": 0}
CACHE_TTL = 300


def _load(db: Session) -> dict:
    now = time()
    if _cache["data"] and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]

    costs = {}
    for r in db.execute(sql_text("SELECT cost_key, monthly_usd FROM operating_costs")).fetchall():
        costs[r[0]] = float(r[1])

    params = {}
    for r in db.execute(sql_text("SELECT param_key, value FROM fee_params")).fetchall():
        params[r[0]] = r[1]

    total_monthly = sum(costs.values())
    expected_txns = max(int(params.get("expected_monthly_txns", "1000")), 1)
    vsp_price = max(float(params.get("vsp_price_usd", "1.30")), 0.01)
    pct_bps = int(params.get("pct_fee_bps", "100"))
    margin_bps = int(params.get("margin_bps", "3000"))
    enabled = params.get("fee_enabled", "true").lower() == "true"

    base_fee_vsp = (total_monthly / expected_txns) / vsp_price

    data = {
        "costs": costs,
        "total_monthly_usd": round(total_monthly, 2),
        "expected_monthly_txns": expected_txns,
        "vsp_price_usd": vsp_price,
        "per_txn_cost_usd": round(total_monthly / expected_txns, 4),
        "base_fee_vsp": round(base_fee_vsp, 4),
        "pct_fee_bps": pct_bps,
        "margin_bps": margin_bps,
        "enabled": enabled,
    }
    _cache["data"] = data
    _cache["ts"] = now
    return data


def invalidate_cache():
    _cache["data"] = None
    _cache["ts"] = 0


def compute_fee(db: Session, tx_type: str, value_vsp: float) -> dict:
    """Compute fee for a transaction. Returns fee breakdown."""
    fp = _load(db)
    if not fp["enabled"]:
        return {"fee_vsp": 0, "fee_usd": 0, "base_fee": 0, "pct_fee": 0,
                "margin": 0, "breakdown": "Fees disabled"}

    base = fp["base_fee_vsp"]
    pct = value_vsp * fp["pct_fee_bps"] / 10_000
    cost_fee = max(base, pct)
    margin = cost_fee * fp["margin_bps"] / 10_000
    total = cost_fee + margin
    total_usd = total * fp["vsp_price_usd"]

    return {
        "fee_vsp": round(total, 6),
        "fee_usd": round(total_usd, 4),
        "base_fee": round(base, 6),
        "pct_fee": round(pct, 6),
        "margin": round(margin, 6),
        "breakdown": f"{'base' if base >= pct else 'pct'}: {cost_fee:.4f} + {fp['margin_bps']/100:.0f}% margin: {margin:.4f} = {total:.4f} VSP (${total_usd:.2f})",
    }


def get_fee_schedule(db: Session) -> dict:
    """Full fee schedule for API display."""
    fp = _load(db)
    fp["examples"] = {
        "buy_1_vsp": compute_fee(db, "buy", 1),
        "buy_10_vsp": compute_fee(db, "buy", 10),
        "buy_100_vsp": compute_fee(db, "buy", 100),
        "stake_1_vsp": compute_fee(db, "stake", 1),
        "create_claim": compute_fee(db, "create", 1),
    }
    return fp
