"""
Tests for patch12d: Chainlink XAU/USD primary oracle on Avalanche mainnet.

Mocks the aggregator contract via web3 mocks. Real RPC is not touched.
"""
import pytest
import time
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def _clear_oracle_state():
    """Each test starts with empty oracle cache + reset Chainlink state."""
    from mm import oracle
    oracle._cache.clear()
    oracle._chainlink_w3 = None
    oracle._chainlink_contract = None
    oracle._chainlink_decimals_cached = None
    yield
    oracle._cache.clear()
    oracle._chainlink_w3 = None
    oracle._chainlink_contract = None
    oracle._chainlink_decimals_cached = None


def _mock_chainlink_contract(*, answer=None, updated_at=None, decimals=8,
                             latest_raises=None, decimals_raises=None,
                             round_id=1, answered_in_round=1):
    """Build a mock aggregator contract.

    answer: int256 returned by latestRoundData (in raw units, not scaled).
    updated_at: uint256 timestamp returned by latestRoundData.
    decimals: int returned by decimals().
    latest_raises: exception to raise on latestRoundData() call.
    decimals_raises: exception to raise on decimals() call.
    round_id / answered_in_round: for staleness-guard tests.
    """
    contract = MagicMock()

    if decimals_raises is not None:
        contract.functions.decimals.return_value.call.side_effect = decimals_raises
    else:
        contract.functions.decimals.return_value.call.return_value = decimals

    if latest_raises is not None:
        contract.functions.latestRoundData.return_value.call.side_effect = latest_raises
    else:
        ts = updated_at if updated_at is not None else int(time.time())
        ans = answer if answer is not None else 0
        contract.functions.latestRoundData.return_value.call.return_value = (
            round_id,          # roundId
            ans,               # answer
            ts,                # startedAt
            ts,                # updatedAt
            answered_in_round, # answeredInRound
        )
    return contract


# ─── 1: Chainlink read returns expected price ────────────────────────

def test_chainlink_returns_expected_price():
    """answer=4565_44000000 with decimals=8 should yield 4565.44."""
    from mm import oracle

    mock = _mock_chainlink_contract(
        answer=4565_44000000,
        updated_at=int(time.time()) - 60,  # 1 min old, fresh
        decimals=8,
    )
    with patch.object(oracle, "_get_chainlink_contract", return_value=mock):
        price = oracle._gold_from_chainlink()
    assert price == pytest.approx(4565.44)


# ─── 2: Stale Chainlink data rejected ────────────────────────────────

def test_chainlink_stale_rejected():
    """updatedAt older than 24h → return None, fall through."""
    from mm import oracle

    too_old = int(time.time()) - (oracle.CHAINLINK_MAX_AGE_SECONDS + 1)
    mock = _mock_chainlink_contract(
        answer=4565_44000000,
        updated_at=too_old,
        decimals=8,
    )
    with patch.object(oracle, "_get_chainlink_contract", return_value=mock):
        price = oracle._gold_from_chainlink()
    assert price is None


# ─── 3: Out-of-bounds (low) rejected ─────────────────────────────────

def test_chainlink_out_of_bounds_low_rejected():
    """Chainlink returns $50/oz (below MIN) — reject."""
    from mm import oracle

    mock = _mock_chainlink_contract(
        answer=50_00000000,  # $50.00 in 8 decimals
        updated_at=int(time.time()),
        decimals=8,
    )
    with patch.object(oracle, "_get_chainlink_contract", return_value=mock):
        price = oracle._gold_from_chainlink()
    assert price is None  # below MIN_GOLD_PRICE = 1000


# ─── 4: Out-of-bounds (high) rejected ────────────────────────────────

def test_chainlink_out_of_bounds_high_rejected():
    """Chainlink returns $50,000/oz (above MAX) — reject."""
    from mm import oracle

    mock = _mock_chainlink_contract(
        answer=50_000_00000000,  # $50,000.00 in 8 decimals
        updated_at=int(time.time()),
        decimals=8,
    )
    with patch.object(oracle, "_get_chainlink_contract", return_value=mock):
        price = oracle._gold_from_chainlink()
    assert price is None  # above MAX_GOLD_PRICE = 20000


# ─── 5: Chainlink failure falls through to HTTP APIs ─────────────────

def test_chainlink_failure_falls_through():
    """If Chainlink raises, the public API tries HTTP sources."""
    from mm import oracle

    mock_chainlink = _mock_chainlink_contract(
        latest_raises=ConnectionError("RPC down"),
    )

    def fake_goldapi():
        return 4500.0

    with patch.object(oracle, "_get_chainlink_contract", return_value=mock_chainlink):
        with patch.object(oracle, "_gold_from_goldapi", side_effect=fake_goldapi):
            with patch.object(oracle, "_gold_from_metalpriceapi", return_value=None):
                with patch.object(oracle, "_gold_from_kitco", return_value=None):
                    price = oracle.get_gold_price_usd_per_oz()
    assert price == pytest.approx(4500.0)


