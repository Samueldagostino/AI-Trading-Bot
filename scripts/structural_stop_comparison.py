"""
Structural Stop Comparison
===========================
Runs the ReplaySimulator on period_4 with the new structural stop
placement and compares against the ATR-only baseline.

Usage:
    python scripts/structural_stop_comparison.py
"""

import asyncio
import json
import math
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
OUTPUT_FILE = LOGS_DIR / "structural_stop_comparison.json"

sys.path.insert(0, str(PROJECT_DIR))

from scripts.replay_simulator import ReplaySimulator

# ── Config ──
DATA_DIR = str(REPO_ROOT / "data" / "firstrate" / "historical" / "aggregated"
               / "period_4_2023-09_to_2024-02")
MAX_EXEC_BARS = 50_000
EXEC_TF = "2m"

# ATR-only baseline from the last run (prior to structural stop changes)
BASELINE = {
    "total_trades": 100,
    "win_rate": 52.0,
    "profit_factor": 1.65,
    "total_pnl": 1497.94,
    "max_drawdown_pct": 0.56,
    "c1_pnl": 769.50,
    "c2_pnl": 728.44,
    "expectancy": 14.98,
    "avg_stop_distance": None,  # Will be estimated
    "shadow_max_stop_count": 692,
    "shadow_max_stop_pnl": 30631.26,
}


def estimate_end_date(data_dir: str, start: str, max_bars: int) -> str:
    nq_2m = Path(data_dir) / "NQ_2m.csv"
    if not nq_2m.exists():
        return "2024-03-01"
    line_count = sum(1 for _ in open(str(nq_2m))) - 1
    total_months = 6
    bars_per_month = line_count / total_months if total_months > 0 else 15000
    months_needed = max_bars / bars_per_month
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_month = start_dt.month + int(months_needed)
    end_year = start_dt.year + (end_month - 1) // 12
    end_month = ((end_month - 1) % 12) + 1
    return f"{end_year}-{end_month:02d}-01"


