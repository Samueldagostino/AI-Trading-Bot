# NQ Trading Bot — Project Context

## What This Is

An institutional-grade automated trading bot for **MNQ (Micro Nasdaq-100 Futures)** on Tradovate. It uses a multi-timeframe structure analysis pipeline with a 2-contract scale-out execution strategy, governed by a **High-Conviction Filter** derived from backtested forensic analysis.

This bot's job is **survival first, profit second**. Every design decision prioritizes capital preservation over signal frequency.

---

## Architecture Overview

```
HTF Bars (1D/4H/1H/30m/15m/5m)
        │
        ▼
  HTF Bias Engine ──► Directional Gate (long/short allowed?)
        │
Exec Bars (2m) ──► Feature Engine ──► Signal Aggregator
        │                                     │
        │                          ┌──────────┴──────────┐
        │                          │  HIGH-CONVICTION     │
        │                          │  FILTER (2 gates)    │
        │                          │  1. Score ≥ 0.75     │
        │                          │  2. Stop  ≤ 30 pts   │
        │                          └──────────┬──────────┘
        │                                     │
        ▼                                     ▼
  Risk Engine ──────────────────► Scale-Out Executor
        │                          C1: Time exit (10 bars)
        │                          C2: Trail (ATR-based)
        ▼
  Monitoring / Dashboard
```

### Data Flow (per execution bar)

1. HTF bars route to `HTFBiasEngine` → updates directional consensus
2. Execution-TF bar routes to `NQFeatureEngine` → computes OB, FVG, sweeps, VWAP, delta
3. `RegimeDetector` classifies market state
4. If position open → `ScaleOutExecutor.update()` manages stops/targets/trails
5. If flat → `SignalAggregator` produces signal → **HC Filter gates** → `RiskEngine` evaluates → `ScaleOutExecutor.enter_trade()`

---

## High-Conviction Filter (THE CORE RULES)

These two rules are **non-negotiable hard gates** in `main.py`. They exist because backtesting proved that only the intersection of tight stops + strong signals produces durable edge.

| Rule | Gate | Why |
|------|------|-----|
| **Min Signal Score** | `combined_score ≥ 0.75` | Eliminates low-conviction noise trades |
| **Max Stop Distance** | `stop_distance ≤ 30 pts` | Caps tail risk; worst loss ~$124 |

C1 exits via **time-based rule** (10 bars, if profitable), configured in `ScaleOutConfig.c1_time_exit_bars`. This replaced the old fixed TP1 = stop x 1.5 target based on C1 exit research showing 2x PnL and 59% less drawdown (see `docs/c1_exit_research.md`).

**Do not loosen these gates without new backtested evidence.**

### Constants Location

```python
# main.py — module-level constants (lines ~45-58)
HIGH_CONVICTION_MIN_SCORE = 0.75
HIGH_CONVICTION_MAX_STOP_PTS = 30.0

# config/settings.py — ScaleOutConfig
c1_time_exit_bars = 10  # Exit C1 after 10 bars if profitable
```

---

## Project Structure

```
nq-trading-bot/                    # Root — CLAUDE.md goes here
├── CLAUDE.md                      # THIS FILE — project brain
├── main.py                        # Orchestrator — HC filter lives here
├── config/
│   └── settings.py                # All dataclass configs (BotConfig, RiskConfig, etc.)
├── features/
│   ├── engine.py                  # NQFeatureEngine — OB, FVG, sweeps, VWAP, delta
│   └── htf_engine.py             # HTFBiasEngine — multi-TF directional consensus
├── signals/
│   └── aggregator.py             # SignalAggregator — confluence scoring
├── risk/
│   ├── engine.py                 # RiskEngine — position sizing, stop computation
│   └── regime_detector.py        # RegimeDetector — market state classification
├── execution/
│   └── scale_out_executor.py     # ScaleOutExecutor — 2-contract lifecycle
├── monitoring/
│   └── engine.py                 # MonitoringEngine — health, metrics
├── data_pipeline/
│   └── pipeline.py               # DataPipeline, MultiTimeframeIterator, bar converters
├── database/
│   └── connection.py             # DatabaseManager (PostgreSQL)
├── broker/
│   └── tradovate_client.py       # TradovateClient — paper/live execution
├── dashboard/
│   └── server.py                 # Dashboard web server
├── scripts/
│   ├── run_backtest.py           # Backtest runner (--tv for TradingView CSVs)
│   ├── aggregate_1m.py           # 1m → 2m/3m/5m/15m/30m/1H/4H/1D aggregator
│   └── run_oos_validation.py     # Monthly-segmented OOS validation runner
├── docs/
│   ├── validation_report.html    # Institutional-grade OOS report (dark-themed)
│   └── out_of_sample_validation.md  # Generated OOS results markdown
└── data/
    ├── tradingview/              # TradingView CSV exports (multi-TF)
    └── firstrate/                # FirstRate 1m data + aggregated TFs (gitignored)

# DEPRECATED — do NOT import or use:
# discord_ingestion/              # Removed. HTF Bias Engine replaced Discord signals.
```

