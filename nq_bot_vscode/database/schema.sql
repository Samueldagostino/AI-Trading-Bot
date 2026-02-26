-- ============================================================
-- NQ Trading Bot — PostgreSQL Schema
-- ============================================================
-- Design principles:
--   1. Time-series data uses TimescaleDB hypertables where possible
--   2. All timestamps are UTC with timezone
--   3. No data is ever deleted — only soft-deleted or archived
--   4. Every trade has full audit trail
-- ============================================================

-- Enable extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
-- CREATE EXTENSION IF NOT EXISTS timescaledb;  -- Uncomment if TimescaleDB installed

-- ============================================================
-- DATA LAYER TABLES
-- ============================================================

-- Raw NQ price bars (1-minute OHLCV)
CREATE TABLE IF NOT EXISTS nq_bars_1m (
    id              BIGSERIAL PRIMARY KEY,
    timestamp_utc   TIMESTAMPTZ NOT NULL,
    symbol          VARCHAR(10) NOT NULL DEFAULT 'NQ',
    contract        VARCHAR(20) NOT NULL,          -- e.g., 'NQH2025' for roll tracking
    open            NUMERIC(12,2) NOT NULL,
    high            NUMERIC(12,2) NOT NULL,
    low             NUMERIC(12,2) NOT NULL,
    close           NUMERIC(12,2) NOT NULL,
    volume          BIGINT NOT NULL DEFAULT 0,
    tick_count      INTEGER DEFAULT 0,             -- Number of ticks in bar
    vwap            NUMERIC(12,2),                 -- Bar VWAP
    bid_volume      BIGINT DEFAULT 0,              -- Volume at bid (sell aggression)
    ask_volume      BIGINT DEFAULT 0,              -- Volume at ask (buy aggression)
    delta           BIGINT DEFAULT 0,              -- ask_volume - bid_volume
    data_version    INTEGER DEFAULT 1,             -- For data corrections/reloads
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(timestamp_utc, symbol, contract, data_version)
);

CREATE INDEX idx_nq_bars_1m_ts ON nq_bars_1m(timestamp_utc DESC);
CREATE INDEX idx_nq_bars_1m_symbol_ts ON nq_bars_1m(symbol, timestamp_utc DESC);