async def main():
    t0 = time.time()
    start_date = "2023-09-01"
    end_date = estimate_end_date(DATA_DIR, start_date, MAX_EXEC_BARS)

    print("=" * 74)
    print("  STRUCTURAL STOP COMPARISON — Baseline vs New")
    print(f"  Dataset: period_4 (Sep 2023 – Feb 2024)")
    print("=" * 74)

    # ── Run replay with structural stops ──
    print("\n  Running replay with structural stop placement...")
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

    results = await sim.run()
    elapsed = time.time() - t0
    print(f"  Replay completed in {elapsed:.1f}s")

    # ── Extract new metrics ──
    trades = sim.bot.executor._trade_history
    total_trades = len(trades)

    if total_trades == 0:
        print("\n  ERROR: No trades executed. Check structural stop changes.")
        return

    pnls = [t.total_net_pnl for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]

    win_rate = len(winners) / total_trades * 100 if total_trades > 0 else 0
    total_pnl = sum(pnls)
    gross_wins = sum(winners)
    gross_losses = abs(sum(losers))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float('inf')
    expectancy = total_pnl / total_trades if total_trades > 0 else 0

    c1_pnl = sum(t.c1.net_pnl for t in trades)
    c2_pnl = sum(t.c2.net_pnl for t in trades)

    # Compute max drawdown
    equity = 25000.0  # Starting equity
    peak = equity
    max_dd_pct = 0.0
    for pnl in pnls:
        equity += pnl
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        max_dd_pct = max(max_dd_pct, dd)

    # Average stop distance from trades
    stop_distances = []
    for t in trades:
        if t.initial_stop and t.entry_price:
            stop_distances.append(abs(t.entry_price - t.initial_stop))

    avg_stop = sum(stop_distances) / len(stop_distances) if stop_distances else 0

    # Compute average R:R achieved
    rr_achieved = []
    for t in trades:
        if t.initial_stop and t.entry_price and t.total_net_pnl != 0:
            stop_dist = abs(t.entry_price - t.initial_stop)
            if stop_dist > 0:
                pts = abs(t.c1.exit_price - t.entry_price) if t.c1.exit_price else 0
                rr_achieved.append(pts / stop_dist)
    avg_rr = sum(rr_achieved) / len(rr_achieved) if rr_achieved else 0

    # Shadow signals analysis
    shadow_max_stop_count = 0
    shadow_max_stop_pnl = 0.0
    for shadow in sim._iter_all_shadow_signals():
        reason = shadow.get("rejection_reason", "")
        if "Max stop exceeded" in reason:
            shadow_max_stop_count += 1

    # Compute shadow PnL for max-stop blocked
    shadow_results = sim._simulate_shadow_trades()
    for gate_name, gate_data in shadow_results.get("by_gate", {}).items():
        if "Max stop" in gate_name:
            shadow_max_stop_pnl += gate_data.get("shadow_total_pnl", 0)

    new_metrics = {
        "total_trades": total_trades,
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "total_pnl": round(total_pnl, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "c1_pnl": round(c1_pnl, 2),
        "c2_pnl": round(c2_pnl, 2),
        "expectancy": round(expectancy, 2),
        "avg_stop_distance": round(avg_stop, 2),
        "avg_rr_achieved": round(avg_rr, 2),
        "shadow_max_stop_count": shadow_max_stop_count,
        "shadow_max_stop_pnl": round(shadow_max_stop_pnl, 2),
    }

    # ── Print comparison table ──
    print(f"\n{'=' * 74}")
    print(f"  BACKTEST COMPARISON: ATR-Only vs Structural Stops")
    print(f"{'=' * 74}")
    print(f"  {'Metric':<28} {'ATR-Only (Base)':>16} {'Structural (New)':>17} {'Delta':>12}")
    print(f"  {'─' * 74}")

    def fmt_row(label, old_val, new_val, fmt_str=".2f", suffix="", better="higher"):
        old_s = f"{old_val:{fmt_str}}{suffix}" if old_val is not None else "N/A"
        new_s = f"{new_val:{fmt_str}}{suffix}" if new_val is not None else "N/A"
        if old_val is not None and new_val is not None:
            diff = new_val - old_val
            if isinstance(diff, float):
                delta_s = f"{diff:+{fmt_str}}{suffix}"
            else:
                delta_s = f"{diff:+}{suffix}"
        else:
            delta_s = "---"
        print(f"  {label:<28} {old_s:>16} {new_s:>17} {delta_s:>12}")

    fmt_row("Total Trades", BASELINE["total_trades"], new_metrics["total_trades"], "d", "")
    fmt_row("Win Rate", BASELINE["win_rate"], new_metrics["win_rate"], ".1f", "%")
    fmt_row("Profit Factor", BASELINE["profit_factor"], new_metrics["profit_factor"], ".2f", "")
    fmt_row("Net PnL", BASELINE["total_pnl"], new_metrics["total_pnl"], ".2f", "$")
    fmt_row("Max Drawdown", BASELINE["max_drawdown_pct"], new_metrics["max_drawdown_pct"], ".2f", "%")
    fmt_row("C1 PnL", BASELINE["c1_pnl"], new_metrics["c1_pnl"], ".2f", "$")
    fmt_row("C2 PnL", BASELINE["c2_pnl"], new_metrics["c2_pnl"], ".2f", "$")
    fmt_row("Expectancy/Trade", BASELINE["expectancy"], new_metrics["expectancy"], ".2f", "$")
    fmt_row("Avg Stop Distance", BASELINE["avg_stop_distance"], new_metrics["avg_stop_distance"], ".1f", "pt")
    fmt_row("Shadow Max-Stop Count", BASELINE["shadow_max_stop_count"],
            new_metrics["shadow_max_stop_count"], "d", "")
    fmt_row("Shadow Max-Stop PnL", BASELINE["shadow_max_stop_pnl"],
            new_metrics["shadow_max_stop_pnl"], ".2f", "$")

    print(f"  {'─' * 74}")

    # ── Summary ──
    trades_delta = new_metrics["total_trades"] - BASELINE["total_trades"]
    pnl_delta = new_metrics["total_pnl"] - BASELINE["total_pnl"]
    gate_reduction = BASELINE["shadow_max_stop_count"] - new_metrics["shadow_max_stop_count"]

    print(f"\n  SUMMARY:")
    print(f"  Structural stops added {trades_delta:+d} trades "
          f"({pnl_delta:+,.2f}$ PnL change)")
    print(f"  Max-stop gate blocks reduced by {gate_reduction} signals")
    if new_metrics["avg_stop_distance"]:
        print(f"  Average stop distance: {new_metrics['avg_stop_distance']:.1f}pts "
              f"(structural placement)")

    # ── Save results ──
    output = {
        "comparison": "structural_stop_vs_atr_only",
        "generated": datetime.now(timezone.utc).isoformat(),
        "dataset": "period_4 (Sep 2023 – Feb 2024)",
        "baseline_atr_only": BASELINE,
        "new_structural": new_metrics,
        "deltas": {
            "trades": trades_delta,
            "pnl": round(pnl_delta, 2),
            "win_rate_pp": round(new_metrics["win_rate"] - BASELINE["win_rate"], 1),
            "pf": round(new_metrics["profit_factor"] - BASELINE["profit_factor"], 2),
            "gate_reduction": gate_reduction,
        },
    }

    with open(str(OUTPUT_FILE), "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n  Results written to: {OUTPUT_FILE}")
    print(f"  Total time: {time.time() - t0:.1f}s")

    return output


if __name__ == "__main__":
    asyncio.run(main())
