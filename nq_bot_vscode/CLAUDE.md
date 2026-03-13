# NQ Trading Bot — Project Context (Version 1.3.3)

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

An institutional-grade automated trading bot for **MNQ (Micro Nasdaq-100 Futures)** on IBKR (Interactive Brokers). It uses a multi-timeframe structure analysis pipeline with a **5-contract scale-out execution strategy** (C1=1, C2=1, C3=3), governed by a **High-Conviction Filter** and the **Delayed C3 Runner** architecture.

**Version 1.3.3 — In Development**: V1.3.2 baseline (Path C+, HTF hysteresis, 5-contract scale-out) + GainzAlgo Suite (5 modules for entry perfection).

This bot's job is **survival first, profit second**. Every design decision prioritizes capital preservation over signal frequency.

### Version History

| Version | Key Changes |
|---------|-------------|
| **V1.3.1** | 2-contract scale-out (C1+C2), HC filter, sweep detector. PF 2.86, 396 trades. |
| **V1.3.2** | 5-contract scale-out (C1=1, C2=1, C3=3), Path C+ dual-trigger, HTF hysteresis (anti-flip-flop), HTF backtest_mode (staleness fix). |
| **V1.3.3** | GainzAlgo Suite integration — 5 modules (VolPercentile, SAMSM, CSTA, CSMRM, AdaptiveConfidence). Adaptive HC gate, cross-signal synergy boosts. Kill switch: `GAINZ_MODULES_ENABLED`. |

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
  Risk Engine ──────────────────► Scale-Out Executor (5 contracts)
        │                          C1: 5-bar time exit (canary)
        │                          C2: Structural target + delayed BE
        │                          C3: ATR trail runner (DELAYED ENTRY)
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

These two rules are **non-negotiable hard gates**. They exist because backtesting proved that only the intersection of tight stops + strong signals produces durable edge.

| Rule | Gate | Why |
|------|------|-----|
| **Min Signal Score** | `combined_score ≥ 0.75` | Eliminates low-conviction noise trades |
| **Max Stop Distance** | `stop_distance ≤ 30 pts` | Caps tail risk per trade |

### Execution Architecture (v1.3.1 — 5-Contract Scale-Out)

- **C1 (1 contract)** — The Canary: 5-bar time exit. Validates direction.
- **C2 (1 contract)** — The Structure: Structural target (nearest swing point) + delayed BE.
- **C3 (3 contracts)** — The Runner: ATR trailing stop (DELAYED ENTRY: only stays open when C1 exits profitably. If C1 loses → C3 closed immediately).

**Delayed C3 Runner (THE KEY EDGE)**: Saved $38,430, reduced max DD 8.62% → 1.60%. 120/396 trades had C3 blocked (30.3%).

**Do not loosen these gates or modify the C3 delay logic without new backtested evidence.**

### Constants Location

