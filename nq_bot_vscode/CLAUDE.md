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
        │                          │  FILTER (3 gates)    │
        │                          │  1. Score ≥ 0.75     │
        │                          │  2. Stop  ≤ 30 pts   │
        │                          │  3. TP1 = Stop × 1.5 │
        │                          └──────────┬──────────┘
        │                                     │
        ▼                                     ▼
  Risk Engine ──────────────────► Scale-Out Executor
        │                          C1: Target (R:R-derived)
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

These three rules are **non-negotiable hard gates** in `main.py`. They exist because backtesting proved that only the intersection of tight stops + strong signals produces durable edge.

| Rule | Gate | Why |
|------|------|-----|
| **Min Signal Score** | `combined_score ≥ 0.75` | Eliminates low-conviction noise trades |
| **Max Stop Distance** | `stop_distance ≤ 30 pts` | Caps tail risk; worst loss ~$124 |
| **TP1 R:R Ratio** | `C1 target = stop × 1.5` | Ensures reward scales with entry precision |

### Backtest Evidence (167 → 62 trades)

| Metric | Before Filter | After Filter |
|--------|--------------|--------------|
| Trades | 167 | 62 |
| Win Rate | 61.1% | 56.5% |
| Profit Factor | 1.43 | 2.35 |
| Avg Winner | $140 | $170 |
| Avg Loser | -$154 | -$94 |
| Worst Loss | -$512 | -$135 |
| Max Drawdown | 3.91% | 1.03% |
| Expectancy/trade | $25.82 | $55.12 |

**Do not loosen these gates without new backtested evidence.**

### Constants Location

```python
# main.py — module-level constants (lines ~45-59)
HIGH_CONVICTION_MIN_SCORE = 0.75
HIGH_CONVICTION_MAX_STOP_PTS = 30.0
HIGH_CONVICTION_TP1_RR_RATIO = 1.5
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
│   └── run_backtest.py           # Backtest runner (--tv for TradingView CSVs)
└── data/
    └── tradingview/              # TradingView CSV exports (multi-TF)

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

Manages the 2-contract lifecycle. `enter_trade()` accepts an optional `c1_target_override` parameter. When present (always, under HC filter), it overrides the config-based ATR target with the R:R-derived value.

Key methods:
- `enter_trade(... c1_target_override=None)`: Entry with optional TP1 override
- `update(price, time)`: Per-bar position management
- `_manage_phase_1()`: Both contracts open, watching for C1 target or stop
- `_manage_runner()`: C2 trailing logic
- `_compute_trailing_stop()`: ATR-based or fixed trail for C2

### `config/settings.py` — BotConfig

All configuration dataclasses. The `ScaleOutConfig.c1_target_*` values are now **fallback defaults** — the HC filter in main.py overrides them. Do not remove them; they serve as the safety net if HC filter is bypassed.

Key configs:
- `RiskConfig.account_size`: $50,000
- `RiskConfig.nq_point_value_micro`: $2.00/point
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
  ↓
  Price hits C1 target (stop × 1.5)?
    YES → Close C1 at target, move C2 stop to breakeven + 1pt
    NO  → Price hits stop? → Close both (full loss)
  ↓
Phase 2 (RUNNING):
  C2 trails with ATR-based trailing stop
  C2 exits via: trailing stop, time stop (120min), or max target (150pts)
```

### Dollar Math (MNQ at $2/point)

| Scenario | C1 | C2 | Total |
|----------|----|----|-------|
| Both stopped (20pt stop) | -$40 | -$40 | **-$80** |
| C1 target + C2 breakeven | +$60 | +$2 | **+$62** |
| C1 target + C2 runs 80pts | +$60 | +$160 | **+$220** |

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
- All TP1:Stop ratios between 1.4 and 1.6
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

### Working
- HC filter (3 gates) fully operational
- 2-contract scale-out lifecycle complete
- Multi-timeframe backtest pipeline functional
- Paper trading mode via Tradovate

### Planned / In Progress
- TradingView-style chart tab in dashboard (trade overlay visualization)
- Potential regime gate for `trending_down` (33% WR, net negative in backtest — needs more data)
- Live trading validation

### Watch Items
- `trending_down` regime: 12 trades, 33% WR, -$283. Consider gating but need larger sample.
- Slippage model can push stop distances to ~30.3pts (just past 30pt cap). This is acceptable — it's fill slippage, not a filter leak.
- C2 runner generates 71% of total PnL ($2,423 of $3,418). System is highly dependent on C2 trailing mechanics.

---

## Baseline Metrics (Current System)

These are the numbers any change must be compared against:

```
Total Trades:     62
Win Rate:         56.5%
Profit Factor:    2.35
Total PnL:        $3,417.52
Expectancy/Trade: $55.12
Avg Winner:       $170.03
Avg Loser:        -$93.84
Largest Win:      $413.26
Largest Loss:     -$134.58
Max Drawdown:     1.03%
Max Loss Streak:  5 trades ($414.90)
C1 PnL:           $994.52
C2 PnL:           $2,423.00
```

**Any proposed change that degrades Profit Factor below 2.0 or increases Max Drawdown above 2.0% should be rejected unless supported by compelling new evidence.**
