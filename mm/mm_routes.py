# app/mm_routes.py
# ============================================================
# PROPRIETARY — Market Maker API routes.
# Do NOT reference in whitepaper or public documentation.
# ============================================================

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import text

from db import get_db
from fee_calculator import compute_fee as calc_fee
from mm.erc20 import allowance, transfer, transfer_from
from config import USDC_ADDRESS, VSP_ADDRESS, MM_ADDRESS
from mm.mm_pricing import (
    get_spot_quote,
    compute_buy_fill,
    compute_sell_fill,
    get_floor_price,
    DEFAULT_UNIT_AU,
    DEFAULT_HALF_SPREAD,
)

import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mm", tags=["market-maker"])


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def _load_mm_state(db: Session, *, for_update: bool = False):
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


def _update_mm_state(db, net_vsp, usdc_reserves, vsp_circulating):
    db.execute(
        text(
            "UPDATE mm_state "
            "SET net_vsp = :n, usdc_reserves = :r, vsp_circulating = :c, "
            "    updated_at = now() "
            "WHERE id = TRUE"
        ),
        {"n": net_vsp, "r": usdc_reserves, "c": vsp_circulating},
    )


def _log_trade(db, *, side, user_address, qty_vsp, total_usdc, avg_price_usd,
               net_vsp_before, net_vsp_after, usdc_reserves_after, vsp_circulating_after):
    db.execute(
        text(
            "INSERT INTO mm_trade "
            "(side, user_address, qty_vsp, total_usdc, avg_price_usd, "
            " net_vsp_before, net_vsp_after, usdc_reserves_after, vsp_circulating_after) "
            "VALUES (:side, :user, :qty, :total, :avg, :nb, :na, :ra, :ca)"
        ),
        {"side": side, "user": user_address, "qty": qty_vsp, "total": total_usdc,
         "avg": avg_price_usd, "nb": net_vsp_before, "na": net_vsp_after,
         "ra": usdc_reserves_after, "ca": vsp_circulating_after},
    )


# ── EIP-2612 permit execution ──────────────────────────────

PERMIT_ABI = [
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "deadline", "type": "uint256"},
            {"name": "v", "type": "uint8"},
            {"name": "r", "type": "bytes32"},
            {"name": "s", "type": "bytes32"},
        ],
        "name": "permit",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "nonces",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def _execute_permit(token_address: str, owner: str, spender: str,
                    value: int, deadline: int, v: int, r: str, s: str):
    """Call permit() on an ERC-2612 token. MM pays gas."""
    from web3 import Web3
    from mm_wallet import w3, sign_and_send

    contract = w3.eth.contract(
        address=Web3.to_checksum_address(token_address),
        abi=PERMIT_ABI,
    )

    r_bytes = bytes.fromhex(r.removeprefix("0x"))
    s_bytes = bytes.fromhex(s.removeprefix("0x"))

    permit_fn = contract.functions.permit(
        Web3.to_checksum_address(owner),
        Web3.to_checksum_address(spender),
        value,
        deadline,
        v,
        r_bytes,
        s_bytes,
    )
    # Static call first to catch revert reason
    try:
        permit_fn.call({'from': Web3.to_checksum_address(MM_ADDRESS)})
    except Exception as e:
        err_msg = str(e)
        logger.warning('Permit static call failed: %s', err_msg)
        try:
            on_chain_nonce = contract.functions.nonces(Web3.to_checksum_address(owner)).call()
            logger.warning('  Owner permit nonce on-chain: %d', on_chain_nonce)
        except Exception:
            pass
        raise HTTPException(400, f'Permit would revert: {err_msg[:200]}')
    tx = permit_fn.build_transaction({
        'from': Web3.to_checksum_address(MM_ADDRESS),
        'gas': 120_000,
    })
    tx_hash = sign_and_send(tx)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    if receipt.status == 0:
        raise HTTPException(400, 'Permit transaction reverted on-chain')
    logger.info("Permit executed: token=%s owner=%s value=%d tx=%s", token_address[:10], owner[:10], value, tx_hash)
    return tx_hash


# ────────────────────────────────────────────────────────────
# Public endpoint: liquidation floor
# ────────────────────────────────────────────────────────────

@router.get("/floor")
def mm_floor(db: Session = Depends(get_db)):
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
# Spot quote
# ────────────────────────────────────────────────────────────


