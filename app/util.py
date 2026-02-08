# app/app/util.py
import re


def normalize_claim_text(text: str) -> str:
    """
    Canonical normalization for claim identity.
    """
    t = text.lower().strip()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t

