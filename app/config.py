import os
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY","")
OPENAI_MODEL = os.getenv("OPENAI_MODEL","gpt-4o-mini")
DATABASE_URL = os.getenv("DATABASE_URL","")
EMBEDDINGS_PROVIDER = os.getenv("EMBEDDINGS_PROVIDER","stub").lower()
EMBEDDINGS_MODEL = os.getenv("EMBEDDINGS_MODEL","text-embedding-3-large")
DUPLICATE_THRESHOLD = float(os.getenv("DUPLICATE_THRESHOLD","0.95"))
NEAR_DUPLICATE_THRESHOLD = float(os.getenv("NEAR_DUPLICATE_THRESHOLD","0.85"))
