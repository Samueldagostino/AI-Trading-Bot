"""
Fast Shadow-Trade Diagnostic — UCL v2 Edition
===============================================
Quick diagnostic to verify the shadow-trade machinery works and get
a preliminary read on which gates are blocking what.

Prints a 3-way comparison: Baseline vs UCL v1 (bad) vs UCL v2 (new).

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

# ── UCL v1 A/B test results (hard-coded from completed experiment) ──
UCL_V1_BASELINE = {
    "total_trades": 26,
    "win_rate": 38.5,
    "profit_factor": 0.63,
    "total_pnl": -294,
    "c1_pnl": -187,
    "c2_pnl": -107,
    "max_drawdown_pct": 1.0,
}

# ── Pre-UCL baseline (hard-coded from validated backtest) ──
PRE_UCL_BASELINE = {
    "total_trades": 43,
    "win_rate": 58.1,
    "profit_factor": 2.35,
    "total_pnl": 1286,
    "c1_pnl": 651,
    "c2_pnl": 635,
    "max_drawdown_pct": 0.4,
}

# ── C2 BE fix — Variant D (immediate BE) reference numbers ──
# From task description: period_4 with Variant D (original behavior)
C2_BE_VARIANT_D = {
    "total_trades": 250,
    "profit_factor": 1.41,
    "c2_breakeven_exits": 119,   # ~47.6% of 250
    "c2_trailing_exits": None,   # unknown
    "c2_stop_exits": None,       # unknown
    "c2_pnl": None,              # unknown
    "be_variant": "D (immediate)",
}


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


def print_3way_comparison(baseline, v1, v2, ucl_metrics):
    """Print the 3-way comparison table."""
    print(f"\n{'=' * 72}")
    print(f"  3-WAY COMPARISON: Baseline vs UCL v1 (bad) vs UCL v2 (new)")
    print(f"{'=' * 72}")
    print(f"  {'Metric':<22} {'Baseline':>12} {'UCL v1 (bad)':>14} {'UCL v2 (new)':>14}")
    print(f"  {'─' * 66}")

    def fmt_val(val, fmt_str="{:>12}"):
        if val is None:
            return fmt_str.format("?")
        return fmt_str.format(val)

    rows = [
        ("Trades",
         f"{baseline['total_trades']:>12}",
         f"{v1['total_trades']:>14}",
         f"{v2.get('total_trades', '?'):>14}"),
        ("Win Rate",
         f"{baseline['win_rate']:>11.1f}%",
         f"{v1['win_rate']:>13.1f}%",
         f"{v2.get('win_rate', 0):>13.1f}%"),
        ("Profit Factor",
         f"{baseline['profit_factor']:>12.2f}",
         f"{v1['profit_factor']:>14.2f}",
         f"{v2.get('profit_factor', 0):>14.2f}"),
        ("Total PnL",
         f"${baseline['total_pnl']:>+10,}",
         f"${v1['total_pnl']:>+12,}",
         f"${v2.get('total_pnl', 0):>+12,.2f}"),
        ("C1 PnL",
         f"${baseline['c1_pnl']:>+10,}",
         f"${v1['c1_pnl']:>+12,}",
         f"${v2.get('c1_pnl', 0):>+12,.2f}"),
        ("C2 PnL",
         f"${baseline['c2_pnl']:>+10,}",
         f"${v1['c2_pnl']:>+12,}",
         f"${v2.get('c2_pnl', 0):>+12,.2f}"),
        ("Max Drawdown",
         f"{baseline['max_drawdown_pct']:>11.1f}%",
         f"{v1['max_drawdown_pct']:>13.1f}%",
         f"{v2.get('max_drawdown_pct', 0):>13.1f}%"),
    ]
    for label, b, v1_val, v2_val in rows:
        print(f"  {label:<22} {b} {v1_val} {v2_val}")

    # UCL v2 Activity section
    print(f"\n  {'─' * 66}")
    print(f"  UCL v2 Activity:")
    print(f"  {'─' * 66}")
    for key, label in [
        ("fvg_boosts_applied", "FVG Boosts Applied"),
        ("signals_boosted_past_075", "Signals boosted past 0.75"),
        ("wide_stop_watches_created", "Wide-Stop Watches Created"),
        ("wide_stop_watches_confirmed", "Wide-Stop Watches Confirmed"),
        ("wide_stop_converted_trades", "Wide-Stop Converted Trades"),
        ("avg_original_stop", "Avg Original Stop"),
        ("avg_confirmed_stop", "Avg Confirmed Stop"),
        ("wide_stop_converted_pnl", "Wide-Stop Converted PnL"),
    ]:
        val = ucl_metrics.get(key, "?")
        if isinstance(val, float):
            if "pnl" in key.lower():
                print(f"  {label + ':':<35} ${val:+,.2f}")
            elif "stop" in key.lower():
                print(f"  {label + ':':<35} {val:.1f} pts")
            else:
                print(f"  {label + ':':<35} {val:.0f}")
        else:
            print(f"  {label + ':':<35} {val}")


def extract_ucl_metrics(sim, results):
    """Extract UCL v2 activity metrics from the simulator."""
    metrics = {
        "fvg_boosts_applied": 0,
        "signals_boosted_past_075": 0,
        "wide_stop_watches_created": 0,
        "wide_stop_watches_confirmed": 0,
        "wide_stop_converted_trades": 0,
        "avg_original_stop": 0.0,
        "avg_confirmed_stop": 0.0,
        "wide_stop_converted_pnl": 0.0,
    }

    if not sim.bot:
        return metrics

    # Watch state stats
    watch_stats = sim.bot._watch_manager.get_stats()
    metrics["wide_stop_watches_created"] = watch_stats.get("created", 0)
    metrics["wide_stop_watches_confirmed"] = watch_stats.get("confirmed", 0)

    # FVG detector stats
    fvg_stats = sim.bot._fvg_detector.get_stats()
    metrics["fvg_total_detected"] = fvg_stats.get("total_detected", 0)
    metrics["fvg_active"] = fvg_stats.get("total_active", 0)

    # Scan trade log for UCL-specific entries
    trade_log = getattr(sim, '_trade_log', []) or []
    original_stops = []
    confirmed_stops = []

    for trade in trade_log:
        source = trade.get("signal_source", "")
        if source and "ucl_confirmed" in source:
            metrics["wide_stop_converted_trades"] += 1
            pnl = trade.get("total_pnl", 0)
            if isinstance(pnl, (int, float)):
                metrics["wide_stop_converted_pnl"] += pnl

    if original_stops:
        metrics["avg_original_stop"] = sum(original_stops) / len(original_stops)
    if confirmed_stops:
        metrics["avg_confirmed_stop"] = sum(confirmed_stops) / len(confirmed_stops)

    return metrics


def extract_c2_exit_reasons(sim) -> dict:
    """Extract C2 exit reason breakdown from the simulator trade log.

    Reads sim.state.trades_log entries, counts c2_reason values.
    Returns dict with breakeven/trailing/stop/time_stop/max_target counts and C2 PnL by reason.
    """
    trades_log = getattr(sim, "state", None)
    if trades_log is None:
        return {}
    log = getattr(trades_log, "trades_log", []) or []

    reason_counts: dict = {}
    reason_pnl: dict = {}

    for entry in log:
        if entry.get("event") != "trade_closed":
            continue
        reason = entry.get("c2_reason") or "unknown"
        c2_pnl = entry.get("c2_pnl", 0) or 0
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        reason_pnl[reason] = reason_pnl.get(reason, 0.0) + c2_pnl

    total = sum(reason_counts.values()) or 1
    return {
        "counts": reason_counts,
        "pnl": {k: round(v, 2) for k, v in reason_pnl.items()},
        "pct": {k: round(v / total * 100, 1) for k, v in reason_counts.items()},
        "total_trades": total,
    }


def print_c2_be_comparison(before: dict, after_counts: dict, after_results: dict):
    """Print before/after C2 breakeven fix comparison table."""
    total = after_counts.get("total_trades", 1)
    counts = after_counts.get("counts", {})
    pnl_by_reason = after_counts.get("pnl", {})
    pct = after_counts.get("pct", {})

    be_after   = counts.get("breakeven", 0)
    trail_after = counts.get("trailing", 0)
    stop_after  = counts.get("stop", 0)
    time_after  = counts.get("time_stop", 0)
    max_after   = counts.get("max_target", 0)

    be_before   = before.get("c2_breakeven_exits", "?")
    total_before = before.get("total_trades", "?")

    print(f"\n{'=' * 72}")
    print(f"  C2 BREAKEVEN FIX — BEFORE vs AFTER")
    print(f"  Variant D (immediate BE) → Variant B (delayed BE, MFE >= 1.5x stop)")
    print(f"{'=' * 72}")
    print(f"  {'Metric':<30} {'Before (D)':>14} {'After (B)':>14}")
    print(f"  {'─' * 60}")

    def row(label, b_val, a_val):
        b_str = f"{b_val}" if b_val is not None else "?"
        a_str = f"{a_val}" if a_val is not None else "?"
        print(f"  {label:<30} {b_str:>14} {a_str:>14}")

    row("Total trades", total_before, after_results.get("total_trades", "?"))
    row("Profit factor", before.get("profit_factor", "?"),
        f"{after_results.get('profit_factor', 0):.2f}")
    row("C2 PnL ($)", before.get("c2_pnl", "?"),
        f"${after_results.get('c2_pnl', 0):+,.2f}")
    print(f"  {'─' * 60}")
    row("C2 breakeven exits", be_before,
        f"{be_after} ({pct.get('breakeven', 0):.1f}%)")
    row("C2 trailing exits", before.get("c2_trailing_exits", "?"),
        f"{trail_after} ({pct.get('trailing', 0):.1f}%)")
    row("C2 stop exits", before.get("c2_stop_exits", "?"),
        f"{stop_after} ({pct.get('stop', 0):.1f}%)")
    row("C2 time-stop exits", "?",
        f"{time_after} ({pct.get('time_stop', 0):.1f}%)")
    row("C2 max-target exits", "?",
        f"{max_after} ({pct.get('max_target', 0):.1f}%)")
    print(f"  {'─' * 60}")

    print(f"\n  C2 PnL by exit reason (After/Variant B):")
    for reason, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        p = pnl_by_reason.get(reason, 0)
        avg = p / cnt if cnt > 0 else 0
        print(f"    {reason:<20} {cnt:>4} trades  ${p:>+8,.2f} total  ${avg:>+6.2f}/trade")

    # Verdict
    if be_after < be_before if isinstance(be_before, int) else False:
        reduction = be_before - be_after
        reduction_pct = reduction / be_before * 100 if be_before > 0 else 0
        print(f"\n  VERDICT: Breakeven exits reduced by {reduction} ({reduction_pct:.1f}%)")
        if trail_after > 0:
            print(f"           Trailing exits: {trail_after} — runners survived the BE gate")
    else:
        print(f"\n  NOTE: Before/After comparison requires Variant D baseline run to compare.")


async def main():
    t0 = time.time()
    start_date = "2023-09-01"

    print("=" * 72)
    print("  FAST SHADOW-TRADE DIAGNOSTIC — UCL v2 Edition")
    print(f"  Dataset: period_4 (Sep 2023 – Feb 2024, chop regime)")
    print(f"  Max exec bars: {MAX_EXEC_BARS:,}")
    print("=" * 72)

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

    # ── Extract UCL v2 metrics ──
    ucl_metrics = extract_ucl_metrics(sim, results)

    # ── Gather metadata ──
    exec_bars = results.get("exec_bars", 0)
    exec_bar_list = sim._exec_bars or []
    date_range_start = (exec_bar_list[0].timestamp.strftime("%Y-%m-%d %H:%M")
                        if exec_bar_list else "?")
    date_range_end = (exec_bar_list[-1].timestamp.strftime("%Y-%m-%d %H:%M")
                      if exec_bar_list else "?")

    # ── Extract C2 exit reason breakdown ──
    c2_exit_breakdown = extract_c2_exit_reasons(sim)

    # ── Print 3-way comparison ──
    v2_results = {
        "total_trades": results.get("total_trades", 0),
        "win_rate": results.get("win_rate", 0),
        "profit_factor": results.get("profit_factor", 0),
        "total_pnl": results.get("total_pnl", 0),
        "c1_pnl": results.get("c1_pnl", 0),
        "c2_pnl": results.get("c2_pnl", 0),
        "max_drawdown_pct": results.get("max_drawdown_pct", 0),
    }
    print_3way_comparison(PRE_UCL_BASELINE, UCL_V1_BASELINE, v2_results, ucl_metrics)

    # ── C2 BE fix — before/after ──
    print_c2_be_comparison(C2_BE_VARIANT_D, c2_exit_breakdown, v2_results)

    # ── Shadow Analysis Detail ──
    ranking = shadow_analysis.get("gate_value_ranking", [])
    if ranking:
        print(f"\n{'=' * 72}")
        print(f"  SHADOW ANALYSIS — GATE VALUE RANKING")
        print(f"{'=' * 72}")
        print(f"  {'Gate':<25} {'Count':>6} {'Shadow PnL':>12} {'Verdict':>12}")
        print(f"  {'─' * 58}")
        for r in ranking:
            print(f"  {r['gate']:<25} {r['count']:>6} "
                  f"${r['shadow_pnl']:>+10,.2f} {r['verdict']:>12}")

        # KEY QUESTION
        max_stop_row = None
        for r in ranking:
            if "max stop" in r["gate"].lower() or "stop" in r["gate"].lower():
                max_stop_row = r
                break
        if max_stop_row:
            print(f"\n  KEY QUESTION: Did the max stop gate shadow PnL change?")
            print(f"  Previously: +$31,656 COSTING")
            print(f"  Now:        ${max_stop_row['shadow_pnl']:+,.2f} {max_stop_row['verdict']}")
            if max_stop_row['shadow_pnl'] > 0:
                delta = 31656 - max_stop_row['shadow_pnl']
                print(f"  Delta:      ${delta:+,.2f} (reduction in blocked profitable trades)")

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

    # ── Build output ──
    output = {
        "diagnostic": "fast_shadow_ucl_v2",
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
        "comparison": {
            "baseline": PRE_UCL_BASELINE,
            "ucl_v1": UCL_V1_BASELINE,
            "ucl_v2": v2_results,
        },
        "ucl_v2_metrics": ucl_metrics,
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
        "c2_exit_breakdown": c2_exit_breakdown,
        "c2_be_fix": {
            "variant": "B",
            "description": "Delayed BE — triggers only after C2 MFE >= 1.5x stop distance",
            "before_variant": "D",
            "before_variant_description": "Immediate BE on C1 exit",
            "before_c2_breakeven_pct": 47.6,
        },
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
    print(f"\n  Total time: {time.time() - t0:.1f}s")
    return output


if __name__ == "__main__":
    asyncio.run(main())
