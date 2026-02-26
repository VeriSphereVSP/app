# app/mm_routes.py
# ============================================================
# PROPRIETARY — Market Maker API routes.
# Do NOT reference in whitepaper or public documentation.
# ============================================================
#
# The ONLY public commitment: /api/mm/floor always returns the
# real-time liquidation floor price (reserves / circulating supply).
# All other pricing details are implementation internals.

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import text

from db import get_db
from erc20 import allowance, transfer, transfer_from
from config import USDC_ADDRESS, VSP_ADDRESS, MM_ADDRESS
from mm_pricing import (
    get_spot_quote,
    compute_buy_fill,
    compute_sell_fill,
    get_floor_price,
    DEFAULT_UNIT_AU,
    DEFAULT_HALF_SPREAD,
)

router = APIRouter(prefix="/api/mm", tags=["market-maker"])


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def _load_mm_state(db: Session, *, for_update: bool = False):
    """Load mm_state row. Returns (net_vsp, unit_au, half_spread, usdc_reserves, vsp_circulating)."""
    suffix = " FOR UPDATE" if for_update else ""
    row = db.execute(
        text(
            "SELECT net_vsp, unit_au, half_spread, usdc_reserves, vsp_circulating "
            f"FROM mm_state WHERE id = TRUE{suffix}"
        )
    ).fetchone()
    if not row:
        raise HTTPException(503, "MM state not initialized")
    return row


def _update_mm_state(
    db: Session,
    net_vsp: int,
    usdc_reserves: float,
    vsp_circulating: float,
):
    db.execute(
        text(
            "UPDATE mm_state "
            "SET net_vsp = :n, usdc_reserves = :r, vsp_circulating = :c, "
            "    updated_at = now() "
            "WHERE id = TRUE"
        ),
        {"n": net_vsp, "r": usdc_reserves, "c": vsp_circulating},
    )


def _log_trade(
    db: Session,
    *,
    side: str,
    user_address: str,
    qty_vsp: float,
    total_usdc: float,
    avg_price_usd: float,
    net_vsp_before: int,
    net_vsp_after: int,
    usdc_reserves_after: float,
    vsp_circulating_after: float,
):
    db.execute(
        text(
            "INSERT INTO mm_trade "
            "(side, user_address, qty_vsp, total_usdc, avg_price_usd, "
            " net_vsp_before, net_vsp_after, usdc_reserves_after, vsp_circulating_after) "
            "VALUES (:side, :user, :qty, :total, :avg, :nb, :na, :ra, :ca)"
        ),
        {
            "side": side,
            "user": user_address,
            "qty": qty_vsp,
            "total": total_usdc,
            "avg": avg_price_usd,
            "nb": net_vsp_before,
            "na": net_vsp_after,
            "ra": usdc_reserves_after,
            "ca": vsp_circulating_after,
        },
    )


# ────────────────────────────────────────────────────────────
# Public endpoint: liquidation floor
# ────────────────────────────────────────────────────────────

