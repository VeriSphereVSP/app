# app/app/oracle.py
from __future__ import annotations

import os
import time
import re
import logging
from typing import Optional, Dict, Tuple

import requests

logger = logging.getLogger(__name__)


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

# Sanity bounds: reject any source returning a value outside this range.
# These are a COARSE magnitude rail (catch decimals/unit/wrong-feed errors,
# which are usually 10–1000× off) — not a bound on real market moves. The
# upper bound being too low is the dangerous case: a legitimate spike past it
# rejects every source and fail-closes quoting exactly when gold is most
# volatile, so it is set generously. patch_bundle08_oracle_cap_config: now
# env-tunable (no code change to move them) and the default cap is raised to
# $50k (~8× the 2026 high, ~11× spot) after gold's run toward $5–6k.
MIN_GOLD_PRICE = float(os.getenv("MIN_GOLD_PRICE", "1000"))
MAX_GOLD_PRICE = float(os.getenv("MAX_GOLD_PRICE", "50000"))

# patch_bundle08_oracle_cap_config: warn when a *valid* gold price crosses an
# absolute "watch level", decoupled from MAX_GOLD_PRICE so the heads-up and the
# rejection cap are independent knobs (a high cap no longer silences the alert).
# Default $8k is above 2026 spot (~$4.5k) and the major-bank year-end forecasts
# (~$6–6.3k), so it fires only when gold enters genuinely unusual territory.
GOLD_PRICE_ALERT_THRESHOLD = float(os.getenv("GOLD_PRICE_ALERT_USD", "8000"))
GOLD_ALERT_MIN_INTERVAL = int(os.getenv("GOLD_ALERT_MIN_INTERVAL", "3600"))  # ≤1 alert/hr
_last_gold_alert_ts: float = 0.0

# AVAX/USD sanity bounds. patch_bundle08_oracle_cap_config: env-tunable. Kept
# wide by default (AVAX spot ~$8; historically ~$10–$150) since the AVAX path
# fails OPEN (Chainlink→CoinGecko→config) rather than fail-closed, so these are
# a coarse filter, not a backstop. Tighten via env now that it's a one-liner.
MIN_AVAX_PRICE = float(os.getenv("MIN_AVAX_PRICE", "1"))
MAX_AVAX_PRICE = float(os.getenv("MAX_AVAX_PRICE", "1000"))


def _alert(kind: str, message: str, **fields) -> None:
    """Structured WARN-level alert. Mirrors treasury_worker.alert's log shape
    ('ALERT <kind>: ...') so a future Bundle 8 sink can tail one grep target
    across services. Never raises — alerting must not break price resolution."""
    try:
        logger.warning("ALERT %s: %s %s", kind, message, fields if fields else "")
    except Exception:
        pass
    # patch_bundle08_alert_sink: also fan out to the notifier (no-op if unconfigured).
    try:
        import notify
        notify.send_alert(kind, message, **fields)
    except Exception:
        pass


# ============================================================
# In-memory cache
# ============================================================

_cache: Dict[str, Tuple[float, float]] = {}
# key -> (price, timestamp)


def _now() -> float:
    return time.time()


def _valid_gold_price(p: Optional[float]) -> bool:
    return isinstance(p, (int, float)) and MIN_GOLD_PRICE < p < MAX_GOLD_PRICE


def _valid_avax_price(p: Optional[float]) -> bool:
    # patch_bundle08_oracle_hardening
    return isinstance(p, (int, float)) and MIN_AVAX_PRICE < p < MAX_AVAX_PRICE


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

# patch_bundle10_rpc_failover_p2: Chainlink reads run against Avalanche MAINNET; fall
# back to the mainnet public endpoint. If CHAINLINK_RPC_URL already IS the public
# endpoint (the default), this dedups to a single endpoint -> no behavior change.
CHAINLINK_RPC_URLS = list(dict.fromkeys(
    u for u in (CHAINLINK_RPC_URL, "https://api.avax.network/ext/bc/C/rpc") if u
))

