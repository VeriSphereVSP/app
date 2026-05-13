"""
Tests for patch12a: MM chain-state reads.

These tests mock the web3 contract calls. The actual chain RPC is
not touched.
"""
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def _clear_caches():
    """Each test starts with an empty cache."""
    from chain import chain_reader
    chain_reader._cache.clear()
    chain_reader._vsp_token_contract = None
    chain_reader._usdc_token_contract = None
    yield
    chain_reader._cache.clear()


def _make_token_mock(total_supply_wei=None, balance_of_returns=None,
                     balance_of_micro=None):
    """Build a mock contract. balance_of_returns can be a dict mapping
    address -> wei, or a single value used for any address."""
    contract = MagicMock()

    if total_supply_wei is not None:
        contract.functions.totalSupply.return_value.call.return_value = total_supply_wei

    if balance_of_returns is not None:
        if isinstance(balance_of_returns, dict):
            def _bal(addr):
                m = MagicMock()
                m.call.return_value = balance_of_returns.get(addr.lower(), 0)
                return m
            contract.functions.balanceOf.side_effect = _bal
        else:
            contract.functions.balanceOf.return_value.call.return_value = balance_of_returns

    if balance_of_micro is not None:
        contract.functions.balanceOf.return_value.call.return_value = balance_of_micro

    return contract


# ─── 1: read_vsp_circulating returns totalSupply - MM_balance ────────

def test_vsp_circulating_basic():
    """totalSupply 1000 VSP, MM holds 600 VSP -> circulating = 400 VSP."""
    from chain import chain_reader

    mock_vsp = _make_token_mock(
        total_supply_wei=1000 * 10**18,
        balance_of_micro=600 * 10**18,  # MM balance in wei
    )
    with patch.object(chain_reader, "_get_vsp_token", return_value=mock_vsp):
        result = chain_reader.read_vsp_circulating()
    assert result == pytest.approx(400.0)


# ─── 2: read_usdc_reserves returns USDC.balanceOf(MM) ────────────────

def test_usdc_reserves_basic():
    """MM holds 1500.50 USDC -> reserves = 1500.50."""
    from chain import chain_reader

    mock_usdc = _make_token_mock(balance_of_micro=1500_500_000)  # 1500.50 USDC
    with patch.object(chain_reader, "_get_usdc_token", return_value=mock_usdc):
        result = chain_reader.read_usdc_reserves()
    assert result == pytest.approx(1500.50)


# ─── 3: cache TTL respected (no second RPC call within window) ───────

def test_circulating_cache_hit():
    """Two reads within TTL window -> only one RPC call."""
    from chain import chain_reader

    mock_vsp = _make_token_mock(
        total_supply_wei=1000 * 10**18,
        balance_of_micro=600 * 10**18,
    )
    with patch.object(chain_reader, "_get_vsp_token", return_value=mock_vsp):
        r1 = chain_reader.read_vsp_circulating()
        r2 = chain_reader.read_vsp_circulating()
    assert r1 == r2
    # totalSupply was only called once (cached on second call).
    assert mock_vsp.functions.totalSupply.return_value.call.call_count == 1


# ─── 4: invalidate forces re-read ────────────────────────────────────

def test_invalidate_forces_reread():
    """After invalidate, next read hits RPC."""
    from chain import chain_reader

    mock_vsp = _make_token_mock(
        total_supply_wei=1000 * 10**18,
        balance_of_micro=600 * 10**18,
    )
    with patch.object(chain_reader, "_get_vsp_token", return_value=mock_vsp):
        chain_reader.read_vsp_circulating()
        chain_reader.invalidate_mm_chain_state_cache()
        chain_reader.read_vsp_circulating()
    assert mock_vsp.functions.totalSupply.return_value.call.call_count == 2


# ─── 5: RPC failure raises (no silent stale-data return) ─────────────

def test_rpc_failure_raises():
    """If totalSupply() raises, we propagate."""
    from chain import chain_reader

    mock_vsp = MagicMock()
    mock_vsp.functions.totalSupply.return_value.call.side_effect = \
        ConnectionError("RPC unreachable")

    with patch.object(chain_reader, "_get_vsp_token", return_value=mock_vsp):
        with pytest.raises(ConnectionError):
            chain_reader.read_vsp_circulating()


# ─── 6: yield-mint scenario raises circulating ───────────────────────

def test_yield_mint_increases_circulating():
    """
    Before yield: total=1000, MM=600 -> circ=400.
    After yield mint of 50 VSP to user: total=1050, MM=600 -> circ=450.
    """
    from chain import chain_reader

    # Before yield
    mock_before = _make_token_mock(
        total_supply_wei=1000 * 10**18,
        balance_of_micro=600 * 10**18,
    )
    with patch.object(chain_reader, "_get_vsp_token", return_value=mock_before):
        before = chain_reader.read_vsp_circulating()
    assert before == pytest.approx(400.0)

    chain_reader.invalidate_mm_chain_state_cache()

    # After yield
    mock_after = _make_token_mock(
        total_supply_wei=1050 * 10**18,
        balance_of_micro=600 * 10**18,
    )
    with patch.object(chain_reader, "_get_vsp_token", return_value=mock_after):
        after = chain_reader.read_vsp_circulating()
    assert after == pytest.approx(450.0)


# ─── 7: USDC donation shows up in reserves ───────────────────────────

def test_usdc_donation_increases_reserves():
    """
    Before donation: MM holds 100 USDC -> reserves=100.
    After 50 USDC donation: MM holds 150 -> reserves=150.
    """
    from chain import chain_reader

    mock_before = _make_token_mock(balance_of_micro=100_000_000)  # 100 USDC
    with patch.object(chain_reader, "_get_usdc_token", return_value=mock_before):
        before = chain_reader.read_usdc_reserves()
    assert before == pytest.approx(100.0)

    chain_reader.invalidate_mm_chain_state_cache()

    mock_after = _make_token_mock(balance_of_micro=150_000_000)  # 150 USDC
    with patch.object(chain_reader, "_get_usdc_token", return_value=mock_after):
        after = chain_reader.read_usdc_reserves()
    assert after == pytest.approx(150.0)


# ─── 8: Negative-net edge case (MM has bought back more than sold) ──
#
# This test confirms that even when MM holdings exceed what we'd
# expect from net_vsp (say, MM holds extra inventory pre-bootstrap),
# the formula is still total - MM, never negative.

def test_mm_holds_more_than_total_returns_zero():
    """Pathological case: MM holds more than totalSupply (shouldn't
    happen, but max(0, ...) keeps us safe)."""
    from chain import chain_reader

    mock_vsp = _make_token_mock(
        total_supply_wei=100 * 10**18,
        balance_of_micro=200 * 10**18,  # impossible but defensive
    )
    with patch.object(chain_reader, "_get_vsp_token", return_value=mock_vsp):
        result = chain_reader.read_vsp_circulating()
    assert result == 0.0
