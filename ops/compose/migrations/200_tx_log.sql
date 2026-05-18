-- Migration: 200_tx_log.sql
--
-- Bundle 4a: transaction-log table for async relay notifications.
--
-- Records every transaction submitted through /api/relay/async with its
-- pending/confirmed/reverted/dropped status, action context, and any error
-- message. The chain indexer's poll loop resolves pending rows by fetching
-- receipts.
--
-- Retention policy: keep forever. Periodic archival/purge is a post-launch
-- concern, not handled by this migration.
--
-- Indexes:
--   user_status: drives /api/notifications/{address} (pending, recent)
--   status_submitted: drives the watcher's pending-queue scan
--   post_id: lookups by post (frontend may show "your stake on post 7")
--   tx_hash: unique constraint + lookup by hash

CREATE TABLE IF NOT EXISTS tx_log (
  id              BIGSERIAL PRIMARY KEY,
  tx_hash         TEXT NOT NULL UNIQUE,
  user_address    TEXT NOT NULL,
  to_address      TEXT NOT NULL,
  calldata        TEXT NOT NULL,
  action_type     TEXT NOT NULL,
  action_value    DOUBLE PRECISION,
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','confirmed','reverted','dropped')),
  block_number    BIGINT,
  gas_used        BIGINT,
  post_id         BIGINT,
  error_message   TEXT,
  submitted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tx_log_user_status
    ON tx_log (user_address, status, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_tx_log_status_submitted
    ON tx_log (status, submitted_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_tx_log_post_id
    ON tx_log (post_id)
    WHERE post_id IS NOT NULL;