CHAINLINK_XAU_USD_AVALANCHE = os.getenv(
    "CHAINLINK_XAU_USD",
    "0x1F41EF93dece881Ad0b98082B2d44D3f6F0C515B",
)

CHAINLINK_MIN_PERIOD = int(os.getenv("CHAINLINK_MIN_PERIOD", "60"))   # 60s cache
CHAINLINK_MAX_PERIOD = int(os.getenv("CHAINLINK_MAX_PERIOD", "300"))  # 5min stale OK
CHAINLINK_MAX_AGE_SECONDS = int(os.getenv("CHAINLINK_MAX_AGE_SECONDS", "86400"))  # 24h

# patch_bundle08_oracle_hardening: Chainlink AVAX/USD on Avalanche MAINNET.
# Default is the documented Avalanche C-Chain AVAX/USD aggregator (8 decimals).
# IMPORTANT: verify this address on-chain before trusting it
#   cast call <addr> "latestRoundData()(uint80,int256,uint256,uint256,uint80)" \
#        --rpc-url https://api.avax.network/ext/bc/C/rpc
# and confirm the scaled answer is a sane AVAX price (~$10–$150). The design is
# fail-safe regardless: a bad/zero/stale/out-of-bounds read falls through to
# CoinGecko and then the AVAX_PRICE_USD config fallback (today's behavior).
CHAINLINK_AVAX_USD_AVALANCHE = os.getenv(
    "CHAINLINK_AVAX_USD",
    "0x0A77230d17318075983913bC2145DB16C7366156",
)
AVAX_CHAINLINK_MAX_AGE_SECONDS = int(os.getenv("AVAX_CHAINLINK_MAX_AGE_SECONDS", "3600"))  # AVAX/USD heartbeat ~ short

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
# patch_bundle08_oracle_hardening: separate handle/decimals for AVAX/USD feed.
_avax_chainlink_contract = None
_avax_decimals_cached: Optional[int] = None


def _get_chainlink_w3():
    """Lazy init of the Web3 connection to Avalanche mainnet for
    Chainlink reads. Independent from chain_reader._get_w3() so the
    relay can read protocol state from Fuji while reading prices from
    mainnet."""
    global _chainlink_w3
    if _chainlink_w3 is None:
        from tx_signer import build_w3
        _chainlink_w3 = build_w3(CHAINLINK_RPC_URLS, require_connected=False)
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


def _get_avax_chainlink_contract():
    """Lazy init of the AVAX/USD aggregator handle (patch_bundle08_oracle_hardening).
    Reuses the same mainnet w3 and the generic AggregatorV3Interface ABI."""
    global _avax_chainlink_contract
    if _avax_chainlink_contract is None:
        from web3 import Web3
        w3 = _get_chainlink_w3()
        _avax_chainlink_contract = w3.eth.contract(
            address=Web3.to_checksum_address(CHAINLINK_AVAX_USD_AVALANCHE),
            abi=_CHAINLINK_ABI,
        )
    return _avax_chainlink_contract


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
        round_id = round_data[0]
        answer = round_data[1]
        updated_at = round_data[3]
        answered_in_round = round_data[4]

        if answer <= 0:
            return None

        # patch_bundle08_oracle_hardening: Chainlink-standard staleness guards.
        # updatedAt == 0 means the round never completed; answeredInRound <
        # roundId means latestRoundData returned a carried-over answer from an
        # older round (stuck feed). With XAU/USD's 24h heartbeat on Avalanche,
        # a stuck round is more likely than for high-velocity feeds. Reject both
        # and fall through to the HTTP sources.
        if int(updated_at) == 0:
            return None
        if int(answered_in_round) < int(round_id):
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


