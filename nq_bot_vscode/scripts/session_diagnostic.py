#!/usr/bin/env python3
"""
Session-Tagged Trade Diagnostic
================================
Runs the ReplaySimulator for the validated backtest periods (Mar 2025 – Mar 2026),
extracts per-trade entry timestamps, classifies each trade by trading session,
and generates a comprehensive session performance breakdown.

Session Taxonomy (all times Eastern Time, DST-aware via ZoneInfo):
  ETH_ASIA    : 6:00 PM – 2:00 AM  (Post-maintenance through Tokyo/Hong Kong)
  ETH_LONDON  : 2:00 AM – 9:30 AM  (European institutional flow through US pre-market)
  RTH_EARLY   : 9:30 AM – 12:00 PM (Opening drive, IB break, morning momentum)
  RTH_LATE    : 12:00 PM – 4:00 PM (Midday lull into closing flow)
  POST_RTH    : 4:00 PM – 5:00 PM  (Post-RTH wind-down, pre-maintenance)

Usage:
    python scripts/session_diagnostic.py
"""

import asyncio
import json
import sys
import time as time_module
from collections import defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# ── Paths ──
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent  # nq_bot_vscode/
REPO_ROOT = PROJECT_DIR.parent
OUTPUT_PATH = REPO_ROOT / "logs" / "session_diagnostic.json"

sys.path.insert(0, str(PROJECT_DIR))

from scripts.replay_simulator import ReplaySimulator

ET = ZoneInfo("America/New_York")

# ── Session definitions ──
SESSIONS = {
    "ETH_ASIA":   (time(18, 0), time(2, 0)),    # 6:00 PM – 2:00 AM (crosses midnight)
    "ETH_LONDON": (time(2, 0),  time(9, 30)),    # 2:00 AM – 9:30 AM
    "RTH_EARLY":  (time(9, 30), time(12, 0)),    # 9:30 AM – 12:00 PM
    "RTH_LATE":   (time(12, 0), time(16, 0)),    # 12:00 PM – 4:00 PM
    "POST_RTH":   (time(16, 0), time(17, 0)),    # 4:00 PM – 5:00 PM
}

# Period definitions for Mar 2025 – Mar 2026
PERIODS = [
    {
        "id": "period_5b",
        "label": "Sep 2024 – Aug 2025",
        "data_dir": str(REPO_ROOT / "data" / "firstrate" / "historical" / "aggregated" / "period_5b_2024-09_to_2025-08"),
        "start": "2025-03-01",
        "end": "2025-09-01",
    },
    {
        "id": "period_6",
        "label": "Sep 2025 – Feb 2026",
        "data_dir": str(PROJECT_DIR / "data" / "firstrate"),
        "start": "2025-09-01",
        "end": "2026-04-01",
    },
]


def classify_session(entry_ts: datetime) -> str:
    """Classify a trade entry timestamp into a session tag.

    All times are in Eastern Time. Boundary rule: if entry falls exactly on
    a boundary (e.g., 9:30:00 AM), assign to the LATER session.
    """
    et = entry_ts.astimezone(ET)
    t = et.time()

    # POST_RTH: 4:00 PM – 5:00 PM (>= 16:00 and < 17:00)
    if t >= time(16, 0) and t < time(17, 0):
        return "POST_RTH"

    # RTH_LATE: 12:00 PM – 4:00 PM (>= 12:00 and < 16:00)
    if t >= time(12, 0) and t < time(16, 0):
        return "RTH_LATE"

    # RTH_EARLY: 9:30 AM – 12:00 PM (>= 9:30 and < 12:00)
    if t >= time(9, 30) and t < time(12, 0):
        return "RTH_EARLY"

    # ETH_LONDON: 2:00 AM – 9:30 AM (>= 2:00 and < 9:30)
    if t >= time(2, 0) and t < time(9, 30):
        return "ETH_LONDON"

    # ETH_ASIA: 6:00 PM – 2:00 AM (>= 18:00 OR < 2:00) — crosses midnight
    if t >= time(18, 0) or t < time(2, 0):
        return "ETH_ASIA"

    # Maintenance window (5:00 PM – 6:00 PM) — shouldn't happen but handle it
    return "POST_RTH"


