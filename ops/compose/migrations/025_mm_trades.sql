-- Migration: 025_mm_trades.sql
-- Immutable audit log for all MM trades.
-- Required for reserve accounting and regulatory transparency.

CREATE TABLE IF NOT EXISTS mm_trade (
  trade_id      BIGSERIAL PRIMARY KEY,
  side          TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
  user_address  TEXT NOT NULL,
  qty_vsp       DOUBLE PRECISION NOT NULL,
  total_usdc    DOUBLE PRECISION NOT NULL,
  avg_price_usd DOUBLE PRECISION NOT NULL,
  net_vsp_before BIGINT NOT NULL,
  net_vsp_after  BIGINT NOT NULL,
  usdc_reserves_after DOUBLE PRECISION NOT NULL,
  vsp_circulating_after DOUBLE PRECISION NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mm_trade_user ON mm_trade (user_address);
CREATE INDEX IF NOT EXISTS idx_mm_trade_created ON mm_trade (created_at);
