-- 210_atomic_indexer.sql
-- Bundle 4 patch 1: schema changes for atomic indexer with derived-state queue.
--
-- 1. Single global cursor `last_block_global` initialized to min of the three
--    existing per-contract cursors (so we never skip events). The three old
--    keys are left in place as forensic record; new code reads only the new key.
--
-- 2. `derived_state_queue` table holding pending derivation work
--    (article-system updates, dupe-grouping, topic detection) decoupled from
--    the indexer's atomic poll loop.

BEGIN;

-- ── (1) Single global cursor ──────────────────────────────────────
INSERT INTO chain_indexer_state (key, value, updated_at)
SELECT 'last_block_global',
       GREATEST(
         LEAST(
           COALESCE((SELECT NULLIF(value,'')::bigint FROM chain_indexer_state WHERE key='last_block_StakeEngine'),  0),
           COALESCE((SELECT NULLIF(value,'')::bigint FROM chain_indexer_state WHERE key='last_block_PostRegistry'), 0),
           COALESCE((SELECT NULLIF(value,'')::bigint FROM chain_indexer_state WHERE key='last_block_LinkGraph'),    0)
         ),
         0
       )::text,
       now()
WHERE NOT EXISTS (SELECT 1 FROM chain_indexer_state WHERE key='last_block_global');

-- ── (2) Derived-state queue ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS derived_state_queue (
    id            bigserial PRIMARY KEY,
    post_id       bigint    NOT NULL,
    queue_kind    text      NOT NULL CHECK (queue_kind IN ('post_create', 'post_update')),
    queued_at     timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    completed_at  timestamptz,
    attempt_count int       NOT NULL DEFAULT 0,
    last_error    text,
    status        text      NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'in_progress', 'completed', 'failed'))
);

-- Hot path: claim-and-dispatch loop queries by (status, queued_at). Partial
-- index keeps the index size bounded to actively-processable rows.
CREATE INDEX IF NOT EXISTS idx_dsq_status_queued
  ON derived_state_queue (status, queued_at)
  WHERE status IN ('pending', 'in_progress');

-- Per-post lookups (for self-audit, debugging).
CREATE INDEX IF NOT EXISTS idx_dsq_post
  ON derived_state_queue (post_id, status);

COMMIT;
