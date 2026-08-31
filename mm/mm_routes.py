# app/mm_routes.py
# ============================================================
# PROPRIETARY — Market Maker API routes.
# Do NOT reference in whitepaper or public documentation.
# ============================================================

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, Header, Request  # patch_postreview_mm_trade_limit: +Request
from pydantic import BaseModel, Field, AliasChoices, ConfigDict
from sqlalchemy.orm import Session
from sqlalchemy import text
sql_text = text  # alias: legacy code in this file uses both names

from db import get_db
from fee_calculator import compute_fee as calc_fee
from mm.erc20 import allowance, transfer, transfer_from
from config import USDC_ADDRESS, VSP_ADDRESS, MM_ADDRESS, TREASURY_ADDRESS, SERVICE_API_TOKEN, REQUIRE_SERVICE_TOKEN
from rate_limit import check_mm_trade, _client_ip  # patch_postreview_mm_trade_limit
from mm import mm_reconcile  # patch_f7rg_record_buy
from mm.mm_pricing import (
    get_spot_quote,
    compute_buy_fill,
    compute_sell_fill,
    get_floor_price,
    DEFAULT_UNIT_AU,
    DEFAULT_HALF_SPREAD,
)

import logging
import os
import hmac  # patch_followup_service_token_guard

logger = logging.getLogger(__name__)

# patch12a: chain-state helpers for vsp_circulating and usdc_reserves.
from chain.chain_reader import (
    read_vsp_circulating,
    read_usdc_reserves,
    read_vsp_balance_of_mm,
    read_usdc_balance_of_mm,
    invalidate_mm_chain_state_cache,
)

# patch_trackb_mm_darkgate: Track B kill-line for the entire MM surface. Default ON
# (unset/anything but "false") — zero behavior change until MM_ROUTES_ENABLED=false,
# at which point every /api/mm/* route returns 410 Gone (the public AMM replaces the
# MM). Distinct from the runtime halt flag (503 = temporary): 410 = decommissioned.
# Flip on Fuji during the mock rehearsal, then on mainnet at MM retirement.
MM_ROUTES_ENABLED = os.getenv("MM_ROUTES_ENABLED", "true").strip().lower() != "false"

def _require_mm_routes_enabled():
    if not MM_ROUTES_ENABLED:
        raise HTTPException(410, "MM trading is decommissioned; use the public AMM")

router = APIRouter(prefix="/api/mm", tags=["market-maker"],
                   dependencies=[Depends(_require_mm_routes_enabled)])

# patch_followup_service_token_guard: require X-Service-Token on the MM money
# endpoints when SERVICE_API_TOKEN is configured (prod). No-op when unset (dev).
def require_service_token(x_service_token: str | None = Header(default=None)):
    if not REQUIRE_SERVICE_TOKEN:
        return
    if not x_service_token or not hmac.compare_digest(x_service_token, SERVICE_API_TOKEN):
        raise HTTPException(status_code=401, detail="service token required")


