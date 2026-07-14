-- Migration: 290_claim_topic_m2m.sql

-- pgvector is already enabled by earlier migrations (120/160); assert defensively.
CREATE EXTENSION IF NOT EXISTS vector;
-- Phase 1 of the claim<->topic many-to-many model (see CLAIM-TOPIC-M2M-DESIGN.md).
-- Additive and reversible: creates the schema only. Nothing reads it yet; the
-- reconciler / heal #0 and the read/write flip land in later migrations/patches.
--
-- Identity is the cluster (topic_id), not the label string (design §3). Associations
-- (claim_topic) are the source of truth; placement will project from them. topic_edge
-- is a multi-parent DAG with acyclicity enforced on write.

-- ── topic: stable identity + mutable label (design §4.1) ────────────────────────
CREATE TABLE IF NOT EXISTS topic (
    topic_id    BIGSERIAL PRIMARY KEY,
    label       TEXT        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'deprecated')),
    centroid    vector(1536),                 -- cluster centroid; populated by reconciler
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_topic_status ON topic(status);
-- label is a display attribute, not identity, so it is NOT unique — two active
-- topics could transiently share a label mid-reconcile. A case-insensitive lookup
-- index helps the resolve-then-mint step without constraining identity.
CREATE INDEX IF NOT EXISTS ix_topic_label_lower ON topic(lower(label));

-- ── claim_topic: associations = source of truth (design §4.2) ───────────────────
CREATE TABLE IF NOT EXISTS claim_topic (
    claim_id    BIGINT      NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
    topic_id    BIGINT      NOT NULL REFERENCES topic(topic_id) ON DELETE CASCADE,
    is_primary  BOOLEAN     NOT NULL DEFAULT FALSE,
    source      TEXT        NOT NULL
                CHECK (source IN ('detected', 'snapped', 'relevant', 'inherited')),
    relevance   REAL,                          -- cosine; NULL for substring/inherited
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (claim_id, topic_id)
);
-- Exactly one primary per claim (design §5, R4).
CREATE UNIQUE INDEX IF NOT EXISTS ux_claim_topic_primary
    ON claim_topic(claim_id) WHERE is_primary;
CREATE INDEX IF NOT EXISTS ix_claim_topic_topic ON claim_topic(topic_id);

-- ── topic_edge: multi-parent DAG, child_id -> parent_id (design §4.3) ────────────
CREATE TABLE IF NOT EXISTS topic_edge (
    child_id   BIGINT NOT NULL REFERENCES topic(topic_id) ON DELETE CASCADE,
    parent_id  BIGINT NOT NULL REFERENCES topic(topic_id) ON DELETE CASCADE,
    PRIMARY KEY (child_id, parent_id),
    CHECK (child_id <> parent_id)              -- no self-loop
);
CREATE INDEX IF NOT EXISTS ix_topic_edge_parent ON topic_edge(parent_id);

-- Acyclicity: reject any edge (c, p) that would make c an ancestor of itself.
-- Adding (c -> p) [c is a child of p] creates a cycle iff c is ALREADY an ancestor
-- of p, i.e. p can reach c by following child->parent edges. Walk p's ancestors;
-- if c is among them, reject.
CREATE OR REPLACE FUNCTION topic_edge_no_cycle() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        WITH RECURSIVE anc(id) AS (
            SELECT parent_id FROM topic_edge WHERE child_id = NEW.parent_id
            UNION
            SELECT e.parent_id FROM topic_edge e JOIN anc ON e.child_id = anc.id
        )
        SELECT 1 FROM anc WHERE id = NEW.child_id
    ) THEN
        RAISE EXCEPTION
            'topic_edge (% -> %) would create a cycle', NEW.child_id, NEW.parent_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_topic_edge_no_cycle ON topic_edge;
CREATE TRIGGER trg_topic_edge_no_cycle
    BEFORE INSERT OR UPDATE ON topic_edge
    FOR EACH ROW EXECUTE FUNCTION topic_edge_no_cycle();
