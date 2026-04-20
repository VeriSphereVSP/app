-- APP-02: Display-side moderation flag
ALTER TABLE chain_claim_text ADD COLUMN IF NOT EXISTS is_moderated BOOLEAN DEFAULT FALSE;