```python
# config/constants.py — SINGLE SOURCE OF TRUTH for all policy constants
# All modules import from here. Do NOT redefine locally.
HIGH_CONVICTION_MIN_SCORE = 0.75
HIGH_CONVICTION_MAX_STOP_PTS = 30.0
SWEEP_MIN_SCORE = 0.50           # Sweep must score >= 0.50 to be eligible
SWEEP_CONFLUENCE_BONUS = 0.05    # Boost when signal + sweep fire together
HTF_STRENGTH_GATE = 0.3          # Config D — do NOT change without backtest
CONTEXT_AGGREGATOR_BOOST = 0.05  # Layer 2 context boost
CONTEXT_OB_BOOST = 0.05          # Order block proximity boost
CONTEXT_FVG_BOOST = 0.05         # FVG proximity boost

# config/settings.py — ScaleOutConfig
c1_time_exit_bars = 5            # Exit C1 at market after 5 bars if profitable
c1_max_bars_fallback = 12        # Fallback market exit if still profitable
c3_delayed_entry_enabled = True  # C3 only stays when C1 profits (THE KEY EDGE)
max_daily_loss_pct = 1.0         # $500 daily limit (v1.3.1 validated)
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

### Entry Mode: PATH C+ (Dual-Trigger Architecture)

v1.3.2 uses **PATH C+**: both liquidity sweeps AND high-conviction aggregator signals can independently trigger trades. This was re-enabled Mar 2026 after analysis showed aggregator-only trades produced +$12,626 across 1,025 trades — more total profit than sweep-only (+$9,896 across 338 trades). There was no documented evidence that demoting the aggregator to context-only (original PATH C) improved performance.

| Mode | Condition | Behavior |
|------|-----------|----------|
| **Sweep trigger** | Sweep score >= 0.50 | Sweep acts as primary signal source (must pass HC >= 0.75) |
| **Confluence** | Sweep + aggregator alignment | Score boosted by context boosts (+0.05 each for aggregator, OB, FVG) |
| **Aggregator standalone** | Aggregator score >= 0.75 (no sweep) | Aggregator triggers trade independently at high conviction |

HTF bias disagreement applies a -0.10 score penalty (soft gate, not a hard block).

Config flags in `config/constants.py`:
- `AGGREGATOR_STANDALONE_ENABLED = True` — enables/disables aggregator standalone path
- `AGGREGATOR_STANDALONE_MIN_SCORE = 0.75` — minimum score for aggregator to trigger independently

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
│   ├── constants.py               # HC constants — SINGLE SOURCE OF TRUTH
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
│   ├── scale_out_executor.py     # ScaleOutExecutor — 5-contract lifecycle (v1.3.1)
│   └── orchestrator.py          # IBKRLivePipeline — IBKR live trading orchestrator
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
│   ├── SECURITY_AUDIT.md         # Consolidated security audit (3 phases)
│   ├── validation_report.html    # Institutional-grade OOS report (dark-themed)
│   └── out_of_sample_validation.md  # Generated OOS results markdown
├── requirements.txt              # Pinned dependencies for reproducible installs
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
| Contracts per Trade | 5 (C1=1 + C2=1 + C3=3) |
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

### System Status: v2.0.0-rc1 — PAPER TRADING APPROVED

**Version:** v2.0.0-rc1
**Tests:** 449 passed (all offline, 0 network calls, ~2s)
**Security Audit:** COMPLETE — 9 CRITICAL, 8 HIGH fixed; 0 remain (see `docs/SECURITY_AUDIT.md`)
**Deployment Readiness:** Paper trading approved on Tradovate demo

Config D + Variant C (Trail from Profit) + Sweep Detector + Calibrated Slippage complete. 6-month OOS validated on FirstRate 1-minute absolute-adjusted NQ data (Sep 2025 – Feb 2026) with realistic slippage model (avg 0.96pt/fill). PF 1.73 with slippage — system survives real-world friction.

**Current Config:**
- HC filter: score >= 0.75, stop <= 30pts
- HTF gate: strength >= 0.3 (Config D)
- C1 exit: Trail from +3pts (2.5pt trail, 12-bar fallback) — Variant C
- C2: ATR-based trailing runner
- Sweep detector: additive (PDH/PDL, session H/L, PWH/PWL, VWAP, round numbers)
- Slippage: Calibrated (RTH 0.50pt, ETH 1.00pt, caps 1.50/2.50/3.00pt)

### Critical Fixes Applied (Security Audit)

| Fix | Severity | Impact |
|-----|----------|--------|
| NaN guard on all safety gates (7 locations) | CRITICAL | `isfinite()` prevents silent bypass of HC, kill switch, loss limits |
| HTF gate fail-safe (defaults `False`) | CRITICAL | No HTF data → trades blocked (not allowed) |
| DST-safe timezone (`ZoneInfo("America/New_York")`) | CRITICAL | Session boundaries correct year-round, no DST drift |
| 4-layer NaN/Inf data pipeline defense | CRITICAL | Corrupted prices rejected at parse → tick → candle → bar |
| Emergency flatten 3-attempt retry | CRITICAL | Network blip won't leave positions orphaned |
| Gateway 24h session expiry detection | CRITICAL | Pipeline halts instead of silent 401 order failures |
| asyncio.Lock on bar processing + reconciliation | HIGH | No race conditions between concurrent tasks |
| RotatingFileHandler on all log files | HIGH | Logs capped at ~60 MB per runner |
| Pinned `requirements.txt` | HIGH | Reproducible installs for financial system |
| Windows shutdown compatibility | HIGH | Cross-platform Ctrl+C with 30s timeout |

### Hardening Fixes Applied (MEDIUM — Post-Audit)

| Fix | Audit ID | Impact |
|-----|----------|--------|
| HC constants in `config/constants.py` — single source of truth | P1-M1 | Eliminates drift between main.py, orchestrator.py, full_backtest.py |
| HTF data staleness detection | P1-M4 | Stale TFs downgraded to neutral + warning logged |
| Position state persistence to disk | P2-M7 | `logs/position_state.json` — atomic write, crash recovery via `load_state()` |
| Candle aggregator duplicate/out-of-order detection | P2-M3/M4 | Replayed ticks dropped, duplicate candles skipped |
| JSONL decision/trade logs with daily rotation | P3-M1 | Replaces load-rewrite pattern — bounded growth |

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
- IBKR live integration pipeline (orchestrator.py, ibkr_client.py, order_executor.py, position_manager.py)
- Full security audit (3 phases) — all CRITICAL/HIGH fixed

### Next Milestone
- Paper trading on Tradovate demo
- Monitor live PnL vs backtest baseline

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

## Architecture Changes (Mar 2026)

This section documents significant structural and configuration changes implemented on 2026-03-07.

### 1. C1 Exit Strategy Shift: Trail-from-Profit to Time-Based Exit

**Old Approach (Variant C):** C1 exited via trail-from-profit once unrealized profit reached 3.0pts, using a 2.5pt trailing stop with 12-bar fallback (PF 1.81 without sweep detector).

**New Approach (Mar 2026):** C1 now exits via **B:5 bars time-based exit** (PF 1.81 with same sweep detector integration). This provides deterministic exit timing and reduces exposure to tail whipsaws in choppy/ranging regimes.

**Implementation:**
- Old method archived as `_manage_phase_1_trail_from_profit()` for A/B testing and historical reference
- New time-based exit managed by updated `_manage_phase_1()` logic
- Config params in `config/settings.py`:
  - `c1_time_exit_bars = 5` (exit C1 at bar 5 of position)
  - `c1_max_bars_fallback = 12` (ultimate fallback if bar-5 exit fails for technical reasons)
- Edge case: if C1 reaches break-even + 2pts before bar 5, capture that profit; otherwise exit at market on bar 5
- **DO NOT REVERT** to trail-from-profit without backtesting evidence showing degradation of Profit Factor below 1.65

**Rationale:** Time-based exits reduce decision complexity and protect against whipsaw losses in choppy intraday regimes. The 5-bar window (10 minutes on 2m exec timeframe) is short enough to avoid extended drawdowns while long enough to capture most trending moves.

### 2. Breakeven Buffer Increase: 1.0pt → 2.0pts

**Change:** `c2_breakeven_buffer_points` increased from 1.0 to 2.0 pts.

**Justification (Osler 2005):** Stop-hunting occurs at exact entry levels. A 2pt buffer moves the BE trigger away from institutional hunt targets. This reduces false stops and improves C2 runner probability.

**Location:** `config/settings.py`, `ScaleOutConfig.c2_breakeven_buffer_points = 2.0`

**Impact:** When C1 closes profitably, C2's break-even stop moves to entry + 2.0pts instead of entry + 1.0pt.

### 3. MFE (Maximum Favorable Excursion) Tracking Added

**New Fields** in `ScaleOutTrade` dataclass:
- `c1_mfe` (float): Maximum favorable excursion for C1, in points
- `c2_mfe` (float): Maximum favorable excursion for C2, in points

**Coverage:** MFE is tracked for **ALL exit types**, including:
- Profit-based exits (trail-from-profit, time-based)
- Stop losses
- Target exits
- Time-based exits

**Access:** Use `get_trade_history()` method or query `logs/position_state.json` to retrieve completed trades with MFE data.

**Purpose:** MFE analysis enables post-trade research on "what could have been captured" vs. actual exit price. Useful for:
- Validating C1 time-based exit window (5 bars) against missed upside
- Analyzing runner (C2) performance gaps
- Identifying market regimes where trailing (old approach) would have outperformed time-based exits

### 4. C1 Exit Metrics for C2 Optimization Research

**New Fields** in `ScaleOutTrade` dataclass:
- `c1_exit_profit_pts` (float): Actual P&L captured on C1 in points
- `c1_exit_bars` (int): Number of bars from C1 entry to C1 exit
- `c1_price_velocity` (float): Points/bar velocity of price move from entry to C1 exit

**Purpose:** These metrics support deeper research on C2 optimization:
- Analyze whether fast C1 exits (high velocity) correlate with strong C2 runners
- Identify market conditions where C1 exits early but C2 could have run 80+ points
- Optimize C2 runner entry trigger and trailing mechanics based on C1 exit context

**Access:** Same as MFE fields — `get_trade_history()` or JSON logs.

### 5. Adaptive Exit Configuration Framework

**Module:** `execution/adaptive_exit_config.py`

**Status:** DISABLED by default (`adaptive_exits_enabled = False`). Must pass walk-forward validation before enabling.

**What It Does:** Provides regime-adaptive breakeven trigger distance and trailing stop width based on market conditions (trending vs. ranging).

**Mechanism:**
- Uses ADX (Average Directional Index) with hysteresis:
  - **Trending regime:** ADX > 25 → wider trailing stops (more room for noise)
  - **Ranging regime:** ADX < 20 → tighter stops (protect against whipsaws)
  - **Hysteresis:** Prevents rapid regime flips on each bar
- **Parameters Adapted:** Only 2 parameters to prevent overfitting:
  1. Breakeven buffer distance (c2_breakeven_buffer_points)
  2. C2 trailing stop width (c2_trailing_atr_multiplier)
- **Static Gates:** HC filter (0.75 score, 30pt stop) remain **unchanged** regardless of regime

**Why Disabled:** Adaptive logic adds complexity and overfitting risk. Must validate via walk-forward testing framework before deployment.

**How to Enable (after validation):**
```python
# config/settings.py
adaptive_exits_enabled = True
```

**Testing:** See walk_forward_validation.py (section 6 below).

### 6. New Analysis & Validation Scripts

#### 6a. `scripts/deflated_sharpe.py` — Deflated Sharpe Ratio Calculator

**Purpose:** Implement David Balyasnikov Lopez de Prado's Deflated Sharpe Ratio (DSR) to account for multiple testing (backtesting bias).

**Usage:**
```bash
python scripts/deflated_sharpe.py --trades-file logs/position_state.json --output-file dsr_report.json
```

**Output:** DSR value and probability that observed Sharpe ratio is due to overfitting vs. genuine edge.

#### 6b. `scripts/walk_forward_validation.py` — Walk-Forward Validation Framework

**Purpose:** Systematically test parameter changes over rolling windows to prevent overfitting.

**Features:**
- Partitions 6-month backtest data into in-sample (4 months) + out-of-sample (1 month) rolling windows
- Optimizes parameters on IS data, validates on OOS data
- Reports walkforward efficiency (OOS performance / IS performance)
- Includes `--demo` mode to test framework without full backtest

**Usage:**
```bash
# Demo mode (10 bars, 1 window)
python scripts/walk_forward_validation.py --demo

