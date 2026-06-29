-- Migration: 260_ops_metrics.sql
--
-- Admin-dashboard support: a generic time-series sink for OPERATIONAL/HOST
-- metrics that must NOT be read by the dashboard reaching for the Docker socket
-- (socket access ~= root on the host; unacceptable next to funds). Instead, the
-- services that already run inside the compose network write their own health
-- metrics here on a cadence, and Grafana reads this table read-only.
--
-- Design: narrow, append-only, generic (metric name + value + optional labels).
-- One row per (metric, sample). Grafana time-series panels group by metric.
--
--   metric        e.g. 'container_up', 'error_rate_5m', 'indexer_lag_blocks',
--                      'rpc_latency_ms', 'cpu_pct', 'mem_pct', 'idle_tx_count'
--   value_num     the numeric sample
--   labels        optional JSONB, e.g. {"container":"verisphere-worker-1"}
--   sampled_at    timestamp of the sample
--
-- Retention: a simple time-based prune (keep ~30 days) handled by the writer or a
-- cron; this table is not a long-term store.

CREATE TABLE IF NOT EXISTS ops_metrics (
    id          BIGSERIAL PRIMARY KEY,
    metric      TEXT NOT NULL,
    value_num   DOUBLE PRECISION,
    labels      JSONB,
    sampled_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Fast time-series scans per metric (Grafana queries are WHERE metric=... ORDER BY sampled_at).
CREATE INDEX IF NOT EXISTS ops_metrics_metric_time_idx
    ON ops_metrics (metric, sampled_at DESC);

-- Optional retention helper (call from the writer or a cron):
--   DELETE FROM ops_metrics WHERE sampled_at < now() - interval '30 days';
