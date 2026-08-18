-- Migration: 310_topic_article_one_to_one.sql
-- Permanent one-article-per-topic invariant (2026-07-19 incident: topic 3
-- "January 6 Capitol Attack" spawned THREE articles — keys 'january 6 capitol
-- attack', 'january 6th capitol attack', 'january 6th insurrection' — because
-- the article layer resolved by fuzzy STRING match (prefix LIKE) instead of by
-- the centroid-resolved topic_id. Claims then cross-indexed onto all three.
--
-- Fix: bind each article to a topic_id and forbid duplicates in the SCHEMA, so
-- no code path (injector, indexer, reconciler, future patch) can ever recreate
-- the fan-out. Topic RESOLUTION stays centroid-based (semantic) and is untouched
-- — this only ties the article to the topic that resolution already picks, so
-- same-name/different-meaning topics (distinct centroids -> distinct topic_id)
-- keep distinct articles.
--
-- NOTE: the UNIQUE(topic_id) constraint is added by the PATCH DRIVER *after* it
-- collapses all existing multi-article topics to one each — the constraint
-- cannot be created while duplicates exist. This migration adds the nullable
-- column + FK + backfill scaffolding only; the constraint is a separate guarded
-- step. (Kept in the driver, not here, so a failed collapse can't leave a
-- half-applied migration.)

ALTER TABLE topic_article
    ADD COLUMN IF NOT EXISTS topic_id INTEGER REFERENCES topic(topic_id);

CREATE INDEX IF NOT EXISTS idx_topic_article_topic_id ON topic_article(topic_id);

-- Backfill topic_id for existing articles by exact normalized-label match where
-- unambiguous. Articles whose key matches exactly one topic label (case/space-
-- normalized) get bound. Ambiguous or unmatched articles are left NULL for the
-- driver's centroid-based reconciler to resolve, so we never guess here.
UPDATE topic_article ta
SET topic_id = t.topic_id
FROM topic t
WHERE ta.topic_id IS NULL
  AND lower(btrim(t.label)) = lower(btrim(ta.title));
