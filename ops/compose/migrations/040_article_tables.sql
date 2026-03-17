-- Migration: 040_article_tables.sql
-- Moves article schema out of article_store.py ensure_tables()
-- into the proper migration pipeline.
-- Place at: app/ops/compose/migrations/040_article_tables.sql

CREATE TABLE IF NOT EXISTS topic_article (
    article_id  SERIAL PRIMARY KEY,
    topic_key   TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT NOW(),
    updated_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS article_section (
    section_id  SERIAL PRIMARY KEY,
    article_id  INTEGER NOT NULL REFERENCES topic_article(article_id),
    heading     TEXT NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS article_sentence (
    sentence_id SERIAL PRIMARY KEY,
    section_id  INTEGER NOT NULL REFERENCES article_section(section_id),
    sort_order  INTEGER NOT NULL DEFAULT 0,
    text        TEXT NOT NULL,
    post_id     INTEGER,
    replaced_by INTEGER REFERENCES article_sentence(sentence_id)
);

CREATE INDEX IF NOT EXISTS idx_ta_key ON topic_article(topic_key);
CREATE INDEX IF NOT EXISTS idx_as_article ON article_section(article_id);
CREATE INDEX IF NOT EXISTS idx_sent_section ON article_sentence(section_id);
CREATE INDEX IF NOT EXISTS idx_sent_postid ON article_sentence(post_id);
