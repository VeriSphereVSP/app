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

MIN_GOLD_PRICE = 500.0
MAX_GOLD_PRICE = 10_000.0


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
# Public API
# ============================================================

def get_gold_price_usd_per_oz() -> float:
    """
    Resolution order (with caching):
    1) GoldAPI
    2) MetalPriceAPI
    3) Kitco HTML scrape

    Hard-fail if all unavailable.
    """

    for fn in (
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

