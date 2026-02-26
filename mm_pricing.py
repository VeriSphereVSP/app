# app/mm_pricing.py
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
from dataclasses import dataclass
from typing import Optional

from oracle import get_gold_price_usd_per_oz, get_avax_price_usd


# ────────────────────────────────────────────────────────────
# Configuration (loaded from mm_state or defaults)
# ────────────────────────────────────────────────────────────

DEFAULT_UNIT_AU = 0.0002        # Fraction of troy oz per unit
DEFAULT_HALF_SPREAD = 0.00125   # 0.125% half-spread → 0.25% round trip


# ────────────────────────────────────────────────────────────
# Core price curve
# ────────────────────────────────────────────────────────────

def _base_price(n: float, gold_usd: float, unit_au: float) -> float:
    """
    Base (mid) price at a given net_vsp position.

    For n >= 0: log-squared supply curve anchored to gold.
        price = log10(n + 10)^2 * unit_au * gold_usd

    For n < 0: reserve distribution curve.
        price = liquidation_floor * decay_factor
        where decay_factor smoothly approaches 0 as reserves drain.
        This is handled separately in _reserve_price().

    Returns price in USD per 1 VSP.
    """
    if n >= 0:
        return (math.log10(n + 10) ** 2) * unit_au * gold_usd
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
    """Sell price at a specific net_vsp position."""
    if n >= 0:
        return _base_price(n, gold_usd, unit_au) * (1 - half_spread)
    else:
        # Below zero: use reserve distribution, still apply spread
        reserve_p = _reserve_price(n, usdc_reserves, vsp_circulating)
        return reserve_p * (1 - half_spread)


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
    sell_p = mid * (1 - half_spread)

    floor = usdc_reserves / vsp_circulating if vsp_circulating > 0 else 0.0

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

    # Safety: never pay out more than reserves
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
) -> float:
    """
    Public liquidation floor: reserves / circulating supply.
    This is the ONLY pricing information that is publicly documented.
    """
    if vsp_circulating <= 0:
        return 0.0
    return usdc_reserves / vsp_circulating