def compute_session_metrics(trades: List[dict]) -> dict:
    """Compute comprehensive metrics for a set of trades."""
    if not trades:
        return {
            "trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "profit_factor": 0.0,
            "total_pnl": 0.0, "avg_pnl_per_trade": 0.0,
            "c1_pnl": 0.0, "c2_pnl": 0.0,
            "avg_winner": 0.0, "avg_loser": 0.0,
            "largest_winner": 0.0, "largest_loser": 0.0,
            "pct_of_total_trades": 0.0, "pct_of_total_pnl": 0.0,
        }

    pnls = [t["total_pnl"] for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    total_pnl = sum(pnls)
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))

    pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (
        99.0 if gross_profit > 0 else 0.0)

    return {
        "trades": len(trades),
        "wins": len(winners),
        "losses": len(losers),
        "win_rate": round(len(winners) / len(trades) * 100, 1),
        "profit_factor": pf,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl_per_trade": round(total_pnl / len(trades), 2),
        "c1_pnl": round(sum(t.get("c1_pnl", 0) for t in trades), 2),
        "c2_pnl": round(sum(t.get("c2_pnl", 0) for t in trades), 2),
        "avg_winner": round(sum(winners) / len(winners), 2) if winners else 0.0,
        "avg_loser": round(sum(losers) / len(losers), 2) if losers else 0.0,
        "largest_winner": round(max(pnls), 2) if pnls else 0.0,
        "largest_loser": round(min(pnls), 2) if pnls else 0.0,
        "pct_of_total_trades": 0.0,  # filled in later
        "pct_of_total_pnl": 0.0,     # filled in later
    }


async def run_period(period: dict) -> List[dict]:
    """Run ReplaySimulator for a single period and extract per-trade data with entry timestamps."""
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

    # Extract per-trade data with entry timestamps from executor history
    trades = []
    if sim.bot and sim.bot.executor:
        for th in sim.bot.executor._trade_history:
            entry_time = th.entry_time
            if entry_time is None:
                continue

            # Compute per-trade PnL (with C3 delayed logic matching the weekly breakdown)
            c1_pnl = th.c1.net_pnl if th.c1.contracts > 0 else 0.0
            c2_pnl = th.c2.net_pnl if th.c2.contracts > 0 else 0.0
            c3_pnl = th.c3.net_pnl if th.c3.contracts > 0 else 0.0

            # C3 delayed entry: if C1 lost, C3 PnL is zeroed
            c3_blocked = (c1_pnl <= 0) if th.c3.contracts > 0 else True
            if c3_blocked:
                c3_pnl = 0.0

            total_pnl = c1_pnl + c2_pnl + c3_pnl

            trades.append({
                "trade_id": th.trade_id,
                "entry_time": entry_time.isoformat(),
                "exit_time": th.closed_at.isoformat() if th.closed_at else None,
                "direction": th.direction,
                "entry_price": th.entry_price,
                "total_pnl": round(total_pnl, 2),
                "c1_pnl": round(c1_pnl, 2),
                "c2_pnl": round(c2_pnl, 2),
                "c3_pnl": round(c3_pnl, 2),
                "c1_exit_reason": th.c1.exit_reason,
                "c2_exit_reason": th.c2.exit_reason,
                "c3_blocked": c3_blocked,
                "signal_score": th.signal_score,
                "regime": th.market_regime,
            })

    # Also collect from trades_log for any missing data
    if not trades and sim.state.trades_log:
        for t in sim.state.trades_log:
            trades.append({
                "trade_id": "",
                "entry_time": t.get("timestamp", ""),  # exit time as fallback
                "exit_time": t.get("timestamp", ""),
                "direction": t.get("direction", ""),
                "entry_price": t.get("entry_price", 0),
                "total_pnl": round(t.get("total_pnl", 0), 2),
                "c1_pnl": round(t.get("c1_pnl", 0), 2),
                "c2_pnl": round(t.get("c2_pnl", 0), 2),
                "c3_pnl": 0.0,
                "c1_exit_reason": t.get("c1_reason", ""),
                "c2_exit_reason": t.get("c2_reason", ""),
                "c3_blocked": True,
                "signal_score": 0.0,
                "regime": "",
            })

    return trades


