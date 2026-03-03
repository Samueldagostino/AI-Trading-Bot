# Hardening Backlog

Identified issues to test **individually** against the baseline backtest.
Each item must be a standalone PR verified against the unmodified-code baseline.

---

## 1. HTF Staleness Guards

**Files:** `nq_bot_vscode/features/htf_engine.py`

| # | Issue | Severity | Detail |
|---|-------|----------|--------|
| 1a | Staleness limits hardcoded in engine | MEDIUM | `{"5m": 15, "15m": 45, "30m": 90, ...}` (lines 52-62) should live in `config/constants.py`. |
| 1b | `get_bias()` silently skips staleness check when `timestamp=None` | MEDIUM | Line 92: `if timestamp` guard means a missing timestamp bypasses staleness entirely. Should raise `ValueError`. |
| 1c | `_stale_warned` set never resets | LOW | Once a TF is marked warned, operator never sees another staleness alert for it—even if it stays stale for hours. Reset daily or re-warn every N minutes. |
| 1d | No NaN / inf guard in `_compute_tf_bias()` | LOW | Line 172 guards `avg_range == 0` but doesn't check `isfinite()`. Edge-case NaN could propagate. |

---

## 2. Position State Persistence

**Files:** `nq_bot_vscode/Broker/position_manager.py`

| # | Issue | Severity | Detail |
|---|-------|----------|--------|
| 2a | No schema version field in saved state | MEDIUM | Adding/removing fields (e.g. `position_tags`) will cause `KeyError` on next load with no migration path. |
| 2b | Corrupted JSON is silently discarded | MEDIUM | Lines 654-658 catch `JSONDecodeError`, log it, and return `False`. Corrupted file should be rotated to `.corrupted` for forensics, not deleted. |
| 2c | Orphan positions after partial restore | MEDIUM | If `open_positions` contains IDs not present in `scale_out_groups`, orphan positions can never be closed via group logic. No consistency check on load. |
| 2d | Atomic rename failure silently loses state | LOW | `tmp_path.rename(path)` (line 638) can fail (disk full, permissions). Exception is caught but state is lost. |
| 2e | `_fetch_broker_positions()` returns `[]` on API failure | MEDIUM | Reconciliation then compares 0 vs 0, matches, and misses the actual failure. Should raise or return `None`. |

---

## 3. CandleAggregator / Data Pipeline

**Files:** `nq_bot_vscode/scripts/aggregate_1m.py`, `nq_bot_vscode/data_pipeline/pipeline.py`

| # | Issue | Severity | Detail |
|---|-------|----------|--------|
| 3a | No duplicate / out-of-order detection in 1m CSV | HIGH | Duplicate timestamps (common in replayed data) silently double-count volume in aggregated bars. |
| 3b | Timestamp parsing returns `None` on failure | MEDIUM | Multiple format attempts with silent fallthrough. `None` timestamp proceeds into aggregation—should raise. |
| 3c | `TF_PRIORITY` dict missing guard for unknown TFs | LOW | Adding a new TF (e.g. `"15s"`) causes `KeyError` in sort. |
| 3d | No gap detection in time series | MEDIUM | Missing 1m bars produce partial aggregated bars. No warning logged. |
| 3e | No NaN detection on bar import | HIGH | If a CSV row has empty/NA close, the row is silently skipped. Missing bars go undetected. |

---

## 4. Constants Extraction

**Files:** Various — `htf_engine.py`, `scale_out_executor.py`, `main.py`, `order_executor.py`

| # | Issue | Severity | Detail |
|---|-------|----------|--------|
| 4a | HTF `STRENGTH_GATE = 0.3` lives in `htf_engine.py` | HIGH | `main.py:54` asserts against it with `_EXPECTED_HTF_GATE = 0.3`. Two copies of the same constant—drift risk. Move to `config/constants.py`. |
| 4b | C1/C2 exit parameters are inline literals | HIGH | Profit threshold (3.0 pts), trail distance (2.5 pts), fallback bars (12), C2 target (150 pts), time stop (120 min) in `scale_out_executor.py`. Should be named constants. |
| 4c | HTF staleness limits in engine, not constants | MEDIUM | `{"5m": 15, "15m": 45, ...}` belongs in `config/constants.py`. |
| 4d | Safety constants marked "DO NOT CHANGE" via comment only | MEDIUM | `order_executor.py` lines 39-43: `MAX_CONTRACTS_PER_ORDER=2`, `DAILY_LOSS_LIMIT_DOLLARS=500`. Should be config-gated with runtime assertions. |
| 4e | No startup config summary log | LOW | Operator can't verify thresholds without reading code. Add a single INFO log at startup dumping all policy constants. |