@router.get("/floor")
def mm_floor(db: Session = Depends(get_db)):
    """
    PUBLIC: Real-time liquidation floor price.
    This is the minimum price at which any VSP holder can sell.
    floor = USDC reserves / total VSP in circulation.
    """
    try:
        row = _load_mm_state(db)
        net_vsp, unit_au, half_spread, usdc_reserves, vsp_circulating = row
        floor = get_floor_price(usdc_reserves, vsp_circulating)
        return {
            "floor_price_usd": round(floor, 8),
            "usdc_reserves": round(usdc_reserves, 2),
            "vsp_circulating": round(vsp_circulating, 2),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to get floor: {e}")


# ────────────────────────────────────────────────────────────
# Spot quote (indicative, not guaranteed)
# ────────────────────────────────────────────────────────────

@router.get("/quote")
def mm_quote(db: Session = Depends(get_db)):
    """
    Indicative spot prices. Actual fills are volume-integrated and
    may differ from spot, especially for large orders.
    """
    try:
        row = _load_mm_state(db)
        net_vsp, unit_au, half_spread, usdc_reserves, vsp_circulating = row
        q = get_spot_quote(
            net_vsp=net_vsp,
            usdc_reserves=usdc_reserves,
            vsp_circulating=vsp_circulating,
            unit_au=unit_au,
            half_spread=half_spread,
        )
        return {
            "mid_price_usd": round(q.mid_price_usd, 8),
            "buy_price_usd": round(q.buy_price_usd, 8),
            "sell_price_usd": round(q.sell_price_usd, 8),
            "floor_price_usd": round(q.floor_price_usd, 8),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to get quote: {e}")


# ────────────────────────────────────────────────────────────
# Volume-priced fill preview
# ────────────────────────────────────────────────────────────

class FillPreviewRequest(BaseModel):
    side: str = Field(..., pattern="^(buy|sell)$")
    qty_vsp: float = Field(..., gt=0)


@router.post("/preview")
def mm_preview(req: FillPreviewRequest, db: Session = Depends(get_db)):
    """
    Preview what a fill would cost / yield at current state.
    Does NOT execute the trade. Returns the volume-integrated total.
    """
    try:
        row = _load_mm_state(db)
        net_vsp, unit_au, half_spread, usdc_reserves, vsp_circulating = row

        if req.side == "buy":
            fill = compute_buy_fill(
                net_vsp, req.qty_vsp,
                usdc_reserves, vsp_circulating,
                unit_au, half_spread,
            )
        else:
            fill = compute_sell_fill(
                net_vsp, req.qty_vsp,
                usdc_reserves, vsp_circulating,
                unit_au, half_spread,
            )

        return {
            "side": req.side,
            "qty_vsp": req.qty_vsp,
            "total_usdc": round(fill.total_usd, 6),
            "avg_price_usd": round(fill.avg_price_usd, 8),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Preview failed: {e}")


# ────────────────────────────────────────────────────────────
# Execute trade
# ────────────────────────────────────────────────────────────

class MMTradeRequest(BaseModel):
    user_address: str
    qty_vsp: float = Field(..., gt=0)
    max_total_usdc: float = Field(
        ..., gt=0,
        description="Max USDC user is willing to pay (buy) or min willing to receive (sell)",
    )


@router.post("/buy")
def mm_buy(req: MMTradeRequest, db: Session = Depends(get_db)):
    """
    Buy VSP with USDC. Volume-integrated pricing.
    User specifies qty_vsp and max_total_usdc (slippage protection).
    """
    try:
        with db.begin():
            row = _load_mm_state(db, for_update=True)
            net_vsp, unit_au, half_spread, usdc_reserves, vsp_circulating = row

            fill = compute_buy_fill(
                net_vsp, req.qty_vsp,
                usdc_reserves, vsp_circulating,
                unit_au, half_spread,
            )

            if fill.total_usd > req.max_total_usdc:
                raise HTTPException(
                    400,
                    f"Fill cost {fill.total_usd:.6f} USDC exceeds max {req.max_total_usdc:.6f}",
                )

            usdc_micro = int(fill.total_usd * 1_000_000)

            if allowance(USDC_ADDRESS, req.user_address, MM_ADDRESS) < usdc_micro:
                raise HTTPException(400, "USDC allowance too low")

            # Execute on-chain transfers
            transfer_from(USDC_ADDRESS, req.user_address, MM_ADDRESS, usdc_micro)
            transfer(VSP_ADDRESS, req.user_address, int(req.qty_vsp * 10**18))

            # Update reserves
            new_net = fill.new_net_vsp
            new_reserves = usdc_reserves + fill.total_usd
            new_circ = vsp_circulating + req.qty_vsp

            _update_mm_state(db, new_net, new_reserves, new_circ)
            _log_trade(
                db,
                side="buy",
                user_address=req.user_address,
                qty_vsp=req.qty_vsp,
                total_usdc=fill.total_usd,
                avg_price_usd=fill.avg_price_usd,
                net_vsp_before=net_vsp,
                net_vsp_after=new_net,
                usdc_reserves_after=new_reserves,
                vsp_circulating_after=new_circ,
            )

        return {
            "ok": True,
            "qty_vsp": req.qty_vsp,
            "total_usdc": round(fill.total_usd, 6),
            "avg_price_usd": round(fill.avg_price_usd, 8),
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"MM buy error: {e}")
        print(traceback.format_exc())
        raise HTTPException(500, f"Failed to buy VSP: {e}")


@router.post("/sell")
def mm_sell(req: MMTradeRequest, db: Session = Depends(get_db)):
    """
    Sell VSP for USDC. Volume-integrated pricing.
    User specifies qty_vsp and max_total_usdc as *minimum* proceeds
    (slippage protection — trade fails if proceeds < max_total_usdc).
    """
    try:
        with db.begin():
            row = _load_mm_state(db, for_update=True)
            net_vsp, unit_au, half_spread, usdc_reserves, vsp_circulating = row

            fill = compute_sell_fill(
                net_vsp, req.qty_vsp,
                usdc_reserves, vsp_circulating,
                unit_au, half_spread,
            )

            if fill.total_usd < req.max_total_usdc:
                raise HTTPException(
                    400,
                    f"Fill proceeds {fill.total_usd:.6f} USDC below minimum {req.max_total_usdc:.6f}",
                )

            if fill.total_usd > usdc_reserves:
                raise HTTPException(
                    400,
                    "Insufficient USDC reserves to fill this sell order",
                )

            vsp_wei = int(req.qty_vsp * 10**18)

            if allowance(VSP_ADDRESS, req.user_address, MM_ADDRESS) < vsp_wei:
                raise HTTPException(400, "VSP allowance too low")

            # Execute on-chain transfers
            transfer_from(VSP_ADDRESS, req.user_address, MM_ADDRESS, vsp_wei)
            usdc_micro = int(fill.total_usd * 1_000_000)
            transfer(USDC_ADDRESS, req.user_address, usdc_micro)

            # Update reserves
            new_net = fill.new_net_vsp
            new_reserves = usdc_reserves - fill.total_usd
            new_circ = vsp_circulating - req.qty_vsp

            _update_mm_state(db, new_net, new_reserves, new_circ)
            _log_trade(
                db,
                side="sell",
                user_address=req.user_address,
                qty_vsp=req.qty_vsp,
                total_usdc=fill.total_usd,
                avg_price_usd=fill.avg_price_usd,
                net_vsp_before=net_vsp,
                net_vsp_after=new_net,
                usdc_reserves_after=new_reserves,
                vsp_circulating_after=new_circ,
            )

        return {
            "ok": True,
            "qty_vsp": req.qty_vsp,
            "total_usdc": round(fill.total_usd, 6),
            "avg_price_usd": round(fill.avg_price_usd, 8),
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"MM sell error: {e}")
        print(traceback.format_exc())
        raise HTTPException(500, f"Failed to sell VSP: {e}")
