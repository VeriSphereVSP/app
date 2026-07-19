-- Migration: 311_topic_article_unique_constraint.sql
-- Captures the UNIQUE(topic_id) constraint in a migration so a fresh deploy
-- (deploy.sh --fresh) reproduces the one-article-per-topic invariant. The
-- constraint was originally added by the topic-article-canonical patch DRIVER
-- (2026-07-19), which means it lived only in the Fuji DB and would VANISH on a
-- schema rebuild — the same "state-outside-migrations" trap that silently killed
-- the grafana_ro role on the 2026-07-15 fresh redeploy. This migration closes
-- that reproducibility hole.
--
-- Idempotent: only adds the constraint if it isn't already present, so it is a
-- no-op on the box where the driver already added it, and it CREATES it on a
-- fresh database. Depends on 310 (topic_id column). Assumes the data is already
-- one-article-per-topic (true post-cleanup); on a fresh deploy there are zero
-- articles so the constraint is trivially satisfiable.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_topic_article_topic_id'
    ) THEN
        ALTER TABLE topic_article
            ADD CONSTRAINT uq_topic_article_topic_id UNIQUE (topic_id);
    END IF;
END$$;
