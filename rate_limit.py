# app/rate_limit.py
"""
Rate limiting and gas budget protection.

Three layers:
  1. Per-IP rate limiting on all endpoints (general anti-spam)
  2. Per-address rate limiting on relay endpoint (gas budget protection)
  3. MM wallet balance check before relay (circuit breaker)
  4. Per-IP rate limiting on AI endpoints (cost protection)

Uses in-memory sliding windows. No external dependencies (no Redis).
Suitable for single-process deployment. For multi-process, switch to Redis.
"""

import os
import time
import logging
from collections import defaultdict
from functools import wraps
from typing import Optional

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response, JSONResponse

logger = logging.getLogger(__name__)

# patch_followup_limiter_logvis: ensure limiter warnings reach docker logs
# regardless of how uvicorn configures logging. The module logger gets its own
# stdout StreamHandler (idempotent) and stops propagating, so a 429 / abuse
# event is always greppable in `docker compose logs` — the observability gap
# that hid the 2026-06-05 shared-bucket 429s for days.
import sys as _vsp_sys
if not any(getattr(_h, "_vsp_logvis", False) for _h in logger.handlers):
    _vsp_h = logging.StreamHandler(_vsp_sys.stdout)
    _vsp_h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    _vsp_h._vsp_logvis = True
    logger.addHandler(_vsp_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


# ── Sliding window counter ─────────────────────────────────

class SlidingWindow:
    """In-memory sliding window rate limiter."""

    def __init__(self):
        # key -> list of timestamps
        self._hits: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str, max_hits: int, window_seconds: int) -> tuple[bool, int]:
        """Returns (allowed, remaining). Prunes expired entries."""
        now = time.time()
        cutoff = now - window_seconds
        hits = self._hits[key]
        # Prune old entries
        hits[:] = [t for t in hits if t > cutoff]
        if len(hits) >= max_hits:
            return False, 0
        hits.append(now)
        return True, max_hits - len(hits)

    def cleanup(self, max_age: int = 3600):
        """Remove keys with no recent hits. Call periodically."""
        now = time.time()
        dead = [k for k, v in self._hits.items() if not v or v[-1] < now - max_age]
        for k in dead:
            del self._hits[k]


_limiter = SlidingWindow()


# ── Configuration ──────────────────────────────────────────

# General API rate limits (per IP)
# patch_ratelimit_general_env: env-overridable so a single-origin deployment
# (e.g. the dev box, where all browser traffic reaches the app from the one
# Vite-proxy container IP, making this per-IP cap behave as a shared global
# bucket) can raise the ceiling without a code change. Defaults unchanged, so
# prod (real per-client IPs behind nginx) keeps 120/60s.
def _int_env(_name, _default):
    try:
        return int(os.getenv(_name, str(_default)))
    except ValueError:
        return _default
GENERAL_RATE_LIMIT = _int_env("GENERAL_RATE_LIMIT", 120)    # requests per window
GENERAL_RATE_WINDOW = _int_env("GENERAL_RATE_WINDOW", 60)   # seconds

# Relay rate limits (per user address)
RELAY_RATE_LIMIT = 60           # relay txs per window
RELAY_RATE_WINDOW = 300         # 5 minutes

# AI endpoint rate limits (per IP)
AI_RATE_LIMIT = 10              # AI calls per window
AI_RATE_WINDOW = 300            # 5 minutes

# Gas budget: minimum MM wallet AVAX balance before circuit-breaking relays
MIN_MM_AVAX_WEI = 50_000_000_000_000_000  # 0.05 AVAX — ~20 relay txs at 25 gwei
# How often to re-check balance (don't check every request)
BALANCE_CHECK_INTERVAL = 60     # seconds

# patch_bundle06_xff_trusted_proxy (config): how many trusted reverse-
# proxy hops sit in front of the app. 0 = no proxy (dev) -> ignore
# X-Forwarded-For and use the socket peer. 1 = one nginx in front
# (prod) -> trust exactly the right-most XFF entry. Anything beyond the
# trusted hop count is client-controlled and must never be honored.
try:
    TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "0"))
except ValueError:
    TRUSTED_PROXY_HOPS = 0
if TRUSTED_PROXY_HOPS < 0:
    TRUSTED_PROXY_HOPS = 0

# AI cost budget: max AI calls per day (across all users)
AI_DAILY_BUDGET = 500
AI_DAILY_WINDOW = 86400         # 24 hours


# ── Gas budget circuit breaker ─────────────────────────────

_last_balance_check = 0.0
_mm_balance_ok = True


