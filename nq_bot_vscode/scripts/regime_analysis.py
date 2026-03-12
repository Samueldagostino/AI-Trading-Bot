"""
Regime Cross-Analysis -- Priority 3
====================================
Runs the current production pipeline (Config D + C1 time exit) across
the full 6-month OOS window and cross-tabulates PnL by:
  - Regime (trending_up, trending_down, ranging, etc.)
  - HTF consensus direction at entry (bullish, bearish, neutral)
  - Session (overnight, pre-market, morning, midday, afternoon)
  - Trade direction (long, short)

Identifies toxic combos (negative expectancy with 3+ trades) and
high-edge combos (expectancy > $20/trade) for potential blocking.

Usage:
    python scripts/regime_analysis.py
"""

import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Tuple

script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))

from config.settings import CONFIG
from data_pipeline.pipeline import (
    DataPipeline, BarData, MultiTimeframeIterator,
    TradingViewImporter, bardata_to_bar, bardata_to_htfbar,
    MINUTES_TO_LABEL, _parse_tf_from_filename,
)
from main import TradingOrchestrator, HTF_TIMEFRAMES

EXEC_TF = "2m"


def classify_session(ts: datetime) -> str:
    """Classify entry timestamp into trading session (ET)."""
    et_time_obj = ts.astimezone(ZoneInfo("America/New_York"))
    et_hour = et_time_obj.hour
    et_minute = et_time_obj.minute

    et_time = et_hour * 60 + et_minute

    if et_time < 6 * 60:          # 00:00 - 06:00 ET
        return "overnight"
    elif et_time < 9 * 60 + 30:   # 06:00 - 09:30 ET
        return "pre-market"
    elif et_time < 11 * 60 + 30:  # 09:30 - 11:30 ET
        return "morning"
    elif et_time < 14 * 60:       # 11:30 - 14:00 ET
        return "midday"
    elif et_time < 16 * 60:       # 14:00 - 16:00 ET
        return "afternoon"
    else:                          # 16:00 - 00:00 ET
        return "overnight"


def load_firstrate_mtf(data_dir: str) -> Dict[str, List[BarData]]:
    """Load aggregated FirstRate CSVs by timeframe."""
    dir_path = Path(data_dir)
    tf_map = {
        "NQ_1m.csv": "1m", "NQ_2m.csv": "2m", "NQ_3m.csv": "3m",
        "NQ_5m.csv": "5m", "NQ_15m.csv": "15m", "NQ_30m.csv": "30m",
        "NQ_1H.csv": "1H", "NQ_4H.csv": "4H", "NQ_1D.csv": "1D",
    }
    importer = TradingViewImporter(CONFIG)
    tf_bars: Dict[str, List[BarData]] = {}

    for csv_file in sorted(dir_path.glob("NQ_*.csv")):
        tf_label = tf_map.get(csv_file.name)
        if not tf_label:
            tf_label = _parse_tf_from_filename(str(csv_file))
        if not tf_label:
            continue
        bars = importer.import_file(str(csv_file))
        if bars:
            for bar in bars:
                bar.source = "firstrate"
            tf_bars[tf_label] = bars

    return tf_bars


def segment_by_month(tf_bars):
    monthly = defaultdict(lambda: defaultdict(list))
    for tf, bars in tf_bars.items():
        for bar in bars:
            mk = bar.timestamp.strftime("%Y-%m")
            monthly[mk][tf].append(bar)
    return dict(monthly)


