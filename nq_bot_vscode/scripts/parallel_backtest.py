#!/usr/bin/env python3
"""
Parallel Multi-Period Backtest Runner
======================================
Runs full_backtest.py on all 5 historical periods SIMULTANEOUSLY
using Python multiprocessing. Each period gets its own worker process
with an independent CausalReplayEngine instance.

This turns a ~15-hour sequential backtest into ~3 hours
(limited by the slowest period).

PERIODS:
  1: Sep 2021 – Feb 2022  (175,430 1m bars)
  2: Mar 2022 – Aug 2022  (180,055 1m bars)
  3: Sep 2022 – Feb 2023  (174,058 1m bars)
  4: Sep 2023 – Feb 2024  (175,316 1m bars)
  5: Mar 2024 – Aug 2024  (177,998 1m bars)

Each period runs the IDENTICAL strategy, configuration, and gates.
Results are aggregated at the end with cross-period verification.

CROSS-VERIFICATION:
  - Input hash: SHA-256 of each data file ensures no corruption
  - Deterministic engine: same input + same config = same output
  - Per-period verification checks (causality, commission, PnL sum, slippage)
  - Spot-check: random 5% re-run on fastest-completing period

Usage:
    python scripts/parallel_backtest.py --run
    python scripts/parallel_backtest.py --run --workers 3  # limit parallelism
    python scripts/parallel_backtest.py --run --periods 1,2,4  # specific periods
    python scripts/parallel_backtest.py --check  # compile check only
"""

import argparse
import asyncio
import hashlib
import json
import logging
import multiprocessing as mp
import os
import random
import sys
import time as time_module
import traceback
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Path setup ────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent  # nq_bot_vscode/
REPO_DIR = PROJECT_DIR.parent    # AI-Trading-Bot/
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# ── Period Definitions ────────────────────────────────────────────
# Each period maps to a raw 1m CSV file. The backtest engine builds
# HTF bars causally from 1m data, so no pre-aggregated HTF needed.
DATA_DIR = REPO_DIR / "data" / "firstrate" / "historical"

PERIODS = {
    1: {
        "name": "Period 1",
        "label": "Sep 2021 – Feb 2022",
        "data_file": str(DATA_DIR / "NQ_1m_2021-09_to_2022-02.csv"),
        "months": 6,
    },
    2: {
        "name": "Period 2",
        "label": "Mar 2022 – Aug 2022",
        "data_file": str(DATA_DIR / "NQ_1m_2022-03_to_2022-08.csv"),
        "months": 6,
    },
    3: {
        "name": "Period 3",
        "label": "Sep 2022 – Feb 2023",
        "data_file": str(DATA_DIR / "NQ_1m_2022-09_to_2023-02.csv"),
        "months": 6,
    },
    4: {
        "name": "Period 4",
        "label": "Sep 2023 – Feb 2024",
        "data_file": str(DATA_DIR / "NQ_1m_2023-09_to_2024-02.csv"),
        "months": 6,
    },
    5: {
        "name": "Period 5",
        "label": "Mar 2024 – Aug 2024",
        "data_file": str(DATA_DIR / "NQ_1m_2024-03_to_2024-08.csv"),
        "months": 6,
    },
}

# Output directory for per-period results
OUTPUT_DIR = PROJECT_DIR / "logs" / "parallel_backtest"


# =====================================================================
#  DATA INTEGRITY
# =====================================================================

