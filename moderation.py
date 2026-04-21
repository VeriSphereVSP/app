# app/moderation.py
"""
Content moderation for Verisphere.

Two-tier approach:
  1. Fast keyword pre-filter (catches obvious cases, zero latency)
  2. LLM-based policy check (catches nuanced cases, ~200ms)

Used at two points:
  - Relay gate: before submitting createClaim meta-tx
  - Display filter: before returning content to frontend

The chain itself is unmoderated — this is app-layer policy only.
"""

from __future__ import annotations

import logging
import re
import json
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ── Tier 1: Fast keyword pre-filter ──
# These are terms that have NO legitimate use in factual claims.
# Kept intentionally narrow to avoid false positives.
# The LLM tier catches everything else.

_BLOCK_PATTERNS = [
    # Slurs and hate speech (abbreviated patterns to avoid reproducing them)
    r"\b(kike|nigger|faggot|spic|chink|wetback|raghead|towelhead)\b",
    # Explicit sexual content
    r"\b(hardcore\s+porn|child\s+porn|cp\b|kiddie\s+porn|lolicon)",
    # Direct calls to violence
    r"\b(kill\s+all|genocide\s+the|exterminate\s+the|death\s+to\s+all)\b",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in _BLOCK_PATTERNS]


def _fast_check(text: str) -> Optional[str]:
    """
    Tier 1: instant keyword check.
    Returns rejection reason or None if clean.
    """
    for pattern in _COMPILED:
        if pattern.search(text):
            return "Content violates community standards."
    return None


# ── Tier 2: LLM-based policy check ──

_MODERATION_PROMPT = """You are a content moderator for a factual claims platform.

The platform hosts factual assertions that people stake cryptocurrency on (like "Earth is spherical" or "The boiling point of water is 100°C"). Claims can be controversial, politically charged, or scientifically contested — that is fine and expected.

Evaluate this text and determine if it violates ANY of these rules:

BLOCKED content:
1. Hate speech: slurs, dehumanization, or calls for violence against any group based on race, ethnicity, religion, gender, sexual orientation, disability, or national origin.
2. Pornography or sexually explicit content.
3. Direct threats of violence against specific individuals or groups.
4. Content that sexualizes minors in any way.
5. Instructions for creating weapons, explosives, or dangerous substances.
6. Doxxing: sharing private personal information (addresses, phone numbers, etc.)

ALLOWED content (do NOT flag these):
- Controversial factual claims ("Earth is flat", "Vaccines cause autism")
- Political opinions ("Immigration should be restricted")
- Religious claims ("God exists", "There is no god")
- Offensive but non-hateful opinions ("Country X has a bad culture")
- Historical claims about atrocities (factual discussion, not glorification)
- Scientific claims that contradict consensus

Respond with ONLY valid JSON:
{"allowed": true} or {"allowed": false, "reason": "brief explanation"}"""


# APP-05: Circuit breaker for LLM moderation
# After CIRCUIT_BREAKER_THRESHOLD failures in CIRCUIT_BREAKER_WINDOW seconds,
# falls back to keyword-only filtering until the window expires.
import time as _time

_CIRCUIT_BREAKER_THRESHOLD = 5   # failures before tripping
_CIRCUIT_BREAKER_WINDOW = 300    # 5 minute window
_circuit_failures: list[float] = []
_circuit_open = False
_circuit_open_until = 0.0


def _circuit_breaker_check() -> bool:
    """Returns True if LLM is available, False if circuit is open."""
    global _circuit_open, _circuit_open_until
    now = _time.time()

    if _circuit_open:
        if now > _circuit_open_until:
            _circuit_open = False
            _circuit_failures.clear()
            logger.info("LLM moderation circuit breaker: CLOSED (recovered)")
            return True
        return False
    return True


def _circuit_breaker_record_failure():
    """Record an LLM failure. Trip breaker if threshold exceeded."""
    global _circuit_open, _circuit_open_until
    now = _time.time()
    cutoff = now - _CIRCUIT_BREAKER_WINDOW
    _circuit_failures[:] = [t for t in _circuit_failures if t > cutoff]
    _circuit_failures.append(now)

    if len(_circuit_failures) >= _CIRCUIT_BREAKER_THRESHOLD:
        _circuit_open = True
        _circuit_open_until = now + _CIRCUIT_BREAKER_WINDOW
        logger.warning(
            "LLM moderation circuit breaker: OPEN — %d failures in %ds. "
            "Falling back to keyword-only for %ds.",
            len(_circuit_failures), _CIRCUIT_BREAKER_WINDOW, _CIRCUIT_BREAKER_WINDOW
        )


def _llm_check(text: str) -> Optional[str]:
    """
    Tier 2: LLM-based policy check with circuit breaker.
    Returns rejection reason or None if clean.
    Falls back to keyword-only if LLM is consistently failing.
    """
    if not _circuit_breaker_check():
        # Circuit open — skip LLM, rely on keyword filter only
        return None

    try:
        from llm_provider import complete
        response = complete(
            prompt=f"Evaluate this text:\n\n{text}",
            system=_MODERATION_PROMPT,
            max_tokens=100,
            temperature=0.0,
        )

        clean = re.sub(r'^```json\s*|\s*```$', '', response.strip())
        result = json.loads(clean)

        if result.get("allowed", True):
            return None
        return result.get("reason", "Content violates community standards.")

    except Exception as e:
        logger.warning(f"LLM moderation failed: {e}")
        _circuit_breaker_record_failure()
        return None


# ── Public API ──

@dataclass
class ModerationResult:
    allowed: bool
    reason: Optional[str] = None


def check_content(text: str, use_llm: bool = True) -> ModerationResult:
    """
    Check text against content policy.

    Args:
        text: The content to check.
        use_llm: If True, use LLM for nuanced checks (slower).
                 If False, only use fast keyword filter.

    Returns:
        ModerationResult with allowed=True/False and optional reason.
    """
    if not text or not text.strip():
        return ModerationResult(allowed=True)

    # Tier 1: fast keyword check
    reason = _fast_check(text)
    if reason:
        logger.info(f"Content blocked (keyword): {text[:50]}...")
        return ModerationResult(allowed=False, reason=reason)

    # Tier 2: LLM check (if enabled)
    if use_llm:
        reason = _llm_check(text)
        if reason:
            logger.info(f"Content blocked (LLM): {text[:50]}... — {reason}")
            return ModerationResult(allowed=False, reason=reason)

    return ModerationResult(allowed=True)


def check_content_fast(text: str) -> ModerationResult:
    """Fast keyword-only check. Use for display-time filtering."""
    return check_content(text, use_llm=False)