# app/mm/mm_pricing.py
# ============================================================
# PROPRIETARY — Do not reference in whitepaper or public docs.
# ============================================================
#
# Market Maker pricing engine for VSP/USDC.
#
# Design principles:
#   1. Pricing formula is proprietary and unpublished.
#   2. A real-time liquidation floor (reserves / circulating supply)
#      is always publicly available via /api/mm/floor.
#   3. For net_vsp >= 0: log-squared supply curve (gold-anchored).
#      For net_vsp < 0: smooth reserve distribution curve where
#      earlier sellers get better prices.
#   4. Fills are volume-integrated along the curve, not point-priced.
#      The spread is preserved across any order size.
#
# The MM wallet holds USDC reserves and VSP inventory.
# net_vsp = total VSP sold to market minus total VSP bought back.

from __future__ import annotations

import math
import os
import logging
from dataclasses import dataclass
from typing import Optional

from mm.oracle import get_gold_price_usd_per_oz, get_avax_price_usd


# ────────────────────────────────────────────────────────────
# Configuration (loaded from mm_state or defaults)
# ────────────────────────────────────────────────────────────

DEFAULT_UNIT_AU = 0.0001        # Fraction of troy oz per unit
DEFAULT_HALF_SPREAD = 0.0025   # 0.25% half-spread → 0.5% round trip


# ────────────────────────────────────────────────────────────
# Pricing exponent (12c)
# ────────────────────────────────────────────────────────────
#
# Controls the steepness of the supply curve:
#   price(n) = log10(n + 10) ** _PRICE_EXPONENT * unit_au * gold_usd
#
# Default is 2.5 — the live launch value (matches env/common.env). Set MM_PRICE_EXPONENT in
# the environment to override; any positive rational ≤ 10 works.
# Useful for A/B and rollback. The variable is read once at module
# import, so changes require an app restart.
#
# Negative-n pricing (the reserve curve) is unaffected by this
# exponent — that branch uses the constant-product reserve model,
# not the supply curve.

_PRICE_EXPONENT = float(os.getenv("MM_PRICE_EXPONENT", "2.5"))
if not (0.0 < _PRICE_EXPONENT <= 10.0):
    raise ValueError(
        f"MM_PRICE_EXPONENT must be in (0, 10]; got {_PRICE_EXPONENT!r}"
    )

# Epsilon below which a negative net_vsp value is treated as zero.
# Guards against floating-point artifacts in volume integration
# (e.g. 10 - 99*0.1 evaluates to 0.0999... not 0.1, and a subsequent
# subtraction can cross slightly below zero, falsely triggering the
# reserve curve). Well above float step-size noise; well below any
# meaningful order quantity.
_NET_VSP_EPSILON = 1e-9

logging.getLogger(__name__).info(
    "MM pricing initialised: exponent=%s (set MM_PRICE_EXPONENT to override)",
    _PRICE_EXPONENT,
)


# ────────────────────────────────────────────────────────────
# Core price curve
# ────────────────────────────────────────────────────────────

def _base_price(n: float, gold_usd: float, unit_au: float) -> float:
    """
    Base (mid) price at a given net_vsp position.

    For n >= 0: log-power supply curve anchored to gold.
        price = log10(n + 10) ** _PRICE_EXPONENT * unit_au * gold_usd
        (default _PRICE_EXPONENT = 2.5; configurable via env)

    For n < 0: reserve distribution curve.
        Handled separately in _reserve_price().

    Returns price in USD per 1 VSP.
    """
    if n >= 0:
        return math.log10(n + 10) ** _PRICE_EXPONENT * unit_au * gold_usd
    # Negative territory handled by caller via _reserve_price
    # This shouldn't be reached, but defensive:
    return unit_au * gold_usd * 0.01