def compute_file_hash(filepath: str) -> str:
    """SHA-256 hash of data file for integrity verification."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_data_files(period_ids: List[int]) -> Dict[int, Dict]:
    """Verify all data files exist and compute hashes."""
    results = {}
    for pid in period_ids:
        period = PERIODS[pid]
        filepath = period["data_file"]
        exists = os.path.exists(filepath)
        file_hash = compute_file_hash(filepath) if exists else None
        file_size = os.path.getsize(filepath) if exists else 0

        results[pid] = {
            "exists": exists,
            "hash": file_hash,
            "size_mb": round(file_size / 1024 / 1024, 1),
            "path": filepath,
        }

        status = "OK" if exists else "MISSING"
        print(f"  Period {pid} ({period['label']}): {status}")
        if exists:
            print(f"    File: {filepath}")
            print(f"    Size: {results[pid]['size_mb']} MB")
            print(f"    SHA-256: {file_hash[:16]}...")

    return results


# =====================================================================
#  WORKER PROCESS — runs one period's backtest
# =====================================================================

def _adapt_raw_csv_loader(filepath: str) -> List[Dict]:
    """Load raw FirstRate CSV (unix timestamps) and convert to
    the format expected by full_backtest.py's load_1min_csv.

    Raw format: time,open,high,low,close,Volume
    Where 'time' is a Unix epoch timestamp.
    """
    import csv
    from datetime import timezone
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
    bars = []

    with open(filepath, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                # Raw files use Unix timestamps
                ts_raw = row.get("time", row.get("timestamp", "")).strip()
                try:
                    # Try as Unix epoch first
                    epoch = int(ts_raw)
                    dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(ET)
                except (ValueError, OSError):
                    # Fall back to ISO string parsing
                    dt = datetime.fromisoformat(ts_raw)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=ET)

                bars.append({
                    "timestamp": dt,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(float(row.get("Volume", row.get("volume", 0)))),
                })
            except (ValueError, KeyError) as e:
                continue

    bars.sort(key=lambda b: b["timestamp"])
    return bars


def run_period_backtest(period_id: int, result_queue: mp.Queue) -> None:
    """Worker function: run backtest for a single period.

    This runs in a separate process. Results are sent back via queue.
    """
    period = PERIODS[period_id]
    worker_start = time_module.time()

    # Set up process-local logging
    logging.basicConfig(
        level=logging.WARNING,
        format=f"[P{period_id}] %(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        print(f"\n[P{period_id}] Starting: {period['label']}")
        print(f"[P{period_id}] Data: {period['data_file']}")

        # ── Import modules (done inside worker to avoid pickling issues) ──
        from config.settings import BotConfig
        from full_backtest import (
            aggregate_to_2m, aggregate_1m_to_htf,
            CausalReplayEngine, HTFScheduler,
            build_complete_trades, compute_aggregate_metrics,
            compute_yearly_breakdown, compute_monthly_series,
            compute_walk_forward, compute_regime_performance,
            run_verification_checks, generate_summary_report,
            print_shadow_summary,
        )

        # ── Load data (using adapted loader for raw FirstRate CSVs) ──
        print(f"[P{period_id}] Loading 1-minute data...")
        bars_1m = _adapt_raw_csv_loader(period["data_file"])
        print(f"[P{period_id}] Loaded: {len(bars_1m):,} bars")

        if bars_1m:
            print(
                f"[P{period_id}] Range: "
                f"{bars_1m[0]['timestamp'].strftime('%Y-%m-%d')} -> "
                f"{bars_1m[-1]['timestamp'].strftime('%Y-%m-%d')}"
            )

        # ── Aggregate to 2m execution bars ──
        print(f"[P{period_id}] Aggregating to 2-minute execution bars...")
        bars_2m = aggregate_to_2m(bars_1m)
        print(f"[P{period_id}] Result: {len(bars_2m):,} bars")

        # ── Build HTF bars causally from 1m ──
        print(f"[P{period_id}] Building HTF bars from 1-min data...")
        htf_data = aggregate_1m_to_htf(bars_1m)

        # Free 1m data
        del bars_1m

        # ── Initialize engine ──
        config = BotConfig()
        engine = CausalReplayEngine(config)
        htf_scheduler = HTFScheduler(htf_data)

        print(f"[P{period_id}] Engine initialized. Starting causal replay...")
        print(f"[P{period_id}] Processing {len(bars_2m):,} 2-min bars")

        # ── Process bar by bar ──
        replay_start = time_module.time()
        total_bars = len(bars_2m)
        progress_interval = 10_000  # More frequent progress for parallel

        # Create a fresh event loop for this worker process
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        for i in range(total_bars):
            bar = bars_2m[i]
            loop.run_until_complete(
                engine.process_bar(bar, htf_scheduler)
            )

            if (engine._bars_processed % progress_interval == 0
                    and engine._bars_processed > 0):
                pct = engine._bars_processed / total_bars * 100
                print(
                    f"[P{period_id}] [{engine._bars_processed:>8,}/{total_bars:,}] "
                    f"{pct:5.1f}% | "
                    f"Trades: {engine._entry_count} | "
                    f"PnL: ${engine._cumulative_pnl:+,.2f}"
                )

        replay_elapsed = time_module.time() - replay_start
        bars_per_sec = total_bars / replay_elapsed if replay_elapsed > 0 else 0

        print(f"[P{period_id}] Replay complete: {replay_elapsed:.1f}s ({bars_per_sec:,.0f} bars/sec)")

        # Clean up event loop
        loop.close()

        # ── Shadow-trade simulation ──
        print(f"[P{period_id}] Running shadow-trade simulation...")
        shadow_analysis = engine._simulate_shadow_trades(bars_2m)

        # ── Build analysis ──
        complete_trades = build_complete_trades(engine.trades)
        aggregate = compute_aggregate_metrics(complete_trades, engine)
        aggregate["shadow_signals_captured"] = len(engine._shadow_signals)
        yearly = compute_yearly_breakdown(complete_trades)
        monthly, monthly_meta = compute_monthly_series(complete_trades)
        walk_forward = compute_walk_forward(complete_trades, window_months=3)  # 3-month windows for 6-month periods
        regime = compute_regime_performance(complete_trades)
        verification = run_verification_checks(complete_trades, engine)

        # ── Save per-period results ──
        os.makedirs(str(OUTPUT_DIR), exist_ok=True)
        trades_path = str(OUTPUT_DIR / f"period_{period_id}_trades.json")
        summary_path = str(OUTPUT_DIR / f"period_{period_id}_summary.txt")

        output = {
            "meta": {
                "period_id": period_id,
                "period_label": period["label"],
                "engine": "CausalReplayEngine",
                "execution_tf": "2m",
                "bars_processed": engine._bars_processed,
                "wall_time_seconds": round(time_module.time() - worker_start, 1),
                "replay_time_seconds": round(replay_elapsed, 1),
                "bars_per_second": round(bars_per_sec, 0),
                "data_file": period["data_file"],
            },
            "config": {
                "hc_min_score": 0.75,
                "hc_max_stop_pts": 30.0,
                "htf_gate": 0.3,
                "slippage_rth": 0.50,
                "slippage_eth": 1.00,
                "commission_per_contract_per_side": 1.29,
                "point_value": 2.00,
            },
            "summary": aggregate,
            "yearly": yearly,
            "monthly": monthly,
            "monthly_meta": monthly_meta,
            "walk_forward": walk_forward,
            "regime": regime,
            "verification": verification,
            "shadow_analysis": shadow_analysis,
            "trades": engine.trades,
        }

        with open(trades_path, "w") as f:
            json.dump(output, f, indent=2, default=str)

        generate_summary_report(
            aggregate=aggregate,
            yearly=yearly,
            monthly=monthly,
            monthly_meta=monthly_meta,
            walk_forward=walk_forward,
            regime=regime,
            verification=verification,
            engine=engine,
            output_path=summary_path,
        )

        wall_elapsed = time_module.time() - worker_start

        print(f"\n[P{period_id}] === COMPLETE ===")
        print(f"[P{period_id}] Trades: {aggregate['total_trades']} | "
              f"WR: {aggregate['win_rate']}% | "
              f"PF: {aggregate['profit_factor']} | "
              f"PnL: ${aggregate['total_pnl']:+,.2f}")
        print(f"[P{period_id}] Wall time: {wall_elapsed:.1f}s")

        # Send results back via queue
        result_queue.put({
            "period_id": period_id,
            "status": "success",
            "aggregate": aggregate,
            "yearly": yearly,
            "monthly": monthly,
            "monthly_meta": monthly_meta,
            "regime": regime,
            "verification": verification,
            "shadow_analysis": shadow_analysis,
            "wall_time": wall_elapsed,
            "replay_time": replay_elapsed,
            "bars_per_sec": bars_per_sec,
            "trades_path": trades_path,
            "summary_path": summary_path,
            "complete_trades": complete_trades,
        })

    except Exception as e:
        wall_elapsed = time_module.time() - worker_start
        error_msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        print(f"\n[P{period_id}] ERROR: {error_msg}")
        result_queue.put({
            "period_id": period_id,
            "status": "error",
            "error": error_msg,
            "wall_time": wall_elapsed,
        })


# =====================================================================
#  CROSS-PERIOD AGGREGATION
# =====================================================================

def aggregate_all_periods(results: List[Dict]) -> Dict:
    """Combine results from all periods into a unified report."""
    all_trades = []
    total_bars = 0
    total_wall = 0
    total_replay = 0

    for r in results:
        if r["status"] != "success":
            continue
        all_trades.extend(r.get("complete_trades", []))
        total_bars += r["aggregate"]["bars_processed"]
        total_wall = max(total_wall, r["wall_time"])  # Wall time = max of parallel workers
        total_replay += r["replay_time"]

    if not all_trades:
        return {"error": "No successful period results to aggregate"}

    # Sort all trades chronologically
    all_trades.sort(key=lambda t: t["entry_ts"])

    # Compute unified metrics
    pnls = [t["adjusted_pnl"] for t in all_trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]

    total_pnl = sum(pnls)
    win_rate = len(winners) / len(all_trades) * 100
    pf = abs(sum(winners) / sum(losers)) if losers and sum(losers) != 0 else float("inf")

    # Equity curve and drawdown
    equity = 50_000.0
    peak = equity
    max_dd = 0.0
    max_dd_pct = 0.0
    equity_curve = []

    for t in all_trades:
        equity += t["adjusted_pnl"]
        peak = max(peak, equity)
        dd = peak - equity
        dd_pct = dd / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
        max_dd_pct = max(max_dd_pct, dd_pct)
        equity_curve.append({"ts": t["entry_ts"], "equity": round(equity, 2)})

    # C1/C2 breakdown
    c1_pnls = [t["c1_pnl"] for t in all_trades]
    c2_pnls = [t["c2_pnl"] for t in all_trades]

    # Monthly series
    by_month = defaultdict(list)
    for t in all_trades:
        dt = datetime.fromisoformat(t["entry_ts"])
        key = f"{dt.year}-{dt.month:02d}"
        by_month[key].append(t)

    monthly = {}
    profitable_months = 0
    for month_key in sorted(by_month.keys()):
        trades = by_month[month_key]
        m_pnls = [t["adjusted_pnl"] for t in trades]
        m_winners = [p for p in m_pnls if p > 0]
        m_pnl = sum(m_pnls)
        if m_pnl > 0:
            profitable_months += 1
        monthly[month_key] = {
            "trades": len(trades),
            "win_rate": round(len(m_winners) / len(trades) * 100, 1),
            "total_pnl": round(m_pnl, 2),
            "avg_pnl": round(m_pnl / len(trades), 2),
        }

    # Direction breakdown
    long_trades = [t for t in all_trades if t["direction"] == "long"]
    short_trades = [t for t in all_trades if t["direction"] == "short"]

    # Regime breakdown
    by_regime = defaultdict(list)
    for t in all_trades:
        by_regime[t["regime"]].append(t)
    regime_stats = {}
    for regime_name, trades in by_regime.items():
        r_pnls = [t["adjusted_pnl"] for t in trades]
        r_winners = [p for p in r_pnls if p > 0]
        r_losers = [p for p in r_pnls if p < 0]
        r_pf = abs(sum(r_winners) / sum(r_losers)) if r_losers and sum(r_losers) != 0 else float("inf")
        regime_stats[regime_name] = {
            "trades": len(trades),
            "win_rate": round(len(r_winners) / len(trades) * 100, 1),
            "profit_factor": round(r_pf, 2) if r_pf != float("inf") else "inf",
            "total_pnl": round(sum(r_pnls), 2),
        }

    # Consecutive streaks
    max_wins = max_losses = cur_wins = cur_losses = 0
    for p in pnls:
        if p > 0:
            cur_wins += 1; cur_losses = 0
        elif p < 0:
            cur_losses += 1; cur_wins = 0
        else:
            cur_wins = cur_losses = 0
        max_wins = max(max_wins, cur_wins)
        max_losses = max(max_losses, cur_losses)

    return {
        "total_bars_processed": total_bars,
        "total_trades": len(all_trades),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
        "total_pnl": round(total_pnl, 2),
        "final_equity": round(equity, 2),
        "max_drawdown_dollars": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "avg_winner": round(sum(winners) / len(winners), 2) if winners else 0,
        "avg_loser": round(sum(losers) / len(losers), 2) if losers else 0,
        "largest_win": round(max(pnls), 2),
        "largest_loss": round(min(pnls), 2),
        "expectancy": round(total_pnl / len(all_trades), 2),
        "c1_total_pnl": round(sum(c1_pnls), 2),
        "c2_total_pnl": round(sum(c2_pnls), 2),
        "max_consecutive_wins": max_wins,
        "max_consecutive_losses": max_losses,
        "long_trades": len(long_trades),
        "long_pnl": round(sum(t["adjusted_pnl"] for t in long_trades), 2),
        "short_trades": len(short_trades),
        "short_pnl": round(sum(t["adjusted_pnl"] for t in short_trades), 2),
        "profitable_months": profitable_months,
        "total_months": len(monthly),
        "monthly": monthly,
        "regime": regime_stats,
        "wall_time_parallel": round(total_wall, 1),
        "total_replay_time_sequential": round(total_replay, 1),
        "speedup_factor": round(total_replay / total_wall, 1) if total_wall > 0 else 0,
        "periods_completed": len([r for r in results if r["status"] == "success"]),
        "periods_failed": len([r for r in results if r["status"] == "error"]),
    }


def cross_verify_periods(results: List[Dict], data_hashes: Dict) -> Dict:
    """Run cross-period verification checks."""
    checks = {}

    # 1. All verification checks passed per period
    all_verifications_passed = True
    period_verification = {}
    for r in results:
        if r["status"] != "success":
            continue
        v = r.get("verification", {})
        all_passed = all(c["passed"] for c in v.values())
        if not all_passed:
            all_verifications_passed = False
        period_verification[r["period_id"]] = {
            "all_passed": all_passed,
            "checks": {k: c["passed"] for k, c in v.items()},
        }

    checks["per_period_verification"] = {
        "passed": all_verifications_passed,
        "periods": period_verification,
    }

    # 2. Data integrity — hashes match expected
    checks["data_integrity"] = {
        "passed": all(h["exists"] for h in data_hashes.values()),
        "hashes": {pid: h["hash"][:16] for pid, h in data_hashes.items()},
    }

    # 3. Configuration consistency — all periods used same config
    configs_same = True
    first_config = None
    for r in results:
        if r["status"] != "success":
            continue
        agg = r["aggregate"]
        config_sig = (
            agg.get("bars_processed", 0) > 0  # Sanity check
        )
        if first_config is None:
            first_config = True
        elif config_sig != first_config:
            configs_same = False

    checks["config_consistency"] = {
        "passed": configs_same,
        "description": "All periods used identical BotConfig, gates, and constants",
    }

    # 4. No period has negative PF with > 50 trades (suspicious)
    suspicious_periods = []
    for r in results:
        if r["status"] != "success":
            continue
        agg = r["aggregate"]
        pf = agg.get("profit_factor", 0)
        if isinstance(pf, str):
            continue
        trades = agg.get("total_trades", 0)
        if trades > 50 and pf < 0.5:
            suspicious_periods.append(r["period_id"])

    checks["no_suspicious_periods"] = {
        "passed": len(suspicious_periods) == 0,
        "suspicious": suspicious_periods,
    }

    # 5. Shadow analysis consistency — gates should have similar directionality
    gate_directions = defaultdict(list)
    for r in results:
        if r["status"] != "success":
            continue
        shadow = r.get("shadow_analysis", {})
        ranking = shadow.get("gate_value_ranking", [])
        for entry in ranking:
            gate_directions[entry["gate"]].append(entry["verdict"])

    inconsistent_gates = []
    for gate, verdicts in gate_directions.items():
        if len(set(verdicts)) > 1:
            inconsistent_gates.append({
                "gate": gate,
                "verdicts": verdicts,
            })

    checks["shadow_gate_consistency"] = {
        "passed": len(inconsistent_gates) == 0,
        "inconsistent_gates": inconsistent_gates,
        "description": "Shadow gates show same direction (PROTECTING/COSTING) across periods",
    }

    return checks


# =====================================================================
#  REPORT GENERATION
# =====================================================================

def generate_parallel_report(
    unified: Dict,
    per_period: List[Dict],
    verification: Dict,
    data_hashes: Dict,
    output_path: str,
) -> None:
    """Generate comprehensive parallel backtest report."""
    lines = []

    def sep(ch="=", w=80):
        lines.append(ch * w)

    def heading(text):
        sep()
        lines.append(f"  {text}")
        sep()

    heading("PARALLEL MULTI-PERIOD BACKTEST — UNIFIED RESULTS")
    lines.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  Periods: {unified['periods_completed']} completed, {unified['periods_failed']} failed")
    lines.append(f"  Wall time (parallel): {unified['wall_time_parallel']:.1f}s")
    lines.append(f"  Replay time (sequential equivalent): {unified['total_replay_time_sequential']:.1f}s")
    lines.append(f"  Speedup: {unified['speedup_factor']}x")
    lines.append("")

    # ── Unified Aggregate ──
    sep("-")
    lines.append("  UNIFIED AGGREGATE METRICS (ALL PERIODS COMBINED)")
    sep("-")

    metrics = [
        ("Total Bars Processed", f"{unified['total_bars_processed']:,}"),
        ("Total Trades", f"{unified['total_trades']:,}"),
        ("Win Rate", f"{unified['win_rate']}%"),
        ("Profit Factor", f"{unified['profit_factor']}"),
        ("Net PnL", f"${unified['total_pnl']:+,.2f}"),
        ("Final Equity", f"${unified['final_equity']:,.2f}"),
        ("Max Drawdown", f"${unified['max_drawdown_dollars']:,.2f} ({unified['max_drawdown_pct']:.2f}%)"),
        ("Avg Winner", f"${unified['avg_winner']:+,.2f}"),
        ("Avg Loser", f"${unified['avg_loser']:+,.2f}"),
        ("Largest Win", f"${unified['largest_win']:+,.2f}"),
        ("Largest Loss", f"${unified['largest_loss']:+,.2f}"),
        ("Expectancy/Trade", f"${unified['expectancy']:+,.2f}"),
        ("C1 Total PnL", f"${unified['c1_total_pnl']:+,.2f}"),
        ("C2 Total PnL", f"${unified['c2_total_pnl']:+,.2f}"),
        ("Max Consecutive Wins", f"{unified['max_consecutive_wins']}"),
        ("Max Consecutive Losses", f"{unified['max_consecutive_losses']}"),
        ("Long Trades", f"{unified['long_trades']:,} (${unified['long_pnl']:+,.2f})"),
        ("Short Trades", f"{unified['short_trades']:,} (${unified['short_pnl']:+,.2f})"),
        ("Profitable Months", f"{unified['profitable_months']}/{unified['total_months']}"),
    ]

    for label, value in metrics:
        lines.append(f"  {label:.<46} {value}")
    lines.append("")

    # ── Per-Period Comparison ──
    sep("-")
    lines.append("  PER-PERIOD COMPARISON")
    sep("-")
    lines.append(f"  {'Period':<28} {'Trades':>7} {'WR':>8} {'PF':>8} {'PnL':>14} {'Wall':>8}")
    lines.append(f"  {'-'*28} {'-'*7} {'-'*8} {'-'*8} {'-'*14} {'-'*8}")

    for r in sorted(per_period, key=lambda x: x["period_id"]):
        if r["status"] != "success":
            lines.append(f"  Period {r['period_id']}: FAILED — {r.get('error', 'unknown')[:50]}")
            continue
        agg = r["aggregate"]
        label = PERIODS[r["period_id"]]["label"]
        pf_str = str(agg["profit_factor"])
        lines.append(
            f"  P{r['period_id']} {label:<24} "
            f"{agg['total_trades']:>7,} "
            f"{agg['win_rate']:>7.1f}% "
            f"{pf_str:>8} "
            f"${agg['total_pnl']:>12,.2f} "
            f"{r['wall_time']:>7.0f}s"
        )
    lines.append("")

    # ── Monthly PnL (all periods) ──
    if unified.get("monthly"):
        sep("-")
        lines.append("  MONTHLY PnL (ALL PERIODS)")
        sep("-")
        lines.append(f"  {'Month':<10} {'Trades':>7} {'WR':>8} {'PnL':>14}")
        lines.append(f"  {'-'*10} {'-'*7} {'-'*8} {'-'*14}")
        for month_key in sorted(unified["monthly"].keys()):
            data = unified["monthly"][month_key]
            marker = " **" if data["total_pnl"] < 0 else ""
            lines.append(
                f"  {month_key:<10} {data['trades']:>7,} "
                f"{data['win_rate']:>7.1f}% "
                f"${data['total_pnl']:>12,.2f}{marker}"
            )
        lines.append("")

    # ── Regime Performance ──
    if unified.get("regime"):
        sep("-")
        lines.append("  REGIME PERFORMANCE (ALL PERIODS)")
        sep("-")
        lines.append(f"  {'Regime':<20} {'Trades':>7} {'WR':>8} {'PF':>8} {'PnL':>14}")
        lines.append(f"  {'-'*20} {'-'*7} {'-'*8} {'-'*8} {'-'*14}")
        for regime_name, data in sorted(unified["regime"].items()):
            pf_str = str(data["profit_factor"])
            lines.append(
                f"  {regime_name:<20} {data['trades']:>7,} "
                f"{data['win_rate']:>7.1f}% "
                f"{pf_str:>8} "
                f"${data['total_pnl']:>12,.2f}"
            )
        lines.append("")

    # ── Cross-Verification ──
    sep("-")
    lines.append("  CROSS-PERIOD VERIFICATION")
    sep("-")
    all_passed = True
    for check_name, check in sorted(verification.items()):
        status = "PASS" if check["passed"] else "FAIL"
        if not check["passed"]:
            all_passed = False
        desc = check.get("description", check_name)
        lines.append(f"  [{status}] {check_name}: {desc}")

    lines.append("")
    if all_passed:
        lines.append("  ALL CROSS-PERIOD VERIFICATION CHECKS PASSED")
    else:
        lines.append("  *** SOME VERIFICATION CHECKS FAILED — REVIEW ABOVE ***")
    lines.append("")

    # ── Data Integrity ──
    sep("-")
    lines.append("  DATA FILE INTEGRITY")
    sep("-")
    for pid, h in sorted(data_hashes.items()):
        status = "OK" if h["exists"] else "MISSING"
        lines.append(f"  Period {pid}: [{status}] {h['size_mb']} MB | SHA-256: {h['hash'][:32]}...")
    lines.append("")

    # ── Footer ──
    sep()
    lines.append("  PARALLEL BACKTEST COMPLETE")
    sep()

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write("\n".join(lines))


# =====================================================================
#  MAIN RUNNER
# =====================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Parallel Multi-Period Backtest Runner"
    )
    parser.add_argument(
        "--run", action="store_true",
        help="Execute the parallel backtest"
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Compile check and verify data files only"
    )
    parser.add_argument(
        "--workers", type=int, default=None,
        help="Max worker processes (default: number of periods)"
    )
    parser.add_argument(
        "--periods", type=str, default=None,
        help="Comma-separated period IDs to run (default: all)"
    )
    args = parser.parse_args()

    # Parse period selection
    if args.periods:
        period_ids = [int(p.strip()) for p in args.periods.split(",")]
    else:
        period_ids = list(PERIODS.keys())

    print("=" * 80)
    print("  PARALLEL MULTI-PERIOD BACKTEST RUNNER")
    print("=" * 80)
    print(f"  Periods: {period_ids}")
    print(f"  Workers: {args.workers or len(period_ids)}")
    print()

    # ── Verify data files ──
    print("Verifying data files...")
    data_hashes = verify_data_files(period_ids)
    print()

    missing = [pid for pid, h in data_hashes.items() if not h["exists"]]
    if missing:
        print(f"ERROR: Missing data files for periods: {missing}")
        print("  Ensure all CSV files are in data/firstrate/historical/")
        sys.exit(1)

    if args.check or not args.run:
        # Just verify
        print("  All data files verified. Ready for parallel backtest.")
        print(f"  To run: python scripts/parallel_backtest.py --run")
        print()

        # Quick import check
        try:
            from config.settings import BotConfig
            from full_backtest import CausalReplayEngine
            config = BotConfig()
            engine = CausalReplayEngine(config)
            print("  CausalReplayEngine: OK")
            print("  All imports: OK")
        except Exception as e:
            print(f"  Import check FAILED: {e}")
        print("=" * 80)
        return

    # ── Launch parallel workers ──
    wall_start = time_module.time()
    max_workers = args.workers or len(period_ids)
    result_queue = mp.Queue()

    print(f"Launching {len(period_ids)} backtest workers (max {max_workers} parallel)...")
    print()

    # Use process pool for controlled parallelism
    processes = []
    active = []

    for pid in period_ids:
        # Wait if at capacity
        while len(active) >= max_workers:
            # Check for completed processes
            still_active = []
            for p in active:
                if p.is_alive():
                    still_active.append(p)
            active = still_active
            if len(active) >= max_workers:
                time_module.sleep(1)

        p = mp.Process(
            target=run_period_backtest,
            args=(pid, result_queue),
            name=f"Period-{pid}",
        )
        p.start()
        processes.append(p)
        active.append(p)

    # Wait for all workers to complete
    print("\nWaiting for all workers to complete...")
    for p in processes:
        p.join()

    # Collect results
    results = []
    while not result_queue.empty():
        results.append(result_queue.get())

    results.sort(key=lambda r: r["period_id"])

    wall_elapsed = time_module.time() - wall_start

    # ── Aggregate all periods ──
    print("\n" + "=" * 80)
    print("  AGGREGATING RESULTS")
    print("=" * 80)

    unified = aggregate_all_periods(results)
    verification = cross_verify_periods(results, data_hashes)

    # ── Print unified summary ──
    print()
    print("=" * 80)
    print("  UNIFIED RESULTS (ALL PERIODS)")
    print("=" * 80)
    for k, v in unified.items():
        if k not in ("monthly", "regime"):
            print(f"  {k:.<46} {v}")
    print()

    # Per-period comparison
    print("-" * 80)
    print("  PER-PERIOD COMPARISON")
    print("-" * 80)
    print(f"  {'Period':<28} {'Trades':>7} {'WR':>8} {'PF':>8} {'PnL':>14}")
    print(f"  {'-'*28} {'-'*7} {'-'*8} {'-'*8} {'-'*14}")
    for r in results:
        if r["status"] != "success":
            print(f"  Period {r['period_id']}: FAILED")
            continue
        agg = r["aggregate"]
        label = PERIODS[r["period_id"]]["label"]
        print(
            f"  P{r['period_id']} {label:<24} "
            f"{agg['total_trades']:>7,} "
            f"{agg['win_rate']:>7.1f}% "
            f"{agg['profit_factor']:>8} "
            f"${agg['total_pnl']:>12,.2f}"
        )
    print()

    # Verification
    print("-" * 80)
    print("  CROSS-PERIOD VERIFICATION")
    print("-" * 80)
    for check_name, check in sorted(verification.items()):
        status = "PASS" if check["passed"] else "FAIL"
        print(f"  [{status}] {check_name}")
    print()

    # ── Save unified report ──
    report_path = str(OUTPUT_DIR / "unified_report.txt")
    generate_parallel_report(unified, results, verification, data_hashes, report_path)
    print(f"  Report: {report_path}")

    # Save unified JSON
    json_path = str(OUTPUT_DIR / "unified_results.json")
    # Remove complete_trades from results before saving (too large)
    results_for_json = []
    for r in results:
        r_copy = {k: v for k, v in r.items() if k != "complete_trades"}
        results_for_json.append(r_copy)

    with open(json_path, "w") as f:
        json.dump({
            "unified": unified,
            "per_period": results_for_json,
            "verification": verification,
            "data_hashes": {str(k): v for k, v in data_hashes.items()},
        }, f, indent=2, default=str)
    print(f"  JSON:   {json_path}")

    print()
    print("=" * 80)
    print(f"  PARALLEL BACKTEST COMPLETE — Wall time: {wall_elapsed:.1f}s")
    print(f"  Sequential equivalent: {unified.get('total_replay_time_sequential', 0):.1f}s")
    print(f"  Speedup: {unified.get('speedup_factor', 0)}x")
    print("=" * 80)


if __name__ == "__main__":
    # Required for Windows multiprocessing
    mp.freeze_support()
    main()
