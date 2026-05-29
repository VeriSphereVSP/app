-- Migration: 250_mm_trade_fee_usdc.sql
--
-- Bundle 4.5 patch 3.3: per-trade fee tracking.
--
-- fee_usdc records the USDC fee collected on each MM trade (additive
-- on the buy side, subtracted from proceeds on the sell side). Already
-- aggregated in mm_state.fees_collected_usdc as a rolling sum, but
-- per-trade attribution lets the Transactions view show what a user
-- actually paid per trade.
--
-- Nullable: pre-3.3 rows have no recoverable per-trade fee from the
-- rolling sum. They remain NULL and the UI shows "—" for legacy rows.

ALTER TABLE mm_trade
    ADD COLUMN IF NOT EXISTS fee_usdc NUMERIC(18,6);