# Full validation (60-day rolling windows, adaptive exit config)
python scripts/walk_forward_validation.py --test-adaptive-exits

# Output: walk_forward_report.json with OOS metrics per window
```

**Threshold:** Walkforward efficiency >= 0.80 (OOS performance must be >= 80% of IS) before deploying new config.

#### 6c. `scripts/stolen_runner_analysis.py` — Post-BE Price Continuation Analysis

**Purpose:** Analyze how often price continues favorably after C2 breakeven is hit.

**Outputs:**
- Percentage of trades where C2 breaks even and price continues >5pts in trend direction
- Average continuation distance when it occurs
- Win rate of C2 runner when C2 achieves breakeven

**Motivation:** Validates whether BE trigger design (now 2pts buffer) is optimal. If runner continuation is >80% after BE, consider tightening BE distance further.

#### 6d. `scripts/session_time_analysis.py` — Trade Performance by Time of Day

**Purpose:** Measure win rate and profit factor by session/hour.

**Outputs:**
- Performance breakdown: RTH morning (9:30–11:30 ET), midday (11:30–14:00), afternoon (14:00–16:00), ETH
- Identify if C1 time-based exit (5 bars) performs better/worse in specific sessions
- Support decision to potentially disable trading in low-edge sessions

**Known Edge (from existing Regime Analysis):**
- Morning session (9:30–11:30 ET): PF 3.62, 78% WR — strongest
- Shorts outperform longs: +$8,790 vs +$5,904
- Ranging regime: PF 2.07, best profitability

### 7. Research Documentation

#### 7a. `docs/algo_trading_research.md`

**Contents:** 6-area research report covering:
1. C1 exit variant comparison (trail-from-profit vs. time-based vs. fixed target)
2. Breakeven buffer optimization (backtested distances 0.5–3.0pts)
3. Adaptive exit framework validation (ADX thresholds, overfitting tests)
4. MFE/MAE analysis (max favorable vs. adverse excursion by regime)
5. Runner (C2) optimization opportunities
6. Session-specific edge analysis (time-of-day trading windows)

**Purpose:** Reference for future parameter tuning. All claims backed by 6-month backtest data with calibrated slippage.

#### 7b. `docs/action_plan.md`

**Contents:** Ranked 10-item action plan for next optimization cycle.

**Example priorities:**
1. Walk-forward validate adaptive exit config
2. Test C1 exit bar window (3 vs. 5 vs. 7 bars)
3. Optimize C2 trailing width per market regime
4. Reduce noise in signal aggregator (lower score threshold from 0.75 → 0.70 only in high-ADX regimes)
5. Monitor session-specific performance and disable low-edge windows
... (10 items total, ranked by expected PF improvement)

**Purpose:** Structured roadmap for continuous improvement without ad-hoc tuning.

### Critical Notes

**DO NOT REVERT the following without new backtested evidence:**
- C1 exit strategy (time-based 5-bar approach) — tested to PF 1.81
- Breakeven buffer increase to 2.0pts — validated against stop-hunting literature
- HC filter hard gates (0.75 score, 30pt stop) — core risk framework, never relax

**Adaptive Exit Config Status:**
- Currently DISABLED to prevent overfitting
- Requires walk-forward validation (WFE >= 0.80) before enabling
- When enabled, only 2 parameters adapt (BE buffer + trail width); HC gates remain static

**MFE/MAE Tracking:**
- Enables post-trade analysis — does NOT affect live trading logic
- Use for research only; do not implement reactive stops based on MFE data

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

---

## LESSONS LEARNED — NEVER REPEAT THESE MISTAKES

This section documents bugs, design errors, and hard-won lessons. Read this BEFORE making changes.

### 1. HTF Staleness Kills All Backtest Trades (Fixed Mar 2026)

**Bug:** The HTF engine's `_last_update_time` stored the bar's BUCKET START timestamp (e.g., 09:05 for a 5m bar). The staleness check compared this against the current execution bar time. During natural data gaps in the CSV (overnight settlement, pre-market gaps), the age grew to 82+ minutes, exceeding the 15-min staleness limit. Result: HTF bias forced to neutral on EVERY bar → zero trades in entire 179K-bar backtest.

**Fix (two-part):**
1. `htf_engine.py`: Changed `_last_update_time` to store bar COMPLETION time (`bar.timestamp + timedelta(minutes=tf_period)`) instead of start time.
2. `htf_engine.py`: Added `backtest_mode=True` parameter that skips staleness checks entirely. In backtests, the HTFScheduler handles causality — staleness is meaningless for pre-built historical data.
3. `full_backtest.py`: Passes `backtest_mode=True` when instantiating `HTFBiasEngine`.

**Rule:** Live trading keeps `backtest_mode=False` (staleness enforced). Backtests use `backtest_mode=True`. NEVER add staleness to backtest mode.

### 2. Kill Switch Import Caching Bug (Fixed Mar 2026)

**Bug:** `from config.constants import GAINZ_MODULES_ENABLED` creates a LOCAL binding at import time. If the module attribute is changed at runtime (e.g., for testing), the local binding keeps the OLD value. The kill switch appeared broken — setting `GAINZ_MODULES_ENABLED = False` at runtime had no effect.

**Fix:** Changed to `import config.constants as _constants` and check `_constants.GAINZ_MODULES_ENABLED` at runtime. This always reads the CURRENT module attribute.

**Rule:** For any runtime-toggleable flag, NEVER use `from module import FLAG`. Always use `import module` and access `module.FLAG`.

### 3. Backtest Cannot Run in VM — Use Windows Terminal (Mar 2026)

**Issue:** The full backtest processes 179K 2-min bars at 100% CPU. The VM times out (exit code 143 = SIGTERM). Even background execution with `nohup` hits the timeout limit.

**Rule:** Always run backtests from the user's Windows PowerShell terminal:
```powershell
cd C:\Users\dagos\OneDrive\Desktop\AI-Trading-Bot\nq_bot_vscode
Remove-Item .\logs\backtest_checkpoint.json -ErrorAction SilentlyContinue
py -u scripts/full_backtest.py --run 2>&1 | Tee-Object -FilePath logs/backtest_v133.log
```

### 4. Old Backtest Checkpoints Cause Ghost Behavior (Mar 2026)

**Bug:** A checkpoint from a pre-Path C+ run was found at bar 25,000 with 0 entries. Resuming from this checkpoint meant the aggregator standalone trigger was never evaluated for the first 25K bars. The backtest appeared to work but produced wrong results.

**Rule:** ALWAYS delete `logs/backtest_checkpoint.json` before running a backtest with new code. Old checkpoints carry stale state from previous versions.

### 5. Python stdout Buffering Hides Progress (Mar 2026)

**Bug:** When running the backtest with `nohup` or piping output, Python buffers stdout. Progress reports were invisible in the log file.

**Rule:** Always use `python -u` (unbuffered) or set `PYTHONUNBUFFERED=1` when running backtests.

### 6. Timestamp Parsing: dateutil Not fromisoformat (Mar 2026)

**Bug:** Real CSV has timestamps like `2025-03-02 19:00:00-0500` (no colon in timezone offset). Python's `datetime.fromisoformat()` cannot parse this format (requires `-05:00`).

**Fix:** Used `dateutil.parser.parse()` which handles all common timestamp formats.

**Rule:** Always use `dateutil.parser.parse()` for CSV timestamp parsing, never `fromisoformat()`.

### 7. Zero Lookahead Bias Invariant (Design Principle)

**Rule:** ALL computations must use only COMPLETED bars. The current bar is NEVER included in historical calculations. This applies to all modules: feature engine, HTF engine, GainzAlgo modules, regime detector. When adding new features, verify: current data is added to history AFTER computation, not before.

### 8. GainzAlgo Modules Are Purely Additive (V1.3.3 Design Principle)

**Rule:** The 5 GainzAlgo modules (VolPercentile, SAMSM, CSTA, CSMRM, AdaptiveConfidence) feed INTO the existing pipeline — they NEVER replace core trigger logic. The adaptive HC gate adjusts within bounds [0.70, 0.82]. Cross-signal boosts are capped at +0.10. The kill switch (`GAINZ_MODULES_ENABLED=False`) instantly reverts to V1.3.2 behavior.

### 9. Backtest Gates Must Match Live Trading Logic (Fixed Mar 2026)

**Bug:** The backtest had two gates that main.py (live trading) did NOT have:
1. Min stop gate: `if raw_stop < 30.0: return` — combined with the max stop gate (`> 30.0`), the only valid stop was EXACTLY 30.000000 points. ATR-based stop calculations essentially never produce this exact value. Result: 0 trades.
2. Prime hours gate: `if not is_prime_hours(bar["timestamp"]): return` — restricted trades to 9-10 AM ET only. Not present in live trading. Blocked 90%+ of potential trades.

**Fix:** Removed both gates from `full_backtest.py`. The HC filter should be score >= 0.75 AND stop <= 30pts — matching main.py exactly.

**Rule:** The backtest MUST use the same entry gates as main.py. NEVER add filters to the backtest that don't exist in live trading — backtest results won't reflect live performance. If a new gate is being tested, add it to BOTH files simultaneously.

### 10. HTF Hysteresis Prevents Flip-Flopping (V1.3.2)

**Design:** Two-stage anti-flip-flop for HTF bias direction changes:
- Stage 1 (margin): opposing direction must exceed `HTF_HYSTERESIS_MARGIN` (0.3) strength
- Stage 2 (hold): must hold for `HTF_HYSTERESIS_CONFIRM_BARS` (3) consecutive bars

**Rule:** Do NOT reduce these thresholds without backtested evidence. Flip-flopping bias kills profitability.

---

## V1.3.2 Architecture Changes (Mar 2026)

### 5-Contract Scale-Out (C1=1, C2=1, C3=3)

Upgraded from 2-contract (C1+C2) to 5-contract system:
- C1 (1 contract): 5-bar time exit canary — validates direction
- C2 (1 contract): Structural target + delayed breakeven
- C3 (3 contracts): ATR trailing runner — DELAYED ENTRY (only stays if C1 exits profitably; if C1 loses, C3 closes immediately)

C3 constants in `config/constants.py`: `C3_CONTRACTS = 3`

### Path C+ Dual-Trigger Architecture

Both liquidity sweeps AND high-conviction aggregator signals independently trigger trades:
- Sweep trigger: sweep score >= 0.50, must pass HC >= 0.75
- Aggregator standalone: aggregator score >= 0.75 (no sweep needed)
- Confluence: both fire same direction → context boosts applied

Config: `AGGREGATOR_STANDALONE_ENABLED = True`

### HTF Hysteresis (Anti-Flip-Flop)

Two-stage protection against HTF bias direction changes using 5m/15m timeframes. See Lesson #9 above.

---

## V1.3.3 Architecture Changes (Mar 2026) — GainzAlgo Suite

### New File: `features/gainz_modules.py`

Contains 5 GainzAlgo modules aligned with the GainzAlgo Suite 5-Pillar Framework:

1. **VolatilityPercentileNormalizer** — Percentile-ranked ATR (500-bar lookback). Classifies vol_regime: compressed (<20th), normal, expanding (>60th), extreme (>85th). Adapts sweep depth range [2.0, 6.0] based on percentile.

2. **MomentumAccelerationModel (SAMSM)** — Tracks velocity (1st derivative via EMA-smoothed changes) and acceleration (2nd derivative). Detects surge (>2σ) and exhaustion (5+ consecutive deceleration bars). Scores momentum 0.0–1.0.

3. **CycleSlopeTrendAnalyzer (CSTA)** — Dual-EMA slope analysis (fast=8, slow=21). Classifies phases: impulse_up/down, correction_up/down, consolidation. Scores cycle 0.0–1.0.

4. **CandleMicroReversalEvaluator (CSMRM)** — Detects candle patterns: hammer, shooting_star, doji, pin_bar, engulfing. Tracks consecutive rejection bars and pressure asymmetry. Generates reversal score and direction.

5. **AdaptiveConfidenceEngine** — Adjusts HC gate dynamically within bounds [0.70, 0.82] based on vol regime, momentum phase, cycle position, and market regime. Computes cross-signal synergy boosts capped at +0.10.

### Modified Files

- `features/engine.py`: Added 20+ FeatureSnapshot fields, `_compute_gainz_features()`, `_init_gainz_modules()` (lazy init)
- `signals/aggregator.py`: Added `_extract_gainz_signals()`, adaptive gate support, cross-signal boost
- `config/constants.py`: Added 17 V1.3.3 constants (VOL_PERCENTILE_LOOKBACK, SAMSM_*, CSTA_*, CSMRM_*, ADAPTIVE_*, GAINZ_*)
- `main.py`: Passes adaptive_hc_gate + cross_signal_boost to aggregator
- `scripts/full_backtest.py`: Same adaptive gate integration

### Kill Switch

`GAINZ_MODULES_ENABLED = True` in `config/constants.py`. Set to `False` to instantly revert to V1.3.2 behavior. Uses runtime module reference (`_constants.GAINZ_MODULES_ENABLED`) — see Lesson #2.

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
