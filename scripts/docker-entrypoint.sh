#!/bin/bash
# Docker entrypoint script for NQ Trading Bot
# Handles service startup, DB migrations, and multi-mode support
#
# Modes:
#   paper   - Paper trading (default)
#   dashboard - FastAPI dashboard on port 8080
#   backtest  - Run backtest against historical data
#   run - Generic run (same as paper)
#
# Usage:
#   /app/entrypoint.sh run         # Start paper trading
#   /app/entrypoint.sh dashboard   # Start dashboard
#   /app/entrypoint.sh backtest    # Run backtest

set -e

# ================================================================
# Configuration
# ================================================================
APP_HOME="/app"
DB_HOST="${PG_HOST:-db}"
DB_PORT="${PG_PORT:-5432}"
DB_NAME="${PG_DATABASE:-nq_trading}"
DB_USER="${PG_USER:-nq_bot}"
DB_PASSWORD="${PG_PASSWORD:-nq_bot_password}"
BOT_MODE="${BOT_MODE:-paper}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"
MAX_RETRIES=30
RETRY_DELAY=2

# Color output for readability
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ================================================================
# Helper Functions
# ================================================================

log_info() {
    echo -e "${BLUE}[INFO]${NC} $*"
}

log_success() {
    echo -e "${GREEN}[OK]${NC} $*"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*"
}

# ================================================================
# Database Health Check
# ================================================================

wait_for_postgres() {
    log_info "Waiting for PostgreSQL at ${DB_HOST}:${DB_PORT}..."

    retry_count=0
    while [ $retry_count -lt $MAX_RETRIES ]; do
        if pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" 2>/dev/null; then
            log_success "PostgreSQL is ready!"
            return 0
        fi

        retry_count=$((retry_count + 1))
        log_warn "Attempt $retry_count/$MAX_RETRIES: PostgreSQL not ready, retrying in ${RETRY_DELAY}s..."
        sleep $RETRY_DELAY
    done

    log_error "Failed to connect to PostgreSQL after $MAX_RETRIES attempts"
    return 1
}

# ================================================================
# Database Initialization
# ================================================================