def build_diagnostic(all_trades: List[dict]) -> dict:
    """Build the full session diagnostic JSON structure."""
    total_trades = len(all_trades)
    total_pnl = sum(t["total_pnl"] for t in all_trades)

    # Classify each trade by session
    session_trades = defaultdict(list)
    for t in all_trades:
        entry_ts_str = t.get("entry_time", "")
        if not entry_ts_str:
            continue
        try:
            entry_ts = datetime.fromisoformat(entry_ts_str)
        except (ValueError, TypeError):
            continue
        session = classify_session(entry_ts)
        t["session"] = session
        session_trades[session].append(t)

    # Build session breakdown
    session_breakdown = {}
    session_order = ["ETH_ASIA", "ETH_LONDON", "RTH_EARLY", "RTH_LATE", "POST_RTH"]

    for session in session_order:
        trades = session_trades.get(session, [])
        metrics = compute_session_metrics(trades)

        # Fill in percentages
        metrics["pct_of_total_trades"] = round(
            len(trades) / total_trades * 100, 1
        ) if total_trades > 0 else 0.0
        metrics["pct_of_total_pnl"] = round(
            metrics["total_pnl"] / total_pnl * 100, 1
        ) if total_pnl != 0 else 0.0

        session_breakdown[session] = metrics

    # Long vs short by session
    long_vs_short = {}
    for session in session_order:
        trades = session_trades.get(session, [])
        long_trades = [t for t in trades if t["direction"] == "long"]
        short_trades = [t for t in trades if t["direction"] == "short"]
        long_vs_short[session] = {
            "long_trades": len(long_trades),
            "long_pnl": round(sum(t["total_pnl"] for t in long_trades), 2),
            "short_trades": len(short_trades),
            "short_pnl": round(sum(t["total_pnl"] for t in short_trades), 2),
        }

    # Monthly by session
    monthly_by_session = defaultdict(lambda: {s: 0 for s in session_order})
    for t in all_trades:
        entry_ts_str = t.get("entry_time", "")
        if not entry_ts_str:
            continue
        try:
            entry_ts = datetime.fromisoformat(entry_ts_str)
            month_key = entry_ts.strftime("%Y-%m")
            session = t.get("session", "")
            if session:
                monthly_by_session[month_key][session] += 1
        except (ValueError, TypeError):
            continue

    return {
        "total_trades": total_trades,
        "session_breakdown": session_breakdown,
        "long_vs_short_by_session": long_vs_short,
        "monthly_by_session": dict(sorted(monthly_by_session.items())),
    }


def print_comparison_table(diagnostic: dict) -> None:
    """Print formatted comparison table to terminal."""
    sb = diagnostic["session_breakdown"]
    sessions = ["ETH_ASIA", "ETH_LONDON", "RTH_EARLY", "RTH_LATE", "POST_RTH"]

    print()
    print("=" * 100)
    print("  SESSION PERFORMANCE COMPARISON — 396 Trades (Mar 2025 – Mar 2026)")
    print("=" * 100)

    # Header
    header = f"{'Metric':<22}"
    for s in sessions:
        header += f" {s:>14}"
    print(header)
    print("-" * 100)

    # Rows
    metrics = [
        ("Trades", "trades", "d"),
        ("Win Rate (%)", "win_rate", ".1f"),
        ("Profit Factor", "profit_factor", ".2f"),
        ("Total PnL ($)", "total_pnl", "+,.2f"),
        ("Avg PnL/Trade ($)", "avg_pnl_per_trade", "+,.2f"),
        ("C1 PnL ($)", "c1_pnl", "+,.2f"),
        ("C2 PnL ($)", "c2_pnl", "+,.2f"),
        ("Largest Winner ($)", "largest_winner", "+,.2f"),
        ("Largest Loser ($)", "largest_loser", "+,.2f"),
        ("% of Total Trades", "pct_of_total_trades", ".1f"),
        ("% of Total PnL", "pct_of_total_pnl", ".1f"),
    ]

    for label, key, fmt in metrics:
        row = f"  {label:<20}"
        for s in sessions:
            val = sb.get(s, {}).get(key, 0)
            if fmt == "d":
                row += f" {val:>14d}"
            else:
                row += f" {val:>14{fmt}}"
        print(row)

    print("=" * 100)


