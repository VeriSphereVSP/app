-- Migration: 050_claim_post_id.sql
-- Adds the post_id column to claim (was done at runtime by
-- chain/indexer.py _ensure_post_id_column). Also creates the
-- indexer_state table that chain/indexer.py creates at runtime.
-- Place at: app/ops/compose/migrations/050_claim_post_id.sql

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_name='claim' AND column_name='post_id') THEN
    ALTER TABLE claim ADD COLUMN post_id INTEGER DEFAULT NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_claim_post_id ON claim(post_id) WHERE post_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS indexer_state (
    key   TEXT PRIMARY KEY,
    value BIGINT NOT NULL DEFAULT 0
);

INSERT INTO indexer_state (key, value) VALUES ('last_post_id', 0)
ON CONFLICT (key) DO NOTHING;
