#!/usr/bin/env python3
"""
STOLEN RUNNER ANALYSIS - Critical Research Question R1
=======================================================

Research Question: "Of the C2 trades that exited at breakeven, how many
continued 20+ points in the original direction afterward?"

This script quantifies the exact cost of the breakeven stop by:
1. Identifying all C2 trades that exited with reason "breakeven"
2. Looking forward N bars (default 50) in the price data after exit
3. Tracking Maximum Favorable Excursion (MFE) in the original direction
4. Bucketing results to understand the distribution of "stolen runners"
5. Calculating total estimated profit left on the table

METHODOLOGY:
-----------
For each BE exit:
  - Record exit price and exit time
  - Look forward N bars in 2-minute price data
  - Track max favorable price movement from exit in original direction
  - Bucket by MFE size:
    * A: MFE < 5pts   (correctly stopped — would have lost anyway)
    * B: MFE 5-15pts  (marginal — small continuation)
    * C: MFE 15-30pts (moderate stolen runner)
    * D: MFE 30-50pts (significant stolen runner)
    * E: MFE > 50pts  (major stolen runner — big money left on table)

OUTPUT:
------
1. Distribution across buckets (count, %, avg MFE)
2. Total estimated "stolen profit" (buckets C, D, E: MFE × $2/point)
3. Monthly breakdown if data has timestamps
4. Comparison: stolen profit vs actual C2 trailing profit ($18,604)
5. Visual report showing the cost-benefit analysis of BE stops

USAGE:
------
  python stolen_runner_analysis.py --demo
  python stolen_runner_analysis.py --trades logs/trade_results.json --bars data/combined_1min.csv

"""

import argparse
import json
import csv
import statistics
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict, Tuple, Optional
import random


# MNQ constants
MNQ_POINT_VALUE = 2.0  # $2 per point per contract
COMMISSION_PER_CONTRACT = 1.29


class BarData:
    """Represents a single 2-minute bar."""
    __slots__ = ("timestamp", "open", "high", "low", "close")

    def __init__(self, timestamp: str, open_: float, high: float, low: float, close: float):
        self.timestamp = timestamp
        self.open = open_
        self.high = high
        self.low = low
        self.close = close

    def __repr__(self):
        return f"Bar({self.timestamp} OHLC:{self.open:.2f}/{self.high:.2f}/{self.low:.2f}/{self.close:.2f})"


class BeExitAnalysis:
    """Container for a single BE exit analysis."""
    __slots__ = ("trade_id", "direction", "entry_price", "exit_price", "exit_time",
                 "month", "best_price_after_exit", "mfe_points", "bucket", "stolen_pnl")

    def __init__(self, trade_id: str, direction: str, entry_price: float,
                 exit_price: float, exit_time: str, month: str):
        self.trade_id = trade_id
        self.direction = direction
        self.entry_price = entry_price
        self.exit_price = exit_price
        self.exit_time = exit_time
        self.month = month
        self.best_price_after_exit = None
        self.mfe_points = 0.0
        self.bucket = None
        self.stolen_pnl = 0.0

    def __repr__(self):
        return (f"BE_Exit({self.trade_id} {self.direction} @{self.exit_price:.2f} "
                f"MFE={self.mfe_points:.1f}pts bucket={self.bucket})")


def load_trades_from_json(filepath: Path) -> List[Dict]:
    """Load executor_trade_history from backtest_checkpoint.json."""
    with open(filepath, 'r') as f:
        data = json.load(f)
    return data.get("executor_trade_history", [])


