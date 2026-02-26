-- Migration: 020_mm_state.sql
-- Updated to support reserve tracking and volume-integrated pricing.

CREATE TABLE IF NOT EXISTS mm_state (
  id BOOLEAN PRIMARY KEY DEFAULT TRUE,
  net_vsp BIGINT NOT NULL DEFAULT 0,
  unit_au DOUBLE PRECISION NOT NULL DEFAULT 0.0002,
  half_spread DOUBLE PRECISION NOT NULL DEFAULT 0.00125,
  usdc_reserves DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  vsp_circulating DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO mm_state (id, net_vsp)
VALUES (TRUE, 0)
ON CONFLICT (id) DO NOTHING;

-- Migration to add new columns to existing table (idempotent)
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_name='mm_state' AND column_name='usdc_reserves') THEN
    ALTER TABLE mm_state ADD COLUMN usdc_reserves DOUBLE PRECISION NOT NULL DEFAULT 0.0;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_name='mm_state' AND column_name='vsp_circulating') THEN
    ALTER TABLE mm_state ADD COLUMN vsp_circulating DOUBLE PRECISION NOT NULL DEFAULT 0.0;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_name='mm_state' AND column_name='half_spread') THEN
    ALTER TABLE mm_state ADD COLUMN half_spread DOUBLE PRECISION NOT NULL DEFAULT 0.00125;
  END IF;
  -- Migrate from spread_rate to half_spread if old column exists
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name='mm_state' AND column_name='spread_rate') THEN
    UPDATE mm_state SET half_spread = (spread_rate - 1.0) / 2.0 WHERE half_spread = 0.00125;
    ALTER TABLE mm_state DROP COLUMN spread_rate;
  END IF;
END $$;


