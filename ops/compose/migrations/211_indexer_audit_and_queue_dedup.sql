-- 211_indexer_audit_and_queue_dedup.sql
-- Bundle 4 patch 2: queue dedup guard + indexer self-audit log table.
--
-- 1. UNIQUE partial index on derived_state_queue to prevent duplicate
--    pending/in_progress rows for the same (post_id, queue_kind).
--    The chain indexer enqueues a row on every event affecting a post,
--    but the derivation work is idempotent — a single pending row is
--    enough to cover all events on a post until it's processed.
--
-- 2. indexer_audit_log table for the self-audit task to record drift
--    findings (post indexed in DB vs what the chain currently says).

BEGIN;

-- ── (1) Queue dedup partial unique index ──────────────────────────
-- Use a partial index because we only care about uniqueness among
-- active (pending/in_progress) rows. Completed/failed rows accumulate
-- and we don't want them to block re-enqueue.
CREATE UNIQUE INDEX IF NOT EXISTS uq_dsq_active_per_post
  ON derived_state_queue (post_id, queue_kind)
  WHERE status IN ('pending', 'in_progress');

-- ── (2) Indexer audit log ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS indexer_audit_log (
    id           bigserial PRIMARY KEY,
    audited_at   timestamptz NOT NULL DEFAULT now(),
    post_id      bigint NOT NULL,
    field        text NOT NULL,
    db_value     text,
    chain_value  text,
    drift_kind   text NOT NULL CHECK (drift_kind IN ('mismatch', 'missing_db', 'missing_chain'))
);

CREATE INDEX IF NOT EXISTS idx_iaud_audited_at ON indexer_audit_log (audited_at DESC);
CREATE INDEX IF NOT EXISTS idx_iaud_post       ON indexer_audit_log (post_id, audited_at DESC);

COMMIT;
