"""
Weekly Breakdown Generator
============================
Re-runs the full production pipeline (ReplaySimulator) on all 6 periods
and groups trade-level data by ISO calendar week (Mon-Fri).

Outputs: docs/weekly_breakdown.json

Usage:
    python scripts/generate_weekly_breakdown.py
"""

import asyncio
import json
import sys
import time
from collections import OrderedDict
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Dict, List

# Project paths
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
PROJECT_DIR = REPO_ROOT / "nq_bot_vscode"
OUTPUT_PATH = REPO_ROOT / "docs" / "weekly_breakdown.json"

sys.path.insert(0, str(PROJECT_DIR))

from scripts.replay_simulator import ReplaySimulator

# ── Period definitions (mirrors multi_period_backtest.py) ──
PERIODS = OrderedDict([
    ("period_1", {
        "label": "Sep 2021 - Feb 2022",
        "data_dir": str(REPO_ROOT / "data" / "firstrate" / "historical" / "aggregated" / "period_1_2021-09_to_2022-02"),
        "start": "2021-09-01", "end": "2022-03-01",
    }),
    ("period_2", {
        "label": "Mar 2022 - Aug 2022",
        "data_dir": str(REPO_ROOT / "data" / "firstrate" / "historical" / "aggregated" / "period_2_2022-03_to_2022-08"),
        "start": "2022-03-01", "end": "2022-09-01",
    }),
    ("period_3", {
        "label": "Sep 2022 - Aug 2023",
        "data_dir": str(REPO_ROOT / "data" / "firstrate" / "historical" / "aggregated" / "period_3_2022-09_to_2023-08"),
        "start": "2022-09-01", "end": "2023-09-01",
    }),
    ("period_4", {
        "label": "Sep 2023 - Feb 2024",
        "data_dir": str(REPO_ROOT / "data" / "firstrate" / "historical" / "aggregated" / "period_4_2023-09_to_2024-02"),
        "start": "2023-09-01", "end": "2024-03-01",
    }),
    ("period_5", {
        "label": "Mar 2024 - Aug 2024",
        "data_dir": str(REPO_ROOT / "data" / "firstrate" / "historical" / "aggregated" / "period_5_2024-03_to_2024-08"),
        "start": "2024-03-01", "end": "2024-09-01",
    }),
    ("period_6", {
        "label": "Sep 2025 - Feb 2026",
        "data_dir": str(PROJECT_DIR / "data" / "firstrate"),
        "start": "2025-09-01", "end": "2026-03-01",
    }),
])


def iso_week_bounds(d: date):
    """Return (Monday, Friday) of the ISO week containing date d."""
    monday = d - timedelta(days=d.weekday())
    friday = monday + timedelta(days=4)
    return monday, friday


def group_trades_by_week(trades_log: List[dict], period_id: str) -> List[dict]:
    """Group closed trades by ISO calendar week and compute per-week stats."""
    # Bucket trades by (year, iso_week)
    buckets: Dict[tuple, list] = {}
    for t in trades_log:
        ts_str = t.get("timestamp", "")
        if not ts_str:
            continue
        try:
            dt = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            continue
        d = dt.date()
        iso_year, iso_week, _ = d.isocalendar()
        key = (iso_year, iso_week)
        buckets.setdefault(key, []).append(t)

    weeks = []
    for (iso_year, iso_week), bucket in sorted(buckets.items()):
        # Determine week bounds from first trade's date
        sample_ts = bucket[0].get("timestamp", "")
        sample_date = datetime.fromisoformat(sample_ts).date()
        mon, fri = iso_week_bounds(sample_date)

        total_pnl = 0.0
        c1_pnl = 0.0
        c2_pnl = 0.0
        wins = 0
        gross_profit = 0.0
        gross_loss = 0.0
        sweep_trades = 0
        sweep_pnl = 0.0
        # Track equity for max drawdown
        equity = 0.0
        peak = 0.0
        max_dd_pct = 0.0

        for t in bucket:
            pnl = t.get("total_pnl", 0)
            total_pnl += pnl
            c1_pnl += t.get("c1_pnl", 0)
            c2_pnl += t.get("c2_pnl", 0)
            if pnl > 0:
                wins += 1
                gross_profit += pnl
            else:
                gross_loss += abs(pnl)

            src = t.get("signal_source", "signal")
            if src == "sweep":
                sweep_trades += 1
                sweep_pnl += pnl

            # Intra-week drawdown (from cumulative PnL)
            equity += pnl
            if equity > peak:
                peak = equity
            if peak > 0:
                dd = (peak - equity) / peak * 100
                if dd > max_dd_pct:
                    max_dd_pct = dd

        n = len(bucket)
        wr = round(wins / n * 100, 1) if n > 0 else 0.0
        pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (
            999.99 if gross_profit > 0 else 0.0)
        exp = round(total_pnl / n, 2) if n > 0 else 0.0

        weeks.append({
            "period": period_id,
            "year": mon.year,
            "month": mon.month,
            "week_start": mon.isoformat(),
            "week_end": fri.isoformat(),
            "trades": n,
            "wins": wins,
            "win_rate": wr,
            "pf": pf,
            "pnl": round(total_pnl, 2),
            "c1_pnl": round(c1_pnl, 2),
            "c2_pnl": round(c2_pnl, 2),
            "max_dd_pct": round(max_dd_pct, 1),
            "expectancy": exp,
            "sweep_trades": sweep_trades,
            "sweep_pnl": round(sweep_pnl, 2),
        })

    return weeks


