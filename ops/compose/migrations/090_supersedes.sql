-- Migration: 090_supersedes.sql
-- Track when one claim supersedes another (via the edit flow).

CREATE TABLE IF NOT EXISTS claim_supersedes (
    id              SERIAL PRIMARY KEY,
    old_post_id     INTEGER NOT NULL,
    new_post_id     INTEGER NOT NULL,
    created_by      TEXT NOT NULL,           -- wallet address of the editor
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE(old_post_id, new_post_id)
);

CREATE INDEX IF NOT EXISTS idx_cs_old ON claim_supersedes(old_post_id);
CREATE INDEX IF NOT EXISTS idx_cs_new ON claim_supersedes(new_post_id);

-- Track user responses to supersede notifications (accept or reject).
-- Once a user responds, the notification disappears.
CREATE TABLE IF NOT EXISTS supersede_response (
    id              SERIAL PRIMARY KEY,
    supersede_id    INTEGER NOT NULL REFERENCES claim_supersedes(id),
    user_address    TEXT NOT NULL,           -- wallet address of the staker
    response        TEXT NOT NULL CHECK (response IN ('accept', 'reject')),
    responded_at    TIMESTAMP DEFAULT NOW(),
    UNIQUE(supersede_id, user_address)
);

CREATE INDEX IF NOT EXISTS idx_sr_user ON supersede_response(user_address);