> **NOTE**: If the folder structure differs from above, run `find . -name "*.py" -not -path "*/node_modules/*" -not -path "*/__pycache__/*" | head -60` to discover the actual layout.

---

## Key Files — What Each Does

### `main.py` — TradingOrchestrator

The brain. Processes every bar through the full pipeline. **All three HC filter gates are enforced here** (not in config, not in the executor). This is intentional — the gates are architectural decisions, not tunable parameters.

Key sections:
- Lines ~45-59: HC constants
- Lines ~246-296: HC gate logic (score → stop → TP1 override → entry)
- `process_bar()`: Core per-bar pipeline
- `run_backtest_mtf()`: Multi-timeframe backtest loop

### `execution/scale_out_executor.py` — ScaleOutExecutor

Manages the 2-contract lifecycle. C1 exits via time-based rule (10 bars if profitable). C2 trails as runner with ATR-based trailing stop.

Key methods:
- `enter_trade(...)`: Entry — no fixed C1 target, time-based exit managed by `_manage_phase_1()`
- `update(price, time)`: Per-bar position management
- `_manage_phase_1()`: Both contracts open, counting bars, watching for C1 time exit or stop
- `_manage_runner()`: C2 trailing logic
- `_compute_trailing_stop()`: ATR-based or fixed trail for C2

### `config/settings.py` — BotConfig

All configuration dataclasses. C1 exit is configured via `ScaleOutConfig.c1_time_exit_bars` (default: 10).

Key configs:
- `RiskConfig.account_size`: $50,000
- `RiskConfig.nq_point_value_micro`: $2.00/point
- `ScaleOutConfig.c1_time_exit_bars`: 10 (exit C1 after 10 bars if profitable)
- `ScaleOutConfig.c2_trailing_atr_multiplier`: 2.0
- `ScaleOutConfig.c2_time_stop_minutes`: 120

### `features/engine.py` — NQFeatureEngine

Computes execution-TF features: order blocks, fair value gaps, liquidity sweeps, VWAP bands, cumulative delta, ATR. Returns a feature snapshot consumed by SignalAggregator.

### `signals/aggregator.py` — SignalAggregator

Combines technical structure signals with optional ML confirmation. Produces `combined_score` (0-1) and `should_trade` boolean. HTF gating is a hard filter here (not weighted).

### `risk/engine.py` — RiskEngine

Evaluates trade risk. Computes `suggested_stop_distance` based on ATR and market conditions. The HC filter in main.py rejects this if > 30 points.

---

## 2-Contract Scale-Out Lifecycle

```
Phase 1 (PHASE_1):
  Both C1 and C2 open at same entry price
  Same initial stop on both
  Count bars since entry
  ↓
  After 10 bars, is C1 in profit?
    YES → Close C1 at market, move C2 stop to breakeven + 1pt
    NO  → Keep checking each bar until profitable, or stop hits
  Price hits stop? → Close both (full loss)
  ↓
Phase 2 (RUNNING):
  C2 trails with ATR-based trailing stop
  C2 exits via: trailing stop, time stop (120min), or max target (150pts)
```

### Dollar Math (MNQ at $2/point)

| Scenario | C1 | C2 | Total |
|----------|----|----|-------|
| Both stopped (20pt stop) | -$40 | -$40 | **-$80** |
| C1 time exit 10pts + C2 breakeven | +$20 | +$2 | **+$22** |
| C1 time exit 10pts + C2 runs 80pts | +$20 | +$160 | **+$180** |