-- Continuous contract mapping (for roll adjustments)
CREATE TABLE IF NOT EXISTS contract_rolls (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(10) NOT NULL DEFAULT 'NQ',
    front_contract  VARCHAR(20) NOT NULL,
    back_contract   VARCHAR(20) NOT NULL,
    roll_date       DATE NOT NULL,
    price_adjustment NUMERIC(12,2) NOT NULL,       -- Back-adjustment amount
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- VIX data for regime detection
CREATE TABLE IF NOT EXISTS vix_data (
    id              BIGSERIAL PRIMARY KEY,
    timestamp_utc   TIMESTAMPTZ NOT NULL,
    value           NUMERIC(8,2) NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_vix_ts ON vix_data(timestamp_utc DESC);

-- Economic calendar events
CREATE TABLE IF NOT EXISTS economic_events (
    id              SERIAL PRIMARY KEY,
    event_name      VARCHAR(200) NOT NULL,
    event_time_utc  TIMESTAMPTZ NOT NULL,
    impact_level    VARCHAR(10) NOT NULL CHECK (impact_level IN ('low', 'medium', 'high', 'critical')),
    actual_value    VARCHAR(50),
    forecast_value  VARCHAR(50),
    previous_value  VARCHAR(50),
    currency        VARCHAR(5) DEFAULT 'USD',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_econ_events_time ON economic_events(event_time_utc);

-- ============================================================
-- DISCORD INGESTION TABLES
-- ============================================================

-- Raw Discord messages from monitored channels
CREATE TABLE IF NOT EXISTS discord_messages (
    id              BIGSERIAL PRIMARY KEY,
    message_id      VARCHAR(30) NOT NULL UNIQUE,   -- Discord message snowflake ID
    channel_id      VARCHAR(30) NOT NULL,
    channel_name    VARCHAR(100),
    author_id       VARCHAR(30) NOT NULL,
    author_name     VARCHAR(100),
    content         TEXT NOT NULL,
    timestamp_utc   TIMESTAMPTZ NOT NULL,
    -- Parsed fields
    detected_bias   VARCHAR(10) CHECK (detected_bias IN ('bullish', 'bearish', 'neutral', 'mixed')),
    bias_confidence NUMERIC(4,3),                  -- 0.000 to 1.000
    bias_keywords   TEXT[],                         -- Which keywords triggered
    processed       BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_discord_ts ON discord_messages(timestamp_utc DESC);
CREATE INDEX idx_discord_bias ON discord_messages(detected_bias, bias_confidence DESC);

-- Discord signal author reliability tracking
CREATE TABLE IF NOT EXISTS discord_author_stats (
    author_id       VARCHAR(30) PRIMARY KEY,
    author_name     VARCHAR(100),
    total_signals   INTEGER DEFAULT 0,
    correct_signals INTEGER DEFAULT 0,
    win_rate        NUMERIC(5,3) DEFAULT 0.0,
    avg_confidence  NUMERIC(5,3) DEFAULT 0.0,
    reliability_score NUMERIC(5,3) DEFAULT 0.5,    -- Bayesian-updated reliability
    last_signal_at  TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- FEATURE LAYER TABLES
-- ============================================================

-- Computed features per bar (order blocks, FVGs, etc.)
CREATE TABLE IF NOT EXISTS computed_features (
    id              BIGSERIAL PRIMARY KEY,
    bar_timestamp   TIMESTAMPTZ NOT NULL,
    symbol          VARCHAR(10) NOT NULL DEFAULT 'NQ',
    
    -- Volatility
    atr_14          NUMERIC(10,2),
    realized_vol_20 NUMERIC(10,4),
    
    -- VWAP
    session_vwap    NUMERIC(12,2),
    vwap_dev_1      NUMERIC(12,2),
    vwap_dev_2      NUMERIC(12,2),
    vwap_dev_neg1   NUMERIC(12,2),
    vwap_dev_neg2   NUMERIC(12,2),
    price_vs_vwap   NUMERIC(10,2),                 -- Distance from VWAP in points
    
    -- Order Flow
    cumulative_delta BIGINT,
    delta_divergence BOOLEAN DEFAULT FALSE,         -- Price up + delta down or vice versa
    volume_imbalance NUMERIC(8,4),                  -- (ask_vol - bid_vol) / total_vol
    
    -- Market Structure
    is_swing_high    BOOLEAN DEFAULT FALSE,
    is_swing_low     BOOLEAN DEFAULT FALSE,
    trend_direction  VARCHAR(10),                   -- 'up', 'down', 'none'
    trend_strength   NUMERIC(5,3),                  -- 0 to 1
    
    -- Regime
    detected_regime  VARCHAR(20),
    vix_level        NUMERIC(8,2),
    
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(bar_timestamp, symbol)
);

CREATE INDEX idx_features_ts ON computed_features(bar_timestamp DESC);

-- Order Blocks
CREATE TABLE IF NOT EXISTS order_blocks (
    id              BIGSERIAL PRIMARY KEY,
    symbol          VARCHAR(10) DEFAULT 'NQ',
    detected_at     TIMESTAMPTZ NOT NULL,           -- When the OB was identified
    direction       VARCHAR(10) NOT NULL CHECK (direction IN ('bullish', 'bearish')),
    zone_high       NUMERIC(12,2) NOT NULL,
    zone_low        NUMERIC(12,2) NOT NULL,
    displacement_size NUMERIC(10,2),                -- How far price moved away
    is_valid        BOOLEAN DEFAULT TRUE,
    mitigated       BOOLEAN DEFAULT FALSE,          -- Has price returned to it?
    mitigated_at    TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,                    -- OB expiration timestamp
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_ob_valid ON order_blocks(is_valid, mitigated, direction);

-- Fair Value Gaps
CREATE TABLE IF NOT EXISTS fair_value_gaps (
    id              BIGSERIAL PRIMARY KEY,
    symbol          VARCHAR(10) DEFAULT 'NQ',
    detected_at     TIMESTAMPTZ NOT NULL,
    gap_type        VARCHAR(10) NOT NULL CHECK (gap_type IN ('bullish', 'bearish')),
    gap_high        NUMERIC(12,2) NOT NULL,
    gap_low         NUMERIC(12,2) NOT NULL,
    gap_size_points NUMERIC(10,2) NOT NULL,
    is_inverse      BOOLEAN DEFAULT FALSE,          -- IFVG flag
    filled_pct      NUMERIC(5,3) DEFAULT 0.0,
    is_valid        BOOLEAN DEFAULT TRUE,
    respected       BOOLEAN DEFAULT FALSE,          -- Did price react at it?
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_fvg_valid ON fair_value_gaps(is_valid, gap_type);

-- Liquidity Sweep Events
CREATE TABLE IF NOT EXISTS liquidity_sweeps (
    id              BIGSERIAL PRIMARY KEY,
    symbol          VARCHAR(10) DEFAULT 'NQ',
    detected_at     TIMESTAMPTZ NOT NULL,
    sweep_type      VARCHAR(20) NOT NULL CHECK (sweep_type IN ('buy_side', 'sell_side')),
    swept_level     NUMERIC(12,2) NOT NULL,         -- The level that was swept
    sweep_high      NUMERIC(12,2),
    sweep_low       NUMERIC(12,2),
    volume_at_sweep BIGINT,
    wick_ratio      NUMERIC(5,3),
    confirmed       BOOLEAN DEFAULT FALSE,          -- Confirmed by displacement
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_sweeps_ts ON liquidity_sweeps(detected_at DESC);

-- ============================================================
-- SIGNAL & TRADE TABLES
-- ============================================================

-- Aggregated trade signals
CREATE TABLE IF NOT EXISTS trade_signals (
    id              BIGSERIAL PRIMARY KEY,
    signal_time     TIMESTAMPTZ NOT NULL,
    symbol          VARCHAR(10) DEFAULT 'NQ',
    direction       VARCHAR(10) NOT NULL CHECK (direction IN ('long', 'short')),
    
    -- Confluence scoring
    discord_score   NUMERIC(5,3) DEFAULT 0.0,
    technical_score NUMERIC(5,3) DEFAULT 0.0,
    ml_score        NUMERIC(5,3) DEFAULT 0.0,
    combined_score  NUMERIC(5,3) NOT NULL,
    
    -- What contributed to the signal
    contributing_signals JSONB,                     -- Array of signal names + scores
    num_signals_aligned INTEGER,
    
    -- Regime context
    market_regime   VARCHAR(20),
    vix_at_signal   NUMERIC(8,2),
    atr_at_signal   NUMERIC(10,2),
    
    -- Risk parameters computed at signal time
    suggested_stop  NUMERIC(12,2),
    suggested_target NUMERIC(12,2),
    suggested_size  INTEGER,                        -- Number of contracts
    risk_reward_ratio NUMERIC(5,2),
    
    -- Status
    status          VARCHAR(20) DEFAULT 'pending' 
                    CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'executed')),
    rejection_reason TEXT,
    
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_signals_status ON trade_signals(status, signal_time DESC);

-- Executed trades (full audit trail)
CREATE TABLE IF NOT EXISTS trades (
    id              BIGSERIAL PRIMARY KEY,
    trade_id        UUID DEFAULT uuid_generate_v4() UNIQUE,
    signal_id       BIGINT REFERENCES trade_signals(id),
    symbol          VARCHAR(10) DEFAULT 'NQ',
    contract        VARCHAR(20),
    direction       VARCHAR(10) NOT NULL CHECK (direction IN ('long', 'short')),
    
    -- Entry
    entry_time      TIMESTAMPTZ,
    entry_price     NUMERIC(12,2),
    entry_slippage  NUMERIC(10,2) DEFAULT 0,
    
    -- Exit
    exit_time       TIMESTAMPTZ,
    exit_price      NUMERIC(12,2),
    exit_slippage   NUMERIC(10,2) DEFAULT 0,
    exit_reason     VARCHAR(30),                    -- 'target', 'stop', 'trailing', 'manual', 'kill_switch', 'time'
    
    -- Position
    contracts       INTEGER NOT NULL,
    is_micro        BOOLEAN DEFAULT TRUE,
    
    -- Stops & Targets
    stop_loss       NUMERIC(12,2),
    take_profit     NUMERIC(12,2),
    trailing_stop   NUMERIC(12,2),
    
    -- PnL
    gross_pnl       NUMERIC(12,2),
    commission      NUMERIC(10,2),
    net_pnl         NUMERIC(12,2),
    pnl_r_multiple  NUMERIC(6,2),                  -- PnL in R multiples
    
    -- Context
    market_regime   VARCHAR(20),
    atr_at_entry    NUMERIC(10,2),
    vix_at_entry    NUMERIC(8,2),
    
    -- Status
    status          VARCHAR(20) DEFAULT 'open' 
                    CHECK (status IN ('open', 'closed', 'cancelled', 'error')),
    notes           TEXT,
    
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_trades_status ON trades(status, entry_time DESC);
CREATE INDEX idx_trades_direction ON trades(direction, entry_time DESC);

-- ============================================================
-- RISK & MONITORING TABLES
-- ============================================================

-- Daily risk snapshot
CREATE TABLE IF NOT EXISTS daily_risk_snapshot (
    id              SERIAL PRIMARY KEY,
    snapshot_date   DATE NOT NULL UNIQUE,
    starting_equity NUMERIC(14,2),
    ending_equity   NUMERIC(14,2),
    daily_pnl       NUMERIC(12,2),
    daily_pnl_pct   NUMERIC(6,3),
    peak_equity     NUMERIC(14,2),
    current_drawdown_pct NUMERIC(6,3),
    max_drawdown_pct NUMERIC(6,3),
    total_trades    INTEGER DEFAULT 0,
    winning_trades  INTEGER DEFAULT 0,
    losing_trades   INTEGER DEFAULT 0,
    win_rate        NUMERIC(5,3),
    avg_winner      NUMERIC(12,2),
    avg_loser       NUMERIC(12,2),
    profit_factor   NUMERIC(8,3),
    
    -- Risk events
    kill_switch_triggered BOOLEAN DEFAULT FALSE,
    daily_limit_hit BOOLEAN DEFAULT FALSE,
    regime_override BOOLEAN DEFAULT FALSE,
    
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- System health log
CREATE TABLE IF NOT EXISTS system_health_log (
    id              BIGSERIAL PRIMARY KEY,
    timestamp_utc   TIMESTAMPTZ DEFAULT NOW(),
    component       VARCHAR(50) NOT NULL,           -- 'data', 'features', 'signals', 'risk', 'execution', 'discord'
    status          VARCHAR(20) NOT NULL CHECK (status IN ('healthy', 'degraded', 'error', 'offline')),
    latency_ms      INTEGER,
    message         TEXT,
    metadata        JSONB
);

CREATE INDEX idx_health_ts ON system_health_log(timestamp_utc DESC);
CREATE INDEX idx_health_component ON system_health_log(component, status);

-- Kill switch event log
CREATE TABLE IF NOT EXISTS kill_switch_events (
    id              SERIAL PRIMARY KEY,
    triggered_at    TIMESTAMPTZ DEFAULT NOW(),
    reason          TEXT NOT NULL,
    daily_pnl_at_trigger NUMERIC(12,2),
    drawdown_at_trigger  NUMERIC(6,3),
    consecutive_losses   INTEGER,
    resume_at       TIMESTAMPTZ,                    -- When bot can resume trading
    resolved        BOOLEAN DEFAULT FALSE,
    resolved_at     TIMESTAMPTZ
);
