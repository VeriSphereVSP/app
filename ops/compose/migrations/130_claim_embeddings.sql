-- 130_claim_embeddings.sql
-- Add embedding column to chain_claim_text for semantic dedup caching.
-- Uses JSONB for portability (works without pgvector extension).
-- If pgvector is available, a future migration can ALTER to vector(3072)
-- and add an HNSW index.

ALTER TABLE chain_claim_text
    ADD COLUMN IF NOT EXISTS embedding JSONB DEFAULT NULL;

COMMENT ON COLUMN chain_claim_text.embedding IS
    'Cached embedding vector as JSON array. Computed lazily on first similarity check.';