---

## Instrument Details

| Property | Value |
|----------|-------|
| Symbol | MNQ (Micro Nasdaq-100) |
| Point Value | $2.00 per point per contract |
| Tick Size | 0.25 points ($0.50) |
| Contracts per Trade | 2 (C1 + C2) |
| Broker | Tradovate (paper → live) |
| Commission | $1.29 per contract |

---

## Running Backtests

```bash
# Multi-timeframe backtest with TradingView CSVs
python scripts/run_backtest.py --tv

# The backtest outputs backtest_viz_data.json containing:
#   summary: aggregate metrics
#   bars: execution-TF OHLCV
#   trades: entry/exit events with full context
```

### Validating HC Filter is Active

In backtest output, verify:
- All `signal_score` values ≥ 0.75
- All stop distances ≤ 30.3 pts (30 + slippage tolerance)
- C1 exit reasons are `time_10bars` (not fixed targets)
- Log contains `HC REJECT` debug messages for filtered trades

---

## Verification Commands

```bash
# Syntax check all Python files
find . -name "*.py" | xargs -I {} python3 -c "import ast; ast.parse(open('{}').read()); print('OK: {}')"

# Run the backtest
python scripts/run_backtest.py --tv

# Check for import errors
python -c "from main import TradingOrchestrator; print('Import OK')"
```

---

## Coding Conventions

- **Python 3.10+**, async/await throughout
- All prices rounded to 2 decimal places
- All PnL computed through `_compute_leg_pnl()` using `nq_point_value_micro`
- Logging: `logger.info()` for trade events, `logger.debug()` for HC rejections
- No magic numbers — all thresholds are named constants or config values
- Dataclasses for all structured data (trades, configs, features)
- Type hints on all function signatures

---

## Agent Team Roles (Suggested)

When using Claude Code Agent Teams, these roles map to the project:

### Lead Agent
- Receives the human's high-level request
- Breaks it into tasks with dependencies
- Assigns to specialists
- Reviews and synthesizes results

### Strategy Agent
- Owns: `main.py`, `config/settings.py`
- Responsible for: HC filter rules, backtest analysis, parameter tuning
- Verification: runs backtest, compares metrics to baseline

### Execution Agent
- Owns: `execution/scale_out_executor.py`, `risk/engine.py`, `risk/regime_detector.py`
- Responsible for: trade lifecycle, stop/target logic, trailing mechanics
- Verification: syntax check, import test

### Features/Signals Agent
- Owns: `features/engine.py`, `features/htf_engine.py`, `signals/aggregator.py`
- Responsible for: feature computation, signal scoring, HTF bias logic
- Verification: feature output validation

### Infrastructure Agent
- Owns: `dashboard/`, `monitoring/`, `database/`, `data_pipeline/`
- Responsible for: visualization, data flow, dashboard tabs, backtest tooling
- Verification: dashboard renders, data pipeline loads CSVs

---

## Current State & Known Issues

### System Status: VALIDATED FOR PAPER TRADING

Config D + C1 Time Exit passed 6-month out-of-sample validation on FirstRate 1-minute absolute-adjusted NQ data (Sep 2025 – Feb 2026). PF 1.59, 6/6 months profitable, max DD 1.7%. Approved for paper trading.

### Working
- HC filter (2 gates: score >= 0.75, stop <= 30pts) fully operational
- C1 time-based exit (10 bars, if profitable) — validated, 2x PnL vs old target
- HTF Bias Engine validated — Config D (gate=0.3) adopted as production config
- 2-contract scale-out lifecycle complete
- Multi-timeframe backtest pipeline functional (MTF iterator routes only execution_tf to process_bar)
- Paper trading mode via Tradovate
- 6-month OOS validation pipeline (`scripts/aggregate_1m.py` + `scripts/run_oos_validation.py`)
- Institutional-grade validation report (`docs/validation_report.html`)

### Planned / In Progress
- Live paper trading deployment
- Investigate toxic filter combos identified in MTF confluence analysis (see `docs/mtf_confluence_analysis.md`)

