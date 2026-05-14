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


# ─── 2b: patch06 — virtual reserves sum MM + cold safes ──────────

def test_usdc_reserves_with_cold_safe(monkeypatch):
    """One cold safe configured. reserves = MM bal + cold bal."""
    import config
    from chain import chain_reader

    mm_addr   = "0x744a16c4Fe6B618E29D5Cb05C5a9cBa72175e60a"
    cold_addr = "0x1111111111111111111111111111111111111111"

    monkeypatch.setattr(config, "MM_ADDRESS", mm_addr)
    monkeypatch.setattr(config, "COLD_SAFE_ADDRESSES", [cold_addr])

    # MM has 1000 USDC, cold safe has 5000 USDC.
    balances = {
        mm_addr.lower():   1000_000_000,   # 1000 USDC (micro)
        cold_addr.lower(): 5000_000_000,   # 5000 USDC (micro)
    }
    mock_usdc = _make_token_mock(balance_of_returns=balances)
    with patch.object(chain_reader, "_get_usdc_token", return_value=mock_usdc):
        result = chain_reader.read_usdc_reserves()
    assert result == pytest.approx(6000.0)


def test_usdc_reserves_multiple_cold_safes(monkeypatch):
    """Multiple cold safes are all summed in."""
    import config
    from chain import chain_reader

    mm_addr    = "0x744a16c4Fe6B618E29D5Cb05C5a9cBa72175e60a"
    cold1      = "0x1111111111111111111111111111111111111111"
    cold2      = "0x2222222222222222222222222222222222222222"
    cold3      = "0x3333333333333333333333333333333333333333"

    monkeypatch.setattr(config, "MM_ADDRESS", mm_addr)
    monkeypatch.setattr(config, "COLD_SAFE_ADDRESSES", [cold1, cold2, cold3])

    balances = {
        mm_addr.lower(): 100_000_000,    # 100 USDC
        cold1.lower():   200_000_000,    # 200 USDC
        cold2.lower():   300_000_000,    # 300 USDC
        cold3.lower():   400_000_000,    # 400 USDC
    }
    mock_usdc = _make_token_mock(balance_of_returns=balances)
    with patch.object(chain_reader, "_get_usdc_token", return_value=mock_usdc):
        result = chain_reader.read_usdc_reserves()
    assert result == pytest.approx(1000.0)


def test_usdc_reserves_no_cold_safe(monkeypatch):
    """Empty COLD_SAFE_ADDRESSES -> behaves identically to pre-patch06."""
    import config
    from chain import chain_reader

    monkeypatch.setattr(
        config, "MM_ADDRESS",
        "0x744a16c4Fe6B618E29D5Cb05C5a9cBa72175e60a"
    )
    monkeypatch.setattr(config, "COLD_SAFE_ADDRESSES", [])

    mock_usdc = _make_token_mock(balance_of_micro=750_000_000)  # 750 USDC
    with patch.object(chain_reader, "_get_usdc_token", return_value=mock_usdc):
        result = chain_reader.read_usdc_reserves()
    assert result == pytest.approx(750.0)


# ─── 2c: patch06 — config parsing ────────────────────────────────

def test_parse_cold_safe_addresses_empty():
    """Empty string returns empty list."""
    from config import _parse_cold_safe_addresses
    assert _parse_cold_safe_addresses("") == []
    assert _parse_cold_safe_addresses("   ") == []


def test_parse_cold_safe_addresses_single():
    """Single address."""
    from config import _parse_cold_safe_addresses
    result = _parse_cold_safe_addresses(
        "0x1111111111111111111111111111111111111111"
    )
    assert result == ["0x1111111111111111111111111111111111111111"]


def test_parse_cold_safe_addresses_whitespace_tolerance():
    """Whitespace around commas is OK."""
    from config import _parse_cold_safe_addresses
    raw = (
        "  0x1111111111111111111111111111111111111111  , "
        "0x2222222222222222222222222222222222222222 "
    )
    result = _parse_cold_safe_addresses(raw)
    assert len(result) == 2
    assert "0x1111111111111111111111111111111111111111" in result
    assert "0x2222222222222222222222222222222222222222" in result


def test_parse_cold_safe_addresses_drops_duplicates():
    """Duplicate addresses appear only once."""
    from config import _parse_cold_safe_addresses
    raw = (
        "0x1111111111111111111111111111111111111111,"
        "0x1111111111111111111111111111111111111111"
    )
    result = _parse_cold_safe_addresses(raw)
    assert len(result) == 1


def test_parse_cold_safe_addresses_drops_zero():
    """Zero address is filtered out."""
    from config import _parse_cold_safe_addresses
    raw = "0x0000000000000000000000000000000000000000"
    assert _parse_cold_safe_addresses(raw) == []


def test_parse_cold_safe_addresses_malformed_raises():
    """Malformed address raises at config load time (fail-fast)."""
    import pytest as _pytest
    from config import _parse_cold_safe_addresses
    with _pytest.raises(ValueError, match="malformed address"):
        _parse_cold_safe_addresses("0xnothex")
    with _pytest.raises(ValueError, match="malformed address"):
        _parse_cold_safe_addresses("0x111")  # too short
    with _pytest.raises(ValueError, match="malformed address"):
        _parse_cold_safe_addresses("not_an_address")


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