def _reserve_price(
    n: float,
    usdc_reserves: float,
    vsp_circulating: float,
) -> float:
    """
    Price when net_vsp < 0 (MM is absorbing more VSP than it sold).

    Uses constant-product reserve model:
        price = reserves_remaining / supply_remaining

    At n=0 (transition point), this equals the floor price.
    As more is sold back (n goes more negative), price decreases
    because reserves shrink faster than supply.

    The first seller below n=0 gets the best price (close to floor).
    The last seller gets the worst price (approaching 0).
    """
    if vsp_circulating <= 0 or usdc_reserves <= 0:
        return 0.0

    # How far below zero we are, as a fraction of circulating supply
    absorbed = abs(n)
    remaining_fraction = max(0.0, 1.0 - (absorbed / vsp_circulating))

    # Constant-product: price = R * remaining / S
    # This ensures integral of price over all sells = total reserves
    floor = usdc_reserves / vsp_circulating
    return floor * remaining_fraction


# ────────────────────────────────────────────────────────────
# Volume-integrated fills
# ────────────────────────────────────────────────────────────

def _integrate_buy_cost(
    n_start: float,
    qty: float,
    gold_usd: float,
    unit_au: float,
    half_spread: float,
    steps: int = 100,
) -> float:
    """
    Integrate buy price along the curve from n_start to n_start + qty.
    Buy price = base_price * (1 + half_spread) at each point.

    Returns total USDC cost for the entire order.
    Uses trapezoidal integration with `steps` intervals.
    """
    if qty <= 0:
        return 0.0

    step_size = qty / steps
    total = 0.0

    for i in range(steps):
        n_lo = n_start + i * step_size
        n_hi = n_lo + step_size
        p_lo = _base_price(n_lo, gold_usd, unit_au) * (1 + half_spread)
        p_hi = _base_price(n_hi, gold_usd, unit_au) * (1 + half_spread)
        total += (p_lo + p_hi) / 2 * step_size

    return total


def _integrate_sell_proceeds(
    n_start: float,
    qty: float,
    gold_usd: float,
    unit_au: float,
    half_spread: float,
    usdc_reserves: float,
    vsp_circulating: float,
    steps: int = 100,
) -> float:
    """
    Integrate sell price along the curve from n_start down to n_start - qty.
    Sell price = base_price * (1 - half_spread) at each point.

    For n >= 0: uses the standard supply curve.
    For n < 0: transitions to reserve distribution curve.

    Returns total USDC proceeds for the entire order.
    """
    if qty <= 0:
        return 0.0

    step_size = qty / steps
    total = 0.0

    for i in range(steps):
        n_hi = n_start - i * step_size
        n_lo = n_hi - step_size

        p_hi = _sell_price_at(
            n_hi, gold_usd, unit_au, half_spread,
            usdc_reserves, vsp_circulating,
        )
        p_lo = _sell_price_at(
            n_lo, gold_usd, unit_au, half_spread,
            usdc_reserves, vsp_circulating,
        )
        total += (p_hi + p_lo) / 2 * step_size

    return max(0.0, total)


def _sell_price_at(
    n: float,
    gold_usd: float,
    unit_au: float,
    half_spread: float,
    usdc_reserves: float,
    vsp_circulating: float,
) -> float:
    """Sell price at a specific net_vsp position.

    Note: n values within [-_NET_VSP_EPSILON, 0) are treated as zero
    to handle floating-point artifacts from volume integration. A real
    negative position requires |n| >> 1e-9, which any meaningful order
    will produce.
    """
    if n >= -_NET_VSP_EPSILON:
        return _base_price(max(n, 0.0), gold_usd, unit_au) * (1 - half_spread)
    else:
        # Below zero: use reserve distribution, still apply spread
        reserve_p = _reserve_price(n, usdc_reserves, vsp_circulating)
        return reserve_p * (1 - half_spread)


