"""
C1 Deep Comparison — Risk Analysis for Top 3 vs Current
==========================================================
Runs the Phase 1 capture once, then does detailed risk analysis:
  - Trade-by-trade equity curves
  - Max drawdown per month ($ and %)
  - Consecutive loss streaks
  - Worst single trade
  - Reversal risk: trades that went 1x+ profit then reversed
  - Win rate by month
  - Loss severity distribution
"""

import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))

# Import the experiment infrastructure
from scripts.c1_exit_experiments import (
    load_data, filter_to_months, run_baseline_and_capture,
    replay_trade_standard, replay_trade_pure_runner,
    replay_trade_be_step, replay_trade_time_exit,
    TradeCapture, ReplayResult, BarSnapshot,
    POINT_VALUE, COMMISSION, FEB_BASELINE,
)
from config.settings import CONFIG

ACCOUNT_SIZE = 25_000  # $25K account for DD% calc


def analyze_config(captures: List[TradeCapture], replay_fn, label: str) -> Dict:
    """Deep analysis of one configuration."""
    results_by_month = defaultdict(list)
    all_results = []

    for cap in captures:
        r = replay_fn(cap)
        if r:
            all_results.append(r)
            results_by_month[r.month].append(r)

    if not all_results:
        return {"label": label, "error": "no trades"}

    # ── Equity Curve & Drawdown ──
    equity = 0.0
    peak = 0.0
    max_dd_dollar = 0.0
    max_dd_pct = 0.0
    equity_curve = []
    drawdown_curve = []

    for r in all_results:
        equity += r.total_pnl
        equity_curve.append(equity)
        peak = max(peak, equity)
        dd = peak - equity
        dd_pct = dd / ACCOUNT_SIZE * 100
        drawdown_curve.append(dd)
        if dd > max_dd_dollar:
            max_dd_dollar = dd
            max_dd_pct = dd_pct

    # ── Monthly Breakdown with Drawdown ──
    monthly_stats = {}
    for mk in sorted(results_by_month.keys()):
        month_results = results_by_month[mk]
        trades = len(month_results)
        wins = sum(1 for r in month_results if r.is_win)
        wr = round(100 * wins / trades, 1) if trades > 0 else 0

        c1_pnl = sum(r.c1_pnl for r in month_results)
        c2_pnl = sum(r.c2_pnl for r in month_results)
        total_pnl = sum(r.total_pnl for r in month_results)

        gross_wins = sum(r.total_pnl for r in month_results if r.total_pnl > 0)
        gross_losses = abs(sum(r.total_pnl for r in month_results if r.total_pnl < 0))
        pf = round(gross_wins / gross_losses, 2) if gross_losses > 0 else 99.0

        # Monthly drawdown
        m_equity = 0.0
        m_peak = 0.0
        m_max_dd = 0.0
        for r in month_results:
            m_equity += r.total_pnl
            m_peak = max(m_peak, m_equity)
            m_dd = m_peak - m_equity
            m_max_dd = max(m_max_dd, m_dd)

        monthly_stats[mk] = {
            "trades": trades, "wins": wins, "wr": wr, "pf": pf,
            "c1_pnl": round(c1_pnl, 2), "c2_pnl": round(c2_pnl, 2),
            "total_pnl": round(total_pnl, 2),
            "max_dd_month": round(m_max_dd, 2),
            "max_dd_month_pct": round(m_max_dd / ACCOUNT_SIZE * 100, 1),
        }

    # ── Consecutive Losses ──
    max_consec_losses = 0
    current_streak = 0
    for r in all_results:
        if r.total_pnl < 0:
            current_streak += 1
            max_consec_losses = max(max_consec_losses, current_streak)
        else:
            current_streak = 0

    # ── Worst Trade ──
    worst_trade = min(all_results, key=lambda r: r.total_pnl)

    # ── Best Trade ──
    best_trade = max(all_results, key=lambda r: r.total_pnl)

    # ── Loss Severity Distribution ──
    losses = [r.total_pnl for r in all_results if r.total_pnl < 0]
    loss_buckets = {"$0-20": 0, "$20-40": 0, "$40-60": 0, "$60-80": 0, "$80-100": 0, "$100+": 0}
    for loss in losses:
        abs_loss = abs(loss)
        if abs_loss <= 20:
            loss_buckets["$0-20"] += 1
        elif abs_loss <= 40:
            loss_buckets["$20-40"] += 1
        elif abs_loss <= 60:
            loss_buckets["$40-60"] += 1
        elif abs_loss <= 80:
            loss_buckets["$60-80"] += 1
        elif abs_loss <= 100:
            loss_buckets["$80-100"] += 1
        else:
            loss_buckets["$100+"] += 1

    # ── Aggregate Stats ──
    total_trades = len(all_results)
    wins_total = sum(1 for r in all_results if r.is_win)
    total_pnl = sum(r.total_pnl for r in all_results)
    c1_pnl = sum(r.c1_pnl for r in all_results)
    c2_pnl = sum(r.c2_pnl for r in all_results)
    gross_wins = sum(r.total_pnl for r in all_results if r.total_pnl > 0)
    gross_losses = abs(sum(r.total_pnl for r in all_results if r.total_pnl < 0))
    pf = round(gross_wins / gross_losses, 2) if gross_losses > 0 else 99.0
    exp = round(total_pnl / total_trades, 2) if total_trades > 0 else 0
    avg_win = round(gross_wins / wins_total, 2) if wins_total > 0 else 0
    avg_loss = round(gross_losses / (total_trades - wins_total), 2) if (total_trades - wins_total) > 0 else 0

    return {
        "label": label,
        "trades": total_trades,
        "wins": wins_total,
        "wr": round(100 * wins_total / total_trades, 1),
        "pf": pf,
        "c1_pnl": round(c1_pnl, 2),
        "c2_pnl": round(c2_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "exp": exp,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "max_dd_dollar": round(max_dd_dollar, 2),
        "max_dd_pct": round(max_dd_pct, 1),
        "max_consec_losses": max_consec_losses,
        "worst_trade": round(worst_trade.total_pnl, 2),
        "best_trade": round(best_trade.total_pnl, 2),
        "loss_buckets": loss_buckets,
        "monthly": monthly_stats,
        "equity_curve": equity_curve,
        "drawdown_curve": drawdown_curve,
    }


def print_comparison(configs: List[Dict]):
    """Print side-by-side comparison of configs."""

    print(f"\n{'=' * 90}")
    print(f"  DEEP RISK COMPARISON — Top 3 C1 Strategies vs Current Production")
    print(f"{'=' * 90}\n")

    # ── Aggregate Metrics ──
    print(f"{'AGGREGATE METRICS':^90}")
    print(f"{'─' * 90}")
    header = f"  {'Metric':<28}"
    for c in configs:
        header += f" {c['label']:>14}"
    print(header)
    print(f"  {'─' * 86}")

    metrics = [
        ("Trades", "trades", "d"),
        ("Win Rate", "wr", ".1f", "%"),
        ("Profit Factor", "pf", ".2f"),
        ("Total PnL", "total_pnl", "+,.0f", "$"),
        ("C1 PnL", "c1_pnl", "+,.0f", "$"),
        ("C2 PnL", "c2_pnl", "+,.0f", "$"),
        ("Expectancy/Trade", "exp", ".2f", "$"),
        ("Avg Winner", "avg_win", ".2f", "$"),
        ("Avg Loser", "avg_loss", ".2f", "$"),
        ("Max Drawdown $", "max_dd_dollar", ",.0f", "$"),
        ("Max Drawdown %", "max_dd_pct", ".1f", "%"),
        ("Max Consec Losses", "max_consec_losses", "d"),
        ("Worst Single Trade", "worst_trade", "+,.2f", "$"),
        ("Best Single Trade", "best_trade", "+,.2f", "$"),
    ]

    for m in metrics:
        name, key, fmt = m[0], m[1], m[2]
        prefix = m[3] if len(m) > 3 else ""
        row = f"  {name:<28}"
        for c in configs:
            val = c.get(key, 0)
            if prefix == "$":
                row += f" ${val:{fmt}}".rjust(15)
            elif prefix == "%":
                row += f" {val:{fmt}}%".rjust(15)
            else:
                row += f" {val:{fmt}}".rjust(15)
        print(row)

    # ── Monthly Breakdown ──
    print(f"\n{'MONTHLY BREAKDOWN':^90}")
    print(f"{'─' * 90}")

    months = sorted(set().union(*[c.get("monthly", {}).keys() for c in configs]))

    for mk in months:
        print(f"\n  {mk}")
        print(f"  {'Metric':<20}", end="")
        for c in configs:
            print(f" {c['label']:>14}", end="")
        print()
        print(f"  {'─' * 78}")

        for metric_name, metric_key, fmt in [
            ("Trades", "trades", "d"),
            ("Win Rate", "wr", ".1f"),
            ("PF", "pf", ".2f"),
            ("C1 PnL", "c1_pnl", "+,.0f"),
            ("C2 PnL", "c2_pnl", "+,.0f"),
            ("Total PnL", "total_pnl", "+,.0f"),
            ("Max DD (month)", "max_dd_month", ",.0f"),
            ("Max DD % (month)", "max_dd_month_pct", ".1f"),
        ]:
            row = f"  {metric_name:<20}"
            for c in configs:
                ms = c.get("monthly", {}).get(mk, {})
                val = ms.get(metric_key, 0)
                if metric_key in ("c1_pnl", "c2_pnl", "total_pnl", "max_dd_month"):
                    row += f" ${val:{fmt}}".rjust(15)
                elif metric_key == "max_dd_month_pct":
                    row += f" {val:{fmt}}%".rjust(15)
                elif metric_key == "wr":
                    row += f" {val:{fmt}}%".rjust(15)
                else:
                    row += f" {val:{fmt}}".rjust(15)
            print(row)

    # ── Loss Distribution ──
    print(f"\n{'LOSS SEVERITY DISTRIBUTION':^90}")
    print(f"{'─' * 90}")
    header = f"  {'Bucket':<20}"
    for c in configs:
        header += f" {c['label']:>14}"
    print(header)
    print(f"  {'─' * 78}")

    for bucket in ["$0-20", "$20-40", "$40-60", "$60-80", "$80-100", "$100+"]:
        row = f"  {bucket:<20}"
        for c in configs:
            count = c.get("loss_buckets", {}).get(bucket, 0)
            row += f" {count:>14d}"
        print(row)

    # ── Reversal Risk Assessment ──
    print(f"\n{'REVERSAL RISK ASSESSMENT':^90}")
    print(f"{'─' * 90}")
    print(f"  Pure Runner:  Both contracts trail → if price reverses from 1.5x+, BOTH lose")
    print(f"  BE Step:      C1 at BE after 1x → if reversal, C1 exits at ~$0, only C2 exposed")
    print(f"  Time 10 bars: C1 exits after 10 bars if profitable → C1 locks early, C2 exposed")
    print(f"  Current 1.5x: C1 locks at 1.5x → C1 always captures fixed gain, C2 exposed")
    print()

    # Summarize which is best on risk-adjusted basis
    print(f"{'RISK-ADJUSTED RANKING':^90}")
    print(f"{'─' * 90}")
    for i, c in enumerate(sorted(configs, key=lambda x: x["total_pnl"] / max(x["max_dd_dollar"], 1), reverse=True), 1):
        ratio = c["total_pnl"] / max(c["max_dd_dollar"], 1)
        print(f"  {i}. {c['label']:<20} PnL/MaxDD: {ratio:.2f}  "
              f"(${c['total_pnl']:+,.0f} / ${c['max_dd_dollar']:,.0f} DD)")

    print(f"\n{'=' * 90}")


async def main():
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%H:%M:%S")
    for name in ["main", "execution", "signals", "features", "risk", "data_pipeline"]:
        logging.getLogger(name).setLevel(logging.WARNING)

    print(f"\n{'=' * 70}")
    print(f"  C1 DEEP COMPARISON — Risk Analysis")
    print(f"  Config D | Sep 2025 – Feb 2026 | FirstRate 1m")
    print(f"{'=' * 70}\n")

    print("Loading data...")
    tf_bars = load_data()
    target_months = {"2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02"}
    month_keys = sorted(target_months)

    print("Running Phase 1 (capturing trade entries)...")
    captures, all_bars, _ = await run_baseline_and_capture(tf_bars, month_keys)
    print(f"  Captured {len(captures)} trades\n")

    # ── Analyze all 4 configs ──
    configs = []

    print("Analyzing: Current (1.5x stop)...")
    configs.append(analyze_config(captures, lambda cap: replay_trade_standard(cap, 1.5), "Current 1.5x"))

    print("Analyzing: Pure Runner...")
    configs.append(analyze_config(captures, replay_trade_pure_runner, "Pure Runner"))

    print("Analyzing: BE Step...")
    configs.append(analyze_config(captures, replay_trade_be_step, "BE Step"))

    print("Analyzing: Time 10 bars...")
    configs.append(analyze_config(captures, lambda cap: replay_trade_time_exit(cap, 10), "Time 10 bars"))

    # Print comparison
    print_comparison(configs)


if __name__ == "__main__":
    asyncio.run(main())
