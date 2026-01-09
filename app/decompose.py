import re
_SPLIT_RE = re.compile(r"\s+(?:and|but|however|;|\.|\n)\s+", re.IGNORECASE)
def bounded_decompose(text: str, max_claims: int = 10, max_len: int = 2000):
    text = (text or "").strip()
    if not text: return []
    if len(text) > max_len: text = text[:max_len]
    parts = [p.strip() for p in _SPLIT_RE.split(text) if p.strip()]
    return (parts or [text])[:max_claims]