def _coverage_ratio(
    net_vsp: float,
    usdc_reserves: float,
    gold_usd: float,
    unit_au: float,
    half_spread: float,
    vsp_circulating: float,
) -> float:
    """patch_bundle11_reserve_dampening: buyback reserve-coverage ratio k.

    k = min(1, R / C_full(n)) where C_full(n) is the curve cost to buy back
    the ENTIRE circulating supply (integral of the sell curve from 0..n).
    Buyback prices are the supply curve scaled by k, so the MM stays solvent
    in aggregate for any liquidation (draining everything pays exactly R).
    k is invariant as holders sell -> path-independent / splitting-resistant.

    net_vsp <= 0 (nothing circulating, or the legacy reserve branch) returns
    1.0: no extra dampening, continuous with the n -> 0+ limit where
    C_full -> 0 and R / C_full -> inf (clamped to 1).
    """
    if usdc_reserves <= 0:
        return 0.0
    if net_vsp <= 0:
        return 1.0
    c_full = _integrate_sell_proceeds(
        net_vsp, net_vsp, gold_usd, unit_au, half_spread,
        usdc_reserves, vsp_circulating,
    )
    if c_full <= 0:
        return 1.0
    return min(1.0, usdc_reserves / c_full)


# ────────────────────────────────────────────────────────────
# Public interface
# ────────────────────────────────────────────────────────────

@dataclass
class MMQuote:
    """Quote for a specific order or spot price."""
    mid_price_usd: float       # Mid price at current net_vsp
    buy_price_usd: float       # Spot buy price (per VSP)
    sell_price_usd: float      # Spot sell price (per VSP)
    floor_price_usd: float     # Liquidation floor (reserves / supply)
    gold_usd_per_oz: float
    avax_usd: float
    buy_avax: float
    sell_avax: float


@dataclass
class MMFillResult:
    """Result of a volume-integrated fill."""
    total_usd: float           # Total cost (buy) or proceeds (sell)
    avg_price_usd: float       # Average price per VSP
    qty_vsp: float             # Quantity filled
    new_net_vsp: int           # Updated net_vsp after fill


def get_spot_quote(
    net_vsp: int,
    usdc_reserves: float,
    vsp_circulating: float,
    unit_au: float = DEFAULT_UNIT_AU,
    half_spread: float = DEFAULT_HALF_SPREAD,
) -> MMQuote:
    """
    Get current spot prices (for display / quote endpoint).
    These are indicative — actual fills use volume integration.
    """
    gold = get_gold_price_usd_per_oz()
    avax = get_avax_price_usd()

    n = float(net_vsp)

    if n >= 0:
        mid = _base_price(n, gold, unit_au)
    else:
        mid = _reserve_price(n, usdc_reserves, vsp_circulating)

    # Defensive floor
    if mid <= 0:
        mid = unit_au * gold * 0.01

    buy = mid * (1 + half_spread)
    # patch_bundle11_reserve_dampening: spot buyback quote scaled by k.
    _k_spot = _coverage_ratio(n, usdc_reserves, gold, unit_au, half_spread, vsp_circulating)
    sell_p = mid * (1 - half_spread) * _k_spot

    # Floor semantics: the maximum buyback price Verisphere can guarantee
    # if a holder liquidates back to the MM. That is min(reserve_floor,
    # sell_p) — bounded above by the current sell quote because the MM
    # never quotes a buyback above the pricing curve, even when reserves
    # would theoretically support a higher ratio. Confirmed 2026-05-15.
    reserve_floor = usdc_reserves / vsp_circulating if vsp_circulating > 0 else 0.0
    floor = min(reserve_floor, sell_p)

    return MMQuote(
        mid_price_usd=mid,
        buy_price_usd=buy,
        sell_price_usd=sell_p,
        floor_price_usd=floor,
        gold_usd_per_oz=gold,
        avax_usd=avax,
        buy_avax=buy / avax if avax > 0 else 0.0,
        sell_avax=sell_p / avax if avax > 0 else 0.0,
    )


