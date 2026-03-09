# app/lang_detect.py
"""
Lightweight language detection for adapting LLM prompts.
Uses character-range heuristics — no external dependencies.
Falls back to 'en' if ambiguous.
"""

import re
import unicodedata
from typing import Optional

# Unicode block ranges for script detection
_SCRIPT_RANGES = [
    (0x0590, 0x05FF, "he"),  # Hebrew
    (0x0600, 0x06FF, "ar"),  # Arabic
    (0x0400, 0x04FF, "ru"),  # Cyrillic (Russian default)
    (0x4E00, 0x9FFF, "zh"),  # CJK Unified (Chinese default)
    (0x3040, 0x309F, "ja"),  # Hiragana (Japanese)
    (0x30A0, 0x30FF, "ja"),  # Katakana (Japanese)
    (0xAC00, 0xD7AF, "ko"),  # Hangul (Korean)
    (0x0E00, 0x0E7F, "th"),  # Thai
    (0x0900, 0x097F, "hi"),  # Devanagari (Hindi default)
    (0x0A00, 0x0A7F, "pa"),  # Gurmukhi (Punjabi)
    (0x0B80, 0x0BFF, "ta"),  # Tamil
]

_LANG_NAMES = {
    "en": "English", "he": "Hebrew", "ar": "Arabic", "ru": "Russian",
    "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "th": "Thai",
    "hi": "Hindi", "pa": "Punjabi", "ta": "Tamil", "es": "Spanish",
    "fr": "French", "de": "German", "pt": "Portuguese", "it": "Italian",
    "tr": "Turkish", "nl": "Dutch", "pl": "Polish", "uk": "Ukrainian",
}

_RTL_LANGS = {"he", "ar"}


def detect_language(text: str) -> str:
    """
    Detect the primary language of text based on script.
    Returns ISO 639-1 code (e.g. 'en', 'he', 'ar', 'zh').
    Falls back to 'en' for Latin scripts and ambiguous cases.
    """
    if not text or not text.strip():
        return "en"

    # Count characters by script
    script_counts: dict[str, int] = {}
    latin_count = 0
    total = 0

    for ch in text:
        cp = ord(ch)
        if ch.isspace() or unicodedata.category(ch).startswith("P"):
            continue
        total += 1

        matched = False
        for lo, hi, lang in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                script_counts[lang] = script_counts.get(lang, 0) + 1
                matched = True
                break

        if not matched and (0x0041 <= cp <= 0x007A or 0x00C0 <= cp <= 0x024F):
            latin_count += 1

    if total == 0:
        return "en"

    # If any non-Latin script has >30% of characters, use it
    for lang, count in sorted(script_counts.items(), key=lambda x: -x[1]):
        if count / total > 0.3:
            return lang

    # Default to English for Latin-based scripts
    return "en"


def lang_name(code: str) -> str:
    """Human-readable language name."""
    return _LANG_NAMES.get(code, code.upper())


def is_rtl(code: str) -> bool:
    """Whether the language uses right-to-left script."""
    return code in _RTL_LANGS


def lang_instruction(code: str) -> str:
    """
    Returns an instruction string to append to LLM prompts.
    For English, returns empty string (no extra instruction needed).
    """
    if code == "en":
        return ""
    name = lang_name(code)
    return f"\n\nIMPORTANT: The user's input is in {name}. You MUST write your ENTIRE response in {name}. All headings, section titles, claims, and text must be in {name}."