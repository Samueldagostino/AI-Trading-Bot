# NQ Trading Bot

Institutional-grade automated trading system for **MNQ (Micro Nasdaq-100 Futures)** on Tradovate. Multi-timeframe structure analysis with a 2-contract scale-out execution strategy, governed by a backtested High-Conviction Filter.

## Performance (Backtest: Jan–Feb 2026)

| Metric | Value |
|--------|-------|
| Trades | 62 |
| Win Rate | 56.5% |
| Profit Factor | 2.35 |
| Total PnL | $3,417 |
| Expectancy/Trade | $55.12 |
| Worst Loss | -$135 |
| Max Drawdown | 1.03% |

## Architecture

```
HTF Bars (1D/4H/1H/30m/15m/5m) → HTF Bias Engine → Directional Gate
Exec Bars (2m) → Feature Engine → Signal Aggregator → HC Filter → Risk → Executor
```

**High-Conviction Filter (3 hard gates):**
1. Signal score ≥ 0.75
2. Stop distance ≤ 30 points
3. TP1 = stop × 1.5 R:R

## Quick Start

```bash
# 1. Clone and install
git clone <your-repo-url>
cd nq-trading-bot
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your Tradovate credentials

# 3. Run backtest
python scripts/run_backtest.py --tv

# 4. Launch dashboard
uvicorn dashboard.server:app --port 8080
```

## Project Structure

```
├── main.py                    # Orchestrator + HC filter
├── config/settings.py         # All configuration
├── features/
│   ├── engine.py              # Execution-TF features
│   └── htf_engine.py          # HTF bias consensus
├── signals/aggregator.py      # Signal scoring
├── risk/
│   ├── engine.py              # Position sizing + stops
│   └── regime_detector.py     # Market state classification
├── execution/
│   └── scale_out_executor.py  # 2-contract lifecycle
├── data_pipeline/pipeline.py  # Multi-TF data loading
├── dashboard/server.py        # Web dashboard
└── scripts/run_backtest.py    # Backtest runner
```

## Development with Claude Code

This repo includes a `CLAUDE.md` that provides full project context to Claude Code and Agent Teams. See `QUICKSTART.md` for setup instructions.

```bash
# Enable agent teams
export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1

# Launch
claude
```

## Branch Strategy

- `main` — production-ready, backtest-validated code only
- `develop` — active development, may have untested changes
- `feature/*` — individual features (e.g., `feature/chart-overlay-tab`)
- `backtest/*` — parameter experiments (e.g., `backtest/stop-cap-25`)
