CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS claim (
  claim_id      BIGSERIAL PRIMARY KEY,
  claim_text    TEXT NOT NULL,
  content_hash  TEXT NOT NULL UNIQUE,
  created_tms   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS claim_embedding (
  claim_id         BIGINT PRIMARY KEY REFERENCES claim(claim_id) ON DELETE CASCADE,
  embedding_model  TEXT NOT NULL,
  embedding        vector(3072),
  updated_tms      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS claim_cluster (
  cluster_id           BIGSERIAL PRIMARY KEY,
  canonical_claim_id   BIGINT NOT NULL REFERENCES claim(claim_id) ON DELETE RESTRICT,
  created_tms          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS claim_cluster_member (
  cluster_id   BIGINT NOT NULL REFERENCES claim_cluster(cluster_id) ON DELETE CASCADE,
  claim_id     BIGINT NOT NULL REFERENCES claim(claim_id) ON DELETE CASCADE,
  similarity   DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  created_tms  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (cluster_id, claim_id)
);