# patch_bundle06_mm_sell_slippage_floor: server-side guard against a degenerate
# sell minimum. `min_total_usdc` on the sell side is the *minimum* NET proceeds
# the user will accept (gross - fee); Pydantic only enforces gt=0, so a broken
# FE or a direct-API caller can send a near-zero value that passes the
# `net < min` check with no real floor. We reject any sell whose stated minimum
# is below this fraction of the freshly-quoted NET fill (patch_g34_sell_net_guard
# moved this from gross to net). Loose default (0.80) catches fat-finger/
# degenerate values without rejecting normal sells (the FE sends ~0.99*net
# preview). NOTE: this compares against the execution-time net, so tightening
# toward 0.95 also makes
# a sharp *favorable* price move (fill rises far above the user's fixed minimum)
# trip the floor — harmless (the FE re-quotes and retries at the better price),
# but the reason to keep it loose for B1.
MM_SELL_SLIPPAGE_FLOOR_FRAC = float(os.getenv("MM_SELL_SLIPPAGE_FLOOR_FRAC", "0.95"))  # patch_f7rg_f8_slippage: 0.80 -> 0.95 (pre-mainnet tighten; FE sends ~0.99*net)


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def _load_mm_state(db: Session, *, for_update: bool = False):
    """
    Returns (net_vsp, unit_au, half_spread, usdc_reserves, vsp_circulating).

    All values except unit_au and half_spread are derived from chain:
      usdc_reserves    = balanceOf(MM) + Σ balanceOf(cold safes)  [USDC]
      vsp_circulating  = totalSupply - balanceOf(MM)              [VSPToken]
      net_vsp          = round(vsp_circulating)
        -- In current accounting net_vsp == vsp_circulating (the n<0 branch
        -- in pricing is unreachable). Returned as int for legacy callers
        -- (compute_buy_fill/compute_sell_fill/get_spot_quote take int).

    unit_au and half_spread are MM parameters from the mm_state DB row.

    Raises HTTP 503 if MM state row is missing OR if the chain RPC
    is unreachable. We fail closed rather than serve mispriced
    quotes from stale tracked values.
    """
    suffix = " FOR UPDATE" if for_update else ""
    row = db.execute(
        text(
            "SELECT unit_au, half_spread "
            f"FROM mm_state WHERE id = TRUE{suffix}"
        )
    ).fetchone()
    if not row:
        raise HTTPException(503, "MM state not initialized")
    unit_au, half_spread = row

    try:
        usdc_reserves = read_usdc_reserves()
        vsp_circulating = read_vsp_circulating()
    except Exception as e:
        logger.error("MM chain-state read failed: %s", e)
        raise HTTPException(503, f"MM chain state unavailable: {e}")

    net_vsp = int(round(vsp_circulating))
    return (net_vsp, unit_au, half_spread, usdc_reserves, vsp_circulating)


def _update_mm_state(db, net_vsp=None, usdc_reserves=None, vsp_circulating=None):
    """
    Touches updated_at on the mm_state row. Previously also wrote net_vsp,
    usdc_reserves, vsp_circulating — those columns were dropped in migration
    031 (derived from chain instead). Args retained for caller compatibility
    but unused; callers may be cleaned up in a future pass.
    """
    db.execute(
        text("UPDATE mm_state SET updated_at = now() WHERE id = TRUE")
    )


