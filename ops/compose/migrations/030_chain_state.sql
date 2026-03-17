-- Migration: 030_chain_state.sql
-- Indexed on-chain state for fast API reads.
-- Populated by the chain_indexer background process.
-- Source of truth: on-chain contracts. This is a read cache.

-- Per-post stake totals and VS
CREATE TABLE IF NOT EXISTS chain_post (
    post_id         INTEGER PRIMARY KEY,
    content_type    SMALLINT NOT NULL DEFAULT 0,  -- 0=claim, 1=link
    creator         TEXT,
    support_total   DOUBLE PRECISION NOT NULL DEFAULT 0,
    challenge_total DOUBLE PRECISION NOT NULL DEFAULT 0,
    base_vs         DOUBLE PRECISION NOT NULL DEFAULT 0,
    effective_vs    DOUBLE PRECISION NOT NULL DEFAULT 0,
    is_active       BOOLEAN NOT NULL DEFAULT FALSE,
    created_epoch   BIGINT,
    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-user per-post stake positions
CREATE TABLE IF NOT EXISTS chain_user_stake (
    user_address    TEXT NOT NULL,
    post_id         INTEGER NOT NULL,
    side            SMALLINT NOT NULL,  -- 0=support, 1=challenge
    amount          DOUBLE PRECISION NOT NULL DEFAULT 0,
    weighted_position DOUBLE PRECISION NOT NULL DEFAULT 0,
    entry_epoch     BIGINT,
    tranche         INTEGER NOT NULL DEFAULT 0,
    position_weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_address, post_id, side)
);

-- Evidence links (from LinkGraph)
CREATE TABLE IF NOT EXISTS chain_link (
    link_post_id    INTEGER PRIMARY KEY,
    from_post_id    INTEGER NOT NULL,
    to_post_id      INTEGER NOT NULL,
    is_challenge    BOOLEAN NOT NULL DEFAULT FALSE,
    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Claim text (from PostRegistry)
CREATE TABLE IF NOT EXISTS chain_claim_text (
    post_id         INTEGER PRIMARY KEY,
    claim_text      TEXT NOT NULL,
    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexer state: track last processed block
CREATE TABLE IF NOT EXISTS chain_indexer_state (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Global protocol stats (cached)
CREATE TABLE IF NOT EXISTS chain_global (
    key             TEXT PRIMARY KEY,
    value_num       DOUBLE PRECISION,
    value_text      TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_chain_post_vs ON chain_post (effective_vs);
CREATE INDEX IF NOT EXISTS idx_chain_post_stake ON chain_post (support_total, challenge_total);
CREATE INDEX IF NOT EXISTS idx_chain_user_stake_user ON chain_user_stake (user_address);
CREATE INDEX IF NOT EXISTS idx_chain_user_stake_post ON chain_user_stake (post_id);
CREATE INDEX IF NOT EXISTS idx_chain_link_from ON chain_link (from_post_id);
CREATE INDEX IF NOT EXISTS idx_chain_link_to ON chain_link (to_post_id);