def print_key_findings(diagnostic: dict) -> None:
    """Print key findings about session performance."""
    sb = diagnostic["session_breakdown"]
    sessions = ["ETH_ASIA", "ETH_LONDON", "RTH_EARLY", "RTH_LATE", "POST_RTH"]

    # Find best/worst PF
    session_pfs = {s: sb[s]["profit_factor"] for s in sessions if sb[s]["trades"] > 0}
    if session_pfs:
        best_pf_session = max(session_pfs, key=session_pfs.get)
        worst_pf_session = min(session_pfs, key=session_pfs.get)

        print()
        print("=" * 80)
        print("  KEY FINDINGS")
        print("=" * 80)
        print(f"  Highest PF:    {best_pf_session} — PF {session_pfs[best_pf_session]:.2f}")
        print(f"  Lowest PF:     {worst_pf_session} — PF {session_pfs[worst_pf_session]:.2f}")
        print()

        # Where does the edge concentrate?
        session_pnls = {s: sb[s]["total_pnl"] for s in sessions if sb[s]["trades"] > 0}
        best_pnl_session = max(session_pnls, key=session_pnls.get)
        print(f"  Edge concentrates in: {best_pnl_session} — ${session_pnls[best_pnl_session]:+,.2f} "
              f"({sb[best_pnl_session]['pct_of_total_pnl']:.1f}% of total PnL)")
        print()

        # Any session with PF < 1.0?
        losing_sessions = [s for s, pf in session_pfs.items() if pf < 1.0]
        if losing_sessions:
            print(f"  *** NET LOSING SESSIONS (PF < 1.0): {', '.join(losing_sessions)} ***")
            for s in losing_sessions:
                print(f"      {s}: PF {session_pfs[s]:.2f}, PnL ${sb[s]['total_pnl']:+,.2f}")
        else:
            print("  All sessions profitable (PF >= 1.0)")

        # POST_RTH check
        post_rth = sb.get("POST_RTH", {})
        if post_rth.get("trades", 0) > 0:
            print()
            print(f"  *** WARNING: {post_rth['trades']} trades found in POST_RTH (4:00-5:00 PM ET) ***")
            print(f"      These should be ZERO once maintenance flatten is active.")
        else:
            print()
            print("  POST_RTH: 0 trades (maintenance window clean)")

    print("=" * 80)
    print()


async def main():
    print()
    print("#" * 70)
    print("  SESSION-TAGGED TRADE DIAGNOSTIC")
    print("  Re-running backtest for Mar 2025 – Mar 2026")
    print("#" * 70)
    print()

    all_trades = []
    total_t0 = time_module.time()

    for period in PERIODS:
        print(f"  [{period['id']}] {period['label']} ({period['start']} → {period['end']}) ...",
              end=" ", flush=True)
        t0 = time_module.time()

        trades = await run_period(period)
        elapsed = time_module.time() - t0
        print(f"{len(trades)} trades ({elapsed:.1f}s)")
        all_trades.extend(trades)

    total_elapsed = time_module.time() - total_t0
    print(f"\n  Total trades: {len(all_trades)} ({total_elapsed:.0f}s)")

    if not all_trades:
        print("\n  ERROR: No trades generated. Check data directories.")
        print("  Searched:")
        for period in PERIODS:
            print(f"    {period['data_dir']}")
        sys.exit(1)

    # Build diagnostic
    diagnostic = build_diagnostic(all_trades)

    # Save to JSON
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(str(OUTPUT_PATH), "w") as f:
        json.dump(diagnostic, f, indent=2)
    print(f"\n  Session diagnostic saved to: {OUTPUT_PATH}")

    # Print formatted table
    print_comparison_table(diagnostic)

    # Print key findings
    print_key_findings(diagnostic)


if __name__ == "__main__":
    asyncio.run(main())
