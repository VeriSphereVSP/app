-- Migration: 110_article_dedup.sql
-- Adds columns to support:
--   1. Persisted semantic dedup (is_hidden flag on article_sentence)
--   2. Cached per-sentence embeddings (embedding JSONB)
--   3. Per-article cached render response (cached_response, response_hash on topic_article)
--   4. Last-refreshed timestamp for cache invalidation decisions
--
-- These were previously applied via ensure_tables() at startup, but belong
-- in the migration pipeline.

ALTER TABLE article_sentence
  ADD COLUMN IF NOT EXISTS is_hidden BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE article_sentence
  ADD COLUMN IF NOT EXISTS embedding JSONB;

ALTER TABLE topic_article
  ADD COLUMN IF NOT EXISTS cached_response JSONB;

ALTER TABLE topic_article
  ADD COLUMN IF NOT EXISTS response_hash VARCHAR(16);

ALTER TABLE topic_article
  ADD COLUMN IF NOT EXISTS last_refreshed_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_as_is_hidden ON article_sentence(is_hidden) WHERE is_hidden = FALSE;
