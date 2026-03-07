# Multi-stage Dockerfile for NQ Trading Bot
# Supports: paper trading, backtesting, dashboard via BOT_MODE env var
# Build: docker build -t nq-trading-bot .
# Run: docker run --env-file .env nq-trading-bot

# ================================================================
# Stage 1: Builder
# ================================================================
FROM python:3.11-slim as builder

WORKDIR /build

# Install system dependencies for building
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY nq_bot_vscode/requirements /build/requirements.txt
RUN pip install --no-cache-dir --user -r /build/requirements.txt

# ================================================================
# Stage 2: Runtime
# ================================================================
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Create non-root user for security
RUN groupadd -r botuser && useradd -r -g botuser botuser

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy Python dependencies from builder
COPY --from=builder /root/.local /home/botuser/.local

# Copy application code
COPY nq_bot_vscode/ /app/

# Set environment variables
ENV PATH=/home/botuser/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Switch to non-root user
RUN chown -R botuser:botuser /app
USER botuser

# Copy entrypoint script
COPY scripts/docker-entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Default to paper mode
ENV BOT_MODE=paper \
    PG_HOST=db \
    PG_PORT=5432 \
    PG_DATABASE=nq_trading \
    PG_USER=nq_bot \
    DASHBOARD_API_TOKEN=

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8080/api/health || exit 1

# Default command - can be overridden
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["run"]
