-- Fee tracking
ALTER TABLE mm_state ADD COLUMN IF NOT EXISTS fees_collected_usdc DOUBLE PRECISION DEFAULT 0;
ALTER TABLE mm_state ADD COLUMN IF NOT EXISTS relay_fees_collected_vsp DOUBLE PRECISION DEFAULT 0;
ALTER TABLE mm_state ADD COLUMN IF NOT EXISTS total_gas_spent_avax DOUBLE PRECISION DEFAULT 0;

CREATE TABLE IF NOT EXISTS relay_fee_log (
    id SERIAL PRIMARY KEY,
    tx_hash TEXT,
    user_address TEXT NOT NULL,
    fee_charged_vsp DOUBLE PRECISION,
    tx_value_vsp DOUBLE PRECISION,
    tx_type TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_relay_fee_user ON relay_fee_log (user_address);

CREATE TABLE IF NOT EXISTS fee_exempt_addresses (
    address TEXT PRIMARY KEY,
    reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