async def run_month_with_trade_details(tf_bars, month_key):
    """Run one month and return paired entry+close details."""
    CONFIG.execution.paper_trading = True
    bot = TradingOrchestrator(CONFIG)
    await bot.initialize(skip_db=True)

    pipeline = DataPipeline(CONFIG)
    mtf_iterator = pipeline.create_mtf_iterator(tf_bars)

    if len(mtf_iterator) == 0:
        return []

    # Run backtest and collect trade log
    results = await bot.run_backtest_mtf(mtf_iterator, execution_tf=EXEC_TF)
    trade_log = results.get("trade_log", [])

    # Pair entries with closes
    paired_trades = []
    pending_entry = None

    for event in trade_log:
        if event.get("action") == "entry":
            pending_entry = event
        elif event.get("action") == "trade_closed" and pending_entry:
            ts_str = pending_entry.get("timestamp", "")
            try:
                entry_ts = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                entry_ts = None

            paired_trades.append({
                "month": month_key,
                "direction": pending_entry.get("direction", "?"),
                "entry_price": pending_entry.get("entry_price", 0),
                "regime": pending_entry.get("regime", "unknown"),
                "htf_bias": pending_entry.get("htf_bias", "n/a"),
                "htf_strength": pending_entry.get("htf_strength", 0),
                "signal_score": pending_entry.get("signal_score", 0),
                "session": classify_session(entry_ts) if entry_ts else "unknown",
                "entry_timestamp": ts_str,
                "c1_pnl": event.get("c1_pnl", 0),
                "c2_pnl": event.get("c2_pnl", 0),
                "total_pnl": event.get("total_pnl", 0),
                "c1_exit_reason": event.get("c1_exit_reason", ""),
                "c2_exit_reason": event.get("c2_exit_reason", ""),
            })
            pending_entry = None

    return paired_trades