def compute_buy_fill(
    net_vsp: int,
    qty_vsp: float,
    usdc_reserves: float,
    vsp_circulating: float,
    unit_au: float = DEFAULT_UNIT_AU,
    half_spread: float = DEFAULT_HALF_SPREAD,
) -> MMFillResult:
    """
    Compute the total USDC cost to buy `qty_vsp` VSP.
    Integrates along the buy curve from net_vsp to net_vsp + qty.
    """
    gold = get_gold_price_usd_per_oz()
    n = float(net_vsp)

    total_cost = _integrate_buy_cost(n, qty_vsp, gold, unit_au, half_spread)
    avg_price = total_cost / qty_vsp if qty_vsp > 0 else 0.0

    return MMFillResult(
        total_usd=total_cost,
        avg_price_usd=avg_price,
        qty_vsp=qty_vsp,
        new_net_vsp=net_vsp + int(qty_vsp),
    )


def compute_sell_fill(
    net_vsp: int,
    qty_vsp: float,
    usdc_reserves: float,
    vsp_circulating: float,
    unit_au: float = DEFAULT_UNIT_AU,
    half_spread: float = DEFAULT_HALF_SPREAD,
) -> MMFillResult:
    """
    Compute the total USDC proceeds from selling `qty_vsp` VSP.
    Integrates along the sell curve from net_vsp down to net_vsp - qty.

    For sells that cross n=0, the integration naturally transitions
    from the supply curve to the reserve distribution curve.
    """
    gold = get_gold_price_usd_per_oz()
    n = float(net_vsp)

    total_proceeds = _integrate_sell_proceeds(
        n, qty_vsp, gold, unit_au, half_spread,
        usdc_reserves, vsp_circulating,
    )

    # patch_bundle11_reserve_dampening: scale buyback proceeds by the reserve
    # coverage ratio k = min(1, R / C_full(n)) so the MM stays solvent in
    # aggregate for the quoted price (Decision 2026-06-10).
    _k = _coverage_ratio(n, usdc_reserves, gold, unit_au, half_spread, vsp_circulating)
    total_proceeds *= _k

    # Safety: never pay out more than reserves (belt-and-suspenders; with
    # dampening total_proceeds <= reserves already holds).
    total_proceeds = min(total_proceeds, usdc_reserves)

    avg_price = total_proceeds / qty_vsp if qty_vsp > 0 else 0.0

    return MMFillResult(
        total_usd=total_proceeds,
        avg_price_usd=avg_price,
        qty_vsp=qty_vsp,
        new_net_vsp=net_vsp - int(qty_vsp),
    )


def get_floor_price(
    usdc_reserves: float,
    vsp_circulating: float,
    unit_au: float = None,
    gold_usd: float = None,
    net_vsp: float = 0,
    half_spread: float = None,
) -> float:
    """
    Liquidation floor: min(reserves/outstanding, current_sell_price).
    The worst-case price a holder can expect.
    """
    if unit_au is None:
        unit_au = DEFAULT_UNIT_AU
    if half_spread is None:
        half_spread = DEFAULT_HALF_SPREAD
    if gold_usd is None:
        # Use the module-level import (line ~27); a local re-import
        # would bypass test fixtures patching mm.mm_pricing's name.
        gold_usd = get_gold_price_usd_per_oz()
    reserve_floor = usdc_reserves / vsp_circulating if vsp_circulating > 0 else 0.0
    # patch_bundle11_reserve_dampening: dampen the marginal sell by k so the
    # floor reconciles with the reserve-aware buyback.
    _k_floor = _coverage_ratio(net_vsp, usdc_reserves, gold_usd, unit_au, half_spread, vsp_circulating)
    sell_price = _base_price(net_vsp, gold_usd, unit_au) * (1 - half_spread) * _k_floor
    # See get_spot_quote: floor = max guaranteed buyback, never above curve.
    return min(reserve_floor, sell_price)
