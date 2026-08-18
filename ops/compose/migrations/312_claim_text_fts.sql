-- Full-text index on claim text, for phrase-scoped candidate lookup.
--
-- The Verity browser extension underlines on-chain claims inside Wikipedia
-- articles. To do that it sends the salient phrases of a page (title, wikilink
-- anchors, headings) and asks for claims that could plausibly appear there.
-- That lookup is a tsquery over claim_text, so it needs a GIN index or it
-- degrades to a sequential scan of the whole corpus on every page load.
--
-- The expression must match /api/claims/locate's query exactly
-- (to_tsvector('english', claim_text)) for the planner to use the index.
CREATE INDEX IF NOT EXISTS idx_chain_claim_text_fts
    ON chain_claim_text USING GIN (to_tsvector('english', claim_text));
