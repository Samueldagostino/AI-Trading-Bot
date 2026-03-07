# NQ Trading Bot — Project Context

## Git Workflow (MANDATORY)

After making ANY file change in this project, immediately run:

```bash
git add <changed files>
git commit -m "<short description of change>"
git push origin main
```

This keeps the live GitHub Pages website at https://samueldagostino.github.io/AI-Trading-Bot/ in sync with every local edit. Never leave local changes unpushed.

---

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
        │                │                    │
        │                ▼                    │
        │        Sweep Detector ──────────────┤
        │     (PDH/PDL, VWAP, rounds)         │
        │     (additive — 3 entry modes)      │
        │                          ┌──────────┴──────────┐
        │                          │  HIGH-CONVICTION     │
        │                          │  FILTER (2 gates)    │
        │                          │  1. Score ≥ 0.75     │
        │                          │  2. Stop  ≤ 30 pts   │
        │                          └──────────┬──────────┘
        │                                     │
        ▼                                     ▼
  Risk Engine ──────────────────► Scale-Out Executor
        │                          C1: Trail from +3pts (2.5pt trail)
        │                          C2: Trail (ATR-based)
        ▼
  Monitoring / Dashboard
```

### Data Flow (per execution bar)

1. HTF bars route to `HTFBiasEngine` → updates directional consensus
2. Execution-TF bar routes to `NQFeatureEngine` → computes OB, FVG, sweeps, VWAP, delta
3. `RegimeDetector` classifies market state
3b. `LiquiditySweepDetector` runs on every bar (additive, never replaces existing signals)
4. If position open → `ScaleOutExecutor.update()` manages stops/targets/trails
5. If flat → `SignalAggregator` produces signal + sweep detector output → **HC Filter gates** → `RiskEngine` evaluates → `ScaleOutExecutor.enter_trade()`

---

## High-Conviction Filter (THE CORE RULES)

These two rules are **non-negotiable hard gates** in `main.py`. They exist because backtesting proved that only the intersection of tight stops + strong signals produces durable edge.

| Rule | Gate | Why |
|------|------|-----|
| **Min Signal Score** | `combined_score ≥ 0.75` | Eliminates low-conviction noise trades |
| **Max Stop Distance** | `stop_distance ≤ 30 pts` | Caps tail risk; worst loss ~$124 |

C1 exits via **trail-from-profit** (Variant C): once unrealized profit >= 3.0pts, a 2.5pt trailing stop activates from the high-water mark. Fallback: market exit at bar 12 if trailing never activates. Configured in `ScaleOutConfig.c1_profit_threshold_pts`, `c1_trail_distance_pts`, `c1_max_bars_fallback`. This replaced the old Time 10 bars exit (and before that, fixed TP1 = stop x 1.5) — validated Feb 2026 with calibrated slippage (PF 1.61, 6/6 months profitable).

**Do not loosen these gates without new backtested evidence.**

### Constants Location

```python
# main.py — module-level constants (lines ~60-70)
HIGH_CONVICTION_MIN_SCORE = 0.75
HIGH_CONVICTION_MAX_STOP_PTS = 30.0
SWEEP_MIN_SCORE = 0.70           # Sweep must score >= 0.70 to be eligible
SWEEP_CONFLUENCE_BONUS = 0.05    # Boost when signal + sweep fire together

