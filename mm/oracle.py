# app/app/oracle.py
from __future__ import annotations

import os
import time
import re
from typing import Optional, Dict, Tuple

import requests


# ============================================================
# Configuration (env)
# ============================================================

GOLDAPI_TOKEN = os.getenv("GOLDAPI_TOKEN", "").strip()
METALPRICEAPI_TOKEN = os.getenv("METALPRICEAPI_TOKEN", "").strip()

GOLDAPI_MIN_PERIOD = int(os.getenv("GOLDAPI_MIN_PERIOD", "300"))      # seconds
GOLDAPI_MAX_PERIOD = int(os.getenv("GOLDAPI_MAX_PERIOD", "3600"))

METAL_MIN_PERIOD = int(os.getenv("METALPRICEAPI_MIN_PERIOD", "300"))
METAL_MAX_PERIOD = int(os.getenv("METALPRICEAPI_MAX_PERIOD", "3600"))

KITCO_MIN_PERIOD = int(os.getenv("KITCO_MIN_PERIOD", "300"))
KITCO_MAX_PERIOD = int(os.getenv("KITCO_MAX_PERIOD", "3600"))

HTTP_TIMEOUT = 6.0  # seconds

# Sanity bounds: reject any source that returns a value outside this
# range. Bumped 2026-05 to reflect current gold prices (~$4,500/oz)
# and give 4x headroom on the upper bound. Adjustable via app config
# rebuild + restart if gold ever rallies past these limits.
MIN_GOLD_PRICE = 1_000.0
MAX_GOLD_PRICE = 20_000.0


# ============================================================
# In-memory cache
# ============================================================

_cache: Dict[str, Tuple[float, float]] = {}
# key -> (price, timestamp)


def _now() -> float:
    return time.time()


def _valid_gold_price(p: Optional[float]) -> bool:
    return isinstance(p, (int, float)) and MIN_GOLD_PRICE < p < MAX_GOLD_PRICE


def _get_cached(
    key: str, min_period: int, max_period: int
) -> Optional[float]:
    if key not in _cache:
        return None

    price, ts = _cache[key]
    age = _now() - ts

    if age < min_period:
        return price

    if age > max_period:
        return None

    return price


def _set_cache(key: str, price: float):
    _cache[key] = (price, _now())


# ============================================================
# GoldAPI
# ============================================================

def _gold_from_goldapi() -> Optional[float]:
    cached = _get_cached("goldapi", GOLDAPI_MIN_PERIOD, GOLDAPI_MAX_PERIOD)
    if cached is not None:
        return cached

    if not GOLDAPI_TOKEN:
        return None

    try:
        r = requests.get(
            "https://www.goldapi.io/api/XAU/USD",
            headers={"x-access-token": GOLDAPI_TOKEN},
            timeout=HTTP_TIMEOUT,
        )

        if r.status_code != 200:
            return None

        price = r.json().get("price")
        if not _valid_gold_price(price):
            return None

        price = float(price)
        _set_cache("goldapi", price)
        return price

    except Exception:
        return None


# ============================================================
# MetalPriceAPI
# ============================================================

def _gold_from_metalpriceapi() -> Optional[float]:
    cached = _get_cached("metalpriceapi", METAL_MIN_PERIOD, METAL_MAX_PERIOD)
    if cached is not None:
        return cached

    if not METALPRICEAPI_TOKEN:
        return None

    try:
        r = requests.get(
            "https://api.metalpriceapi.com/v1/latest",
            params={
                "api_key": METALPRICEAPI_TOKEN,
                "base": "USD",
                "currencies": "XAU",
            },
            timeout=HTTP_TIMEOUT,
        )

        if r.status_code != 200:
            return None

        rates = r.json().get("rates", {})
        xau_per_usd = rates.get("XAU")

        if not isinstance(xau_per_usd, (int, float)) or xau_per_usd <= 0:
            return None

        usd_per_xau = 1.0 / float(xau_per_usd)
        if not _valid_gold_price(usd_per_xau):
            return None

        _set_cache("metalpriceapi", usd_per_xau)
        return usd_per_xau

    except Exception:
        return None


# ============================================================
# Kitco (HTML scrape, regex-based — mirrors your Node code)
# ============================================================

_KITCO_REGEX = re.compile(
    r"Bid</div><div class=\"mb-2 text-right\"><h3 class=\".*?\">"
    r"(\d{1,3}(?:,\d{3})*\.\d{2})"
)


def _gold_from_kitco() -> Optional[float]:
    cached = _get_cached("kitco", KITCO_MIN_PERIOD, KITCO_MAX_PERIOD)
    if cached is not None:
        return cached

    try:
        r = requests.get(
            "https://www.kitco.com/charts/gold",
            timeout=HTTP_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/html",
            },
            allow_redirects=True,
        )

        if r.status_code != 200:
            return None

        m = _KITCO_REGEX.search(r.text)
        if not m:
            return None

        raw = m.group(1).replace(",", "")
        price = float(raw) + 0.5  # matches your Node logic

        if not _valid_gold_price(price):
            return None

        _set_cache("kitco", price)
        return price

    except Exception:
        return None