---

## 5. Error Handling & Resilience

**Files:** `Broker/ibkr_client.py`, `execution/orchestrator.py`, `data_pipeline/pipeline.py`

| # | Issue | Severity | Detail |
|---|-------|----------|--------|
| 5a | WebSocket receive timeout = 30 s (hardcoded) | MEDIUM | Network stall → 30 s hang. Should be configurable, shorter default (10 s). |
| 5b | No retry on failed `_process_bar()` | MEDIUM | If one bar fails, pipeline silently skips it. Should retry or circuit-break. |
| 5c | CSV parse errors silently capped at 5 warnings | LOW | After 5 parse errors, remaining errors are suppressed. Add aggregated summary at end. |
| 5d | No exponential backoff / jitter on reconnect | MEDIUM | Reconnect attempts are fixed-interval. Can cause thundering-herd on reconnect. |
| 5e | Paper executor has no retry on order rejection | MEDIUM | If order fails, no retry. Partial fills not recovered. |

---

## 6. Concurrency & Race Conditions

**Files:** `execution/orchestrator.py`

| # | Issue | Severity | Detail |
|---|-------|----------|--------|
| 6a | `_htf_bias` read without lock in execution path | MEDIUM | Lines 269, 562 read shared state without acquiring `_bar_lock`. If HTF bar and exec bar arrive concurrently, data race. |
| 6b | `_on_bar()` sync callback can create multiple event loops | MEDIUM | If multiple bars arrive concurrently, each call to `asyncio.run()` creates a new loop. Use bounded async queue instead. |
| 6c | Lock ownership undocumented | LOW | `_bar_lock` exists but no docstring specifies which fields it protects. |

---

## 7. Startup & Shutdown

**Files:** `execution/orchestrator.py`, `main.py`

| # | Issue | Severity | Detail |
|---|-------|----------|--------|
| 7a | No startup gate for HTF warmup | MEDIUM | Process starts trading immediately. First 60 s has no HTF data → all trades blocked by gate, silently. Should block `start()` until at least 1 bar per TF arrives. |
| 7b | Graceful shutdown doesn't flush pending orders | MEDIUM | `stop()` disconnects immediately. Submitted-but-unfilled orders are abandoned. |
| 7c | No `atexit` / signal handler | MEDIUM | SIGTERM kills process without calling `shutdown()`. Background tasks may leak. |

---

## 8. Logging & Observability

| # | Issue | Severity | Detail |
|---|-------|----------|--------|
| 8a | No structured trade event log (JSONL) | MEDIUM | Trade events are mixed into `ibkr_trading.log` with debug noise. Need separate JSONL file. |
| 8b | Signal rejection reasons not aggregated | LOW | Operator can't see "95 % of signals blocked by HTF gate" without parsing debug logs. Add `get_rejection_summary()`. |
| 8c | Exit reasons are free-text strings | LOW | `"trail_hit"`, `"time_exit"`, `"stop_loss"` — should be an `Enum` for reliable analytics. |
| 8d | HTF staleness "still stale" not re-logged | LOW | (Duplicate of 1c.) Periodic re-warning needed. |

---

## Priority Matrix

| Priority | Items | When |
|----------|-------|------|
| **P0 — Immediate** | 1b, 3a, 3e, 4a, 4b | After baseline established |
| **P1 — Short-term** | 1a, 2a, 2b, 2c, 2e, 4c, 4d, 5b | Week 2-3 |
| **P2 — Medium-term** | 5a, 5d, 5e, 6a, 6b, 7a, 7b, 7c, 8a | Week 4-6 |
| **P3 — Backlog** | 1c, 1d, 2d, 3b, 3c, 3d, 4e, 5c, 6c, 8b, 8c, 8d | Opportunistic |

---

## Testing Protocol

Every item above must be:

1. Branched from the commit that produced the **baseline backtest**.
2. Changed in **isolation** (one item per PR).
3. Full backtest re-run on the changed code.
4. Compared to baseline: PnL, trade count, win rate, max drawdown.
5. Merged only if metrics are **neutral or improved**.
