-- Migration: 311_topic_article_claim_backfill.sql
-- Corrects the topic_article.topic_id backfill. Migration 310 added the column
-- and did a first-pass EXACT-TITLE-MATCH backfill, which missed variant-title
-- articles ("January 6th Capitol Attack" vs topic label "January 6 Capitol
-- Attack") — the very "6 vs 6th" variation behind the 3-article fan-out.
--
-- This backfills the still-NULL articles by the SEMANTIC binding the system
-- already computed: an article belongs to the topic its staked CLAIMS resolve to
-- (claim_topic linkage, centroid-based). Robust to title variation. Idempotent:
-- only touches rows still NULL; ADD COLUMN IF NOT EXISTS makes it self-contained.
-- Claim-less articles remain NULL for the patch reconciler's centroid-summary
-- binding (_bind_unbound_articles).

ALTER TABLE topic_article
    ADD COLUMN IF NOT EXISTS topic_id INTEGER REFERENCES topic(topic_id);

UPDATE topic_article ta
SET topic_id = sub.topic_id
FROM (
    SELECT s.article_id, ct.topic_id,
           ROW_NUMBER() OVER (PARTITION BY s.article_id
                              ORDER BY COUNT(*) DESC) AS rnk
    FROM (
        SELECT sec.article_id, asent.post_id
        FROM article_sentence asent
        JOIN article_section sec ON asent.section_id = sec.section_id
        WHERE asent.post_id IS NOT NULL
    ) s
    JOIN chain_claim_text cct ON cct.post_id = s.post_id
    JOIN claim c ON c.claim_text = cct.claim_text
    JOIN claim_topic ct ON ct.claim_id = c.claim_id
    GROUP BY s.article_id, ct.topic_id
) sub
WHERE sub.article_id = ta.article_id AND sub.rnk = 1 AND ta.topic_id IS NULL;
