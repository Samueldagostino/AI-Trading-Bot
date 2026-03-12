#!/usr/bin/env python3
"""
Sequential Backtest Runner — Single-Pass with Full HTF Context
================================================================
Combines TradingView .txt files, optionally filters by start date,
builds HTF bars, then runs full_backtest.py in a single sequential pass.

This ensures the HTF gate accumulates full history — no cold-start problem.

Usage:
    python scripts/run_sequential_backtest.py --start-date 2022-07-01 --run
    python scripts/run_sequential_backtest.py --start-date 2022-07-01        # dry-run (compile check)
    python scripts/run_sequential_backtest.py --run                           # full dataset, no filter
"""

import argparse
import asyncio
import csv
import os
import sys
import time as time_module
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent  # nq_bot_vscode/
REPO_ROOT = PROJECT_DIR.parent   # AI-Trading-Bot/

# Data directories
TV_DIR = REPO_ROOT / "data" / "tradingview"
OUTPUT_DIR = PROJECT_DIR / "data" / "historical"
LOGS_DIR = PROJECT_DIR / "logs"


# =====================================================================
#  DATA LOADING
# =====================================================================

def parse_tv_file(filepath: str) -> List[Dict]:
    """Parse a TradingView 1-minute .txt export."""
    bars = []
    skipped = 0
    filename = os.path.basename(filepath)

    with open(filepath, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or not line[0].isdigit():
                continue  # Skip headers and blank lines

            parts = line.split(",")
            if len(parts) < 6:
                skipped += 1
                continue

            try:
                dt_naive = datetime.strptime(parts[0].strip(), "%Y-%m-%d %H:%M:%S")
                dt = dt_naive.replace(tzinfo=ET)

                o = float(parts[1])
                h = float(parts[2])
                lo = float(parts[3])
                c = float(parts[4])
                v = int(float(parts[5]))

                if h < lo or o <= 0:
                    skipped += 1
                    continue

                bars.append({
                    "timestamp": dt,
                    "open": o,
                    "high": h,
                    "low": lo,
                    "close": c,
                    "volume": v,
                })
            except (ValueError, IndexError):
                skipped += 1
                continue

    if skipped > 0:
        print(f"    WARNING: {skipped} rows skipped in {filename}")

    bars.sort(key=lambda b: b["timestamp"])
    return bars


def deduplicate(bars: List[Dict]) -> Tuple[List[Dict], int]:
    """Remove duplicate timestamps, keeping first occurrence."""
    seen = set()
    unique = []
    dupes = 0
    for bar in bars:
        key = bar["timestamp"]
        if key not in seen:
            seen.add(key)
            unique.append(bar)
        else:
            dupes += 1
    return unique, dupes


def write_csv(bars: List[Dict], filepath: str) -> None:
    """Write bars to CSV with header row."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for bar in bars:
            writer.writerow([
                bar["timestamp"].strftime("%Y-%m-%d %H:%M:%S%z"),
                bar["open"],
                bar["high"],
                bar["low"],
                bar["close"],
                bar["volume"],
            ])


# =====================================================================
#  HTF BAR BUILDER (from prepare_historical_data.py)
# =====================================================================

HTF_CONFIGS = [
    ("5m",  5),
    ("15m", 15),
    ("30m", 30),
    ("1H",  60),
    ("4H",  240),
    ("1D",  1440),
]


def build_htf_bars(bars_1m: List[Dict], tf_minutes: int) -> List[Dict]:
    """Build higher-timeframe bars from 1-minute data.

    Uses simple time-bucketing: group 1m bars into tf_minutes windows,
    compute OHLCV for each window. No look-ahead — only completed bars.

    For daily bars (tf_minutes=1440), uses CME session boundary (6 PM ET).
    """
    if not bars_1m:
        return []

    htf_bars = []

    if tf_minutes == 1440:
        # Daily bars: group by CME trading day (6 PM boundary)
        days = {}
        for bar in bars_1m:
            dt = bar["timestamp"]
            if dt.hour >= 18:
                trading_day = (dt + timedelta(days=1)).date()
            else:
                trading_day = dt.date()

            if trading_day not in days:
                days[trading_day] = []
            days[trading_day].append(bar)

        for day_key in sorted(days.keys()):
            group = days[day_key]
            htf_bars.append({
                "timestamp": group[0]["timestamp"],
                "open": group[0]["open"],
                "high": max(b["high"] for b in group),
                "low": min(b["low"] for b in group),
                "close": group[-1]["close"],
                "volume": sum(b["volume"] for b in group),
            })
    else:
        # Intraday: group by aligned time buckets
        buckets = {}
        for bar in bars_1m:
            dt = bar["timestamp"]
            minutes_since_midnight = dt.hour * 60 + dt.minute
            bucket_start = (minutes_since_midnight // tf_minutes) * tf_minutes
            bucket_hour = bucket_start // 60
            bucket_minute = bucket_start % 60
            bucket_ts = dt.replace(hour=bucket_hour, minute=bucket_minute, second=0)

            if bucket_ts not in buckets:
                buckets[bucket_ts] = []
            buckets[bucket_ts].append(bar)

        for ts in sorted(buckets.keys()):
            group = buckets[ts]
            htf_bars.append({
                "timestamp": group[0]["timestamp"],
                "open": group[0]["open"],
                "high": max(b["high"] for b in group),
                "low": min(b["low"] for b in group),
                "close": group[-1]["close"],
                "volume": sum(b["volume"] for b in group),
            })

    return htf_bars


# =====================================================================
#  MAIN
# =====================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Sequential Backtest — Single-Pass with Full HTF Context"
    )
    parser.add_argument(
        "--start-date", type=str, default=None,
        help="Filter bars before this date (YYYY-MM-DD). Bars before this are excluded."
    )
    parser.add_argument(
        "--run", action="store_true",
        help="Actually execute the backtest"
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume from checkpoint if one exists"
    )
    parser.add_argument(
        "--log-level", type=str, default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: WARNING for speed)"
    )
    args = parser.parse_args()

    # Parse start date filter
    start_filter = None
    if args.start_date:
        try:
            start_filter = datetime.strptime(args.start_date, "%Y-%m-%d").replace(tzinfo=ET)
        except ValueError:
            print(f"ERROR: Invalid date format: {args.start_date} (use YYYY-MM-DD)")
            sys.exit(1)

    tag = f"_from_{args.start_date}" if args.start_date else "_full"
    combined_csv = str(OUTPUT_DIR / f"combined_1min{tag}.csv")
    htf_output_dir = str(OUTPUT_DIR)
    output_path = str(LOGS_DIR / f"sequential_backtest{tag}_trades.json")
    summary_path = str(LOGS_DIR / f"sequential_backtest{tag}_summary.txt")

    print("=" * 72)
    print("  SEQUENTIAL BACKTEST — DATA PREPARATION")
    print("=" * 72)
    print(f"  TradingView dir: {TV_DIR}")
    print(f"  Start filter:    {args.start_date or 'None (full dataset)'}")
    print(f"  Output CSV:      {combined_csv}")
    print()

    # ── Step 1: Find and load all .txt files ──
    txt_files = sorted(TV_DIR.glob("*.txt"))
    if not txt_files:
        print(f"ERROR: No .txt files found in {TV_DIR}")
        sys.exit(1)

    print(f"Found {len(txt_files)} data files:")
    all_bars = []
    for tf in txt_files:
        t0 = time_module.time()
        bars = parse_tv_file(str(tf))
        elapsed = time_module.time() - t0
        if bars:
            date_range = f"{bars[0]['timestamp'].strftime('%Y-%m-%d')} -> {bars[-1]['timestamp'].strftime('%Y-%m-%d')}"
        else:
            date_range = "EMPTY"
        print(f"  {tf.name}: {len(bars):,} bars ({date_range}) [{elapsed:.1f}s]")
        all_bars.extend(bars)

    # ── Step 2: Sort and deduplicate ──
    print(f"\nTotal raw bars: {len(all_bars):,}")
    all_bars.sort(key=lambda b: b["timestamp"])
    all_bars, dupes = deduplicate(all_bars)
    print(f"After dedup:    {len(all_bars):,} ({dupes:,} duplicates removed)")

    if all_bars:
        print(f"Full range:     {all_bars[0]['timestamp'].strftime('%Y-%m-%d')} -> "
              f"{all_bars[-1]['timestamp'].strftime('%Y-%m-%d')}")

    # ── Step 3: Apply start date filter ──
    if start_filter:
        before = len(all_bars)
        all_bars = [b for b in all_bars if b["timestamp"] >= start_filter]
        removed = before - len(all_bars)
        print(f"\nFiltered:       {removed:,} bars before {args.start_date} removed")
        print(f"Remaining:      {len(all_bars):,} bars")
        if all_bars:
            print(f"Filtered range: {all_bars[0]['timestamp'].strftime('%Y-%m-%d')} -> "
                  f"{all_bars[-1]['timestamp'].strftime('%Y-%m-%d')}")

    if not all_bars:
        print("ERROR: No bars remain after filtering!")
        sys.exit(1)

    # ── Step 4: Write combined CSV ──
    print(f"\nWriting combined 1-min CSV...")
    write_csv(all_bars, combined_csv)
    print(f"  Written: {len(all_bars):,} bars -> {combined_csv}")

    # ── Step 5: Build HTF bars ──
    print(f"\nBuilding HTF bars from 1-min data...")
    os.makedirs(htf_output_dir, exist_ok=True)
    for tf_label, tf_minutes in HTF_CONFIGS:
        htf_bars = build_htf_bars(all_bars, tf_minutes)
        htf_path = os.path.join(htf_output_dir, f"htf_{tf_label}.csv")
        write_csv(htf_bars, htf_path)
        if htf_bars:
            print(f"  {tf_label:>4s}: {len(htf_bars):>8,} bars  "
                  f"({htf_bars[0]['timestamp'].strftime('%Y-%m-%d')} -> "
                  f"{htf_bars[-1]['timestamp'].strftime('%Y-%m-%d')})")
        else:
            print(f"  {tf_label:>4s}: 0 bars")

    # Free memory
    del all_bars

    print()
    print("=" * 72)
    print("  DATA PREPARATION COMPLETE")
    print("=" * 72)
    print(f"  Combined CSV:  {combined_csv}")
    print(f"  HTF directory: {htf_output_dir}")
    print()

    if not args.run:
        print("  Dry run — data prepared but backtest NOT executed.")
        print(f"  To run: python scripts/run_sequential_backtest.py --start-date {args.start_date or '2022-07-01'} --run")
        print("=" * 72)
        return

    # ══════════════════════════════════════════════════════════════════
    #  PHASE 2: Run the actual backtest
    # ══════════════════════════════════════════════════════════════════
    print("  STARTING SEQUENTIAL BACKTEST...")
    print("=" * 72)
    print()

    # Import and configure logging
    import logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Import the backtest runner
    sys.path.insert(0, str(PROJECT_DIR))
    from scripts.full_backtest import run_backtest

    # Run it
    asyncio.run(run_backtest(
        data_path=combined_csv,
        htf_dir=htf_output_dir,
        output_path=output_path,
        summary_path=summary_path,
        resume=args.resume,
    ))


if __name__ == "__main__":
    main()
