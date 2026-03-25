-- Migration: 060_article_views.sql
-- Track article view counts for popular topics on the landing page.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_name='topic_article' AND column_name='view_count') THEN
    ALTER TABLE topic_article ADD COLUMN view_count INTEGER NOT NULL DEFAULT 0;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ta_views ON topic_article(view_count DESC);