def load_period6_cached() -> List[dict]:
    """Load period 6 trade-level data from paper_trades.json (if dates match).

    NOTE: paper_trades.json must contain trades in the period 6 date range
    (Sep 2025+). If it contains stale data from another period, we skip it
    and fall through to re-run the simulator.
    """
    paper_path = PROJECT_DIR / "logs" / "paper_trades.json"
    if paper_path.exists():
        with open(str(paper_path)) as f:
            trades = json.load(f)
        if trades and len(trades) > 1000:
            # Validate date range — period 6 trades must be 2025+
            sample_ts = trades[0].get("timestamp", "")
            if sample_ts and sample_ts.startswith("2025"):
                return trades
            print(f"[WARN] paper_trades.json has wrong dates ({sample_ts[:10]}), skipping cache")

    return []


async def run_period(period_id: str, period: dict) -> List[dict]:
    """Run backtest for a single period, return trades_log."""
    data_dir = period["data_dir"]
    if not Path(data_dir).exists():
        print(f"  ERROR: Data dir missing: {data_dir}")
        return []

    nq_2m = Path(data_dir) / "NQ_2m.csv"
    if not nq_2m.exists():
        print(f"  ERROR: NQ_2m.csv missing in {data_dir}")
        return []

    sim = ReplaySimulator(
        speed="max",
        start_date=period.get("start"),
        end_date=period.get("end"),
        validate=True,
        data_dir=data_dir,
        c1_variant="C",
        quiet=True,
        sweep_enabled=True,
    )

    await sim.run()
    return sim.state.trades_log


async def main():
    print(f"\n{'#' * 60}")
    print(f"  WEEKLY BREAKDOWN GENERATOR")
    print(f"  Re-running all 6 periods for trade-level weekly grouping")
    print(f"{'#' * 60}\n")

    all_weeks = []
    total_t0 = time.time()

    for pid, period in PERIODS.items():
        print(f"  [{pid}] {period['label']} ...", end=" ", flush=True)
        t0 = time.time()

        # For period 6, try using cached trade data first
        if pid == "period_6":
            trades = load_period6_cached()
            if trades and len(trades) > 1000:
                weeks = group_trades_by_week(trades, pid)
                elapsed = time.time() - t0
                print(f"{len(trades)} trades -> {len(weeks)} weeks ({elapsed:.1f}s) [cached]")
                all_weeks.extend(weeks)
                continue

        trades = await run_period(pid, period)
        weeks = group_trades_by_week(trades, pid)
        elapsed = time.time() - t0
        print(f"{len(trades)} trades -> {len(weeks)} weeks ({elapsed:.1f}s)")
        all_weeks.extend(weeks)

    # Save output
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    output = {"weeks": all_weeks}
    with open(str(OUTPUT_PATH), "w") as f:
        json.dump(output, f, indent=2)

    total_elapsed = time.time() - total_t0
    print(f"\n  Total: {len(all_weeks)} weeks across 6 periods")
    print(f"  Saved: {OUTPUT_PATH}")
    print(f"  Elapsed: {total_elapsed:.0f}s ({total_elapsed/60:.1f}min)")


if __name__ == "__main__":
    asyncio.run(main())
