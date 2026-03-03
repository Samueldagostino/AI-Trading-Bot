# Universal Confirmation Layer (UCL) — Design Document

> **Status:** DESIGN — not yet implemented
> **Author:** Auto-generated from architectural spec
> **Date:** 2026-03-03
> **Scope:** New subsystem added alongside existing pipeline — replaces nothing

---

## Table of Contents

1. [Motivation & Overview](#1-motivation--overview)
2. [Watch State Manager](#2-watch-state-manager)
3. [Confirmation Conditions by Setup Type](#3-confirmation-conditions-by-setup-type)
4. [FVG Detector (New Module)](#4-fvg-detector-new-module)
5. [HC Score Boost from Confirmation](#5-hc-score-boost-from-confirmation)
6. [Risk Parameters](#6-risk-parameters)
7. [Integration with Existing System](#7-integration-with-existing-system)
8. [Backtesting Requirements](#8-backtesting-requirements)
9. [What This Replaces vs What It Adds](#9-what-this-replaces-vs-what-it-adds)

---

## 1. Motivation & Overview

The current trading pipeline is **predictive**: a signal fires and the system either enters immediately or rejects it.

```
CURRENT FLOW:
  Signal → HC filter (score ≥ 0.75, stop ≤ 30pts) → HTF gate → Execute
```

The HC gate analysis revealed **3,968 blocked signals with a collective PF of 1.05** — marginal edge exists in that pool, but it's buried under noise. Entering all of them would destroy the account. Entering none of them leaves edge on the table.

The Universal Confirmation Layer introduces a **reactive** entry mode: signals that don't immediately pass the HC gate enter a *watch state* and wait for the market to structurally confirm the setup before entry.

```
PROPOSED FLOW:
  Signal → WATCH STATE → Confirmation conditions met →
  HC filter (boosted score) → HTF gate → Execute
```

This splits the 3,968 blocked signals into:
- **Confirmed** (market structure validates the setup) → tradeable at higher PF
- **Unconfirmed** (signal expires or invalidates) → filtered out as actual losers

The confirmation layer does **not replace** the existing immediate-entry system. It adds a second entry mode for signals in the 0.60–0.84 score range.

---

## 2. Watch State Manager

### Location

New class: `WatchStateManager`, hosted in `main.py` (`TradingOrchestrator`) as `self._watch_manager`.

Called from `TradingOrchestrator.process_bar()` on every execution-TF bar, after signal detection and before the HC gate check.

### WatchState Data Structure

```python
@dataclass
class WatchState:
    setup_type: str           # "sweep" | "break_retest" | "ob_reaction" | "fvg_tap"
    direction: str            # "LONG" | "SHORT"
    trigger_bar: int          # bar index when the signal fired
    trigger_price: float      # price at signal time
    key_level: float          # the structural level being watched
    invalidation_price: float # if price reaches here → cancel watch
    expiry_bars: int          # auto-cancel if no confirmation within N bars
    confirmation_conditions: List[str]   # what must happen (e.g. ["RECLAIM", "FVG_FORM", "FVG_TAP"])
    confirmations_met: Dict[str, bool]   # progress tracker (e.g. {"RECLAIM": True, "FVG_FORM": False, ...})
    metadata: Dict            # setup-specific data (FVG bounds, sweep low, OB zone, etc.)
    base_score: float         # original signal score at trigger time
    created_at: datetime      # wall-clock time of creation
```

### WatchStateManager Class

```python
class WatchStateManager:
    MAX_ACTIVE_WATCHES = 3

    def add_watch(self, watch: WatchState) -> bool
    def update(self, bar, fvg_detector: FVGDetector) -> List[ConfirmedSignal]
    def cancel(self, watch_id: str) -> None
    def get_active_watches(self) -> List[WatchState]
    def get_stats(self) -> Dict
```

### Rules

| Rule | Detail |
|------|--------|
| **Max concurrent watches** | 3 active at any time. If a 4th is requested, it is dropped (oldest-first eviction). |
| **Uniqueness** | Only one watch state per `(direction, setup_type)` pair. A new sweep-LONG watch replaces an existing sweep-LONG watch. |
| **Expiry** | Each watch has an `expiry_bars` countdown. Decremented every execution-TF bar. When it reaches 0, the watch is silently removed. |
| **Invalidation** | Checked every bar. If the current bar's close breaches `invalidation_price`, the watch is immediately cancelled. For LONG watches: `close < invalidation_price`. For SHORT watches: `close > invalidation_price`. |
| **Confirmation** | Each bar, the manager evaluates all `confirmation_conditions` against current market state. When **all** conditions in `confirmations_met` flip to `True`, the watch emits a `ConfirmedSignal`. |
| **Emission** | A `ConfirmedSignal` carries the original signal data plus the confirmation context (which conditions met, how many bars elapsed, FVG data if applicable). It re-enters the pipeline at the HC gate with a boosted score. |

### Lifecycle Diagram

```
Signal fires (score 0.60–0.84)
    │
    ▼
WatchStateManager.add_watch()
    │
    ├─ [each bar] ──► check invalidation → cancel if breached
    │                 check expiry → remove if expired
    │                 evaluate confirmations → update confirmations_met
    │
    ├─ ALL confirmations met ──► emit ConfirmedSignal
    │                            ──► HC gate (boosted score)
    │                            ──► HTF gate
    │                            ──► Execute
    │
    └─ Expired / Invalidated ──► silently removed, logged for shadow analysis
```

---

## 3. Confirmation Conditions by Setup Type

### 3.1 Liquidity Sweep

**Signal source:** `LiquiditySweepDetector` (existing, `signals/liquidity_sweep.py`)

**Watch state parameters:**

| Field | Value |
|-------|-------|
| `setup_type` | `"sweep"` |
| `key_level` | Swept level price |
| `invalidation_price` | Sweep low − 10 pts (for longs); sweep high + 10 pts (for shorts) |
| `expiry_bars` | 60 (= 2 hours on 2m TF) |
| `confirmation_conditions` | `["RECLAIM", "FVG_FORM", "FVG_TAP"]` |
| `metadata` | `{"sweep_low": float, "sweep_depth": float, "levels_swept": List[str]}` |

**Confirmation logic:**

| # | Condition | Rule |
|---|-----------|------|
| 1 | **RECLAIM** | Price closes back above swept level (longs) or below (shorts). |
| 2 | **FVG_FORM** | A 3-candle Fair Value Gap forms during the recovery move (detected by `FVGDetector`). |
| 3 | **FVG_TAP** | Price returns to the FVG zone and holds — does not close below FVG low (longs) or above FVG high (shorts). |

**Entry:** Next bar after `FVG_TAP` confirms.
**Stop:** Below sweep low (longs) / above sweep high (shorts) — tight, structural.

---

### 3.2 Break and Retest

**Signal source:** New detection logic (Phase 2). Identifies decisive breaks of significant structure levels.

**Watch state parameters:**

| Field | Value |
|-------|-------|
| `setup_type` | `"break_retest"` |
| `key_level` | Broken structure level |
| `invalidation_price` | Close back below level by > 5 pts (longs); above by > 5 pts (shorts) |
| `expiry_bars` | 90 (retests can take longer) |
| `confirmation_conditions` | `["BREAKOUT", "PULLBACK", "HOLD"]` |
| `metadata` | `{"break_bar": int, "break_magnitude": float}` |

**Confirmation logic:**

| # | Condition | Rule |
|---|-----------|------|
| 1 | **BREAKOUT** | Price closes decisively beyond level (already true at watch creation). |
| 2 | **PULLBACK** | Price returns to within 5 pts of the broken level. |
| 3 | **HOLD** | Price closes back in the breakout direction from the level. |
| — | **REJECTION** *(optional bonus)* | Candle shows wick rejection from level. Not required for confirmation but contributes to score boost if present. |

**Entry:** Next bar after `HOLD` confirms.
**Stop:** Below the retest low (longs) / above the retest high (shorts).

---

### 3.3 Order Block Reaction

**Signal source:** Existing OB detection in `NQFeatureEngine` (`features/engine.py`), promoted to standalone signal in Phase 2.

**Watch state parameters:**

| Field | Value |
|-------|-------|
| `setup_type` | `"ob_reaction"` |
| `key_level` | OB zone midpoint |
| `invalidation_price` | Price closes through OB zone entirely |
| `expiry_bars` | 30 (OB reactions should be fast) |
| `confirmation_conditions` | `["TAP", "REJECTION", "DISPLACEMENT"]` |
| `metadata` | `{"ob_high": float, "ob_low": float, "ob_type": str}` |

**Confirmation logic:**

| # | Condition | Rule |
|---|-----------|------|
| 1 | **TAP** | Price enters OB zone (high/low of bar overlaps OB zone). |
| 2 | **REJECTION** | Strong rejection candle — large wick (≥ 60% of range), small body (≤ 40% of range). |
| 3 | **DISPLACEMENT** | Next candle continues in the rejection direction with a close beyond the rejection candle's body. |

**Entry:** Next bar after `DISPLACEMENT` confirms.
**Stop:** Beyond OB zone boundary (below OB low for longs, above OB high for shorts).

---

### 3.4 FVG / IFVG Tap (Standalone)

**Signal source:** `FVGDetector` (new module, see Section 4). Standalone FVG taps, not part of a sweep confirmation.

**Watch state parameters:**

| Field | Value |
|-------|-------|
| `setup_type` | `"fvg_tap"` |
| `key_level` | FVG midpoint (CE — consequent encroachment) |
| `invalidation_price` | Price closes through FVG entirely |
| `expiry_bars` | 45 |
| `confirmation_conditions` | `["ENTER_ZONE", "HOLD", "CONTINUATION"]` |
| `metadata` | `{"fvg_high": float, "fvg_low": float, "is_inverse": bool, "formation_bar": int}` |

**Confirmation logic:**

| # | Condition | Rule |
|---|-----------|------|
| 1 | **ENTER_ZONE** | Price enters FVG zone (bar low ≤ fvg_high and bar high ≥ fvg_low). |
| 2 | **HOLD** | Candle closes within or above FVG (longs) / within or below FVG (shorts). Does not violate the zone. |
| 3 | **CONTINUATION** | Next candle moves in expected direction (close > prior close for longs, close < prior close for shorts). |

**Entry:** Next bar after `CONTINUATION` confirms.
**Stop:** Beyond FVG boundary (below FVG low for longs, above FVG high for shorts).

---

## 4. FVG Detector (New Module)

### Location

New file: `signals/fvg_detector.py`

### FVG Data Structure

```python
@dataclass
class FairValueGap:
    fvg_high: float
    fvg_low: float
    fvg_midpoint: float          # CE (consequent encroachment)
    formation_bar: int           # bar index when formed
    formation_time: datetime
    direction: str               # "bullish" | "bearish"
    is_inverse: bool             # FVG against prevailing trend
    status: str                  # "UNFILLED" | "PARTIALLY_FILLED" | "FILLED" | "VIOLATED"
    size_points: float           # fvg_high - fvg_low
```

### FVGDetector Class

```python
class FVGDetector:
    MIN_FVG_SIZE = 2.0           # points — ignore micro-gaps
    MAX_ACTIVE_PER_DIRECTION = 20
    EXPIRY_BARS = 500            # remove if never revisited

    def update(self, bar, bar_index: int, trend_direction: str) -> List[FairValueGap]
    def get_active_fvgs(self, direction: str = None) -> List[FairValueGap]
    def check_zone_interaction(self, bar) -> List[Tuple[FairValueGap, str]]
    def get_stats(self) -> Dict
```

### Detection Logic

Scans the **last 3 completed candles** (`candle[0]`, `candle[1]`, `candle[2]` where `[2]` is most recent):

| Type | Condition | Zone |
|------|-----------|------|
| **Bullish FVG** | `candle[0].high < candle[2].low` | Gap between `candle[0].high` (bottom) and `candle[2].low` (top) |
| **Bearish FVG** | `candle[0].low > candle[2].high` | Gap between `candle[2].high` (bottom) and `candle[0].low` (top) |

**Inverse FVG (IFVG):** An FVG that forms against the prevailing trend direction. Example: a bearish FVG forming in an uptrend acts as potential support when price fills it. The `trend_direction` input comes from `NQFeatureEngine`'s trend classification.

### FVG Lifecycle

```
UNFILLED ──► price enters zone ──► PARTIALLY_FILLED
                                        │
                            price touches CE ──► FILLED
                                        │
                     price closes through zone ──► VIOLATED (removed)
```

| Transition | Trigger |
|------------|---------|
| `UNFILLED → PARTIALLY_FILLED` | Bar's wick enters FVG zone but close stays outside or within zone |
| `PARTIALLY_FILLED → FILLED` | Price touches the midpoint (CE) |
| `Any → VIOLATED` | Bar **closes** completely through the zone (close > fvg_high for bearish FVG, close < fvg_low for bullish FVG) |

### Filters

| Filter | Value | Rationale |
|--------|-------|-----------|
| Minimum size | 2.0 pts | Micro-gaps are noise on NQ |
| Max active per direction | 20 | Memory bound; oldest evicted first |
| Expiry | 500 bars (≈ 16.7 hours on 2m) | Stale FVGs lose institutional relevance |
| Violated FVGs | Immediately removed | No longer valid support/resistance |

---

## 5. HC Score Boost from Confirmation

Confirmed signals receive an HC score bonus because structural confluence is empirically higher after market validation.

### Boost Table

| Condition | Boost | Cumulative Example |
|-----------|-------|--------------------|
| Base signal score | — | 0.68 |
| Watch state confirmed (all conditions met) | **+0.10** | 0.78 |
| FVG confluence present at confirmation | **+0.05** | 0.83 |
| Fast confirmation (< 20 bars from trigger) | **+0.05** | 0.88 |
| HTF alignment at confirmation time | **+0.05** | 0.93 |

**Maximum possible boost:** +0.25
**Typical boost:** +0.10 to +0.20

### Worked Example

> A sweep fires at score **0.68** — below the HC threshold of 0.75, so it would be blocked by the current system.
>
> The watch state activates. Over the next 15 bars:
> 1. Price reclaims the swept level → `RECLAIM` ✓
> 2. A 3-candle FVG forms during recovery → `FVG_FORM` ✓
> 3. Price returns to FVG and holds → `FVG_TAP` ✓
>
> Confirmed score: `0.68 + 0.10 (confirmed) + 0.05 (FVG) + 0.05 (fast) = 0.88`
>
> The signal now passes the HC filter with strong conviction. The market proved the setup was valid.

### Why This Works

The HC gate shadow analysis showed a PF of 1.05 on all blocked signals — marginal edge buried under losers. The confirmation layer acts as a second filter:

- **Confirmed signals** → the market structurally validated the setup → higher PF
- **Unconfirmed signals** → expired or invalidated → these were the actual losers dragging PF to 1.05

The confirmation layer **rescues** valid signals that the HC gate would otherwise block, but only when the market proves they deserve it.

### Integration Point

In `TradingOrchestrator.process_bar()`, after `WatchStateManager.update()` emits a `ConfirmedSignal`:

```
# Pseudocode — NOT implementation
confirmed_signals = self._watch_manager.update(bar, self._fvg_detector)
for signal in confirmed_signals:
    boosted_score = signal.base_score + compute_boost(signal)
    # Re-enter pipeline at HC gate with boosted_score
    # Existing HC gate logic (score ≥ 0.75, stop ≤ 30pts) applies as normal
```

---

## 6. Risk Parameters

Post-confirmation entries have structurally different risk characteristics than immediate entries because the market has already proven the setup direction.

### Comparison Table

| Metric | Current System (Immediate) | Post-Confirmation (UCL) |
|--------|---------------------------|------------------------|
| **Stop distance** | 15–30 pts | 8–20 pts |
| **R:R ratio** | 1.5–3.0 | 2.0–6.0 |
| **Win rate** | 58% | 60–65% (estimated) |
| **Trade frequency** | 43 per 44K bars | 25–35 per 44K bars |
| **Fakeout rate** | ~42% (losses) | ~30% (estimated) |

### Why Tighter Stops

Confirmation provides a precise structural anchor for stop placement:

- **Sweep confirmation:** Stop goes below the sweep low — the market already proved it won't go there by reclaiming the level.
- **Break-and-retest:** Stop goes below the retest low — price held and reversed from the level.
- **OB reaction:** Stop goes beyond the OB boundary — the displacement candle proves direction.
- **FVG tap:** Stop goes beyond the FVG boundary — the hold candle proves the zone is respected.

These are tighter than the ATR-based stops in the current system (which use `ATR × multiplier` without structural context).

### C2 Runner Considerations

| Factor | Immediate Entry | Confirmed Entry |
|--------|----------------|-----------------|
| Initial stop room | Wider (15–30 pts) — more room for noise | Tighter (8–20 pts) — less room but confirmed direction |
| C2 runner survival | Higher (wide stop = less likely to get stopped before move) | Potentially lower (tight stop) but direction more certain |
| Net C2 value | High if move materializes | High because move is more likely to materialize |

**Expected net outcome:** Fewer trades, higher win rate, tighter stops, better R:R. Net PnL similar or higher with significantly lower drawdown.

---

## 7. Integration with Existing System

### Architecture Diagram

```
                    ┌──────────────────────────────────────────────┐
                    │         TradingOrchestrator.process_bar()     │
                    │              (main.py)                        │
                    └──────────┬───────────────────────────────────┘
                               │
               ┌───────────────┼───────────────┐
               ▼               ▼               ▼
        NQFeatureEngine   HTFBiasEngine   RegimeDetector
        (features/        (features/      (risk/
         engine.py)        htf_engine.py)  regime_detector.py)
               │               │               │
               └───────┬───────┘               │
                       ▼                       │
              ┌────────────────┐               │
              │ Signal Sources │               │
              ├────────────────┤               │
              │ SignalAggregator│              │
              │ SweepDetector   │              │
              │ ► FVGDetector   │ ◄── NEW     │
              └───────┬────────┘               │
                      │                        │
          ┌───────────┴──────────┐             │
          ▼                      ▼             │
   Score ≥ 0.85            Score 0.60–0.84    │
   IMMEDIATE ENTRY         WATCH STATE         │
          │                      │             │
          │         ┌────────────┴──────┐      │
          │         ▼                   ▼      │
          │    Confirmed           Expired/    │
          │    (all conditions)    Invalidated │
          │         │                   │      │
          │         ▼                   ▼      │
          │    Score + boost        Silently   │
          │         │               removed    │
          │         │              (shadow     │
          │         │               logged)    │
          └────┬────┘                          │
               ▼                               │
        HC Gate (≥ 0.75, stop ≤ 30pts)        │
               │                               │
               ▼                               │
        HTF Gate ◄─────────────────────────────┘
               │
               ▼
        RiskEngine.evaluate_trade()
               │
               ▼
        ScaleOutExecutor.enter_trade()
```

### New Files

| File | Class | Purpose |
|------|-------|---------|
| `signals/fvg_detector.py` | `FVGDetector` | Real-time FVG/IFVG detection and lifecycle tracking |
| `signals/watch_state.py` | `WatchStateManager`, `WatchState`, `ConfirmedSignal` | Watch state management and confirmation evaluation |

### Modified Files

| File | Change |
|------|--------|
| `main.py` | Add `WatchStateManager` and `FVGDetector` instantiation in `__init__`. Add watch-state evaluation in `process_bar()` between signal detection and HC gate. Add routing logic: score ≥ 0.85 → immediate, score 0.60–0.84 → watch state. |
| `config/constants.py` | Add `UCL_WATCH_SCORE_MIN = 0.60`, `UCL_WATCH_SCORE_MAX = 0.84`, `UCL_IMMEDIATE_SCORE_MIN = 0.85`, `UCL_CONFIRMATION_BOOST = 0.10`, `UCL_FVG_BOOST = 0.05`, `UCL_FAST_CONFIRM_BOOST = 0.05`, `UCL_HTF_ALIGN_BOOST = 0.05`. |
| `config/settings.py` | Add `WatchStateConfig` dataclass (max watches, default expiry, invalidation margins). |
| `scripts/run_backtest.py` | Extend to support UCL mode — track watch states, confirmations, shadow trades. |

### Phased Rollout

**Phase 1 — Build first, validate on existing data:**

- `WatchStateManager` class
- `FVGDetector` class
- Sweep → Watch → Confirm pipeline only (leverages existing `LiquiditySweepDetector`)
- Backtest on existing 44K-bar dataset
- Compare: immediate-only vs immediate + confirmed entries

**Phase 2 — After Phase 1 validated (PF improvement confirmed):**

- Break-and-retest detection + confirmation
- Order block reaction detection + confirmation
- Standalone FVG tap signals
- Each new setup type backtested independently before integration

**Phase 3 — After Phase 2 validated:**

- Cross-setup confluence scoring (sweep at FVG at OB = highest score)
- Adaptive expiry based on timeframe and volatility
- Session-aware confirmation windows (e.g., wider expiry during London/NY overlap)

---

## 8. Backtesting Requirements

### Causality Constraints

The confirmation layer must be **fully backtestable** with zero look-ahead bias:

| Requirement | Detail |
|-------------|--------|
| **Bar-by-bar processing** | `WatchStateManager.update()` processes one completed bar at a time, identical to live. |
| **No look-ahead** | Confirmation conditions evaluate only bars `[0..N]` at each step. The FVG detector uses only the last 3 *completed* candles. |
| **Expiry and invalidation** | Checked every bar, exactly as in live. No batch processing or post-hoc evaluation. |
| **FVG detection** | Uses only completed candles. The current (in-progress) candle is never used for FVG formation. |

### Shadow-Trade Analysis

Extend the existing shadow-trade framework to track **watch states that never confirmed**:

```
For each watch state that expires or is invalidated:
  - Record the hypothetical entry (next bar after trigger)
  - Simulate the trade with the same stop/target rules
  - Compute PnL
  - Compare: confirmed-trade PnL vs unconfirmed-trade PnL
```

This answers the critical question: **Is confirmation genuinely filtering losers, or is it also filtering winners?**

Expected result based on HC shadow analysis:
- Confirmed trades: PF > 1.5 (the market validated the setup)
- Unconfirmed trades: PF < 1.0 (these were the noise dragging the pool to PF 1.05)

If unconfirmed trades show PF > 1.0, the confirmation conditions are too strict and need loosening.

### Metrics to Track

| Metric | Purpose |
|--------|---------|
| Watch states created | Volume of signals entering UCL |
| Watch states confirmed | Conversion rate |
| Watch states expired | Timeout rate — if too high, `expiry_bars` may be too short |
| Watch states invalidated | Directional failure rate — validates invalidation levels |
| Avg bars to confirmation | Speed of confirmation — feeds into fast-confirm boost threshold |
| Confirmed PF vs Immediate PF | Core validation: does confirmation add edge? |
| Unconfirmed shadow PF | Validates that filtered signals were actually losers |
| Per-setup-type breakdown | Which setup types confirm most reliably? |

### Integration with Existing Backtest Scripts

The backtest runner (`scripts/run_backtest.py`) and the multi-timeframe iterator (`data_pipeline/pipeline.py: MultiTimeframeIterator`) already process bars sequentially and causally. The UCL components plug directly into `TradingOrchestrator.process_bar()` with no changes to the bar iteration logic.

---

## 9. What This Replaces vs What It Adds

### Replaces Nothing

The existing immediate-entry system remains **completely unchanged**. The confirmation layer is an **additional** entry mode that runs in parallel.

### Two Entry Modes

| Mode | Score Range | Behavior |
|------|-------------|----------|
| **IMMEDIATE** | ≥ 0.85 | Current system. Signal fires, passes HC gate, enters immediately. Very high conviction — no confirmation needed. |
| **CONFIRMED** | 0.60 – 0.84 | New UCL mode. Signal fires but doesn't pass HC gate. Enters watch state. If market confirms → boosted score re-enters HC gate. If not → silently expires. |

### Score Routing

```
Score < 0.60  ──► Rejected (too weak for even watch state)
Score 0.60–0.84 ──► Watch State (await confirmation)
Score ≥ 0.85  ──► Immediate Entry (high conviction, no wait)
```

The 0.75 HC gate threshold remains unchanged. The difference:
- **Before UCL:** Signals scoring 0.60–0.74 are permanently blocked.
- **After UCL:** Signals scoring 0.60–0.74 get a second chance through confirmation. If confirmed, their boosted score (typically +0.10 to +0.20) pushes them above 0.75.

### Edge Captured

The HC gate data showed:
- **3,968 blocked signals** at collective PF 1.05
- These signals contain a mix of valid setups and noise
- Confirmation separates the two: valid setups confirm, noise expires
- Expected result: 30–50% of watch states confirm, at PF > 1.5
- The remaining 50–70% expire silently — these were the losers

This is a **pure addition** of captured edge with no degradation of the existing system's performance.
