"""
Fast Shadow-Trade Diagnostic
==============================
Quick diagnostic to verify the shadow-trade machinery works and get
a preliminary read on which gates are blocking what.

NOT a comprehensive backtest. Uses ONE period, capped at 50K exec bars.

Usage:
    python scripts/fast_shadow_diagnostic.py
"""

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ──
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
PROJECT_DIR = REPO_ROOT / "nq_bot_vscode"
LOGS_DIR = REPO_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = LOGS_DIR / "fast_shadow_results.json"

sys.path.insert(0, str(PROJECT_DIR))

from scripts.replay_simulator import ReplaySimulator, load_firstrate_mtf, filter_by_date

# ── Config ──
# Use period_4 (Sep 2023 – Feb 2024, "chop" regime) — smallest dataset
DATA_DIR = str(REPO_ROOT / "data" / "firstrate" / "historical" / "aggregated"
               / "period_4_2023-09_to_2024-02")
MAX_EXEC_BARS = 50_000
EXEC_TF = "2m"


def estimate_end_date(data_dir: str, start: str, max_bars: int) -> str:
    """Estimate an end date that keeps us under max_bars exec bars.

    Reads the 2m CSV header to compute bars/month, then picks a safe end date.
    """
    nq_2m = Path(data_dir) / "NQ_2m.csv"
    if not nq_2m.exists():
        return "2024-03-01"  # full period fallback

    line_count = sum(1 for _ in open(str(nq_2m))) - 1  # subtract header
    total_months = 6  # period_4 spans 6 months
    bars_per_month = line_count / total_months if total_months > 0 else 15000

    months_needed = max_bars / bars_per_month
    # Start is Sep 2023; add months_needed
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_month = start_dt.month + int(months_needed)
    end_year = start_dt.year + (end_month - 1) // 12
    end_month = ((end_month - 1) % 12) + 1
    return f"{end_year}-{end_month:02d}-01"


async def main():
    t0 = time.time()
    start_date = "2023-09-01"

    print("=" * 62)
    print("  FAST SHADOW-TRADE DIAGNOSTIC")
    print(f"  Dataset: period_4 (Sep 2023 – Feb 2024, chop regime)")
    print(f"  Max exec bars: {MAX_EXEC_BARS:,}")
    print("=" * 62)

    # Estimate end date to stay within bar cap
    end_date = estimate_end_date(DATA_DIR, start_date, MAX_EXEC_BARS)
    print(f"\n  Estimated end date for {MAX_EXEC_BARS:,} bars: {end_date}")

    # ── Create simulator ──
    sim = ReplaySimulator(
        speed="max",
        start_date=start_date,
        end_date=end_date,
        validate=True,
        data_dir=DATA_DIR,
        c1_variant="C",
        quiet=True,
        sweep_enabled=True,
    )

    # ── Run replay ──
    print("\n  Running replay...")
    results = await sim.run()

    elapsed_replay = time.time() - t0
    print(f"  Replay completed in {elapsed_replay:.1f}s")

    # ── Shadow analysis ──
    print("  Running shadow-trade simulation...")
    t1 = time.time()
    shadow_analysis = sim._simulate_shadow_trades()
    elapsed_shadow = time.time() - t1
    print(f"  Shadow simulation completed in {elapsed_shadow:.1f}s")

    # ── Print shadow summary ──
    sim._print_shadow_summary(shadow_analysis)

    # ── Gather metadata ──
    exec_bars = results.get("exec_bars", 0)
    exec_bar_list = sim._exec_bars or []
    date_range_start = (exec_bar_list[0].timestamp.strftime("%Y-%m-%d %H:%M")
                        if exec_bar_list else "?")
    date_range_end = (exec_bar_list[-1].timestamp.strftime("%Y-%m-%d %H:%M")
                      if exec_bar_list else "?")

    # ── Build output ──
    output = {
        "diagnostic": "fast_shadow",
        "generated": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "period": "period_4",
            "label": "Sep 2023 – Feb 2024 (chop regime)",
            "data_dir": DATA_DIR,
            "start_date": start_date,
            "end_date": end_date,
            "date_range_actual": f"{date_range_start} to {date_range_end}",
            "exec_bars": exec_bars,
        },
        "trade_summary": {
            "total_trades": results.get("total_trades", 0),
            "win_rate": results.get("win_rate", 0),
            "profit_factor": results.get("profit_factor", 0),
            "total_pnl": results.get("total_pnl", 0),
            "c1_pnl": results.get("c1_pnl", 0),
            "c2_pnl": results.get("c2_pnl", 0),
            "max_drawdown_pct": results.get("max_drawdown_pct", 0),
            "expectancy": results.get("expectancy", 0),
        },
        "shadow_analysis": shadow_analysis,
        "timing": {
            "replay_seconds": round(elapsed_replay, 1),
            "shadow_sim_seconds": round(elapsed_shadow, 1),
            "total_seconds": round(time.time() - t0, 1),
        },
    }

    # ── Write output ──
    with open(str(OUTPUT_FILE), "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n  Results written to: {OUTPUT_FILE}")

    # ── Print summary table ──
    print(f"\n{'=' * 62}")
    print(f"  SUMMARY")
    print(f"{'=' * 62}")
    print(f"  Dataset:        period_4 ({date_range_start} to {date_range_end})")
    print(f"  Exec bars:      {exec_bars:,}")
    print(f"  Total trades:   {results.get('total_trades', 0):,}")
    print(f"  Win rate:       {results.get('win_rate', 0):.1f}%")
    print(f"  Profit factor:  {results.get('profit_factor', 0):.2f}")
    print(f"  Total PnL:      ${results.get('total_pnl', 0):+,.2f}")
    print(f"  Shadow signals: {shadow_analysis.get('total_shadow_signals', 0):,}")

    ranking = shadow_analysis.get("gate_value_ranking", [])
    if ranking:
        print(f"\n  GATE VALUE RANKING:")
        print(f"  {'Gate':<25} {'Count':>6} {'Shadow PnL':>12} {'Verdict':>12}")
        print(f"  {'─' * 58}")
        for r in ranking:
            print(f"  {r['gate']:<25} {r['count']:>6} "
                  f"${r['shadow_pnl']:>+10,.2f} {r['verdict']:>12}")

    by_gate = shadow_analysis.get("by_gate", {})
    if by_gate:
        print(f"\n  DETAILED GATE STATS:")
        print(f"  {'Gate':<25} {'WR':>6} {'PF':>6} {'AvgMFE':>8} {'AvgMAE':>8}")
        print(f"  {'─' * 58}")
        for gate_name, g in by_gate.items():
            pf = g["shadow_profit_factor"]
            pf_str = f"{pf:.2f}" if isinstance(pf, (int, float)) else pf
            print(f"  {gate_name:<25} {g['shadow_win_rate']:>5.1f}% "
                  f"{pf_str:>6} {g['avg_mfe_points']:>7.2f} {g['avg_mae_points']:>7.2f}")

    print(f"\n  Total time: {time.time() - t0:.1f}s")
    return output


if __name__ == "__main__":
    asyncio.run(main())