def load_bars_from_csv(filepath: Path) -> List[BarData]:
    """Load 2-minute bars from CSV (time, open, high, low, close, volume)."""
    bars = []
    with open(filepath, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                bar = BarData(
                    timestamp=row['time'],
                    open_=float(row['open']),
                    high=float(row['high']),
                    low=float(row['low']),
                    close=float(row['close'])
                )
                bars.append(bar)
            except (ValueError, KeyError) as e:
                print(f"Warning: skipped malformed bar row: {e}")
                continue
    return bars


def extract_be_exits(trades: List[Dict]) -> List[BeExitAnalysis]:
    """Extract all C2 trades that exited at 'breakeven'."""
    be_exits = []

    for trade in trades:
        c2 = trade.get("c2", {})

        # Skip if not filled or still open
        if c2.get("is_open") or not c2.get("is_filled"):
            continue

        # Only analyze breakeven exits
        if c2.get("exit_reason") != "breakeven":
            continue

        exit_time = c2.get("exit_time", "")
        month = exit_time[:7] if exit_time else "unknown"

        be_exit = BeExitAnalysis(
            trade_id=trade.get("trade_id", "unknown"),
            direction=trade.get("direction", "unknown"),
            entry_price=float(c2.get("entry_price", 0.0)),
            exit_price=float(c2.get("exit_price", 0.0)),
            exit_time=exit_time,
            month=month
        )

        be_exits.append(be_exit)

    return be_exits


def find_bars_after_exit(bars: List[BarData], exit_time: str, lookforward: int) -> List[BarData]:
    """
    Find the N bars after exit_time.

    Args:
        bars: List of all bars, assumed sorted by time
        exit_time: ISO format timestamp of exit
        lookforward: Number of bars to look ahead

    Returns:
        List of up to lookforward bars after exit
    """
    # Parse exit_time to datetime for comparison
    try:
        exit_dt = datetime.fromisoformat(exit_time.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return []

    bars_after = []
    found_exit = False

    for bar in bars:
        try:
            bar_dt = datetime.fromisoformat(bar.timestamp.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            continue

        if not found_exit:
            # Find first bar AFTER exit time
            if bar_dt > exit_dt:
                found_exit = True
                bars_after.append(bar)
        else:
            # Collect subsequent bars
            if len(bars_after) < lookforward:
                bars_after.append(bar)
            else:
                break

    return bars_after


def calculate_mfe(be_exit: BeExitAnalysis, bars_after: List[BarData], lookforward: int) -> Tuple[float, Optional[float]]:
    """
    Calculate Maximum Favorable Excursion (MFE) for a breakeven exit.

    MFE is the furthest price moved in the favorable direction (the direction
    the original trade was going) from the exit price.

    Returns:
        (mfe_points, best_price_after_exit)
    """
    if not bars_after:
        return 0.0, None

    exit_price = be_exit.exit_price
    direction = be_exit.direction

    if direction == "long":
        # For long: favorable is UP, so track highest price
        best_price = max(bar.high for bar in bars_after)
        mfe = best_price - exit_price
    else:  # short
        # For short: favorable is DOWN, so track lowest price
        best_price = min(bar.low for bar in bars_after)
        mfe = exit_price - best_price

    return round(mfe, 2), best_price


def assign_bucket(mfe_points: float) -> str:
    """Assign MFE to bucket A, B, C, D, or E."""
    if mfe_points < 5.0:
        return "A"
    elif mfe_points < 15.0:
        return "B"
    elif mfe_points < 30.0:
        return "C"
    elif mfe_points < 50.0:
        return "D"
    else:
        return "E"


def analyze_be_exits(be_exits: List[BeExitAnalysis], bars: List[BarData], lookforward: int = 50) -> None:
    """
    Analyze each BE exit by looking forward N bars and calculating MFE.

    Modifies be_exits in-place with mfe_points, best_price_after_exit, bucket, stolen_pnl.
    """
    for be_exit in be_exits:
        bars_after = find_bars_after_exit(bars, be_exit.exit_time, lookforward)

        if bars_after:
            mfe_points, best_price = calculate_mfe(be_exit, bars_after, lookforward)
            be_exit.mfe_points = mfe_points
            be_exit.best_price_after_exit = best_price
        else:
            # No bars after exit (e.g., end of day or data)
            be_exit.mfe_points = 0.0
            be_exit.best_price_after_exit = None

        be_exit.bucket = assign_bucket(be_exit.mfe_points)

        # Stolen profit = MFE × $2/point (only for profitable buckets C, D, E)
        if be_exit.bucket in ['C', 'D', 'E']:
            be_exit.stolen_pnl = be_exit.mfe_points * MNQ_POINT_VALUE
        else:
            be_exit.stolen_pnl = 0.0


def generate_report(be_exits: List[BeExitAnalysis]) -> Dict:
    """Generate comprehensive report on BE exit analysis."""

    total_analyzed = len(be_exits)

    if total_analyzed == 0:
        return {
            "total_be_exits": 0,
            "message": "No breakeven exits found in data"
        }

    # Distribution by bucket
    bucket_dist = defaultdict(list)
    for be_exit in be_exits:
        bucket_dist[be_exit.bucket].append(be_exit)

    # Bucket descriptions
    bucket_desc = {
        "A": "< 5pts (correctly stopped)",
        "B": "5-15pts (marginal continuation)",
        "C": "15-30pts (moderate stolen runner)",
        "D": "30-50pts (significant stolen runner)",
        "E": "> 50pts (major stolen runner)",
    }

    bucket_summary = {}
    total_stolen_pnl = 0.0
    total_stolen_pnl_c_d_e = 0.0  # Only buckets with stolen profit

    for bucket in ['A', 'B', 'C', 'D', 'E']:
        exits = bucket_dist.get(bucket, [])
        count = len(exits)
        pct = (count / total_analyzed * 100) if total_analyzed > 0 else 0

        mfe_values = [e.mfe_points for e in exits]
        avg_mfe = statistics.mean(mfe_values) if mfe_values else 0.0
        median_mfe = statistics.median(mfe_values) if mfe_values else 0.0

        stolen_values = [e.stolen_pnl for e in exits]
        total_bucket_stolen = sum(stolen_values)
        total_stolen_pnl += total_bucket_stolen

        if bucket in ['C', 'D', 'E']:
            total_stolen_pnl_c_d_e += total_bucket_stolen

        bucket_summary[bucket] = {
            "description": bucket_desc[bucket],
            "count": count,
            "percentage": round(pct, 1),
            "avg_mfe_points": round(avg_mfe, 1),
            "median_mfe_points": round(median_mfe, 1),
            "min_mfe_points": round(min(mfe_values), 1) if mfe_values else 0,
            "max_mfe_points": round(max(mfe_values), 1) if mfe_values else 0,
            "total_stolen_pnl": round(total_bucket_stolen, 2)
        }

    # Monthly breakdown
    by_month = defaultdict(lambda: {"count": 0, "stolen_pnl": 0.0, "avg_mfe": []})
    for be_exit in be_exits:
        month = be_exit.month
        by_month[month]["count"] += 1
        by_month[month]["stolen_pnl"] += be_exit.stolen_pnl
        by_month[month]["avg_mfe"].append(be_exit.mfe_points)

    monthly_summary = {}
    for month in sorted(by_month.keys()):
        data = by_month[month]
        monthly_summary[month] = {
            "count": data["count"],
            "stolen_pnl": round(data["stolen_pnl"], 2),
            "avg_mfe_points": round(statistics.mean(data["avg_mfe"]), 1) if data["avg_mfe"] else 0.0
        }

    # Statistics
    all_mfe = [e.mfe_points for e in be_exits]
    mfe_stats = {
        "mean": round(statistics.mean(all_mfe), 2),
        "median": round(statistics.median(all_mfe), 2),
        "stdev": round(statistics.stdev(all_mfe), 2) if len(all_mfe) > 1 else 0.0,
        "min": round(min(all_mfe), 2),
        "max": round(max(all_mfe), 2)
    }

    # Key findings
    moderate_or_worse = len(bucket_dist['C']) + len(bucket_dist['D']) + len(bucket_dist['E'])
    moderate_pct = (moderate_or_worse / total_analyzed * 100) if total_analyzed > 0 else 0

    # Comparison with C2 trailing profit
    c2_trailing_profit = 18604.0  # From backtest results
    stolen_as_pct_of_profit = (total_stolen_pnl_c_d_e / c2_trailing_profit * 100) if c2_trailing_profit > 0 else 0

    report = {
        "question": "Of C2 trades exiting at breakeven, how many continued 20+ points in the original direction?",
        "analysis_date": datetime.utcnow().isoformat() + "Z",
        "total_be_exits_analyzed": total_analyzed,
        "mfe_statistics": mfe_stats,
        "bucket_distribution": bucket_summary,
        "monthly_breakdown": monthly_summary,
        "key_findings": {
            "be_exits_with_continuation_over_5pts": len(bucket_dist['B']) + moderate_or_worse,
            "be_exits_with_continuation_over_15pts": moderate_or_worse,
            "percentage_with_15plus_pts_continuation": round(moderate_pct, 1),
            "be_exits_with_continuation_over_20pts": len([e for e in be_exits if e.mfe_points >= 20.0]),
            "percentage_with_20plus_pts_continuation": round(
                (len([e for e in be_exits if e.mfe_points >= 20.0]) / total_analyzed * 100) if total_analyzed > 0 else 0,
                1
            )
        },
        "stolen_profit_analysis": {
            "total_stolen_profit_c_d_e": round(total_stolen_pnl_c_d_e, 2),
            "total_all_buckets": round(total_stolen_pnl, 2),
            "c2_trailing_profit_actual": c2_trailing_profit,
            "stolen_as_pct_of_c2_profit": round(stolen_as_pct_of_profit, 1),
            "interpretation": (
                f"The {moderate_or_worse} BE stops that later moved 15+ points "
                f"represent ${round(total_stolen_pnl_c_d_e, 2)} in potential profit "
                f"({round(stolen_as_pct_of_profit, 1)}% of actual C2 trailing profit). "
                f"This is the cost of the breakeven stop."
            )
        },
        "sample_trades": [
            {
                "trade_id": e.trade_id,
                "direction": e.direction,
                "exit_price": round(e.exit_price, 2),
                "best_price_after": round(e.best_price_after_exit, 2) if e.best_price_after_exit else None,
                "mfe_points": e.mfe_points,
                "bucket": e.bucket,
                "stolen_pnl": round(e.stolen_pnl, 2)
            }
            for e in sorted(be_exits, key=lambda x: -x.mfe_points)[:10]  # Top 10 by MFE
        ]
    }

    return report


def print_report(report: Dict) -> None:
    """Print report in a readable format."""

    if "message" in report:
        print(report["message"])
        return

    print("\n" + "=" * 80)
    print("STOLEN RUNNER ANALYSIS - C2 BREAKEVEN EXITS")
    print("=" * 80)

    print(f"\nRESEARCH QUESTION:")
    print(f"  {report['question']}")

    total = report['total_be_exits_analyzed']
    print(f"\nTOTAL BE EXITS ANALYZED: {total}")

    # Key findings
    findings = report['key_findings']
    print(f"\nKEY FINDINGS:")
    print(f"  BE exits with > 5pts continuation:   {findings['be_exits_with_continuation_over_5pts']} "
          f"({findings['be_exits_with_continuation_over_5pts']/total*100:.1f}%)")
    print(f"  BE exits with > 15pts continuation:  {findings['be_exits_with_continuation_over_15pts']} "
          f"({findings['percentage_with_15plus_pts_continuation']:.1f}%)")
    print(f"  BE exits with > 20pts continuation:  {findings['be_exits_with_continuation_over_20pts']} "
          f"({findings['percentage_with_20plus_pts_continuation']:.1f}%)")

    # MFE statistics
    stats = report['mfe_statistics']
    print(f"\nMFE STATISTICS (Points):")
    print(f"  Mean:                 {stats['mean']}")
    print(f"  Median:               {stats['median']}")
    print(f"  Stdev:                {stats['stdev']}")
    print(f"  Min/Max:              {stats['min']} / {stats['max']}")

    # Distribution
    print(f"\nDISTRIBUTION BY BUCKET:")
    buckets = report['bucket_distribution']
    for bucket in ['A', 'B', 'C', 'D', 'E']:
        b = buckets[bucket]
        bar_width = int(b['percentage'] / 2)
        bar = "█" * bar_width
        print(f"  {bucket} {b['description']:35s} "
              f"  {b['count']:4d} ({b['percentage']:5.1f}%)  {bar}")
        print(f"      avg_mfe={b['avg_mfe_points']:6.1f}pts  median={b['median_mfe_points']:6.1f}pts  "
              f"stolen_pnl=${b['total_stolen_pnl']:10,.2f}")

    # Stolen profit
    stolen = report['stolen_profit_analysis']
    print(f"\nSTOLEN PROFIT ANALYSIS:")
    print(f"  Total stolen profit (buckets C,D,E):  ${stolen['total_stolen_profit_c_d_e']:,.2f}")
    print(f"  Total (all buckets):                  ${stolen['total_all_buckets']:,.2f}")
    print(f"  Actual C2 trailing profit:            ${stolen['c2_trailing_profit_actual']:,.2f}")
    print(f"  Stolen as % of C2 profit:             {stolen['stolen_as_pct_of_c2_profit']:.1f}%")
    print(f"\n  INTERPRETATION:")
    for line in stolen['interpretation'].split('. '):
        if line:
            print(f"    {line}.")

    # Monthly breakdown
    monthly = report['monthly_breakdown']
    if monthly and len(monthly) > 1:
        print(f"\nMONTHLY BREAKDOWN:")
        for month in sorted(monthly.keys()):
            m = monthly[month]
            print(f"  {month}:  {m['count']:3d} exits, "
                  f"${m['stolen_pnl']:10,.2f} stolen, avg_mfe={m['avg_mfe_points']:6.1f}pts")

    # Top trades
    samples = report['sample_trades']
    if samples:
        print(f"\nTOP 10 BE EXITS BY CONTINUATION:")
        for i, trade in enumerate(samples[:10], 1):
            print(f"  {i}. {trade['trade_id']:20s} {trade['direction']:5s} "
                  f"exit={trade['exit_price']:8.2f} best={trade['best_price_after']:8.2f} "
                  f"mfe={trade['mfe_points']:6.1f}pts bucket={trade['bucket']} "
                  f"stolen=${trade['stolen_pnl']:10,.2f}")

    print("\n" + "=" * 80)


def generate_synthetic_data() -> Tuple[List[Dict], List[BarData]]:
    """
    Generate synthetic trades and bars for demo mode.

    Creates 50 synthetic trades, of which ~30 are BE exits with various
    MFE continuations to demonstrate the analysis.
    """

    trades = []
    base_time = datetime(2026, 2, 1, 8, 0, 0)
    base_price = 18000.0

    # Generate 50 synthetic trades
    for i in range(50):
        trade_time = base_time + timedelta(hours=2*i)
        entry_price = base_price + random.uniform(-50, 50)
        direction = random.choice(["long", "short"])

        # Decide if this is a BE exit or other exit
        is_be_exit = random.random() < 0.6

        if is_be_exit:
            # BE exit
            exit_price = entry_price + random.uniform(-1.0, 1.0)
            exit_reason = "breakeven"
        else:
            # Other exit (trailing, stop, etc.)
            exit_price = entry_price + (5.0 if direction == "long" else -5.0)
            exit_reason = random.choice(["trailing", "stop", "time_stop"])

        trade = {
            "trade_id": f"synth-{i:03d}",
            "direction": direction,
            "entry_price": entry_price,
            "atr_at_entry": 10.0,
            "c2": {
                "is_open": False,
                "is_filled": True,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "exit_time": trade_time.isoformat() + "Z",
                "exit_reason": exit_reason,
                "net_pnl": 0.0
            }
        }
        trades.append(trade)

    # Generate 2000 synthetic bars (1000 two-minute periods over ~14 days)
    bars = []
    bar_time = base_time
    current_price = base_price

    for i in range(2000):
        bar_time_iso = bar_time.isoformat() + "Z"

        # Random walk for OHLC
        direction = random.choice([-1, 1])
        change = random.uniform(-5, 5)
        open_price = current_price
        high_price = open_price + abs(change) + random.uniform(0, 2)
        low_price = open_price - abs(change) - random.uniform(0, 2)
        close_price = open_price + change

        bar = BarData(
            timestamp=bar_time_iso,
            open_=open_price,
            high=high_price,
            low=low_price,
            close=close_price
        )
        bars.append(bar)

        current_price = close_price
        bar_time += timedelta(minutes=2)

    return trades, bars


def main():
    parser = argparse.ArgumentParser(
        description="Analyze C2 breakeven exits and quantify stolen runner profits"
    )
    parser.add_argument("--demo", action="store_true",
                       help="Generate synthetic data to demonstrate output")
    parser.add_argument("--trades", type=str, default="logs/backtest_checkpoint.json",
                       help="Path to trade results JSON (executor_trade_history)")
    parser.add_argument("--bars", type=str, default="data/combined_1min.csv",
                       help="Path to 2-minute bar data CSV")
    parser.add_argument("--lookforward", type=int, default=50,
                       help="Number of bars to look forward from each BE exit")
    parser.add_argument("--output", type=str, default="logs/stolen_runner_analysis.json",
                       help="Path to write JSON report")

    args = parser.parse_args()

    print("Loading data...")

    if args.demo:
        print("  [DEMO MODE] Generating synthetic data...")
        trades, bars = generate_synthetic_data()
        print(f"  Generated {len(trades)} synthetic trades and {len(bars)} synthetic bars")
    else:
        trades_path = Path(args.trades)
        bars_path = Path(args.bars)

        if not trades_path.exists():
            print(f"ERROR: Trades file not found: {trades_path}")
            return 1

        if not bars_path.exists():
            print(f"ERROR: Bars file not found: {bars_path}")
            return 1

        print(f"  Loading trades from {trades_path}...")
        trades = load_trades_from_json(trades_path)
        print(f"  Loaded {len(trades)} trades")

        print(f"  Loading bars from {bars_path}...")
        bars = load_bars_from_csv(bars_path)
        print(f"  Loaded {len(bars)} bars")

    print("\nExtracting BE exits...")
    be_exits = extract_be_exits(trades)
    print(f"Found {len(be_exits)} breakeven exits")

    if not be_exits:
        print("No breakeven exits found. Exiting.")
        return 0

    print(f"\nAnalyzing {len(be_exits)} BE exits with lookforward={args.lookforward} bars...")
    analyze_be_exits(be_exits, bars, lookforward=args.lookforward)

    print("Generating report...")
    report = generate_report(be_exits)

    # Print to console
    print_report(report)

    # Write JSON report
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"\nJSON report written to: {output_path}")

    return 0


if __name__ == "__main__":
    exit(main())
