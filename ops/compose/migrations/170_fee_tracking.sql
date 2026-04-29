-- Fee tracking: separate reserves from collected fees
ALTER TABLE mm_state ADD COLUMN IF NOT EXISTS fees_collected_usdc DOUBLE PRECISION DEFAULT 0;
ALTER TABLE mm_state ADD COLUMN IF NOT EXISTS fees_collected_vsp DOUBLE PRECISION DEFAULT 0;
ALTER TABLE mm_state ADD COLUMN IF NOT EXISTS relay_fees_collected_vsp DOUBLE PRECISION DEFAULT 0;
ALTER TABLE mm_state ADD COLUMN IF NOT EXISTS total_gas_spent_avax DOUBLE PRECISION DEFAULT 0;

-- Relay fee log
CREATE TABLE IF NOT EXISTS relay_fee_log (
    id SERIAL PRIMARY KEY,
    tx_hash TEXT,
    user_address TEXT NOT NULL,
    gas_estimated INTEGER,
    gas_used INTEGER,
    gas_price_gwei DOUBLE PRECISION,
    gas_cost_avax DOUBLE PRECISION,
    gas_cost_usd DOUBLE PRECISION,
    fee_charged_vsp DOUBLE PRECISION,
    fee_margin_pct DOUBLE PRECISION,
    tx_type TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_relay_fee_user ON relay_fee_log (user_address);
CREATE INDEX IF NOT EXISTS idx_relay_fee_ts ON relay_fee_log (created_at);

-- Fee-exempt allow-list
CREATE TABLE IF NOT EXISTS fee_exempt_addresses (
    address TEXT PRIMARY KEY,
    reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
