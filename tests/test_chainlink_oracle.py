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
                             latest_raises=None, decimals_raises=None):
    """Build a mock aggregator contract.

    answer: int256 returned by latestRoundData (in raw units, not scaled).
    updated_at: uint256 timestamp returned by latestRoundData.
    decimals: int returned by decimals().
    latest_raises: exception to raise on latestRoundData() call.
    decimals_raises: exception to raise on decimals() call.
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
            1,         # roundId
            ans,       # answer
            ts,        # startedAt
            ts,        # updatedAt
            1,         # answeredInRound
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
