# Docker Setup Guide - NQ Trading Bot

This guide provides comprehensive instructions for containerizing and running the NQ Trading Bot in Docker.

## Overview

The Docker setup includes:
- **Multi-stage Dockerfile** with Python 3.11-slim, non-root user, health checks
- **docker-compose.yml** with full production stack
- **docker-compose.override.yml** for development hot-reload
- **.dockerignore** to keep images lean
- **scripts/docker-entrypoint.sh** for graceful startup, DB migrations, multi-mode support

### Supported Modes

The bot can run in multiple modes via the `BOT_MODE` environment variable:

1. **paper** (default) - Paper trading with real-time Tradovate bars
2. **dashboard** - FastAPI web dashboard on port 8080
3. **backtest** - Backtesting against historical data
4. **run** - Alias for paper mode

## Quick Start

### 1. Prepare Environment File

Copy and configure your `.env` file:

```bash
cp nq_bot_vscode/.env.example .env
```

Edit `.env` with your Tradovate credentials:

```env
TRADOVATE_USERNAME=your_demo_username
TRADOVATE_PASSWORD=your_demo_password
TRADOVATE_APP_ID=your_app_name
TRADOVATE_CID=your_client_id
TRADOVATE_SECRET=your_api_secret
TRADOVATE_DEVICE_ID=nq-bot-docker

TRADOVATE_ENVIRONMENT=demo
DAILY_LOSS_LIMIT=500
MAX_POSITION_SIZE=2

# Database (optional, defaults provided)
PG_HOST=db
PG_PORT=5432
PG_DATABASE=nq_trading
PG_USER=nq_bot
PG_PASSWORD=your_secure_password

# Dashboard (optional)
DASHBOARD_API_TOKEN=your_secure_token_here
```

### 2. Build the Docker Image

```bash
docker build -t nq-trading-bot .
```

Or use docker-compose (automatic):

```bash
docker-compose build
```

### 3. Start Services

#### Option A: All services (paper + dashboard + database)

```bash
docker-compose up -d
```

This starts:
- PostgreSQL + TimescaleDB (port 5432)
- Trading Bot in paper mode
- Dashboard (port 8080)

#### Option B: Specific services using profiles

```bash
# Paper trading only
docker-compose --profile paper up -d

# Dashboard only (still needs db)
docker-compose --profile dashboard up -d

# Backtest only (one-off, runs and exits)
docker-compose --profile backtest run backtest
```

#### Option C: Development with hot-reload

```bash
# docker-compose.override.yml is automatically loaded
# Code changes appear in containers instantly
docker-compose up -d
```

### 4. Verify Services

```bash
# Check all containers running
docker-compose ps

# View logs
docker-compose logs -f bot
docker-compose logs -f dashboard
docker-compose logs -f db

# Test dashboard
curl http://localhost:8080/api/status

# Test database
docker-compose exec db psql -U nq_bot -d nq_trading -c "SELECT COUNT(*) FROM bars;"
```

### 5. Stop Services

```bash
docker-compose down

# Also remove volumes (careful - deletes data!)
docker-compose down -v
```

## Architecture Details

### Dockerfile (Multi-Stage Build)

**Stage 1 (Builder):**
- Base: `python:3.11-slim`
- Installs build tools (gcc, g++, make, git)
- Compiles Python dependencies in isolation

**Stage 2 (Runtime):**
- Base: `python:3.11-slim`
- Copies compiled dependencies from Stage 1
- Creates non-root user `botuser` for security
- Installs runtime dependencies only (postgresql-client, curl)
- Sets up entrypoint script
- Includes health check

**Key Features:**
- Minimal image size (avoids build tools in final image)
- Non-root execution for security
- Graceful signal handling (SIGTERM/SIGINT)
- Multi-mode support via `BOT_MODE` env var
- PostgreSQL connectivity

### docker-compose.yml (Production Stack)

#### Services

**db (TimescaleDB)**
- Image: `timescale/timescaledb:latest-pg15`
- Volumes: `db_data:/var/lib/postgresql/data`
- Health check: pg_isready every 10s
- Port: 5432 (exposed for admin tools)
- Resource limits: 2 CPU cores, 2GB RAM