def _log_trade(db, *, side, user_address, qty_vsp, total_usdc, avg_price_usd,
               net_vsp_before, net_vsp_after, usdc_reserves_after, vsp_circulating_after,
               tx_hash=None, fee_usdc=None):  # patch_bundle04_5_p21_mm_log_trade_tx_hash + patch_bundle04_5_p33_log_trade_sig
    txh = None
    if tx_hash:
        txh = tx_hash.lower() if tx_hash.startswith("0x") else ("0x" + tx_hash.lower())
    db.execute(
        text(
            # patch_bundle04_5_p33_log_trade_sql
            "INSERT INTO mm_trade "
            "(side, user_address, qty_vsp, total_usdc, avg_price_usd, "
            " net_vsp_before, net_vsp_after, usdc_reserves_after, vsp_circulating_after, "
            " tx_hash, fee_usdc) "
            "VALUES (:side, :user, :qty, :total, :avg, :nb, :na, :ra, :ca, :tx, :fee)"
        ),
        {"side": side, "user": user_address, "qty": qty_vsp, "total": total_usdc,
         "avg": avg_price_usd, "nb": net_vsp_before, "na": net_vsp_after,
         "ra": usdc_reserves_after, "ca": vsp_circulating_after, "tx": txh,
         "fee": fee_usdc},
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
        floor = get_floor_price(usdc_reserves, vsp_circulating, unit_au=unit_au, net_vsp=net_vsp, half_spread=half_spread)
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

# patch_bundle10c_backend_hardening_mm: /quote is a misleading name (takes no params,
# returns spot prices only). Added /spot as the documented name;
# /quote retained as alias for backwards compatibility with the
# frontend VSPMarketWidget and api/mm.ts callers.
@router.get("/spot")
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


class MMBuyRequest(BaseModel):  # patch_g34_model_split (was MMTradeRequest)
    user_address: str
    qty_vsp: float = Field(..., gt=0)
    max_total_usdc: float = Field(..., gt=0)  # all-in USDC ceiling (gross + fee)
    permit: PermitFields | None = None  # Optional — if provided, MM executes permit first


class MMSellRequest(BaseModel):  # patch_g34_model_split
    # G-34: the sell limit is the *minimum* NET proceeds the user will
    # accept (gross - fee == what they receive). Renamed from the
    # misnamed max_total_usdc; the old name is accepted as a transition
    # alias so the frontend and backend need not deploy atomically.
    model_config = ConfigDict(populate_by_name=True)
    user_address: str
    qty_vsp: float = Field(..., gt=0)
    min_total_usdc: float = Field(
        ..., gt=0,
        validation_alias=AliasChoices("min_total_usdc", "max_total_usdc"),
    )
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
        # patch_bundle09_p3_preview_buy_convergence: one-sided convergence + defensive shave + post-loop consistency
        # Old (buggy) behaviour: symmetric abs() convergence could land slightly
        # over budget; on non-convergence the returned (qty_vsp, total_usdc) pair
        # was internally inconsistent (qty from iter N, total from iter N-1).
        # Both led to FE confirm-time `usdcBalance < preview.total_usdc` failures.
        fill1 = compute_buy_fill(net_vsp, 1.0, usdc_reserves, vsp_circulating, unit_au, half_spread)
        price = fill1.avg_price_usd
        qty_est = usdc_amount / price

        # Helper: compute fill+fee+total for a given qty_est. Pure function of
        # inputs; safe to call multiple times.
        def _price_qty(q: float):
            f = compute_buy_fill(net_vsp, q, usdc_reserves, vsp_circulating, unit_au, half_spread)
            ff = calc_fee(db, "buy", q)
            fu = ff["fee_vsp"] * f.avg_price_usd
            return f, ff, fu, f.total_usd + fu

        TOLERANCE = 0.001  # USDC
        converged = False
        for _ in range(10):
            fill, fee, fee_usdc, total = _price_qty(qty_est)
            # One-sided convergence: must be at or below budget, within tolerance.
            if total <= usdc_amount and (usdc_amount - total) < TOLERANCE:
                converged = True
                break
            # Newton-style rescale; safe even when total > usdc_amount.
            qty_est *= usdc_amount / total
            qty_est = max(qty_est, 0.001)

        if not converged:
            # Defensive shave: trim qty_est by 0.1% per step until it fits.
            # 30 steps * 0.1% = ~3% total reduction, enough to clear any
            # realistic post-loop overshoot.
            for _ in range(30):
                fill, fee, fee_usdc, total = _price_qty(qty_est)
                if total <= usdc_amount:
                    break
                qty_est *= 0.999
                if qty_est < 0.001:
                    break

        # If we still can't fit, the budget is too small to buy any VSP after
        # fees. Surface as a clear error rather than returning misleading numbers.
        if total > usdc_amount or qty_est < 0.001:
            raise HTTPException(
                400,
                f"Budget {usdc_amount:.6f} USDC too small to buy any VSP after fees "
                f"(minimum needed ~{total:.6f} USDC at this market state)",
            )

        # Snap qty to the same 6dp precision the FE will see, then do one final
        # consistency pass. This guarantees (qty_vsp, total_usdc) match exactly
        # at the precision the response carries.
        qty_est = round(qty_est, 6)
        fill, fee, fee_usdc, total = _price_qty(qty_est)
        # Edge case: rounding up by 6dp may have pushed total back over budget by
        # sub-cent fractions. If so, shave once more.
        if total > usdc_amount:
            qty_est = round(qty_est - 0.000001, 6)
            qty_est = max(qty_est, 0.001)
            fill, fee, fee_usdc, total = _price_qty(qty_est)
            if total > usdc_amount:
                raise HTTPException(
                    400,
                    f"Budget {usdc_amount:.6f} USDC too small after 6dp snap "
                    f"(needed ~{total:.6f})",
                )

        return {
            "mode": "usdc",
            "usdc_budget": usdc_amount,
            "qty_vsp": qty_est,
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

# patch_killswitch_mm_halt: runtime trading-halt guard. Trip by creating the flag file
# (default /control/mm_trading.halt); mm_buy/mm_sell return 503 while it
# exists. Read-only endpoints (quote/floor/preview) stay up during a halt.
def _mm_halt_guard() -> None:
    _flag = os.getenv("MM_HALT_FLAG_PATH", "/control/mm_trading.halt")
    if os.path.exists(_flag):
        raise HTTPException(503, "MM trading is temporarily halted")


@router.post("/buy", dependencies=[Depends(require_service_token)])
def mm_buy(req: MMBuyRequest, request: Request, db: Session = Depends(get_db)):
    """
    Buy VSP with USDC.
    If permit is provided, MM executes USDC.permit() first (gasless for user).
    Otherwise, falls back to checking existing allowance.
    """
    _mm_halt_guard()  # patch_killswitch_mm_halt
    check_mm_trade(_client_ip(request), req.user_address)  # patch_postreview_mm_trade_limit
    try:
        with db.begin():
            row = _load_mm_state(db, for_update=True)
            net_vsp, unit_au, half_spread, usdc_reserves, vsp_circulating = row

            fill = compute_buy_fill(
                net_vsp, req.qty_vsp, usdc_reserves, vsp_circulating, unit_au, half_spread,
            )

            # patch_g34_buy_allin_guard: fee is additive on buy (user pays
            # gross + fee), so the slippage cap the FE sends (all-in total *
            # buffer) must be enforced against the all-in cost, not the gross
            # fill. Compute the fee here, up front, so the guard compares like
            # for like. (Was `fill.total_usd > req.max_total_usdc` — loose by
            # exactly one fee.)
            fee_info = calc_fee(db, "buy", req.qty_vsp)
            fee_usdc = fee_info["fee_vsp"] * fill.avg_price_usd
            total_usdc_with_fee = fill.total_usd + fee_usdc
            if total_usdc_with_fee > req.max_total_usdc:
                raise HTTPException(400, f"All-in cost {total_usdc_with_fee:.6f} USDC (incl. {fee_usdc:.6f} fee) exceeds max {req.max_total_usdc:.6f}")
            # patch_bundle04_6_mm_buy_balance_guard: read MM's live VSP balance BEFORE any
            # on-chain transfer. mm_buy is a three-leg flow (USDC reserves
            # in, USDC fee in, VSP out); if the third leg reverts because MM
            # is out of VSP, the user's USDC is already gone on-chain. This
            # guard fails loud first so no user funds move.
            try:
                mm_vsp_balance = read_vsp_balance_of_mm()
            except Exception as e:
                raise HTTPException(503, f"MM balance check failed: {e}")
            if mm_vsp_balance < req.qty_vsp:
                raise HTTPException(
                    503,
                    f"MM temporarily out of VSP — please retry later "
                    f"(have {mm_vsp_balance:.4f}, need {req.qty_vsp:.4f})",
                )

            usdc_micro = int(fill.total_usd * 1_000_000)
            # patch_buy_permit_allin: both transfer_from legs (reserves to MM,
            # fee to treasury) draw on the user's USDC allowance to MM, so the
            # permit/allowance must cover the ALL-IN amount (gross + fee), not
            # just the gross reserves. Was `< usdc_micro` (gross): a permit
            # signed for exactly gross passed, then reverted on the fee leg
            # on-chain. needed_micro mirrors reserves_micro + fee_micro exactly.
            needed_micro = usdc_micro + int(fee_usdc * 1_000_000)

            # Execute permit if provided
            if req.permit:
                if req.permit.value < needed_micro:
                    raise HTTPException(400, f"Permit value {req.permit.value} < needed {needed_micro} (all-in: gross {usdc_micro} + fee {int(fee_usdc * 1_000_000)})")
                _execute_permit(
                    USDC_ADDRESS, req.user_address, MM_ADDRESS,
                    req.permit.value, req.permit.deadline,
                    req.permit.v, req.permit.r, req.permit.s,
                )
            else:
                # Legacy: check existing allowance
                if allowance(USDC_ADDRESS, req.user_address, MM_ADDRESS) < needed_micro:
                    raise HTTPException(400, "USDC allowance too low — provide a permit signature")


            # Fee + all-in total already computed above (patch_g34_buy_allin_guard).
            # Split: reserves to MM, fee to treasury
            reserves_micro = int(fill.total_usd * 1_000_000)
            fee_micro = int(fee_usdc * 1_000_000)

            # Execute on-chain transfers.
            # patch_f7rg_record_buy: F-7 non-atomic-trade recording. Open a durable
            # partial-trade row BEFORE any leg moves; stamp each leg's hash as it
            # lands; close it as 'noop' once all legs are through. If a later leg
            # throws, the row survives (own session, eager commit) and the worker
            # reconciler completes-or-refunds. Recording never blocks a trade: any
            # bookkeeping error is swallowed so the trade path is unaffected.
            _f7_idem = mm_reconcile.make_idem_key("buy", req.user_address, req.qty_vsp)
            _f7_row = mm_reconcile.open_partial(
                side="buy", user_address=req.user_address, qty_vsp=req.qty_vsp,
                reserves_micro=reserves_micro, fee_micro=fee_micro,
                vsp_wei=int(req.qty_vsp * 10**18), idem_key=_f7_idem)
            _h = transfer_from(USDC_ADDRESS, req.user_address, MM_ADDRESS, reserves_micro)
            mm_reconcile.mark_leg(_f7_row, "leg_principal_in_tx", _h)
            if fee_micro > 0 and TREASURY_ADDRESS.lower() != MM_ADDRESS.lower():
                _hf = transfer_from(USDC_ADDRESS, req.user_address, TREASURY_ADDRESS, fee_micro)
                mm_reconcile.mark_leg(_f7_row, "leg_fee_in_tx", _hf)
            elif fee_micro > 0:
                _hf = transfer_from(USDC_ADDRESS, req.user_address, MM_ADDRESS, fee_micro)
                mm_reconcile.mark_leg(_f7_row, "leg_fee_in_tx", _hf)
            _buy_vsp_tx_hash = transfer(VSP_ADDRESS, req.user_address, int(req.qty_vsp * 10**18))  # patch_bundle04_5_p21_mm_buy_tx_hash
            mm_reconcile.mark_leg(_f7_row, "leg_payout_out_tx", _buy_vsp_tx_hash)
            mm_reconcile.close_partial_ok(_f7_row)

            # Force next chain read to skip cache, then re-read chain state
            # so the audit log records observed-on-chain values rather than
            # DB-derived arithmetic (which drifts).
            invalidate_mm_chain_state_cache()
            try:
                new_reserves = read_usdc_reserves()
                new_circ = read_vsp_circulating()
            except Exception as e:
                logger.warning("Post-buy chain reread failed (%s); using arithmetic", e)
                new_reserves = usdc_reserves + fill.total_usd
                new_circ = vsp_circulating + req.qty_vsp
            new_net = int(round(new_circ))

            # mm_state has no live state to update post-031; just touch updated_at.
            _update_mm_state(db)
            _log_trade(db, side="buy", user_address=req.user_address,
                       qty_vsp=req.qty_vsp, total_usdc=fill.total_usd,
                       avg_price_usd=fill.avg_price_usd, net_vsp_before=net_vsp,
                       net_vsp_after=new_net, usdc_reserves_after=new_reserves,
                       vsp_circulating_after=new_circ,
                       tx_hash=_buy_vsp_tx_hash,
                       fee_usdc=fee_usdc)  # patch_bundle04_5_p33_buy_call

            # Track trade fee separately from reserves
            # patch_bundle10c_backend_hardening_mm_buy_dedup: duplicate UPDATE block removed;
            # this counter previously double-counted every buy fee.
            try:
                db.execute(sql_text(
                    "UPDATE mm_state SET fees_collected_usdc = "
                    "COALESCE(fees_collected_usdc, 0) + :fee"
                ), {"fee": fee_usdc})
            except Exception:
                pass

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


@router.post("/sell", dependencies=[Depends(require_service_token)])
def mm_sell(req: MMSellRequest, request: Request, db: Session = Depends(get_db)):
    """
    Sell VSP for USDC.
    If permit is provided, MM executes VSP.permit() first (gasless for user).
    Otherwise, falls back to checking existing allowance.
    """
    _mm_halt_guard()  # patch_killswitch_mm_halt
    check_mm_trade(_client_ip(request), req.user_address)  # patch_postreview_mm_trade_limit
    try:
        with db.begin():
            row = _load_mm_state(db, for_update=True)
            net_vsp, unit_au, half_spread, usdc_reserves, vsp_circulating = row

            fill = compute_sell_fill(
                net_vsp, req.qty_vsp, usdc_reserves, vsp_circulating, unit_au, half_spread,
            )

            # patch_g34_sell_net_guard: the user's minimum applies to NET
            # proceeds (gross - fee) — what they actually receive and what the
            # FE quotes as "you receive". Compute the fee up front so both the
            # degenerate-minimum floor and the user's stated minimum compare on
            # net. (Was: both compared req.max_total_usdc to the gross fill,
            # under-protecting the user by exactly one fee.)
            fee_info = calc_fee(db, "sell", req.qty_vsp)
            fee_usdc = fee_info["fee_vsp"] * fill.avg_price_usd
            net_usdc = fill.total_usd - fee_usdc
            if net_usdc <= 0:
                raise HTTPException(400, "Trade too small to cover fees")

            # patch_bundle06_mm_sell_slippage_floor (retained, now net-based):
            # reject a degenerate minimum (near-zero min_total_usdc) before the
            # user's stated-minimum check.
            _sell_floor = MM_SELL_SLIPPAGE_FLOOR_FRAC * net_usdc
            if req.min_total_usdc < _sell_floor:
                raise HTTPException(
                    400,
                    f"Sell minimum {req.min_total_usdc:.6f} USDC is below the slippage "
                    f"floor {_sell_floor:.6f} ({MM_SELL_SLIPPAGE_FLOOR_FRAC:.0%} of net "
                    f"{net_usdc:.6f} USDC); refresh the quote and retry",
                )

            if net_usdc < req.min_total_usdc:
                raise HTTPException(400, f"Net proceeds {net_usdc:.6f} USDC below minimum {req.min_total_usdc:.6f}")

            if fill.total_usd > usdc_reserves:
                raise HTTPException(400, "Insufficient USDC reserves to fill this sell order")
            # patch_bundle04_6_mm_sell_balance_guard: tight on-chain USDC balance check,
            # BEFORE the user's VSP is pulled. The cached check above
            # uses read_usdc_reserves() (hot+cold sum) and catches the
            # formal-reserves-too-low case; this read is hot-wallet ONLY
            # and uncached, catching the case where reserves are formally
            # sufficient (cold safes hold the difference) but the hot
            # wallet currently can't pay out. We guard against fill.total_usd
            # (gross) because total USDC leaving MM == net (to user) + fee (to
            # treasury) == gross; gross is the real outflow, not a stale value.
            try:
                mm_usdc_balance = read_usdc_balance_of_mm()
            except Exception as e:
                raise HTTPException(503, f"MM balance check failed: {e}")
            if mm_usdc_balance < fill.total_usd:
                raise HTTPException(
                    503,
                    f"MM temporarily out of USDC — please retry later "
                    f"(have {mm_usdc_balance:.6f}, need {fill.total_usd:.6f})",
                )

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

            # Fee + net already computed above (patch_g34_sell_net_guard).

            # Execute on-chain transfers.
            # patch_f7rg_record_sell + patch_sellfee_recording: F-7 recording of
            # all THREE sell legs (user VSP in -> net USDC to user -> fee MM->sink).
            # The fee leg previously sat OUTSIDE the envelope (intent said
            # fee_micro=0 and closed before the fee transfer): a failed fee leg
            # 500'd after the user was fully paid, with no durable record that the
            # sink was short. Ordering stays USER-FIRST: the payout precedes the
            # fee, so a fee-leg failure never delays or reduces what the user gets.
            usdc_micro = int(net_usdc * 1_000_000)
            fee_micro = int(fee_usdc * 1_000_000)
            _fee_leg_active = fee_micro > 0 and TREASURY_ADDRESS.lower() != MM_ADDRESS.lower()
            _f7s_idem = mm_reconcile.make_idem_key("sell", req.user_address, req.qty_vsp)
            _f7s_row = mm_reconcile.open_partial(
                side="sell", user_address=req.user_address, qty_vsp=req.qty_vsp,
                reserves_micro=usdc_micro,
                fee_micro=fee_micro if _fee_leg_active else 0,
                vsp_wei=vsp_wei, idem_key=_f7s_idem)
            _sell_vsp_tx_hash = transfer_from(VSP_ADDRESS, req.user_address, MM_ADDRESS, vsp_wei)  # patch_bundle04_5_p21_mm_sell_tx_hash
            mm_reconcile.mark_leg(_f7s_row, "leg_principal_in_tx", _sell_vsp_tx_hash)
            _payout_h = transfer(USDC_ADDRESS, req.user_address, usdc_micro)
            mm_reconcile.mark_leg(_f7s_row, "leg_payout_out_tx", _payout_h)
            # Send fee to treasury — now inside the F-7 envelope
            if _fee_leg_active:
                _fee_h = transfer(USDC_ADDRESS, TREASURY_ADDRESS, fee_micro)
                mm_reconcile.mark_leg(_f7s_row, "leg_fee_in_tx", _fee_h)
            mm_reconcile.close_partial_ok(_f7s_row)

            # Invalidate then re-read chain so audit log records observed
            # post-trade reality (not DB arithmetic, which drifts).
            invalidate_mm_chain_state_cache()
            try:
                new_reserves = read_usdc_reserves()
                new_circ = read_vsp_circulating()
            except Exception as e:
                logger.warning("Post-sell chain reread failed (%s); using arithmetic", e)
                new_reserves = usdc_reserves - fill.total_usd
                new_circ = vsp_circulating - req.qty_vsp
            new_net = int(round(new_circ))

            # mm_state has no live state to update post-031; just touch updated_at.
            _update_mm_state(db)
            # Track trade fee separately
            # patch_bundle10c_backend_hardening_mm_sell_dedup: duplicate UPDATE block removed;
            # this counter previously double-counted every sell fee.
            try:
                db.execute(sql_text(
                    "UPDATE mm_state SET fees_collected_usdc = "
                    "COALESCE(fees_collected_usdc, 0) + :fee"
                ), {"fee": fee_usdc})
            except Exception:
                pass

            _log_trade(db, side="sell", user_address=req.user_address,
                       qty_vsp=req.qty_vsp, total_usdc=fill.total_usd,
                       avg_price_usd=fill.avg_price_usd, net_vsp_before=net_vsp,
                       net_vsp_after=new_net, usdc_reserves_after=new_reserves,
                       vsp_circulating_after=new_circ,
                       tx_hash=_sell_vsp_tx_hash,
                       fee_usdc=fee_usdc)  # patch_bundle04_5_p33_sell_call

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




class ExecutePermitRequest(BaseModel):
    token: str
    owner: str
    spender: str
    value: str  # String to handle large ints
    deadline: int
    v: int
    r: str
    s: str

@router.post("/execute-permit", dependencies=[Depends(require_service_token)])
def mm_execute_permit(req: ExecutePermitRequest, db: Session = Depends(get_db)):
    """Execute an ERC-2612 permit. MM wallet pays gas.
    Used by batch tool to set VSPToken allowances without going through the relay."""
    from web3 import Web3
    _execute_permit(
        req.token,
        Web3.to_checksum_address(req.owner),
        Web3.to_checksum_address(req.spender),
        int(req.value),
        req.deadline,
        req.v,
        req.r,
        req.s,
    )
    return {"ok": True, "owner": req.owner, "spender": req.spender}

class TransferRequest(BaseModel):
    from_address: str
    to_address: str
    amount_vsp: float
    permit: PermitFields

@router.post("/transfer")
def mm_transfer():
    # DEPRECATED 2026-06-11 (410 Gone): wallet-to-wallet VSP transfer is not a
    # Verisphere protocol operation. VSP is a standard ERC-20 -- move it directly
    # from a wallet. The MM no longer brokers transfers; the old
    # permit -> transferFrom -> transfer flow (and its post-on-chain 500) is gone.
    raise HTTPException(
        status_code=410,
        detail=(
            "Endpoint removed: VSP transfers are not a Verisphere operation. "
            "VSP is a standard ERC-20 token; transfer it directly from your wallet."
        ),
    )


@router.get("/fee-summary")
def fee_summary(db: Session = Depends(get_db)):
    """All collected fees, gas costs, and 24h activity."""
    row = db.execute(sql_text(
        "SELECT fees_collected_usdc, fees_collected_vsp, "
        "relay_fees_collected_vsp, total_gas_spent_avax "
        "FROM mm_state LIMIT 1"
    )).fetchone()

    recent = db.execute(sql_text(
        "SELECT COUNT(*), COALESCE(SUM(fee_charged_vsp), 0), "
        "COALESCE(SUM(gas_cost_avax), 0), COALESCE(AVG(gas_cost_usd), 0) "
        "FROM relay_fee_log WHERE created_at > NOW() - INTERVAL '24 hours'"
    )).fetchone()

    return {
        "trade_fees_usdc": round((row[0] or 0) if row else 0, 4),
        "trade_fees_vsp": round((row[1] or 0) if row else 0, 4),
        "relay_fees_vsp": round((row[2] or 0) if row else 0, 4),
        "total_gas_avax": round((row[3] or 0) if row else 0, 6),
        "last_24h": {
            "relay_count": recent[0] if recent else 0,
            "relay_fees_vsp": round(recent[1], 4) if recent else 0,
            "gas_spent_avax": round(recent[2], 6) if recent else 0,
            "avg_gas_usd": round(recent[3], 4) if recent else 0,
        }
    }
