# app/tests/test_mm_pricing.py
"""
Tests for the volume-integrated MM pricing engine.

These test the mathematical properties of the pricing curves,
NOT the API routes or on-chain transfers.
"""
import math
import pytest
from unittest.mock import patch

# Stub the oracle before importing mm_pricing
MOCK_GOLD = 2900.0
MOCK_AVAX = 35.0


@pytest.fixture(autouse=True)
def _mock_oracle():
    with patch("mm.mm_pricing.get_gold_price_usd_per_oz", return_value=MOCK_GOLD), \
         patch("mm.mm_pricing.get_avax_price_usd", return_value=MOCK_AVAX):
        yield


from mm.mm_pricing import (
    _base_price,
    _reserve_price,
    get_spot_quote,
    compute_buy_fill,
    compute_sell_fill,
    get_floor_price,
    DEFAULT_UNIT_AU,
    DEFAULT_HALF_SPREAD,
)


# ────────────────────────────────────────────────────────────
# Base price curve
# ────────────────────────────────────────────────────────────

class TestBasePriceCurve:
    def test_price_at_zero_is_positive(self):
        p = _base_price(0, MOCK_GOLD, DEFAULT_UNIT_AU)
        assert p > 0

    def test_price_increases_with_net_vsp(self):
        p0 = _base_price(0, MOCK_GOLD, DEFAULT_UNIT_AU)
        p100 = _base_price(100, MOCK_GOLD, DEFAULT_UNIT_AU)
        p10000 = _base_price(10000, MOCK_GOLD, DEFAULT_UNIT_AU)
        assert p0 < p100 < p10000

    def test_log_curve_deceleration(self):
        """Price increase from 100→200 should be larger than 10000→10100.

        Holds for any exponent >= 1 since log10(...)**k is concave-up.
        """
        p100 = _base_price(100, MOCK_GOLD, DEFAULT_UNIT_AU)
        p200 = _base_price(200, MOCK_GOLD, DEFAULT_UNIT_AU)
        p10000 = _base_price(10000, MOCK_GOLD, DEFAULT_UNIT_AU)
        p10100 = _base_price(10100, MOCK_GOLD, DEFAULT_UNIT_AU)
        assert (p200 - p100) > (p10100 - p10000)

    def test_gold_anchor_scales_linearly(self):
        p_normal = _base_price(100, MOCK_GOLD, DEFAULT_UNIT_AU)
        p_double_gold = _base_price(100, MOCK_GOLD * 2, DEFAULT_UNIT_AU)
        assert abs(p_double_gold / p_normal - 2.0) < 0.001


    @pytest.mark.parametrize("exp", [2.0, 2.4, 3.0])
    def test_curve_shape_holds_for_any_exponent(self, exp, monkeypatch):
        """The curve must be strictly increasing, gold-anchored, and
        decelerating for any positive rational exponent.

        This guards the 12c toggle's promise that the env var works
        for any reasonable value, not just the integer cases.
        """
        monkeypatch.setattr("mm.mm_pricing._PRICE_EXPONENT", exp)
        # 1. Strictly increasing in n
        p0 = _base_price(0, MOCK_GOLD, DEFAULT_UNIT_AU)
        p100 = _base_price(100, MOCK_GOLD, DEFAULT_UNIT_AU)
        p10000 = _base_price(10000, MOCK_GOLD, DEFAULT_UNIT_AU)
        assert p0 < p100 < p10000, f"non-monotonic at exp={exp}"
        # 2. Linear in gold
        p_2x = _base_price(100, MOCK_GOLD * 2, DEFAULT_UNIT_AU)
        assert abs(p_2x / p100 - 2.0) < 0.001, f"non-linear in gold at exp={exp}"
        # 3. Decelerates (incremental price grows slower at higher n)
        p200 = _base_price(200, MOCK_GOLD, DEFAULT_UNIT_AU)
        p10100 = _base_price(10100, MOCK_GOLD, DEFAULT_UNIT_AU)
        assert (p200 - p100) > (p10100 - p10000), f"non-decelerating at exp={exp}"



# ────────────────────────────────────────────────────────────
# Reserve distribution curve (n < 0)
# ────────────────────────────────────────────────────────────