**bot (Paper Trading)**
- Depends on: db (healthy)
- Mode: `BOT_MODE=paper`
- Volumes: logs, data, source code (dev)
- Network: nq_network
- Health check: queries dashboard health endpoint
- Restart policy: on-failure:3
- Resource limits: 2 CPU cores, 2GB RAM
- Profile: paper (optional)

**dashboard (FastAPI Web UI)**
- Depends on: db (healthy)
- Mode: `BOT_MODE=dashboard`
- Port: 8080 (web interface)
- Health check: curl /api/health
- Restart policy: unless-stopped
- Resource limits: 1 CPU core, 1GB RAM
- Profile: dashboard (optional)

**backtest (One-off Executor)**
- Depends on: db (healthy)
- Mode: `BOT_MODE=backtest`
- Volumes: data (RO), results (RW)
- Profile: backtest (optional, run manually)
- Entrypoint: custom backtest command

#### Volumes

| Volume | Purpose | Mounted At |
|--------|---------|-----------|
| `db_data` | PostgreSQL data persistence | `/var/lib/postgresql/data` |
| `bot_logs` | Application logs | `/app/logs` |
| `bot_data` | Trading data, cache | `/app/data` |
| `bot_results` | Backtest results | `/app/results` |

#### Networks

- **nq_network**: Internal bridge network
- Subnet: 172.20.0.0/16
- Allows services to communicate by name (e.g., `db`, `dashboard`)

### docker-compose.override.yml (Development)

Automatically loaded by `docker-compose` (takes precedence).

**Changes for Development:**
- Relaxed resource limits (more CPU, more RAM)
- DEBUG log level
- Source code bind mounts (`:cached` flag for Mac/Windows)
- Auto-restart on failure
- Console output unbuffered

**To skip this file:**
```bash
docker-compose -f docker-compose.yml up -d
```

### .dockerignore

Excludes from build context:
- Git files (`.git`, `.gitignore`)
- Python cache (`__pycache__`, `*.pyc`)
- Tests, docs, markdown files
- Environment files (`.env`, secrets)
- Large data files
- IDE/editor configuration

**Benefit:** Faster builds, smaller context transfers

### scripts/docker-entrypoint.sh

**Responsibilities:**

1. **Wait for PostgreSQL** (retry up to 30 times)
   - Uses `pg_isready` to check connectivity
   - Waits before proceeding

2. **Initialize Database**
   - Checks for migrations directory
   - Creates basic schema if needed
   - Tables: `bars`, `trades`, `signals`, `economic_events`
   - Enables TimescaleDB hypertables for compression

3. **Start Service**
   - paper/run: `python -m scripts.run_paper`
   - dashboard: `uvicorn dashboard.server:app`
   - backtest: `python run_backtest --tv --exec-tf 2m`

4. **Signal Handling**
   - Catches SIGTERM/SIGINT
   - Gracefully shuts down bot (flattens positions)
   - Cleans up resources

## Environment Variables

### Required (for Tradovate connection)

```env
TRADOVATE_USERNAME=
TRADOVATE_PASSWORD=
TRADOVATE_APP_ID=
TRADOVATE_CID=
TRADOVATE_SECRET=
TRADOVATE_DEVICE_ID=
TRADOVATE_ENVIRONMENT=demo
```

### Database

```env
PG_HOST=db                    # Default: db (service name)
PG_PORT=5432                  # Default: 5432
PG_DATABASE=nq_trading        # Default: nq_trading
PG_USER=nq_bot                # Default: nq_bot
PG_PASSWORD=nq_bot_password   # Default: nq_bot_password
```

### Bot Configuration

```env
BOT_MODE=paper                # Options: paper, dashboard, backtest, run
LOG_LEVEL=INFO                # Options: DEBUG, INFO, WARNING, ERROR
PYTHONUNBUFFERED=1            # Recommended: 1
```

### Optional (Dashboard & Discord)

```env
DASHBOARD_API_TOKEN=your_secure_token
DISCORD_TOKEN=
DISCORD_CHANNEL_IDS=
```

## Common Commands

### View Logs

```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f bot
docker-compose logs -f dashboard
docker-compose logs -f db

# Last 50 lines, no follow
docker-compose logs --tail=50 bot
```

### Execute Commands in Container