init_database() {
    log_info "Initializing database schema..."

    # Check if migrations directory exists
    if [ ! -d "$APP_HOME/database/migrations" ]; then
        log_warn "No migrations directory found at $APP_HOME/database/migrations"
        log_info "Creating basic schema..."

        # Create basic tables if migrations don't exist
        PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" <<EOF
            -- Enable TimescaleDB extension
            CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

            -- Create bars table for storing OHLCV data
            CREATE TABLE IF NOT EXISTS bars (
                id BIGSERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL,
                timeframe TEXT NOT NULL,
                open FLOAT8 NOT NULL,
                high FLOAT8 NOT NULL,
                low FLOAT8 NOT NULL,
                close FLOAT8 NOT NULL,
                volume BIGINT NOT NULL,
                bid_volume BIGINT,
                ask_volume BIGINT,
                delta BIGINT
            );

            -- Convert to hypertable if not already
            SELECT create_hypertable('bars', 'timestamp', if_not_exists => TRUE);
            CREATE INDEX IF NOT EXISTS idx_bars_timestamp_timeframe
                ON bars (timestamp, timeframe);

            -- Create trades table
            CREATE TABLE IF NOT EXISTS trades (
                id BIGSERIAL PRIMARY KEY,
                entry_timestamp TIMESTAMPTZ NOT NULL,
                exit_timestamp TIMESTAMPTZ,
                direction TEXT NOT NULL,
                entry_price FLOAT8 NOT NULL,
                exit_price FLOAT8,
                contracts INT NOT NULL DEFAULT 2,
                pnl FLOAT8,
                status TEXT DEFAULT 'open'
            );

            CREATE INDEX IF NOT EXISTS idx_trades_timestamp
                ON trades (entry_timestamp DESC);

            -- Create signals table
            CREATE TABLE IF NOT EXISTS signals (
                id BIGSERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL,
                signal_type TEXT NOT NULL,
                direction TEXT NOT NULL,
                score FLOAT8 NOT NULL,
                confluence_score FLOAT8,
                source TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_signals_timestamp
                ON signals (timestamp DESC);

            -- Create economic events table
            CREATE TABLE IF NOT EXISTS economic_events (
                id BIGSERIAL PRIMARY KEY,
                event_name TEXT NOT NULL,
                event_time_utc TIMESTAMPTZ NOT NULL,
                impact_level TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_events_timestamp
                ON economic_events (event_time_utc);

            GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "$DB_USER";
            GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "$DB_USER";
EOF

        if [ $? -eq 0 ]; then
            log_success "Database schema created successfully"
        else
            log_error "Failed to create database schema"
            return 1
        fi
    else
        log_info "Running migrations from $APP_HOME/database/migrations..."
        # Migrations are auto-loaded by postgres on startup from /docker-entrypoint-initdb.d
        log_success "Migrations applied (if any)"
    fi

    return 0
}

# ================================================================
# Service Startup Functions
# ================================================================

start_paper_trading() {
    log_info "Starting NQ Trading Bot - PAPER MODE"
    log_info "Configuration:"
    log_info "  Broker: Tradovate (${TRADOVATE_ENVIRONMENT:-demo})"
    log_info "  Symbol: ${TRADOVATE_SYMBOL:-MNQM5}"
    log_info "  Account: Paper Trading"
    log_info "  Database: ${DB_NAME} @ ${DB_HOST}:${DB_PORT}"

    cd "$APP_HOME"
    python -m scripts.run_paper
}

start_dashboard() {
    log_info "Starting NQ Trading Bot - DASHBOARD"
    log_info "Dashboard will be available at http://localhost:8080"
    log_info "Database: ${DB_NAME} @ ${DB_HOST}:${DB_PORT}"

    cd "$APP_HOME"
    uvicorn dashboard.server:app --host 0.0.0.0 --port 8080 --log-level $LOG_LEVEL
}

start_backtest() {
    log_info "Starting NQ Trading Bot - BACKTEST"
    log_info "Database: ${DB_NAME} @ ${DB_HOST}:${DB_PORT}"

    cd "$APP_HOME"
    python run_backtest --tv --exec-tf 2m
}

start_generic() {
    log_info "Starting NQ Trading Bot - GENERIC (DEFAULT TO PAPER)"
    start_paper_trading
}

# ================================================================
# Main Entrypoint
# ================================================================

main() {
    log_info "=========================================="
    log_info "NQ Trading Bot - Docker Entrypoint"
    log_info "=========================================="
    log_info "Bot Mode: $BOT_MODE"
    log_info "Environment: Python ${PYTHONUNBUFFERED:+unbuffered}"
    log_info ""

    # Step 1: Wait for PostgreSQL
    wait_for_postgres || exit 1

    # Step 2: Initialize database (create schema if needed)
    init_database || exit 1

    # Step 3: Start appropriate service based on BOT_MODE
    log_info ""
    log_success "All prerequisites ready, starting service..."
    log_info ""

    case "$BOT_MODE" in
        paper|run)
            start_paper_trading
            ;;
        dashboard)
            start_dashboard
            ;;
        backtest)
            start_backtest
            ;;
        *)
            log_error "Unknown BOT_MODE: $BOT_MODE"
            log_info "Supported modes: paper, dashboard, backtest, run"
            exit 1
            ;;
    esac
}

# ================================================================
# Signal Handling (Graceful Shutdown)
# ================================================================

trap_handler() {
    log_warn "Received signal, initiating graceful shutdown..."
    kill -TERM "$!" 2>/dev/null
    exit 0
}

trap trap_handler SIGTERM SIGINT

# ================================================================
# Run Main
# ================================================================

# If arguments provided, use them as command override
if [ $# -gt 0 ]; then
    BOT_MODE="${1:-paper}"
fi

main