# ─── 6: Cache TTL respected ──────────────────────────────────────────

def test_chainlink_cache_hit():
    """Second read within TTL doesn't re-query."""
    from mm import oracle

    mock = _mock_chainlink_contract(
        answer=4565_44000000,
        updated_at=int(time.time()),
        decimals=8,
    )
    with patch.object(oracle, "_get_chainlink_contract", return_value=mock):
        oracle._gold_from_chainlink()
        oracle._gold_from_chainlink()
    assert mock.functions.latestRoundData.return_value.call.call_count == 1


# ─── 7: invalidate_xau_cache forces re-read ──────────────────────────

def test_invalidate_forces_reread():
    from mm import oracle

    mock = _mock_chainlink_contract(
        answer=4565_44000000,
        updated_at=int(time.time()),
        decimals=8,
    )
    with patch.object(oracle, "_get_chainlink_contract", return_value=mock):
        oracle._gold_from_chainlink()
        oracle.invalidate_xau_cache()
        oracle._gold_from_chainlink()
    assert mock.functions.latestRoundData.return_value.call.call_count == 2


# ─── 8: All sources fail → RuntimeError ──────────────────────────────

def test_all_sources_fail_raises():
    """If Chainlink and all HTTP sources return None, raise."""
    from mm import oracle

    mock = _mock_chainlink_contract(latest_raises=ConnectionError("down"))

    with patch.object(oracle, "_get_chainlink_contract", return_value=mock):
        with patch.object(oracle, "_gold_from_goldapi", return_value=None):
            with patch.object(oracle, "_gold_from_metalpriceapi", return_value=None):
                with patch.object(oracle, "_gold_from_kitco", return_value=None):
                    with pytest.raises(RuntimeError, match="oracle failure"):
                        oracle.get_gold_price_usd_per_oz()


# ─── 9: Negative answer rejected ─────────────────────────────────────

def test_chainlink_negative_answer_rejected():
    """Chainlink returns a negative answer (shouldn't happen, defense in depth)."""
    from mm import oracle

    mock = _mock_chainlink_contract(
        answer=-100,
        updated_at=int(time.time()),
        decimals=8,
    )
    with patch.object(oracle, "_get_chainlink_contract", return_value=mock):
        price = oracle._gold_from_chainlink()
    assert price is None


# ─── 10: Public API uses Chainlink first ─────────────────────────────

def test_public_api_prefers_chainlink():
    """When Chainlink succeeds, HTTP sources aren't called."""
    from mm import oracle

    mock = _mock_chainlink_contract(
        answer=4565_44000000,
        updated_at=int(time.time()),
        decimals=8,
    )

    goldapi_calls = {"n": 0}
    def fake_goldapi():
        goldapi_calls["n"] += 1
        return 9999.0

    with patch.object(oracle, "_get_chainlink_contract", return_value=mock):
        with patch.object(oracle, "_gold_from_goldapi", side_effect=fake_goldapi):
            price = oracle.get_gold_price_usd_per_oz()

    assert price == pytest.approx(4565.44)  # Chainlink, not GoldAPI
    assert goldapi_calls["n"] == 0           # GoldAPI never called


# ─── 11: Chainlink RPC is independent from chain_reader's RPC ────────

def test_chainlink_rpc_is_independent():
    """The Chainlink read uses _get_chainlink_w3, not the chain_reader's
    _get_w3. This is the architectural property that lets us read
    mainnet prices while running Verisphere on Fuji."""
    from mm import oracle

    # Confirm the function exists and is distinct from chain_reader._get_w3.
    from chain import chain_reader
    assert oracle._get_chainlink_w3 is not chain_reader._get_w3

    # Confirm CHAINLINK_RPC_URL defaults to mainnet.
    assert "avax.network" in oracle.CHAINLINK_RPC_URL
    # Default URL should NOT be Fuji.
    assert "avax-test.network" not in oracle.CHAINLINK_RPC_URL or \
           oracle.CHAINLINK_RPC_URL != "https://api.avax-test.network/ext/bc/C/rpc"


# ─── patch_bundle08_oracle_hardening: stale-round guards (gold) ──────

def test_chainlink_answered_in_round_stale_rejected():
    """answeredInRound < roundId (stuck/carried-over answer) → None."""
    from mm import oracle
    mock = _mock_chainlink_contract(
        answer=4565_44000000, updated_at=int(time.time()) - 60, decimals=8,
        round_id=100, answered_in_round=99,
    )
    with patch.object(oracle, "_get_chainlink_contract", return_value=mock):
        assert oracle._gold_from_chainlink() is None


