import hashlib, re
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
def normalize_text(text: str) -> str:
    text = text.lower()
    text = _PUNCT_RE.sub("", text)
    return " ".join(text.split())
def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()
