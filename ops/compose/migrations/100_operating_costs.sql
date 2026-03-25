CREATE TABLE IF NOT EXISTS operating_costs (
    cost_key        TEXT PRIMARY KEY,
    description     TEXT NOT NULL,
    monthly_usd     NUMERIC(12,2) NOT NULL DEFAULT 0,
    updated_at      TIMESTAMP DEFAULT NOW()
);

INSERT INTO operating_costs (cost_key, description, monthly_usd) VALUES
    ('gcp_server',      'GCP VM + disk + networking',         200.00),
    ('rpc_provider',    'Alchemy/QuickNode RPC',              49.00),
    ('anthropic',       'Anthropic LLM (cleanup, moderation, articles)', 80.00),
    ('openai',          'OpenAI embeddings',                  30.00),
    ('avax_gas',        'AVAX gas for relay transactions',    200.00),
    ('domain_ssl',      'Domain registration + SSL',          5.00),
    ('other',           'Miscellaneous',                      10.00)
ON CONFLICT (cost_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS fee_params (
    param_key       TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    description     TEXT,
    updated_at      TIMESTAMP DEFAULT NOW()
);

INSERT INTO fee_params (param_key, value, description) VALUES
    ('expected_monthly_txns',   '1000',     'Expected transactions per month'),
    ('vsp_price_usd',           '1.30',     'VSP price in USD for fee calculation'),
    ('pct_fee_bps',             '100',      'Percentage fee in basis points (100 = 1%)'),
    ('margin_bps',              '3000',     'Profit margin in basis points (3000 = 30%)'),
    ('fee_enabled',             'true',     'Whether platform fees are active')
ON CONFLICT (param_key) DO NOTHING;
