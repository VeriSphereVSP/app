import hashlib, random
from typing import List
from openai import OpenAI
from .config import OPENAI_API_KEY, EMBEDDINGS_PROVIDER, EMBEDDINGS_MODEL

def embed_stub(text: str, dims: int = 3072) -> List[float]:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    seed = int.from_bytes(h[:8], "big", signed=False)
    rng = random.Random(seed)
    return [rng.random() for _ in range(dims)]

def embed(text: str) -> List[float]:
    if EMBEDDINGS_PROVIDER == "stub":
        return embed_stub(text)
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set (required for EMBEDDINGS_PROVIDER=openai)")
    client = OpenAI(api_key=OPENAI_API_KEY, timeout=20.0)
    resp = client.embeddings.create(model=EMBEDDINGS_MODEL, input=text)
    return list(resp.data[0].embedding)