def test_chainlink_updated_at_zero_rejected():
    """updatedAt == 0 (round never completed) → None."""
    from mm import oracle
    mock = _mock_chainlink_contract(answer=4565_44000000, updated_at=0, decimals=8)
    with patch.object(oracle, "_get_chainlink_contract", return_value=mock):
        assert oracle._gold_from_chainlink() is None


def test_chainlink_fresh_round_accepted():
    """answeredInRound >= roundId and updatedAt != 0 → accepted."""
    from mm import oracle
    mock = _mock_chainlink_contract(
        answer=4565_44000000, updated_at=int(time.time()) - 60, decimals=8,
        round_id=100, answered_in_round=100,
    )
    with patch.object(oracle, "_get_chainlink_contract", return_value=mock):
        assert oracle._gold_from_chainlink() == pytest.approx(4565.44)


# ─── patch_bundle08_oracle_hardening: approaching-cap alert (gold) ───

def test_gold_approaching_cap_alert_fires_once():
    from mm import oracle
    oracle._last_gold_alert_ts = 0.0
    # 17000 is >= 0.8 * 20000 = 16000 and < 20000 (valid)
    mock = _mock_chainlink_contract(
        answer=17000_00000000, updated_at=int(time.time()) - 60, decimals=8)
    with patch.object(oracle, "_get_chainlink_contract", return_value=mock):
        with patch.object(oracle, "_alert") as m_alert:
            p1 = oracle.get_gold_price_usd_per_oz()
            oracle.invalidate_xau_cache()
            p2 = oracle.get_gold_price_usd_per_oz()
    assert p1 == pytest.approx(17000.0)
    assert p2 == pytest.approx(17000.0)
    # throttled: at most one alert within the interval despite two reads
    assert m_alert.call_count == 1
    assert m_alert.call_args[0][0] == "gold_price_watch_level"


def test_gold_below_threshold_no_alert():
    from mm import oracle
    oracle._last_gold_alert_ts = 0.0
    mock = _mock_chainlink_contract(
        answer=4565_44000000, updated_at=int(time.time()) - 60, decimals=8)
    with patch.object(oracle, "_get_chainlink_contract", return_value=mock):
        with patch.object(oracle, "_alert") as m_alert:
            oracle.get_gold_price_usd_per_oz()
    assert m_alert.call_count == 0


# ─── patch_bundle08_oracle_hardening: AVAX/USD resolution ───────────

def test_avax_chainlink_primary():
    from mm import oracle
    mock = _mock_chainlink_contract(
        answer=35_00000000, updated_at=int(time.time()) - 30, decimals=8)
    with patch.object(oracle, "_get_avax_chainlink_contract", return_value=mock):
        assert oracle.get_avax_price_usd() == pytest.approx(35.0)


def test_avax_chainlink_stale_falls_through_to_coingecko():
    from mm import oracle
    mock = _mock_chainlink_contract(
        answer=35_00000000, updated_at=int(time.time()) - 60, decimals=8,
        round_id=100, answered_in_round=99,  # stale → None
    )
    with patch.object(oracle, "_get_avax_chainlink_contract", return_value=mock):
        with patch.object(oracle, "_avax_from_coingecko", return_value=42.0):
            assert oracle.get_avax_price_usd() == pytest.approx(42.0)


def test_avax_all_sources_fail_uses_config_fallback():
    from mm import oracle
    with patch.object(oracle, "_avax_from_chainlink", return_value=None):
        with patch.object(oracle, "_avax_from_coingecko", return_value=None):
            price = oracle.get_avax_price_usd()
    # falls back to config.AVAX_PRICE_USD (20.0) or the hardcoded 20.0
    assert price == pytest.approx(20.0)


def test_avax_out_of_bounds_rejected():
    from mm import oracle
    mock = _mock_chainlink_contract(
        answer=5000_00000000, updated_at=int(time.time()) - 30, decimals=8)  # $5000 > MAX_AVAX_PRICE
    with patch.object(oracle, "_get_avax_chainlink_contract", return_value=mock):
        assert oracle._avax_from_chainlink() is None


# ─── patch_bundle08_oracle_cap_config: raised gold cap ($50k default) ────

def test_gold_within_raised_cap_accepted():
    """$45k would have been rejected under the old $20k cap; now valid (<$50k)."""
    from mm import oracle
    mock = _mock_chainlink_contract(
        answer=45000_00000000, updated_at=int(time.time()) - 60, decimals=8)
    with patch.object(oracle, "_get_chainlink_contract", return_value=mock):
        assert oracle._gold_from_chainlink() == pytest.approx(45000.0)


def test_gold_above_raised_cap_rejected():
    """Still fail-closed above the (raised) MAX_GOLD_PRICE default of $50k."""
    from mm import oracle
    mock = _mock_chainlink_contract(
        answer=55000_00000000, updated_at=int(time.time()) - 60, decimals=8)
    with patch.object(oracle, "_get_chainlink_contract", return_value=mock):
        assert oracle._gold_from_chainlink() is None
