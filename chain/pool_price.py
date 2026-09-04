# app/chain/pool_price.py — patch_trackb_pool_reader
"""
Pool price reader for the public-AMM era (FUJI-MOCK-PLAN Track B).

Reads the rehearsal MockCPAMM (token0=VSP/18dec, token1=USDC/6dec on Fuji):
reserve0()/reserve1()/token0()/token1(). Venue note: the mainnet venue is a
real AMM chosen per MAINNET-PLAN §6; if it is UniswapV2-style the only change
needed is the reserve accessor (getReserves() vs reserve0()/reserve1()) —
add the adapter HERE, never in callers.

House rules (mirrors chain_reader): TTL cache, fail-FAST on RPC errors, no
stale fallback — a stale price rendered as fresh is exactly the fail-open
pattern the 2026-08-19 limiter lesson canonized. Callers decide presentation.
"""
import logging
import time

from web3 import Web3

from tx_signer import build_w3  # shared provider factory

logger = logging.getLogger(__name__)

VSP_DECIMALS = 18
USDC_DECIMALS = 6
_CACHE_TTL = 15  # seconds — the trade surface polls at 30s

# patch_venue: UniV2-interface pair (Joe V1 / Uniswap v2). Token ordering is
# BY ADDRESS SORT — orientation is detected, never assumed.
_UNIV2_ABI = [
    {"type": "function", "name": "token0", "inputs": [],
     "outputs": [{"name": "", "type": "address"}], "stateMutability": "view"},
    {"type": "function", "name": "token1", "inputs": [],
     "outputs": [{"name": "", "type": "address"}], "stateMutability": "view"},
    {"type": "function", "name": "getReserves", "inputs": [],
     "outputs": [{"name": "", "type": "uint112"}, {"name": "", "type": "uint112"},
                 {"name": "", "type": "uint32"}], "stateMutability": "view"},
]
_POOL_ABI = [
    {"type": "function", "name": "token0", "inputs": [],
     "outputs": [{"name": "", "type": "address"}], "stateMutability": "view"},
    {"type": "function", "name": "token1", "inputs": [],
     "outputs": [{"name": "", "type": "address"}], "stateMutability": "view"},
    {"type": "function", "name": "reserve0", "inputs": [],
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
    {"type": "function", "name": "reserve1", "inputs": [],
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
]
_ERC20_ABI = [
    {"type": "function", "name": "totalSupply", "inputs": [],
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
    {"type": "function", "name": "balanceOf",
     "inputs": [{"name": "a", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
]

_w3 = None
_pool = None
_vsp = None
_validated = False
_token0_is_vsp = True  # patch_venue: set during validation; univ2 pairs sort by address
_cache: dict = {}


def _cached(key, fn, ttl=_CACHE_TTL):
    now = time.time()
    if key in _cache:
        val, ts = _cache[key]
        if now - ts < ttl:
            return val
    val = fn()
    _cache[key] = (val, now)
    return val


def _get_w3():
    global _w3
    if _w3 is None:
        from config import RPC_READ_URLS
        _w3 = build_w3(RPC_READ_URLS)
    return _w3


def _get_pool():
    """Pool contract handle. On FIRST use, asserts the pair's tokens match the
    configured VSP/USDC — a mis-wired POOL_PAIR_ADDRESS must fail loudly, not
    quietly serve the price of some other pair."""
    global _pool, _validated, _token0_is_vsp
    from config import (POOL_PAIR_ADDRESS, VSP_TOKEN_ADDRESS, USDC_ADDRESS,
                        POOL_VENUE)
    if not POOL_PAIR_ADDRESS:
        raise RuntimeError("POOL_PAIR_ADDRESS not configured")
    if _pool is None:
        abi = _UNIV2_ABI if POOL_VENUE == "univ2" else _POOL_ABI
        _pool = _get_w3().eth.contract(
            address=Web3.to_checksum_address(POOL_PAIR_ADDRESS), abi=abi
        )
    if not _validated:
        t0 = _pool.functions.token0().call()
        t1 = _pool.functions.token1().call()
        # patch_venue: validate as a SET (univ2 orders by address sort), then
        # record orientation. A mis-wired POOL_PAIR_ADDRESS still fails loudly.
        want = {VSP_TOKEN_ADDRESS.lower(), USDC_ADDRESS.lower()}
        have = {t0.lower(), t1.lower()}
        if have != want:
            raise RuntimeError(
                f"pool_price: pair token mismatch — pool has token0={t0} "
                f"token1={t1}, config has VSP={VSP_TOKEN_ADDRESS} USDC={USDC_ADDRESS}"
            )
        _token0_is_vsp = (t0.lower() == VSP_TOKEN_ADDRESS.lower())
        _validated = True
        logger.info("pool_price: pair validated (%s, venue=%s, token0_is_vsp=%s)",
                    POOL_PAIR_ADDRESS, POOL_VENUE, _token0_is_vsp)
    return _pool


def pool_configured() -> bool:
    from config import POOL_PAIR_ADDRESS
    return bool(POOL_PAIR_ADDRESS)


def read_pool_state() -> dict:
    """Live pool state. Raises on RPC failure or empty pool — callers handle."""
    def _read():
        pool = _get_pool()
        from config import POOL_PAIR_ADDRESS, POOL_VENUE, VENUE_ROUTER
        if POOL_VENUE == "univ2":
            r0, r1, _ts = pool.functions.getReserves().call()
        else:
            r0 = pool.functions.reserve0().call()
            r1 = pool.functions.reserve1().call()
        if r0 <= 0 or r1 <= 0:
            raise RuntimeError("pool_price: pool has zero reserves (unseeded?)")
        # orient by the validated token0 (univ2 sorts by address; mock is fixed
        # VSP=token0 and _token0_is_vsp validates True there too)
        rv, ru = (r0, r1) if _token0_is_vsp else (r1, r0)
        vsp_reserve = rv / 10 ** VSP_DECIMALS
        usdc_reserve = ru / 10 ** USDC_DECIMALS
        return {
            "price_usdc_per_vsp": usdc_reserve / vsp_reserve,
            "vsp_reserve": vsp_reserve,
            "usdc_reserve": usdc_reserve,
            "pair": POOL_PAIR_ADDRESS,
            "venue": POOL_VENUE,
            "router": VENUE_ROUTER or None,
            "token0_is_vsp": _token0_is_vsp,
            "updated_at": int(time.time()),
        }
    return _cached("pool_state", _read)


def read_vsp_circulating_v2() -> float:
    """Post-MM circulating definition (Track B):
        circulating = VSPToken.totalSupply() − Σ balanceOf(a) for a in
                      CIRCULATING_EXCLUDE_ADDRESSES
    (company-controlled balances; defaults to MM + treasury — see config.py).
    Replaces the MM-era definition (totalSupply − balanceOf(MM)) which
    retires with the MM. Fail-fast, cached, VSP units."""
    def _read():
        global _vsp
        from config import VSP_TOKEN_ADDRESS, CIRCULATING_EXCLUDE_ADDRESSES
        if _vsp is None:
            _vsp = _get_w3().eth.contract(
                address=Web3.to_checksum_address(VSP_TOKEN_ADDRESS), abi=_ERC20_ABI
            )
        total = _vsp.functions.totalSupply().call()
        excluded = 0
        for a in CIRCULATING_EXCLUDE_ADDRESSES:
            excluded += _vsp.functions.balanceOf(
                Web3.to_checksum_address(a)
            ).call()
        return max(0, total - excluded) / 10 ** VSP_DECIMALS
    return _cached("vsp_circulating_v2", _read)
