-- ============================================================
-- Migration 003: Execution Metrics Table
-- ============================================================
-- Tracks order execution quality: slippage, latency, fill rate.
-- Used by ExecutionAnalytics for scaling readiness assessment.
-- ============================================================

CREATE TABLE IF NOT EXISTS execution_metrics (
    id              SERIAL PRIMARY KEY,
    order_id        TEXT NOT NULL,
    side            TEXT NOT NULL,
    size            INTEGER NOT NULL,
    expected_price  FLOAT,
    fill_price      FLOAT,
    slippage_ticks  FLOAT,
    latency_ms      INTEGER,
    order_type      TEXT,
    status          TEXT,  -- filled, cancelled, rejected, partial
    order_sent_at   TIMESTAMPTZ,
    fill_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_exec_metrics_order_id ON execution_metrics(order_id);
CREATE INDEX IF NOT EXISTS idx_exec_metrics_sent_at ON execution_metrics(order_sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_exec_metrics_status ON execution_metrics(status);