```bash
# Run command in bot container
docker-compose exec bot python -c "print('hello')"

# Connect to PostgreSQL
docker-compose exec db psql -U nq_bot -d nq_trading

# View bot files
docker-compose exec bot ls -la /app
```

### Rebuild After Code Changes

```bash
# Rebuild image only (source code in volumes auto-updates)
docker-compose build

# Rebuild and restart
docker-compose up -d --build
```

### Clean Up

```bash
# Stop all containers
docker-compose down

# Stop and remove volumes
docker-compose down -v

# Remove dangling images
docker image prune -f

# Full cleanup (careful!)
docker-compose down -v
docker system prune -a -f
```

## Production Deployment Checklist

### Security

- [ ] Change default PostgreSQL password in `.env`
- [ ] Set strong `DASHBOARD_API_TOKEN`
- [ ] Use secret management (AWS Secrets Manager, Kubernetes Secrets)
- [ ] Run with `--no-env-file` in production, use external secrets
- [ ] Enable PostgreSQL SSL (`sslmode=require`)
- [ ] Restrict Tradovate credentials to read-only API keys where possible
- [ ] Run non-root user (done by default)
- [ ] Use network policies to restrict inter-service traffic

### Performance

- [ ] Adjust resource limits based on actual usage
- [ ] Enable PostgreSQL connection pooling (PgBouncer)
- [ ] Use persistent volumes on fast storage (SSD)
- [ ] Enable PostgreSQL caching and indexes
- [ ] Monitor disk usage (logs, data files)

### Reliability

- [ ] Set restart policies appropriately
- [ ] Configure health checks
- [ ] Use orchestration platform (Docker Swarm, Kubernetes)
- [ ] Implement automated backups of PostgreSQL data
- [ ] Set up log aggregation (ELK, CloudWatch)
- [ ] Monitor service health and alerts

### Monitoring

- [ ] Expose Prometheus metrics (add `/metrics` endpoint)
- [ ] Log all trades and signals
- [ ] Track database query performance
- [ ] Monitor container resource usage
- [ ] Set up alerts for kill-switch events

## Troubleshooting

### PostgreSQL Won't Connect

```bash
# Check db health
docker-compose ps db
docker-compose logs db

# Test connection manually
docker-compose exec db psql -U nq_bot -d nq_trading -c "SELECT 1"

# Reset database (destructive!)
docker-compose down -v
docker-compose up db
```

### Bot Not Starting

```bash
# Check logs
docker-compose logs -f bot

# Check entrypoint script
docker-compose exec bot cat /app/entrypoint.sh | head -20

# Test manually
docker-compose exec bot python -m scripts.run_paper
```

### Dashboard Not Accessible

```bash
# Check if running
docker-compose ps dashboard

# Test from host
curl http://localhost:8080/api/status

# Check from inside container
docker-compose exec dashboard curl -v http://localhost:8080/api/health
```

### Backtest Won't Complete

```bash
# Check logs
docker-compose logs backtest

# Run manually with debug
docker-compose run --rm backtest bash
# Then inside: python run_backtest --tv --exec-tf 2m
```

### Out of Disk Space

```bash
# Check volume sizes
docker volume ls
docker system df

# Clean up old images
docker image prune -a -f

# Export data and reset volumes
docker-compose down -v
```

## Next Steps

1. **Configure credentials**: Edit `.env` with Tradovate API credentials
2. **Start services**: `docker-compose up -d`
3. **Verify connectivity**: `curl http://localhost:8080/api/status`
4. **Monitor logs**: `docker-compose logs -f`
5. **Review dashboard**: Open http://localhost:8080 in browser

## References

- Docker Compose: https://docs.docker.com/compose/
- Python 3.11: https://www.python.org/downloads/
- TimescaleDB: https://docs.timescale.com/
- PostgreSQL: https://www.postgresql.org/docs/
- FastAPI: https://fastapi.tiangolo.com/

## Support

For issues or questions:
1. Check logs: `docker-compose logs -f`
2. Test connectivity: `docker-compose exec db psql -U nq_bot -d nq_trading -c "SELECT 1"`
3. Review entrypoint script: `/scripts/docker-entrypoint.sh`
4. Enable DEBUG logging: Set `LOG_LEVEL=DEBUG` in `.env`