# config/settings.py — ScaleOutConfig
c1_profit_threshold_pts = 3.0   # Activate trailing once profit >= this
c1_trail_distance_pts = 2.5     # Trail distance from HWM
c1_max_bars_fallback = 12       # Fallback market exit if trail never activates
```

---

## Liquidity Sweep Detector (Additive Signal Module)

`signals/liquidity_sweep.py` — `LiquiditySweepDetector`

Detects institutional stop hunts at key structural levels. Runs on every execution bar alongside the existing signal pipeline. **Never replaces** existing signals — only adds new entry opportunities or confirms existing ones.

### Key Levels Tracked
- **PDH/PDL** — Prior day high/low
- **Session H/L** — Current session high/low
- **PWH/PWL** — Prior week high/low
- **VWAP** — Session VWAP
- **Round numbers** — Every 50 points (e.g. 21,000, 21,050)

### Sweep Detection Logic
1. Price **breaches** a key level by >= 2pts (wick through)
2. Price **closes back inside** on the same bar → creates a `SweepCandidate`
3. **Reclaim confirmation** within 1-3 bars (close back on the other side)
4. **Volume confirmation**: bar volume >= 1.5x 20-bar average

### Sweep Score (0.0 — 1.0)
- Base: 0.50
- Volume bonus: up to +0.15 (proportional to vol spike)
- Depth bonus: up to +0.10 (deeper breach = stronger sweep)
- Confluence bonus: +0.05 (multiple key levels swept)
- HTF alignment bonus: +0.10 (sweep direction matches HTF bias)
- RTH open bonus: +0.10 (first 30min of RTH)

### Three Entry Modes (in `main.py` process_bar)

| Mode | Condition | Behavior |
|------|-----------|----------|
| **Signal-only** | Existing signal fires, no sweep | Standard pipeline (unchanged) |
| **Sweep-only** | Sweep score >= 0.70, no existing signal | Sweep acts as standalone signal source (must pass HC >= 0.75) |
| **Confluence** | Both fire, same direction | HC score boosted by +0.05 (existing_score + 0.05) |

If sweep and existing signal fire in **conflicting directions**, the existing signal takes priority.

### Key Methods
- `update_bar(bar, vwap, htf_bias, is_rth)` → `Optional[SweepSignal]`
- `_update_session_tracking()` — manages PDH/PDL, session H/L, weekly H/L across day/week boundaries
- `_rebuild_key_levels()` — refreshes the active key levels list
- `_detect_new_sweeps()` — checks all key levels for breach+close-back on current bar
- `_check_reclaims()` — monitors pending candidates for 1-3 bar reclaim confirmation
- `_score_and_emit()` — computes multi-factor 0.0-1.0 score
- `get_stats()` — returns sweep detection statistics

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
│   ├── aggregator.py             # SignalAggregator — confluence scoring
│   └── liquidity_sweep.py        # LiquiditySweepDetector — additive key-level sweep signals
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
│   ├── run_oos_validation.py     # Monthly-segmented OOS validation runner
│   └── replay_simulator.py       # Replay simulator with --sweep-compare A/B testing
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

Manages the 2-contract lifecycle. C1 exits via trail-from-profit (Variant C). C2 trails as runner with ATR-based trailing stop.

Key methods:
- `enter_trade(...)`: Entry — no fixed C1 target, trail-from-profit managed by `_manage_phase_1()`
- `update(price, time)`: Per-bar position management
- `_manage_phase_1()`: Variant C — tracks C1 HWM, activates trailing once profit >= 3pts
- `_close_c1_to_runner()`: Closes C1 and transitions C2 to runner phase with BE stop
- `_manage_phase_1_time10()`: ARCHIVED — old Time 10 bars exit (for A/B testing)
- `_manage_runner()`: C2 trailing logic
- `_compute_trailing_stop()`: ATR-based or fixed trail for C2

### `config/settings.py` — BotConfig

All configuration dataclasses. C1 exit is configured via `ScaleOutConfig` trail-from-profit params.

Key configs:
- `RiskConfig.account_size`: $50,000
- `RiskConfig.nq_point_value_micro`: $2.00/point
- `ScaleOutConfig.c1_profit_threshold_pts`: 3.0 (activate C1 trailing once profit >= 3pts)
- `ScaleOutConfig.c1_trail_distance_pts`: 2.5 (trail distance from HWM)
- `ScaleOutConfig.c1_max_bars_fallback`: 12 (fallback exit if trail never activates)
- `ScaleOutConfig.c2_trailing_atr_multiplier`: 2.0
- `ScaleOutConfig.c2_time_stop_minutes`: 120

### `features/engine.py` — NQFeatureEngine

Computes execution-TF features: order blocks, fair value gaps, liquidity sweeps, VWAP bands, cumulative delta, ATR. Returns a feature snapshot consumed by SignalAggregator.

### `signals/aggregator.py` — SignalAggregator

Combines technical structure signals with optional ML confirmation. Produces `combined_score` (0-1) and `should_trade` boolean. HTF gating is a hard filter here (not weighted).

### `signals/liquidity_sweep.py` — LiquiditySweepDetector

Additive signal module that detects institutional sweeps at key structural levels (PDH/PDL, session H/L, PWH/PWL, VWAP, round numbers). Runs alongside existing signals — never replaces them. Three entry modes: signal-only (unchanged), sweep-only (score >= 0.75), confluence (both fire same direction → +0.05 HC boost). See "Liquidity Sweep Detector" section above for full details.

### `risk/engine.py` — RiskEngine

Evaluates trade risk. Computes `suggested_stop_distance` based on ATR and market conditions. The HC filter in main.py rejects this if > 30 points.

---

## 2-Contract Scale-Out Lifecycle

```
Phase 1 (PHASE_1) — Variant C:
  Both C1 and C2 open at same entry price
  Same initial stop on both
  Track C1 high-water mark (HWM)
  ↓
  Profit >= 3.0pts? → Activate C1 trailing stop (HWM - 2.5pts)
  Trail stop hit? → Close C1, move C2 stop to breakeven + 1pt
  Never reaches 3pts within 12 bars? → Exit C1 at market if profitable
  Price hits initial stop? → Close both (full loss)
  ↓