class TestReserveCurve:
    def test_floor_price(self):
        floor = _reserve_price(0, usdc_reserves=10000, vsp_circulating=5000)
        assert abs(floor - 2.0) < 0.001  # 10000 / 5000

    def test_price_decreases_as_n_goes_negative(self):
        p_0 = _reserve_price(0, 10000, 5000)
        p_neg = _reserve_price(-1000, 10000, 5000)
        p_more_neg = _reserve_price(-2000, 10000, 5000)
        assert p_0 > p_neg > p_more_neg

    def test_approaches_zero_at_full_drain(self):
        p = _reserve_price(-4999, 10000, 5000)
        assert p < 0.01  # near zero

    def test_zero_reserves_returns_zero(self):
        assert _reserve_price(-100, 0, 5000) == 0.0

    def test_zero_circulating_returns_zero(self):
        assert _reserve_price(-100, 10000, 0) == 0.0


# ────────────────────────────────────────────────────────────
# Volume-integrated fills: spread invariant
# ────────────────────────────────────────────────────────────

class TestSpreadInvariant:
    """The core property: buy then immediately sell the same qty always loses money."""

    @pytest.mark.parametrize("net_vsp", [0, 10, 100, 1000, 10000])
    @pytest.mark.parametrize("qty", [1, 10, 100, 500])
    def test_round_trip_always_loses(self, net_vsp, qty):
        reserves = 100_000.0
        circ = 50_000.0

        buy = compute_buy_fill(net_vsp, qty, reserves, circ)
        # After buying, net_vsp increases, reserves increase
        sell = compute_sell_fill(
            buy.new_net_vsp, qty,
            reserves + buy.total_usd,  # reserves grew from the buy
            circ + qty,
        )
        # Cost to buy must exceed proceeds from selling
        assert buy.total_usd > sell.total_usd, (
            f"Round trip profit at net_vsp={net_vsp}, qty={qty}: "
            f"buy={buy.total_usd:.6f}, sell={sell.total_usd:.6f}"
        )

    def test_large_buy_pumps_price_but_spread_protects(self):
        """Even a massive buy that moves the price substantially can't profit on round trip."""
        reserves = 100_000.0
        circ = 50_000.0

        buy = compute_buy_fill(0, 5000, reserves, circ)
        sell = compute_sell_fill(
            buy.new_net_vsp, 5000,
            reserves + buy.total_usd,
            circ + 5000,
        )
        assert buy.total_usd > sell.total_usd


# ────────────────────────────────────────────────────────────
# Volume-integrated fills: monotonicity
# ────────────────────────────────────────────────────────────

class TestFillMonotonicity:
    def test_buy_cost_increases_with_quantity(self):
        r, c = 100_000.0, 50_000.0
        f10 = compute_buy_fill(100, 10, r, c)
        f100 = compute_buy_fill(100, 100, r, c)
        assert f100.total_usd > f10.total_usd
        # Average price should also be higher for larger order
        assert f100.avg_price_usd > f10.avg_price_usd

    def test_sell_proceeds_increase_with_quantity(self):
        r, c = 100_000.0, 50_000.0
        f10 = compute_sell_fill(1000, 10, r, c)
        f100 = compute_sell_fill(1000, 100, r, c)
        assert f100.total_usd > f10.total_usd
        # But average price should be lower for larger sell
        assert f100.avg_price_usd < f10.avg_price_usd

    def test_sell_never_exceeds_reserves(self):
        r, c = 1_000.0, 50_000.0  # very low reserves
        fill = compute_sell_fill(1000, 10000, r, c)
        assert fill.total_usd <= r


# ────────────────────────────────────────────────────────────
# Cross-zero transition
# ────────────────────────────────────────────────────────────

class TestCrossZeroSell:
    def test_sell_crossing_zero_is_smooth(self):
        """Selling from n=50 down to n=-50 should produce reasonable proceeds."""
        r, c = 100_000.0, 50_000.0
        fill = compute_sell_fill(50, 100, r, c)
        assert fill.total_usd > 0
        assert fill.new_net_vsp == -50

    def test_sell_entirely_in_negative_territory(self):
        r, c = 100_000.0, 50_000.0
        fill = compute_sell_fill(-100, 50, r, c)
        assert fill.total_usd > 0
        assert fill.new_net_vsp == -150


# ────────────────────────────────────────────────────────────
# Floor price
# ────────────────────────────────────────────────────────────

