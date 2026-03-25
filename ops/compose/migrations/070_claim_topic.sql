-- Migration: 070_claim_topic.sql
-- Store auto-detected topic on claims for display in Claims Explorer.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_name='claim' AND column_name='topic') THEN
    ALTER TABLE claim ADD COLUMN topic TEXT;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_claim_topic ON claim(topic) WHERE topic IS NOT NULL;