def compute_group_stats(trades: List[dict]) -> dict:
    """Compute stats for a group of trades."""
    n = len(trades)
    if n == 0:
        return {"trades": 0}

    pnls = [t["total_pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    total_pnl = sum(pnls)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else 99.0
    exp = round(total_pnl / n, 2)
    wr = round(100 * wins / n, 1)

    return {
        "trades": n,
        "wins": wins,
        "wr": wr,
        "pf": pf,
        "total_pnl": round(total_pnl, 2),
        "expectancy": exp,
    }


def print_analysis(all_trades: List[dict]):
    """Full cross-analysis report."""
    n_total = len(all_trades)
    total_pnl = sum(t["total_pnl"] for t in all_trades)

    print(f"\n{'=' * 90}")
    print(f"  REGIME CROSS-ANALYSIS -- Priority 3")
    print(f"  Config D + C1 Time Exit | Sep 2025 - Feb 2026 | {n_total} trades")
    print(f"{'=' * 90}")

    # ── 1. PnL by Regime ──
    print(f"\n{'PNL BY REGIME':^90}")
    print(f"{'─' * 90}")
    print(f"  {'Regime':<20} {'Trades':>7} {'WR%':>7} {'PF':>7} {'Total PnL':>12} {'Exp/Trade':>12}")
    print(f"  {'─' * 68}")

    regimes = defaultdict(list)
    for t in all_trades:
        regimes[t["regime"]].append(t)

    for regime in sorted(regimes.keys(), key=lambda r: sum(t["total_pnl"] for t in regimes[r]), reverse=True):
        s = compute_group_stats(regimes[regime])
        print(f"  {regime:<20} {s['trades']:>7} {s['wr']:>6.1f}% {s['pf']:>7.2f} ${s['total_pnl']:>+10,.2f} ${s['expectancy']:>+10.2f}")

    # ── 2. PnL by HTF Direction ──
    print(f"\n{'PNL BY HTF CONSENSUS DIRECTION':^90}")
    print(f"{'─' * 90}")
    print(f"  {'HTF Direction':<20} {'Trades':>7} {'WR%':>7} {'PF':>7} {'Total PnL':>12} {'Exp/Trade':>12}")
    print(f"  {'─' * 68}")

    htf_groups = defaultdict(list)
    for t in all_trades:
        htf_groups[t["htf_bias"]].append(t)

    for htf in sorted(htf_groups.keys(), key=lambda h: sum(t["total_pnl"] for t in htf_groups[h]), reverse=True):
        s = compute_group_stats(htf_groups[htf])
        print(f"  {htf:<20} {s['trades']:>7} {s['wr']:>6.1f}% {s['pf']:>7.2f} ${s['total_pnl']:>+10,.2f} ${s['expectancy']:>+10.2f}")

    # ── 3. PnL by Session ──
    print(f"\n{'PNL BY SESSION (ET)':^90}")
    print(f"{'─' * 90}")
    print(f"  {'Session':<20} {'Trades':>7} {'WR%':>7} {'PF':>7} {'Total PnL':>12} {'Exp/Trade':>12}")
    print(f"  {'─' * 68}")

    session_groups = defaultdict(list)
    for t in all_trades:
        session_groups[t["session"]].append(t)

    for sess in sorted(session_groups.keys(), key=lambda s: sum(t["total_pnl"] for t in session_groups[s]), reverse=True):
        s = compute_group_stats(session_groups[sess])
        print(f"  {sess:<20} {s['trades']:>7} {s['wr']:>6.1f}% {s['pf']:>7.2f} ${s['total_pnl']:>+10,.2f} ${s['expectancy']:>+10.2f}")

    # ── 4. PnL by Trade Direction ──
    print(f"\n{'PNL BY TRADE DIRECTION':^90}")
    print(f"{'─' * 90}")
    print(f"  {'Direction':<20} {'Trades':>7} {'WR%':>7} {'PF':>7} {'Total PnL':>12} {'Exp/Trade':>12}")
    print(f"  {'─' * 68}")

    dir_groups = defaultdict(list)
    for t in all_trades:
        dir_groups[t["direction"]].append(t)

    for d in sorted(dir_groups.keys(), key=lambda x: sum(t["total_pnl"] for t in dir_groups[x]), reverse=True):
        s = compute_group_stats(dir_groups[d])
        print(f"  {d:<20} {s['trades']:>7} {s['wr']:>6.1f}% {s['pf']:>7.2f} ${s['total_pnl']:>+10,.2f} ${s['expectancy']:>+10.2f}")

    # ── 5. PnL by Month ──
    print(f"\n{'PNL BY MONTH':^90}")
    print(f"{'─' * 90}")
    print(f"  {'Month':<20} {'Trades':>7} {'WR%':>7} {'PF':>7} {'Total PnL':>12} {'Exp/Trade':>12}")
    print(f"  {'─' * 68}")

    month_groups = defaultdict(list)
    for t in all_trades:
        month_groups[t["month"]].append(t)

    for mk in sorted(month_groups.keys()):
        s = compute_group_stats(month_groups[mk])
        print(f"  {mk:<20} {s['trades']:>7} {s['wr']:>6.1f}% {s['pf']:>7.2f} ${s['total_pnl']:>+10,.2f} ${s['expectancy']:>+10.2f}")

    # ── 6. Combined Filter Search ──
    print(f"\n{'=' * 90}")
    print(f"  COMBINED FILTER SEARCH (min 5 trades, sorted by expectancy)")
    print(f"{'=' * 90}")

    # Generate all 1-, 2-, and 3-factor combos
    combo_results = []

    # Single factors
    for key_name, grouper in [
        ("regime", lambda t: t["regime"]),
        ("htf", lambda t: t["htf_bias"]),
        ("session", lambda t: t["session"]),
        ("direction", lambda t: t["direction"]),
    ]:
        groups = defaultdict(list)
        for t in all_trades:
            groups[grouper(t)].append(t)
        for val, trades_group in groups.items():
            if len(trades_group) >= 5:
                s = compute_group_stats(trades_group)
                combo_results.append({
                    "filter": f"{key_name}={val}",
                    **s,
                })

    # Two-factor combos
    factor_pairs = [
        ("regime", "htf", lambda t: t["regime"], lambda t: t["htf_bias"]),
        ("regime", "session", lambda t: t["regime"], lambda t: t["session"]),
        ("regime", "direction", lambda t: t["regime"], lambda t: t["direction"]),
        ("htf", "session", lambda t: t["htf_bias"], lambda t: t["session"]),
        ("htf", "direction", lambda t: t["htf_bias"], lambda t: t["direction"]),
        ("session", "direction", lambda t: t["session"], lambda t: t["direction"]),
    ]

    for name_a, name_b, fn_a, fn_b in factor_pairs:
        groups = defaultdict(list)
        for t in all_trades:
            key = (fn_a(t), fn_b(t))
            groups[key].append(t)
        for (va, vb), trades_group in groups.items():
            if len(trades_group) >= 5:
                s = compute_group_stats(trades_group)
                combo_results.append({
                    "filter": f"{name_a}={va} + {name_b}={vb}",
                    **s,
                })

    # Three-factor combos
    triple_factors = [
        ("regime", "htf", "session", lambda t: t["regime"], lambda t: t["htf_bias"], lambda t: t["session"]),
        ("regime", "htf", "direction", lambda t: t["regime"], lambda t: t["htf_bias"], lambda t: t["direction"]),
        ("regime", "session", "direction", lambda t: t["regime"], lambda t: t["session"], lambda t: t["direction"]),
        ("htf", "session", "direction", lambda t: t["htf_bias"], lambda t: t["session"], lambda t: t["direction"]),
    ]

    for na, nb, nc, fa, fb, fc in triple_factors:
        groups = defaultdict(list)
        for t in all_trades:
            key = (fa(t), fb(t), fc(t))
            groups[key].append(t)
        for (va, vb, vc), trades_group in groups.items():
            if len(trades_group) >= 5:
                s = compute_group_stats(trades_group)
                combo_results.append({
                    "filter": f"{na}={va} + {nb}={vb} + {nc}={vc}",
                    **s,
                })

    # Sort by expectancy
    combo_results.sort(key=lambda x: x["expectancy"])

    # Print toxic combos (bottom)
    toxic = [c for c in combo_results if c["expectancy"] < -5]
    if toxic:
        print(f"\n  TOXIC COMBOS (expectancy < -$5/trade):")
        print(f"  {'─' * 86}")
        print(f"  {'Filter':<55} {'Trades':>6} {'WR%':>6} {'PF':>6} {'PnL':>10} {'Exp':>10}")
        print(f"  {'─' * 86}")
        for c in toxic:
            print(f"  {c['filter']:<55} {c['trades']:>6} {c['wr']:>5.1f}% {c['pf']:>6.2f} ${c['total_pnl']:>+8,.0f} ${c['expectancy']:>+8.2f}")

    # Print high-edge combos (top)
    high_edge = [c for c in combo_results if c["expectancy"] > 20]
    if high_edge:
        print(f"\n  HIGH-EDGE COMBOS (expectancy > $20/trade):")
        print(f"  {'─' * 86}")
        print(f"  {'Filter':<55} {'Trades':>6} {'WR%':>6} {'PF':>6} {'PnL':>10} {'Exp':>10}")
        print(f"  {'─' * 86}")
        for c in sorted(high_edge, key=lambda x: x["expectancy"], reverse=True):
            print(f"  {c['filter']:<55} {c['trades']:>6} {c['wr']:>5.1f}% {c['pf']:>6.2f} ${c['total_pnl']:>+8,.0f} ${c['expectancy']:>+8.2f}")

    # ── 7. Impact Assessment ──
    print(f"\n{'=' * 90}")
    print(f"  IMPACT ASSESSMENT -- What if we block the toxic combos?")
    print(f"{'=' * 90}")

    # Simulate blocking: for each toxic combo, see how many trades and PnL would be removed
    if toxic:
        total_blocked_trades = 0
        total_blocked_pnl = 0.0
        for c in toxic:
            total_blocked_trades += c["trades"]
            total_blocked_pnl += c["total_pnl"]

        remaining_pnl = total_pnl - total_blocked_pnl
        remaining_trades = n_total - total_blocked_trades

        print(f"\n  WARNING: Toxic combos overlap -- blocking all would NOT remove")
        print(f"  {total_blocked_trades} trades (some counted in multiple combos).")
        print(f"  Use the individual combo trade counts as upper bounds.")
        print()
        print(f"  Current:     {n_total} trades, ${total_pnl:+,.2f} PnL")
        print(f"  Toxic PnL:   ${total_blocked_pnl:+,.2f} (sum of toxic combos)")
        print(f"  Max uplift:  ${-total_blocked_pnl:+,.2f} (if all unique)")
    else:
        print(f"\n  No toxic combos found with 5+ trades!")

    # ── 8. Specific old toxic combos check ──
    print(f"\n{'=' * 90}")
    print(f"  STATUS OF PREVIOUSLY IDENTIFIED TOXIC COMBOS (from Feb-only analysis)")
    print(f"{'=' * 90}")

    old_toxic = [
        ("regime=trending_up + htf=bearish", lambda t: t["regime"] == "trending_up" and t["htf_bias"] == "bearish"),
        ("session=overnight + htf=bearish", lambda t: t["session"] == "overnight" and t["htf_bias"] == "bearish"),
        ("regime=unknown + htf=bearish", lambda t: t["regime"] == "unknown" and t["htf_bias"] == "bearish"),
        ("session=afternoon + htf=neutral", lambda t: t["session"] == "afternoon" and t["htf_bias"] == "neutral"),
    ]

    print(f"\n  {'Combo':<45} {'Trades':>6} {'WR%':>6} {'PF':>7} {'PnL':>10} {'Exp':>10} {'Status':>10}")
    print(f"  {'─' * 96}")

    for label, filter_fn in old_toxic:
        matched = [t for t in all_trades if filter_fn(t)]
        if matched:
            s = compute_group_stats(matched)
            status = "TOXIC" if s["expectancy"] < -5 else "OK" if s["expectancy"] >= 0 else "WEAK"
            print(f"  {label:<45} {s['trades']:>6} {s['wr']:>5.1f}% {s['pf']:>7.2f} ${s['total_pnl']:>+8,.0f} ${s['expectancy']:>+8.2f} {status:>10}")
        else:
            print(f"  {label:<45}      0     --       --          --          -- {'NO DATA':>10}")

    print(f"\n{'=' * 90}")

    return combo_results


async def main():
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )
    for name in ["main", "execution", "signals", "features", "risk", "data_pipeline"]:
        logging.getLogger(name).setLevel(logging.WARNING)

    print(f"\n{'=' * 70}")
    print(f"  REGIME CROSS-ANALYSIS -- Priority 3")
    print(f"  Config D + C1 Time Exit | Sep 2025 - Feb 2026 | FirstRate 1m")
    print(f"{'=' * 70}\n")

    data_dir = str(project_dir / "data" / "firstrate")
    print("Loading data...")
    tf_bars = load_firstrate_mtf(data_dir)
    if not tf_bars or EXEC_TF not in tf_bars:
        print(f"ERROR: Missing data. Run aggregate_1m.py first.")
        sys.exit(1)

    monthly = segment_by_month(tf_bars)
    month_keys = sorted(monthly.keys())[-6:]

    print(f"Running {len(month_keys)} months: {month_keys}\n")

    all_trades = []
    for mk in month_keys:
        print(f"  Running {mk}...", end=" ", flush=True)
        month_trades = await run_month_with_trade_details(monthly[mk], mk)
        print(f"{len(month_trades)} trades")
        all_trades.extend(month_trades)

    print(f"\n  Total: {len(all_trades)} paired trades")

    combo_results = print_analysis(all_trades)

    # Save raw data for further analysis
    output_path = str(project_dir / "data" / "firstrate" / "regime_analysis.json")
    with open(output_path, "w") as f:
        json.dump({
            "trades": all_trades,
            "combo_results": combo_results,
            "generated": datetime.now(timezone.utc).isoformat(),
        }, f, indent=2, default=str)
    print(f"\n  Raw data saved: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