Phase 2 (RUNNING):
  C2 trails with ATR-based trailing stop
  C2 exits via: trailing stop, time stop (120min), or max target (150pts)
```

### Dollar Math (MNQ at $2/point)

| Scenario | C1 | C2 | Total |
|----------|----|----|-------|
| Both stopped (20pt stop) | -$40 | -$40 | **-$80** |
| C1 trails 5pts + C2 breakeven | +$10 | +$2 | **+$12** |
| C1 trails 10pts + C2 runs 80pts | +$20 | +$160 | **+$180** |

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
- C1 exit reasons are `c1_trail_from_profit` or `time_12bars_fallback`
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

### System Status: LIVE-READY

Config D + Variant C (Trail from Profit) + Sweep Detector + Calibrated Slippage complete. 6-month OOS validated on FirstRate 1-minute absolute-adjusted NQ data (Sep 2025 – Feb 2026) with realistic slippage model (avg 0.96pt/fill). PF 1.73 with slippage — system survives real-world friction.

**Current Config:**
- HC filter: score >= 0.75, stop <= 30pts
- HTF gate: strength >= 0.3 (Config D)
- C1 exit: Trail from +3pts (2.5pt trail, 12-bar fallback) — Variant C
- C2: ATR-based trailing runner
- Sweep detector: additive (PDH/PDL, session H/L, PWH/PWL, VWAP, round numbers)
- Slippage: Calibrated (RTH 0.50pt, ETH 1.00pt, caps 1.50/2.50/3.00pt)

### Working
- HC filter (2 gates: score >= 0.75, stop <= 30pts) fully operational
- C1 trail-from-profit (Variant C) — PF 1.73 with sweep detector + realistic slippage, 6/6 months profitable
- Liquidity sweep detector — additive module, 338 sweep-only trades (WR 61.8%), 161 confluence trades (WR 67.7%)
- HTF Bias Engine validated — Config D (gate=0.3) adopted as production config
- 2-contract scale-out lifecycle complete
- Multi-timeframe backtest pipeline functional (MTF iterator routes only execution_tf to process_bar)
- Paper trading mode via Tradovate
- 6-month OOS validation pipeline (`scripts/aggregate_1m.py` + `scripts/run_oos_validation.py`)
- Institutional-grade validation report (`docs/validation_report.html`)
- Regime cross-analysis complete — no additional gates recommended (`scripts/regime_analysis.py`)

### Next Milestone
- Paper trading on Tradovate demo

### Key Edges (from Regime Analysis)
- **Morning session** (09:30–11:30 ET): PF 3.62, $50/trade, 78% WR — strongest edge
- **Shorts** outperform longs: +$8,790 vs +$5,904
- **HTF bearish** is best HTF direction: +$6,357, PF 1.91
- **Ranging regime**: PF 2.07, $23/trade — best regime
- **High volatility**: PF 2.26, 83% WR — small sample (23 trades) but strong

### Watch Items
- **Oct 2025** is weakest month (PF 1.36, +$2,798 with sweep detector + calibrated slippage) but still profitable
- **C1 is net-positive** with trail-from-profit (+$10,008). Major upgrade from Time 10 (+$776 with slippage).
- Sweep detector adds +$9,687 PnL with zero drawdown increase (338 sweep-only + 161 confluence trades)
- Sweep-only trades maintain 61.8% WR — consistent with existing signal pipeline
- Confluence trades have highest WR at 67.7% — sweep + signal agreement is the strongest setup
- Calibrated slippage costs are realistic — system survives real-world friction
- Slippage model can push stop distances to ~30.3pts (just past 30pt cap). Acceptable — fill slippage, not filter leak.

---

## Baseline Metrics (6-Month OOS + Sweep Detector + Calibrated Slippage)

**Config D + Variant C + Sweep Detector + Calibrated Slippage — HTF gate=0.3 | Data: Sep 2025 – Feb 2026 (FirstRate 1m absolute-adjusted) | 2m exec, all HTFs**

These are the numbers any change must be compared against. They include the liquidity sweep detector (additive module) and realistic calibrated slippage (avg 0.96pt/fill):

```
C1 Exit:             Trail from profit (>=3pts → 2.5pt trail, 12-bar fallback)
HC Filter:           score >= 0.75, stop <= 30pts
HTF Gate:            strength >= 0.3
Sweep Detector:      ENABLED (additive — PDH/PDL, session H/L, PWH/PWL, VWAP, round numbers)
Slippage:            Calibrated v2 (RTH 0.50pt, ETH 1.00pt, news +1pt)

