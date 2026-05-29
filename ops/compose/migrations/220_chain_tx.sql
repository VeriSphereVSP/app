-- Migration: 220_chain_tx.sql
--
-- Bundle 4.5: chain-sourced per-event transaction history.
--
-- Populated by chain_indexer's atomic poll loop in the same DB
-- transaction that advances last_block_global. One row per
-- (event, affected user). For VSP Transfer events, up to two rows
-- per event (one per side: from-side transfer_out, to-side transfer_in),
-- with internal addresses (StakeEngine, MM, COLD_SAFE_ADDRESSES) and
-- the zero address filtered out at write time.
--
-- NOT populated by full_sync — only by forward poll. Backfill of
-- historical events requires the manual backfill CLI (chain_indexer.backfill).
--
-- Retention: keep forever. Postmortem-grade history is the whole point.

CREATE TABLE IF NOT EXISTS chain_tx (
  id              BIGSERIAL PRIMARY KEY,
  block_number    BIGINT NOT NULL,
  tx_hash         TEXT NOT NULL,
  log_index       INTEGER NOT NULL,
  block_epoch     BIGINT,
  contract        TEXT NOT NULL,
  event_name      TEXT NOT NULL,
  action_type     TEXT NOT NULL,
  user_address    TEXT NOT NULL,
  counterparty    TEXT,
  post_id         BIGINT,
  amount_vsp      DOUBLE PRECISION,
  is_challenge    BOOLEAN,
  indexed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_chain_tx_hash_log_user
    ON chain_tx (tx_hash, log_index, user_address);
CREATE INDEX IF NOT EXISTS idx_chain_tx_user
    ON chain_tx (user_address, block_number DESC, log_index DESC);
CREATE INDEX IF NOT EXISTS idx_chain_tx_tx_hash
    ON chain_tx (tx_hash);
