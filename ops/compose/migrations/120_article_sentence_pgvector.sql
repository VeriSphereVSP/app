-- Migration: 120_article_sentence_pgvector.sql
-- Replaces article_sentence.embedding with a proper pgvector column.
--
-- Why: previously stored as JSONB (portable but not query-friendly).
-- pgvector enables in-DB cosine similarity, future cross-claim semantic
-- search, claim clustering, and ANN indexing — all without changing the
-- column shape later.
--
-- text-embedding-3-large produces 3072-dim vectors. pgvector 0.8 supports
-- this dimensionality but its HNSW index is limited to 2000 dims. We
-- therefore add the column without an index for now; sequential scan
-- over a single article's sentences (~500) is fast enough.

CREATE EXTENSION IF NOT EXISTS vector;

-- Drop the old column (JSONB or vector — both possible depending on prior state)
ALTER TABLE article_sentence DROP COLUMN IF EXISTS embedding;

-- Create as pgvector type
ALTER TABLE article_sentence ADD COLUMN embedding vector(3072);
