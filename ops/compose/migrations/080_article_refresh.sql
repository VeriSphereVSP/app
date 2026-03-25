-- Migration: 080_article_refresh.sql
-- Track when articles were last refreshed with new content.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_name='topic_article' AND column_name='last_refreshed_at') THEN
    ALTER TABLE topic_article ADD COLUMN last_refreshed_at TIMESTAMP;
  END IF;
END $$;