def invalidate_avax_cache():
    """Force the next AVAX read to re-query (patch_bundle08_oracle_hardening)."""
    for k in ("avax_chainlink", "avax_coingecko"):
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
            # patch_bundle08_oracle_hardening: warn (throttled) as a valid price
            # approaches the hard rejection cap, before it triggers a fail-closed
            # quoting outage at MAX_GOLD_PRICE.
            global _last_gold_alert_ts
            if price >= GOLD_PRICE_ALERT_THRESHOLD:
                now = _now()
                if now - _last_gold_alert_ts >= GOLD_ALERT_MIN_INTERVAL:
                    _last_gold_alert_ts = now
                    _alert(
                        "gold_price_watch_level",
                        "gold price above the configured watch level (GOLD_PRICE_ALERT_USD)",
                        price=round(price, 2),
                        watch_level=round(GOLD_PRICE_ALERT_THRESHOLD, 2),
                        hard_cap=MAX_GOLD_PRICE,
                        pct_of_cap=round(100.0 * price / MAX_GOLD_PRICE, 1),
                    )
            return price

    raise RuntimeError("Gold oracle failure: no valid source available")


def _avax_from_chainlink() -> Optional[float]:
    """Read AVAX/USD from Chainlink on Avalanche mainnet (patch_bundle08_oracle_hardening).
    Same staleness hardening as the gold feed. None on any failure → caller
    falls through to CoinGecko then the config fallback."""
    cached = _get_cached("avax_chainlink", CHAINLINK_MIN_PERIOD, CHAINLINK_MAX_PERIOD)
    if cached is not None:
        return cached

    global _avax_decimals_cached
    try:
        contract = _get_avax_chainlink_contract()
        if _avax_decimals_cached is None:
            _avax_decimals_cached = int(contract.functions.decimals().call())

        round_data = contract.functions.latestRoundData().call()
        round_id = round_data[0]
        answer = round_data[1]
        updated_at = round_data[3]
        answered_in_round = round_data[4]

        if answer <= 0:
            return None
        if int(updated_at) == 0:
            return None
        if int(answered_in_round) < int(round_id):
            return None
        if int(_now()) - int(updated_at) > AVAX_CHAINLINK_MAX_AGE_SECONDS:
            return None

        price = float(answer) / (10 ** _avax_decimals_cached)
        if not _valid_avax_price(price):
            return None

        _set_cache("avax_chainlink", price)
        return price
    except Exception:
        return None


def _avax_from_coingecko() -> Optional[float]:
    """AVAX/USD from CoinGecko (patch_bundle08_oracle_hardening). Ported from
    fee_calculator's prior inline path so there is one AVAX source of truth."""
    cached = _get_cached("avax_coingecko", CHAINLINK_MIN_PERIOD, CHAINLINK_MAX_PERIOD)
    if cached is not None:
        return cached
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=avalanche-2&vs_currencies=usd",
            timeout=HTTP_TIMEOUT,
        )
        if not r.ok:
            return None
        price = r.json().get("avalanche-2", {}).get("usd")
        if not _valid_avax_price(price):
            return None
        price = float(price)
        _set_cache("avax_coingecko", price)
        return price
    except Exception:
        return None


def get_avax_price_usd() -> float:
    """AVAX/USD resolution (patch_bundle08_oracle_hardening):
       1) Chainlink AVAX/USD on Avalanche mainnet  (primary)
       2) CoinGecko                                 (fallback)
       3) config.AVAX_PRICE_USD                      (final fallback — never raises)
    Unlike the gold oracle this does NOT fail closed: relay-fee pricing must
    keep working, and a stale-but-bounded AVAX estimate is acceptable for fees.
    """
    for fn in (_avax_from_chainlink, _avax_from_coingecko):
        price = fn()
        if price is not None:
            return price
    try:
        from config import AVAX_PRICE_USD
        return float(AVAX_PRICE_USD)
    except Exception:
        return 20.0


def oracle_timestamp() -> int:
    return int(time.time())