### Watch Items
- **Sep 2025 is a losing month** for all strategies. Time 10 bars limits Sep loss to -$698 (vs -$1,403 with old 1.5x target).
- **Oct 2025 was losing with old strategy** (-$387). Time 10 bars turns Oct profitable (+$1,591).
- **C1 is now net-positive** with time-based exit. No longer a drag on the system.
- `trending_up + htf=bearish`: 7 trades, 28.6% WR, -$236. Strong candidate for blocking.
- `session=afternoon + htf=neutral`: 3 trades, 0% WR, -$335. Block if sample grows.
- Slippage model can push stop distances to ~30.3pts (just past 30pt cap). This is acceptable — it's fill slippage, not a filter leak.

---

## Baseline Metrics (6-Month Out-of-Sample Validated)

**Config D + C1 Time Exit — HTF gate=0.3 | Data: Sep 2025 – Feb 2026 (FirstRate 1m absolute-adjusted) | 2m exec, all HTFs**

These are the numbers any change must be compared against:

```
C1 Exit:             Time-based (10 bars, if profitable)
HC Filter:           score >= 0.75, stop <= 30pts
HTF Gate:            strength >= 0.3

Total Trades:        948 (158/month avg)
Win Rate:            68.1%
Profit Factor:       1.59
Total PnL:           $14,543.64 ($2,424/month avg)
Expectancy/Trade:    $15.34
Max Drawdown:        1.7%
C1 PnL:              $3,842.58 (net positive)
C2 PnL:              $10,701.06
HTF Blocked:         12,337 signals
Profitable Months:   6 of 6 (100%)
```

### Monthly Performance Breakdown

| Month | Trades | WR | PF | Total PnL | Max DD | Exp/Trade |
|-------|--------|------|------|-----------|--------|-----------|
| **2025-09** | 174 | 62.6% | **1.10** | +$405 | 1.5% | +$2.33 |
| **2025-10** | 172 | 68.6% | **1.54** | +$2,521 | 1.3% | +$14.66 |
| **2025-11** | 112 | 75.9% | **2.25** | +$3,337 | 0.6% | +$29.79 |
| **2025-12** | 198 | 64.6% | **1.42** | +$2,084 | 1.7% | +$10.52 |
| **2026-01** | 161 | 70.2% | **1.88** | +$3,908 | 0.8% | +$24.27 |
| **2026-02** | 131 | 71.0% | **1.62** | +$2,289 | 1.3% | +$17.47 |

> **All 6 months profitable.** Sep was previously -$1,243 with old 1.5x target; now +$405 with
> time exit. The system no longer has hostile months. Worst month (Sep) still PF 1.10.

### Previous Baseline (C1 = 1.5x stop target)

For reference, the prior production baseline that the time exit replaces:

```
Total Trades:        748 (125/month avg)
Win Rate:            46.7%
Profit Factor:       1.15
Total PnL:           $5,778.30 ($963/month avg)
Expectancy/Trade:    $7.72
Max Drawdown:        3.6%
C1 PnL:              -$903.78 (net negative — all profit from C2)
C2 PnL:              $6,682.08
```

### C1 Exit Research Summary

The time-based C1 exit was adopted based on systematic research across 14 configurations
(see `docs/c1_exit_research.md` and `scripts/c1_deep_comparison.py`):

| Metric | Old (1.5x target) | New (10 bars) | Change |
|--------|--------------------|---------------|--------|
| Total PnL (replay) | $5,787 | $11,132 | **+92%** |
| Max Drawdown | 10.2% | 4.2% | **-59%** |
| PnL/MaxDD ratio | 2.28 | 10.64 | **+4.7x** |
| Win Rate | 46.6% | 70.4% | **+24pp** |
| C1 PnL | -$444 | +$2,750 | **C1 now profitable** |
| Worst Trade | -$811 | -$250 | **-69%** |
| Max Consec Losses | 8 | 5 | **-37%** |

**Any proposed change that degrades Profit Factor below 1.0 or increases Max Drawdown above 5.0% should be rejected unless supported by new backtested evidence across the full 6-month OOS window.**

### Validation Tooling

```bash
# Aggregate FirstRate 1m data into all timeframes
python scripts/aggregate_1m.py --input data/firstrate/NQ_1m_absolute.csv --output-dir data/firstrate/

# Run 6-month OOS validation
python scripts/run_oos_validation.py --data-dir data/firstrate/

# View HTML report
open docs/validation_report.html
```