Total Trades:        1,524 (254/month avg)
Win Rate:            61.9%
Profit Factor:       1.73
Total PnL:           $25,581.00 ($4,264/month avg)
Expectancy/Trade:    $16.79
Max Drawdown:        1.4%
C1 PnL:              $10,008.00
C2 PnL:              $15,573.00
Avg Slippage:        0.96pt/fill
HTF Blocked:         12,405 signals
Profitable Months:   6 of 6 (100%)

Sweep-only trades:   338 (WR 61.8%, PnL +$9,896)
Confluence trades:   161 (WR 67.7%, PnL +$3,059)
Signal-only trades:  1,025 (PnL +$12,626)
```

### Monthly Performance Breakdown (with sweep detector + calibrated slippage)

| Month | Trades | WR | PF | Total PnL | Sweep Trades | Sweep PnL |
|-------|--------|------|------|-----------|-------------|-----------|
| **2025-09** | 240 | 56.7% | **1.79** | +$3,608 | 63 | +$2,920 |
| **2025-10** | 292 | 57.5% | **1.36** | +$2,798 | 81 | +$332 |
| **2025-11** | 172 | 70.9% | **2.78** | +$6,496 | 49 | +$2,520 |
| **2025-12** | 305 | 61.0% | **1.76** | +$4,385 | 59 | +$1,311 |
| **2026-01** | 288 | 60.8% | **1.67** | +$4,702 | 48 | +$2,405 |
| **2026-02** | 227 | 68.7% | **1.58** | +$3,592 | 38 | +$408 |

> **All 6 months profitable with sweep detector + realistic slippage.** Weakest month (Oct) PF 1.36 — comfortably above 1.0.

### Sweep Detector Impact (A/B comparison)

| Config | Trades | WR | PF | PnL | Max DD |
|--------|--------|------|------|---------|--------|
| **With sweep detector** | **1,524** | **61.9%** | **1.73** | **+$25,581** | **1.4%** |
| Without sweep detector | 1,161 | 62.0% | 1.61 | +$15,894 | 1.4% |
| **Delta** | **+363** | **-0.1%** | **+0.12** | **+$9,687** | **+0.0%** |

> Sweep detector adds +$9,687 PnL with zero drawdown increase. 499 total sweep-related trades (338 sweep-only + 161 confluence).

### Previous Baselines (archived)

| Config | Trades | WR | PF | PnL | C1 PnL | Max DD | Slippage |
|--------|--------|------|------|---------|--------|--------|----------|
| **Variant C + sweep + calibrated** | **1,524** | **61.9%** | **1.73** | **+$25,581** | **+$10,008** | **1.4%** | **0.96pt/fill** |
| Variant C + calibrated (no sweep) | 1,161 | 62.0% | 1.61 | +$15,894 | +$6,382 | 1.4% | 0.96pt/fill |
| Time 10 + calibrated | 1,000 | 54.8% | 1.29 | +$9,140 | +$776 | 2.4% | 0.96pt/fill |
| Time 10 (no slippage) | 948 | 68.1% | 1.59 | +$14,544 | +$3,843 | 1.7% | none |
| 1.5x target (original) | 748 | 46.7% | 1.15 | +$5,778 | -$904 | 3.6% | none |

### C1 Exit Variant Comparison (with calibrated slippage, no sweep detector)

| Variant | PF | PnL | C1 PnL | Max DD | Status |
|---------|------|---------|--------|--------|--------|
| **C: Trail from profit** | **1.61** | **+$15,894** | **+$6,382** | **1.4%** | **LIVE-READY** |
| A: Min profit gate | 1.34 | +$10,857 | +$1,248 | 2.5% | LIVE-READY |
| Baseline: Time 10 | 1.29 | +$9,140 | +$776 | 2.4% | MARGINAL |
| D: RTH-only Time 10 | 1.24 | +$2,752 | -$442 | 2.4% | MARGINAL |
| B: Fixed TP 6pts | 1.11 | +$3,110 | -$5,786 | 3.8% | NOT READY |

**Any proposed change that degrades Profit Factor below 1.3 or increases Max Drawdown above 3.0% should be rejected unless supported by new backtested evidence across the full 6-month OOS window with calibrated slippage.**

### Validation Tooling

```bash
# Aggregate FirstRate 1m data into all timeframes
python scripts/aggregate_1m.py --input data/firstrate/NQ_1m_absolute.csv --output-dir data/firstrate/

# Run 6-month OOS validation
python scripts/run_oos_validation.py --data-dir data/firstrate/

# Run replay simulator validation (with sweep detector)
python scripts/replay_simulator.py --validate

# A/B test: baseline (no sweeps) vs test (with sweeps)
python scripts/replay_simulator.py --sweep-compare

# Run replay without sweep detector
python scripts/replay_simulator.py --validate --no-sweep

# View HTML report
open docs/validation_report.html
```
