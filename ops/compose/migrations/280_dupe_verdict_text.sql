-- 280_dupe_verdict_text.sql
--
-- Content-hash verdict cache for the unified grouping module.
--
-- claim_dupe_verdict (migration 190) caches LLM equivalence verdicts keyed by
-- (post_a, post_b) integer post ids — claim<->claim only. The unified grouping
-- module also compares pairs that involve a SENTENCE (which often has no
-- post_id), so those verdicts cannot use the post-keyed cache. This table
-- caches them keyed by the sha256 of each side's normalized text, so a borderline
-- (0.65<=cosine<0.95) sentence pair is LLM-verified ONCE and reused for the life
-- of those texts (converges to ~zero LLM cost as articles stabilize).
--
-- hash_a is always the lexicographically-smaller hash (deterministic ordering,
-- mirrors the (min,max) post ordering of claim_dupe_verdict).

CREATE TABLE IF NOT EXISTS dupe_verdict_text (
    hash_a      TEXT NOT NULL,                 -- sha256(normalized text), smaller
    hash_b      TEXT NOT NULL,                 -- sha256(normalized text), larger
    verdict     TEXT NOT NULL CHECK (verdict IN ('EQUIVALENT', 'DIFFERENT')),
    similarity  DOUBLE PRECISION,              -- cosine at decision time
    model       TEXT,                          -- LLM model that produced it
    decided_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (hash_a, hash_b),
    CHECK (hash_a < hash_b)
);

CREATE INDEX IF NOT EXISTS idx_dupe_verdict_text_decided
    ON dupe_verdict_text (decided_at);