# ============================================================
# Chainlink XAU/USD on Avalanche MAINNET (primary, patch12d)
# ============================================================
#
# Aggregator address (Avalanche mainnet): 0x1F41EF93dece881Ad0b98082B2d44D3f6F0C515B
# Decimals: 8
# Returns spot USD per troy ounce of gold (not an ETF).
#
# Note: this read goes to AVALANCHE MAINNET regardless of which chain
# Verisphere itself runs on. Mainnet is the canonical home of Avalanche
# Chainlink feeds. Reading view functions costs no gas. During Fuji
# practice, this exercises the real Chainlink integration.

CHAINLINK_RPC_URL = os.getenv(
    "CHAINLINK_RPC_URL",
    "https://api.avax.network/ext/bc/C/rpc",  # Avalanche MAINNET
)

CHAINLINK_XAU_USD_AVALANCHE = os.getenv(
    "CHAINLINK_XAU_USD",
    "0x1F41EF93dece881Ad0b98082B2d44D3f6F0C515B",
)

CHAINLINK_MIN_PERIOD = int(os.getenv("CHAINLINK_MIN_PERIOD", "60"))   # 60s cache
CHAINLINK_MAX_PERIOD = int(os.getenv("CHAINLINK_MAX_PERIOD", "300"))  # 5min stale OK
CHAINLINK_MAX_AGE_SECONDS = int(os.getenv("CHAINLINK_MAX_AGE_SECONDS", "86400"))  # 24h

# Minimal AggregatorV3Interface ABI (just the bits we need).
_CHAINLINK_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Dedicated Web3 connection for the Chainlink read. Independent from
# chain_reader's connection so we always read from mainnet for prices,
# regardless of which chain Verisphere state lives on.
_chainlink_w3 = None
_chainlink_contract = None
_chainlink_decimals_cached: Optional[int] = None


def _get_chainlink_w3():
    """Lazy init of the Web3 connection to Avalanche mainnet for
    Chainlink reads. Independent from chain_reader._get_w3() so the
    relay can read protocol state from Fuji while reading prices from
    mainnet."""
    global _chainlink_w3
    if _chainlink_w3 is None:
        from web3 import Web3
        _chainlink_w3 = Web3(Web3.HTTPProvider(CHAINLINK_RPC_URL))
    return _chainlink_w3


def _get_chainlink_contract():
    """Lazy init of the Chainlink aggregator contract handle."""
    global _chainlink_contract
    if _chainlink_contract is None:
        from web3 import Web3
        w3 = _get_chainlink_w3()
        _chainlink_contract = w3.eth.contract(
            address=Web3.to_checksum_address(CHAINLINK_XAU_USD_AVALANCHE),
            abi=_CHAINLINK_ABI,
        )
    return _chainlink_contract


def _gold_from_chainlink() -> Optional[float]:
    """Read XAU/USD from Chainlink on Avalanche mainnet.

    Returns None on any failure (RPC error, stale data, out-of-bounds).
    Caller falls through to HTTP API sources.

    The 60-second cache is intentional: even if RPC is healthy, we
    don't want to pay for an eth_call on every quote endpoint. Stale
    cache up to 5 minutes is OK; past that we re-read.
    """
    cached = _get_cached("chainlink", CHAINLINK_MIN_PERIOD, CHAINLINK_MAX_PERIOD)
    if cached is not None:
        return cached

    global _chainlink_decimals_cached

    try:
        contract = _get_chainlink_contract()

        # decimals() result is constant; cache after first read.
        if _chainlink_decimals_cached is None:
            _chainlink_decimals_cached = int(contract.functions.decimals().call())

        round_data = contract.functions.latestRoundData().call()
        # round_data is (roundId, answer, startedAt, updatedAt, answeredInRound)
        answer = round_data[1]
        updated_at = round_data[3]

        if answer <= 0:
            return None

        # Staleness check: heartbeat is typically 24h, reject if older.
        age = int(_now()) - int(updated_at)
        if age > CHAINLINK_MAX_AGE_SECONDS:
            return None

        price = float(answer) / (10 ** _chainlink_decimals_cached)

        if not _valid_gold_price(price):
            return None

        _set_cache("chainlink", price)
        return price

    except Exception:
        return None


def invalidate_xau_cache():
    """Force the next gold-price read to re-query all sources. Mostly
    useful in tests; production code can usually let the TTL expire."""
    for k in ("chainlink", "goldapi", "metalpriceapi", "kitco"):
        _cache.pop(k, None)


# ============================================================
# Public API
# ============================================================

def get_gold_price_usd_per_oz() -> float:
    """
    Resolution order (with caching):
    1) Chainlink XAU/USD on Avalanche MAINNET  (primary, decentralized)
    2) GoldAPI                                  (fallback)
    3) MetalPriceAPI                            (fallback)
    4) Kitco HTML scrape                        (fallback)

    Hard-fail if all unavailable. The MM will return HTTP 503 to
    quote/fill endpoints — fail closed rather than serve mispriced
    quotes from no source.
    """

    for fn in (
        _gold_from_chainlink,
        _gold_from_goldapi,
        _gold_from_metalpriceapi,
        _gold_from_kitco,
    ):
        price = fn()
        if price is not None:
            return price

    raise RuntimeError("Gold oracle failure: no valid source available")


def get_avax_price_usd() -> float:
    # Still stubbed — replace with Chainlink/Pyth later
    return 35.0


def oracle_timestamp() -> int:
    return int(time.time())

