-- Migration: 240_mm_trade_tx_hash.sql
--
-- Bundle 4.5 patch 2.1: anchor MM trades to on-chain txs.
--
-- Records the VSP transfer's tx_hash for each MM buy/sell. Nullable
-- because pre-existing rows have no recoverable hash. New rows
-- populate via mm_routes._log_trade(... tx_hash=...).
--
-- Used by:
--   - /api/notifications unified feed (to expose Snowtrace link)
--   - postmortem queries linking MM activity to chain history
--
-- NOT used as a dedup key against chain_tx — MM's internal address
-- already filters those Transfer events at the indexer level.

ALTER TABLE mm_trade
    ADD COLUMN IF NOT EXISTS tx_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_mm_trade_tx_hash
    ON mm_trade (tx_hash)
    WHERE tx_hash IS NOT NULL;