def check_mm_balance() -> bool:
    """Check if the MM wallet has enough AVAX to relay.
    Cached for BALANCE_CHECK_INTERVAL seconds."""
    global _last_balance_check, _mm_balance_ok

    now = time.time()
    if now - _last_balance_check < BALANCE_CHECK_INTERVAL:
        return _mm_balance_ok

    try:
        from mm_wallet import w3
        from config import MM_ADDRESS
        from web3 import Web3

        balance = w3.eth.get_balance(Web3.to_checksum_address(MM_ADDRESS))
        _mm_balance_ok = balance >= MIN_MM_AVAX_WEI
        _last_balance_check = now

        if not _mm_balance_ok:
            logger.warning(
                "MM wallet AVAX balance critically low: %s wei (min: %s). "
                "Relay is paused.",
                balance, MIN_MM_AVAX_WEI,
            )
        return _mm_balance_ok
    except Exception as e:
        logger.warning("Failed to check MM balance: %s", e)
        # Fail open — don't block relay if we can't check
        _last_balance_check = now
        return True


# ── Helper to extract client IP ────────────────────────────

def _client_ip(request: Request) -> str:
    # patch_bundle06_xff_trusted_proxy: X-Forwarded-For is attacker-
    # controlled. Honor only TRUSTED_PROXY_HOPS proxy hops at the RIGHT
    # end of the chain.
    #   hops == 0 (dev, no proxy): ignore XFF entirely, use socket peer.
    #   hops == 1 (prod behind one nginx): use the right-most XFF entry,
    #   the client IP our own trusted proxy observed and appended.
    peer = request.client.host if request.client else "unknown"
    if TRUSTED_PROXY_HOPS <= 0:
        return peer
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return peer
    chain = [p.strip() for p in forwarded.split(",") if p.strip()]
    if not chain:
        return peer
    idx = len(chain) - TRUSTED_PROXY_HOPS
    if idx < 0:
        # Chain shorter than the configured trusted-hop count (misconfig
        # or a spoof attempt): fall back to the socket peer, which a
        # client cannot forge via request headers.
        return peer
    return chain[idx]


# ── FastAPI middleware ─────────────────────────────────────

class RateLimitMiddleware(BaseHTTPMiddleware):
    """General per-IP rate limiting for all endpoints."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip health checks
        if request.url.path in ("/healthz", "/docs", "/openapi.json"):
            return await call_next(request)

        ip = _client_ip(request)
        key = f"general:{ip}"
        allowed, remaining = _limiter.check(key, GENERAL_RATE_LIMIT, GENERAL_RATE_WINDOW)

        if not allowed:
            logger.warning("Rate limit exceeded for IP %s on %s", ip, request.url.path)
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please slow down."},
                headers={"Retry-After": str(GENERAL_RATE_WINDOW)},
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response


# ── Decorators for specific endpoints ─────────────────────

def relay_rate_limit(func):
    """Decorator for the relay endpoint. Per-address + gas budget check."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        # Extract request from kwargs or args
        body = kwargs.get("body")
        request = kwargs.get("request")

        # Per-address rate limit
        if body and hasattr(body, "request") and hasattr(body.request, "from_"):
            addr = body.request.from_.lower()
            key = f"relay:{addr}"
            allowed, remaining = _limiter.check(key, RELAY_RATE_LIMIT, RELAY_RATE_WINDOW)
            if not allowed:
                logger.warning("Relay rate limit exceeded for address %s", addr)
                raise HTTPException(
                    429,
                    f"Too many relay requests from this address. "
                    f"Limit: {RELAY_RATE_LIMIT} per {RELAY_RATE_WINDOW}s.",
                )

        # Gas budget circuit breaker
        if not check_mm_balance():
            raise HTTPException(
                503,
                "Relay temporarily unavailable — gas budget depleted. "
                "Please try again later or use a direct wallet transaction.",
            )

        return await func(*args, **kwargs)

    return wrapper


def ai_rate_limit(func):
    """Decorator for AI-calling endpoints. Per-IP + daily global budget."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        # Try to get request from FastAPI dependency injection
        request = kwargs.get("request")
        ip = "unknown"
        if request:
            ip = _client_ip(request)
        elif args:
            # Check if any arg looks like a Request
            for arg in args:
                if isinstance(arg, Request):
                    ip = _client_ip(arg)
                    break

        # Per-IP limit
        key = f"ai:{ip}"
        allowed, remaining = _limiter.check(key, AI_RATE_LIMIT, AI_RATE_WINDOW)
        if not allowed:
            logger.warning("AI rate limit exceeded for IP %s", ip)
            raise HTTPException(
                429,
                f"Too many AI requests. Limit: {AI_RATE_LIMIT} per {AI_RATE_WINDOW // 60} minutes.",
            )

        # Global daily budget
        key_global = "ai:global:daily"
        allowed_g, _ = _limiter.check(key_global, AI_DAILY_BUDGET, AI_DAILY_WINDOW)
        if not allowed_g:
            logger.warning("Global AI daily budget exhausted")
            raise HTTPException(
                503,
                "AI generation temporarily unavailable — daily budget reached. "
                "Try again tomorrow.",
            )

        return func(*args, **kwargs)

    return wrapper


# ── Periodic cleanup (call from a background task) ─────────

def cleanup_rate_limiter():
    """Remove stale entries. Call every ~10 minutes from a background task."""
    _limiter.cleanup(max_age=max(GENERAL_RATE_WINDOW, RELAY_RATE_WINDOW, AI_DAILY_WINDOW) * 2)