# Quote cache: avoid hitting DB on every frontend poll
_quote_cache = {"data": None, "ts": 0}
_QUOTE_CACHE_TTL = 5  # seconds

@router.get("/quote")
def mm_quote(db: Session = Depends(get_db)):
    import time as _t
    if _quote_cache['data'] and _t.time() - _quote_cache['ts'] < _QUOTE_CACHE_TTL:
        return _quote_cache['data']
    try:
        row = _load_mm_state(db)
        net_vsp, unit_au, half_spread, usdc_reserves, vsp_circulating = row
        q = get_spot_quote(
            net_vsp=net_vsp, usdc_reserves=usdc_reserves,
            vsp_circulating=vsp_circulating, unit_au=unit_au, half_spread=half_spread,
        )
        _result = {
            "mid_price_usd": round(q.mid_price_usd, 8),
            "buy_price_usd": round(q.buy_price_usd, 8),
            "sell_price_usd": round(q.sell_price_usd, 8),
            "floor_price_usd": round(q.floor_price_usd, 8),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        _quote_cache['data'] = _result
        _quote_cache['ts'] = _t.time()
        return _result
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
    try:
        row = _load_mm_state(db)
        net_vsp, unit_au, half_spread, usdc_reserves, vsp_circulating = row
        if req.side == "buy":
            fill = compute_buy_fill(net_vsp, req.qty_vsp, usdc_reserves, vsp_circulating, unit_au, half_spread)
        else:
            fill = compute_sell_fill(net_vsp, req.qty_vsp, usdc_reserves, vsp_circulating, unit_au, half_spread)
        return {
            "side": req.side, "qty_vsp": req.qty_vsp,
            "total_usdc": round(total_usdc_with_fee, 6),
            "avg_price_usd": round(fill.avg_price_usd, 8),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Preview failed: {e}")


# ────────────────────────────────────────────────────────────
# Permit nonce lookup (frontend needs this to sign permits)
# ────────────────────────────────────────────────────────────

@router.get("/permit-nonce/{token}/{address}")
def get_permit_nonce(token: str, address: str):
    """Get EIP-2612 permit nonce for an address on a given token."""
    from web3 import Web3
    from mm_wallet import w3

    # Map friendly names to addresses
    token_map = {"usdc": USDC_ADDRESS, "vsp": VSP_ADDRESS}
    token_addr = token_map.get(token.lower(), token)

    contract = w3.eth.contract(
        address=Web3.to_checksum_address(token_addr),
        abi=PERMIT_ABI,
    )
    nonce = contract.functions.nonces(Web3.to_checksum_address(address)).call()
    return {"nonce": nonce, "token": token_addr}


# ────────────────────────────────────────────────────────────
# Execute trades with EIP-2612 permit (gasless for user)
# ────────────────────────────────────────────────────────────

class PermitFields(BaseModel):
    deadline: int
    v: int
    r: str
    s: str
    value: int  # Approved amount in token smallest unit


class MMTradeRequest(BaseModel):
    user_address: str
    qty_vsp: float = Field(..., gt=0)
    max_total_usdc: float = Field(..., gt=0)
    permit: PermitFields | None = None  # Optional — if provided, MM executes permit first



@router.get("/preview-buy")
def preview_buy(qty_vsp: float = None, usdc_amount: float = None, db: Session = Depends(get_db)):
    """Preview buy with fee breakdown.
    Specify qty_vsp (exact VSP output) or usdc_amount (exact USDC budget)."""
    row = _load_mm_state(db)
    net_vsp, unit_au, half_spread, usdc_reserves, vsp_circulating = row

    if qty_vsp and qty_vsp > 0:
        fill = compute_buy_fill(net_vsp, qty_vsp, usdc_reserves, vsp_circulating, unit_au, half_spread)
        fee = calc_fee(db, "buy", qty_vsp)
        fee_usdc = fee["fee_vsp"] * fill.avg_price_usd
        return {
            "mode": "vsp",
            "qty_vsp": qty_vsp,
            "subtotal_usdc": round(fill.total_usd, 6),
            "fee_vsp": fee["fee_vsp"],
            "fee_usdc": round(fee_usdc, 6),
            "total_usdc": round(fill.total_usd + fee_usdc, 6),
            "avg_price": round(fill.avg_price_usd, 6),
            "breakdown": fee["breakdown"],
        }
    elif usdc_amount and usdc_amount > 0:
        # Iterate to find qty that fits budget including fee
        fill1 = compute_buy_fill(net_vsp, 1.0, usdc_reserves, vsp_circulating, unit_au, half_spread)
        price = fill1.avg_price_usd
        qty_est = usdc_amount / price
        for _ in range(5):
            fill = compute_buy_fill(net_vsp, qty_est, usdc_reserves, vsp_circulating, unit_au, half_spread)
            fee = calc_fee(db, "buy", qty_est)
            fee_usdc = fee["fee_vsp"] * fill.avg_price_usd
            total = fill.total_usd + fee_usdc
            if abs(total - usdc_amount) < 0.01:
                break
            qty_est *= usdc_amount / total
            qty_est = max(qty_est, 0.001)
        return {
            "mode": "usdc",
            "usdc_budget": usdc_amount,
            "qty_vsp": round(qty_est, 6),
            "subtotal_usdc": round(fill.total_usd, 6),
            "fee_vsp": fee["fee_vsp"],
            "fee_usdc": round(fee_usdc, 6),
            "total_usdc": round(total, 6),
            "avg_price": round(fill.avg_price_usd, 6),
            "breakdown": fee["breakdown"],
        }
    return {"error": "Specify qty_vsp or usdc_amount"}

@router.get("/preview-sell")
def preview_sell(qty_vsp: float, db: Session = Depends(get_db)):
    """Preview sell with fee breakdown. User sends qty_vsp, receives USDC minus fee."""
    row = _load_mm_state(db)
    net_vsp, unit_au, half_spread, usdc_reserves, vsp_circulating = row
    fill = compute_sell_fill(net_vsp, qty_vsp, usdc_reserves, vsp_circulating, unit_au, half_spread)
    fee = calc_fee(db, "sell", qty_vsp)
    fee_usdc = fee["fee_vsp"] * fill.avg_price_usd
    return {
        "qty_vsp": qty_vsp,
        "gross_usdc": round(fill.total_usd, 6),
        "fee_vsp": fee["fee_vsp"],
        "fee_usdc": round(fee_usdc, 6),
        "net_usdc": round(max(fill.total_usd - fee_usdc, 0), 6),
        "avg_price": round(fill.avg_price_usd, 6),
        "breakdown": fee["breakdown"],
    }

@router.post("/buy")
def mm_buy(req: MMTradeRequest, db: Session = Depends(get_db)):
    """
    Buy VSP with USDC.
    If permit is provided, MM executes USDC.permit() first (gasless for user).
    Otherwise, falls back to checking existing allowance.
    """
    try:
        with db.begin():
            row = _load_mm_state(db, for_update=True)
            net_vsp, unit_au, half_spread, usdc_reserves, vsp_circulating = row

            fill = compute_buy_fill(
                net_vsp, req.qty_vsp, usdc_reserves, vsp_circulating, unit_au, half_spread,
            )

            if fill.total_usd > req.max_total_usdc:
                raise HTTPException(400, f"Fill cost {fill.total_usd:.6f} USDC exceeds max {req.max_total_usdc:.6f}")

            usdc_micro = int(fill.total_usd * 1_000_000)

            # Execute permit if provided
            if req.permit:
                if req.permit.value < usdc_micro:
                    raise HTTPException(400, f"Permit value {req.permit.value} < needed {usdc_micro}")
                _execute_permit(
                    USDC_ADDRESS, req.user_address, MM_ADDRESS,
                    req.permit.value, req.permit.deadline,
                    req.permit.v, req.permit.r, req.permit.s,
                )
            else:
                # Legacy: check existing allowance
                if allowance(USDC_ADDRESS, req.user_address, MM_ADDRESS) < usdc_micro:
                    raise HTTPException(400, "USDC allowance too low — provide a permit signature")


            # Calculate fee (additive: added to USDC cost, user receives full qty)
            fee_info = calc_fee(db, "buy", req.qty_vsp)
            fee_usdc = fee_info["fee_vsp"] * fill.avg_price_usd
            total_usdc_with_fee = fill.total_usd + fee_usdc
            usdc_micro = int(total_usdc_with_fee * 1_000_000)

            # Execute on-chain transfers
            transfer_from(USDC_ADDRESS, req.user_address, MM_ADDRESS, usdc_micro)
            transfer(VSP_ADDRESS, req.user_address, int(req.qty_vsp * 10**18))

            new_net = fill.new_net_vsp
            new_reserves = usdc_reserves + fill.total_usd
            new_circ = vsp_circulating + req.qty_vsp

            _update_mm_state(db, new_net, new_reserves, new_circ)
            _log_trade(db, side="buy", user_address=req.user_address,
                       qty_vsp=req.qty_vsp, total_usdc=fill.total_usd,
                       avg_price_usd=fill.avg_price_usd, net_vsp_before=net_vsp,
                       net_vsp_after=new_net, usdc_reserves_after=new_reserves,
                       vsp_circulating_after=new_circ)

        return {"ok": True, "qty_vsp": req.qty_vsp,
                "fee_vsp": fee_info["fee_vsp"],
                "fee_usdc": round(fee_usdc, 6),
                "gross_usdc": round(fill.total_usd, 6),
                "total_usdc": round(total_usdc_with_fee, 6),
                "avg_price_usd": round(fill.avg_price_usd, 8)}
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
    Sell VSP for USDC.
    If permit is provided, MM executes VSP.permit() first (gasless for user).
    Otherwise, falls back to checking existing allowance.
    """
    try:
        with db.begin():
            row = _load_mm_state(db, for_update=True)
            net_vsp, unit_au, half_spread, usdc_reserves, vsp_circulating = row

            fill = compute_sell_fill(
                net_vsp, req.qty_vsp, usdc_reserves, vsp_circulating, unit_au, half_spread,
            )

            if fill.total_usd < req.max_total_usdc:
                raise HTTPException(400, f"Fill proceeds {fill.total_usd:.6f} USDC below minimum {req.max_total_usdc:.6f}")

            if fill.total_usd > usdc_reserves:
                raise HTTPException(400, "Insufficient USDC reserves to fill this sell order")

            vsp_wei = int(req.qty_vsp * 10**18)

            # Execute permit if provided
            if req.permit:
                if req.permit.value < vsp_wei:
                    raise HTTPException(400, f"Permit value {req.permit.value} < needed {vsp_wei}")
                _execute_permit(
                    VSP_ADDRESS, req.user_address, MM_ADDRESS,
                    req.permit.value, req.permit.deadline,
                    req.permit.v, req.permit.r, req.permit.s,
                )
            else:
                if allowance(VSP_ADDRESS, req.user_address, MM_ADDRESS) < vsp_wei:
                    raise HTTPException(400, "VSP allowance too low — provide a permit signature")

            # Calculate fee (subtracted from USDC proceeds)
            fee_info = calc_fee(db, "sell", req.qty_vsp)
            fee_usdc = fee_info["fee_vsp"] * fill.avg_price_usd
            net_usdc = fill.total_usd - fee_usdc
            if net_usdc <= 0:
                raise HTTPException(400, "Trade too small to cover fees")

            # Execute on-chain transfers
            transfer_from(VSP_ADDRESS, req.user_address, MM_ADDRESS, vsp_wei)
            usdc_micro = int(net_usdc * 1_000_000)
            transfer(USDC_ADDRESS, req.user_address, usdc_micro)

            new_net = fill.new_net_vsp
            new_reserves = usdc_reserves - fill.total_usd
            new_circ = vsp_circulating - req.qty_vsp

            _update_mm_state(db, new_net, new_reserves, new_circ)
            _log_trade(db, side="sell", user_address=req.user_address,
                       qty_vsp=req.qty_vsp, total_usdc=fill.total_usd,
                       avg_price_usd=fill.avg_price_usd, net_vsp_before=net_vsp,
                       net_vsp_after=new_net, usdc_reserves_after=new_reserves,
                       vsp_circulating_after=new_circ)

        return {"ok": True, "qty_vsp": req.qty_vsp,
                "fee_vsp": fee_info["fee_vsp"],
                "fee_usdc": round(fee_usdc, 6),
                "gross_usdc": round(fill.total_usd, 6),
                "total_usdc": round(net_usdc, 6),
                "avg_price_usd": round(fill.avg_price_usd, 8)}
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"MM sell error: {e}")
        print(traceback.format_exc())
        raise HTTPException(500, f"Failed to sell VSP: {e}")