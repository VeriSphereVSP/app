-- Migration: 190_claim_dupe_verdict.sql
-- Cache for LLM verdicts on claim-pair equivalence. Avoids
-- re-asking the same pair on every refresh_all_groups cycle.
-- patch05.

CREATE TABLE IF NOT EXISTS claim_dupe_verdict (
    post_a INTEGER NOT NULL,    -- smaller post_id
    post_b INTEGER NOT NULL,    -- larger post_id
    verdict TEXT NOT NULL CHECK (verdict IN ('EQUIVALENT', 'DIFFERENT')),
    similarity DOUBLE PRECISION,  -- cosine similarity at decision time
    model TEXT,                   -- LLM model that produced the verdict
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (post_a, post_b),
    CHECK (post_a < post_b)
);

CREATE INDEX IF NOT EXISTS idx_dupe_verdict_decided ON claim_dupe_verdict (decided_at);
