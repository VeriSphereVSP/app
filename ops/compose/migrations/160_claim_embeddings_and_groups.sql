-- PD-04: Claim embeddings (1536-dim) + dupe group table

-- Drop JSONB embedding column if it exists, replace with vector
ALTER TABLE chain_claim_text DROP COLUMN IF EXISTS embedding;
ALTER TABLE chain_claim_text ADD COLUMN embedding vector(1536);
CREATE INDEX IF NOT EXISTS idx_claim_embedding ON chain_claim_text
    USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS claim_dupe_group (
    group_id SERIAL PRIMARY KEY,
    canonical_post_id INTEGER NOT NULL,
    canonical_text TEXT,
    member_count INTEGER DEFAULT 1,
    total_support DOUBLE PRECISION DEFAULT 0,
    total_challenge DOUBLE PRECISION DEFAULT 0,
    aggregate_vs DOUBLE PRECISION DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE chain_claim_text ADD COLUMN IF NOT EXISTS dupe_group_id INTEGER
    REFERENCES claim_dupe_group(group_id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_claim_dupe_group ON chain_claim_text (dupe_group_id);