class TestFloorPrice:
    def test_basic_floor(self):
        # floor = min(reserve_floor, sell_price). With default exp=3,
        # gold=2900, unit=0.0001, half=0.0025, n=0:
        #   sell = log10(10)**3 * 0.0001 * 2900 * 0.9975 = 0.289275
        # reserve_floor = 10000/5000 = 2.0; min picks the curve price.
        # log10(10) = 1, so this expectation is exponent-invariant at n=0.
        assert abs(get_floor_price(10000, 5000) - 0.289275) < 0.001

    def test_zero_supply(self):
        assert get_floor_price(10000, 0) == 0.0

    def test_zero_reserves(self):
        assert get_floor_price(0, 5000) == 0.0

# ────────────────────────────────────────────────────────────
# Reserve-aware buyback dampening (patch_bundle11_reserve_dampening)
# ────────────────────────────────────────────────────────────

class TestReserveDampening:
    def test_full_drain_pays_reserves(self):
        R, S = 10_000.0, 5000
        fill = compute_sell_fill(S, float(S), R, float(S))
        # case U: draining the whole supply pays out exactly the reserves
        assert abs(fill.total_usd - R) < R * 1e-3

    @pytest.mark.parametrize("qty", [1.0, 100.0, 1000.0, 4999.0, 5000.0])
    def test_solvency_never_exceeds_reserves(self, qty):
        R, S = 8000.0, 5000
        fill = compute_sell_fill(S, min(qty, float(S)), R, float(S))
        assert fill.total_usd <= R + 1e-6

    def test_case_H_matches_undamped_curve(self):
        from mm.mm_pricing import _integrate_sell_proceeds
        R, S = 10_000_000.0, 1000  # reserves >> C_full -> k == 1
        fill = compute_sell_fill(S, 500.0, R, float(S))
        undamped = _integrate_sell_proceeds(
            float(S), 500.0, MOCK_GOLD, DEFAULT_UNIT_AU, DEFAULT_HALF_SPREAD,
            R, float(S),
        )
        assert abs(fill.total_usd - undamped) < undamped * 1e-6

    def test_larger_order_lower_avg_price(self):
        R, S = 10_000.0, 5000
        small = compute_sell_fill(S, 100.0, R, float(S))
        large = compute_sell_fill(S, 2000.0, R, float(S))
        assert large.avg_price_usd < small.avg_price_usd

    def test_split_equals_single_path_independent(self):
        R, S = 20_000.0, 8000
        single = compute_sell_fill(S, 4000.0, R, float(S))
        f1 = compute_sell_fill(S, 2000.0, R, float(S))
        R2, S2 = R - f1.total_usd, S - 2000
        f2 = compute_sell_fill(S2, 2000.0, R2, float(S2))
        # k invariant under selling at the dampened rate -> split == single
        # (up to trapezoidal discretization).
        assert abs((f1.total_usd + f2.total_usd) - single.total_usd) < single.total_usd * 0.02

    def test_last_unit_pays_positive(self):
        R, S = 5_000.0, 4000
        fill = compute_sell_fill(S, float(S), R, float(S))
        assert fill.total_usd > 0



# ────────────────────────────────────────────────────────────
# Spot quote
# ────────────────────────────────────────────────────────────

class TestSpotQuote:
    def test_buy_above_sell(self):
        q = get_spot_quote(100, 100_000, 50_000)
        assert q.buy_price_usd > q.sell_price_usd

    def test_floor_included(self):
        q = get_spot_quote(100, 100_000, 50_000)
        # patch_bundle11_reserve_dampening: exponent-robust. floor == min(
        # reserve_floor, spot sell). The prior literal 2.0 assumed
        # MM_PRICE_EXPONENT=3; at the live 2.5 the curve sell sits below
        # reserve_floor, so the floor correctly caps at the curve.
        reserve_floor = 100_000 / 50_000
        assert q.floor_price_usd <= reserve_floor + 1e-9
        assert abs(q.floor_price_usd - min(reserve_floor, q.sell_price_usd)) < 1e-9

    def test_negative_net_vsp_uses_reserve_curve(self):
        q = get_spot_quote(-500, 100_000, 50_000)
        # Mid price should be below floor since we're in negative territory
        floor = 100_000 / 50_000
        assert q.mid_price_usd < floor
