#!/usr/bin/env python3
"""
Phase 1 — Historical Data Preparation & HTF Bar Builder
=========================================================
Loads all 1-minute MNQ .txt files, cleans, deduplicates, performs gap
analysis, builds higher-timeframe bars from the 1-min data (zero
look-ahead bias), and saves everything to data/historical/ for
Phase 2 consumption.

Reusable: build_htf_bars() can be imported by other scripts.

Usage:
    python scripts/prepare_historical_data.py
    python scripts/prepare_historical_data.py --input-dir /path/to/raw
    python scripts/prepare_historical_data.py --output-dir /path/to/output
"""

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, date, time
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ─── CME Session Constants ───────────────────────────────────────
# CME MNQ: Sunday 6:00 PM ET -> Friday 5:00 PM ET
# Daily maintenance: 5:00 PM -> 6:00 PM ET each weekday
SESSION_START_HOUR = 18   # 6 PM ET
SESSION_END_HOUR = 17     # 5 PM ET
TRADING_DAY_BOUNDARY_HOUR = 18  # Daily bars roll at 6 PM ET

# ─── HTF Timeframes to Build ─────────────────────────────────────
HTF_CONFIGS = [
    ("5m",  5),
    ("15m", 15),
    ("30m", 30),
    ("1H",  60),
    ("4H",  240),
    ("1D",  1440),
]


# =====================================================================
#  DATA LOADING & PARSING
# =====================================================================

def find_raw_txt_files(input_dir: str) -> List[str]:
    """Find all .txt data files in the input directory."""
    dir_path = Path(input_dir)
    if not dir_path.exists():
        print(f"ERROR: Input directory not found: {input_dir}")
        sys.exit(1)

    txt_files = sorted(dir_path.glob("*.txt"))
    if not txt_files:
        print(f"ERROR: No .txt files found in {input_dir}")
        sys.exit(1)

    return [str(f) for f in txt_files]


def parse_file(filepath: str) -> List[Dict]:
    """Parse a single historical data .txt file.

    Format: YYYY-MM-DD HH:MM:SS,open,high,low,close,volume
    Non-data lines (headers, blank lines) are auto-skipped.
    Timestamps are localized to America/New_York (ET), DST-aware.
    """
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
        print(f"  WARNING: {skipped} rows skipped in {filename}")

    bars.sort(key=lambda b: b["timestamp"])
    return bars


def deduplicate_bars(bars: List[Dict]) -> Tuple[List[Dict], int]:
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


# =====================================================================
#  SESSION / TRADING-DAY UTILITIES
# =====================================================================

def get_trading_day(dt: datetime) -> date:
    """Get the CME trading day for a given ET datetime.

    CME convention: session runs 6:00 PM ET day D-1 to 5:00 PM ET day D.
    The trading day is labeled as day D (the date of the RTH session).

    - time >= 18:00 ET -> belongs to NEXT calendar day's trading session
    - time <  18:00 ET -> belongs to CURRENT calendar day's trading session
    """
    if dt.hour >= TRADING_DAY_BOUNDARY_HOUR:
        return (dt + timedelta(days=1)).date()
    return dt.date()


def is_weekend(dt: datetime) -> bool:
    """Check if timestamp falls during weekend closure.

    CME MNQ: closed Fri 5:00 PM ET -> Sun 6:00 PM ET.
    """
    wd = dt.weekday()  # Mon=0 ... Sun=6
    h = dt.hour

    if wd == 4 and h >= SESSION_END_HOUR:  # Fri after 5 PM
        return True
    if wd == 5:  # Saturday
        return True
    if wd == 6 and h < SESSION_START_HOUR:  # Sunday before 6 PM
        return True
    return False


# =====================================================================
#  GAP ANALYSIS
# =====================================================================

def _spans_weekend(prev_ts: datetime, curr_ts: datetime) -> bool:
    """Check if the gap between two timestamps spans a weekend.

    CME MNQ closes Friday ~5 PM ET and reopens Sunday 6 PM ET.
    Data often has last bar at 16:59 (before 5 PM close).
    """
    # Check if any Saturday falls between the two timestamps
    d = prev_ts.date()
    end_d = curr_ts.date()
    while d <= end_d:
        if d.weekday() == 5:  # Saturday
            return True
        d += timedelta(days=1)
    return False


def gap_analysis(bars: List[Dict]) -> List[Dict]:
    """Identify gaps > expected in the 1-minute data.

    Expected gaps: weekends, holidays, daily maintenance (5-6 PM ET).
    Unexpected gaps: missing data during a trading session.
    """
    gaps = []
    if len(bars) < 2:
        return gaps

    for i in range(1, len(bars)):
        prev_ts = bars[i - 1]["timestamp"]
        curr_ts = bars[i]["timestamp"]
        gap_minutes = (curr_ts - prev_ts).total_seconds() / 60

        # Normal 1-min spacing or very small gap
        if gap_minutes <= 5:
            continue

        # Check if gap spans a weekend (Saturday between prev and curr)
        spans_wknd = _spans_weekend(prev_ts, curr_ts)

        # Check if gap is the daily maintenance break (5 PM - 6 PM ET, ~61 min)
        is_maintenance = (
            not spans_wknd
            and gap_minutes <= 120
            and prev_ts.hour >= 16
            and curr_ts.hour <= 18
        )

        # Holiday early close + next day gap (e.g. Thanksgiving, Christmas)
        # These are non-weekend gaps > 2 hours that end at 18:00
        is_holiday = (
            not spans_wknd
            and not is_maintenance
            and curr_ts.hour == 18
            and curr_ts.minute == 0
            and gap_minutes > 120
        )

        # Short holiday early close (e.g. 13:14 -> 18:00 same day)
        is_early_close = (
            not spans_wknd
            and not is_maintenance
            and gap_minutes <= 360  # 6 hours
            and curr_ts.hour == 18
            and prev_ts.hour >= 9
        )

        if is_maintenance:
            gap_type = "maintenance"
        elif spans_wknd:
            gap_type = "weekend/holiday"
        elif is_holiday or is_early_close:
            gap_type = "holiday"
        else:
            gap_type = "MISSING DATA"

        gaps.append({
            "start": prev_ts,
            "end": curr_ts,
            "gap_minutes": round(gap_minutes, 1),
            "gap_type": gap_type,
        })

    return gaps


# =====================================================================
#  HTF BAR BUILDER  (reusable across phases)
# =====================================================================

def build_htf_bars(one_min_bars: List[Dict], tf_minutes: int) -> List[Dict]:
    """Aggregate 1-minute bars into higher-timeframe bars.

    CRITICAL: Zero look-ahead bias guaranteed.

    Rules:
    - Floor each 1-min bar's timestamp to the HTF period boundary
    - Group by floored timestamp
    - OHLCV: first open, max high, min low, last close, sum volume
    - A HTF bar is COMPLETE only when the next HTF period begins
    - Incomplete (forming) bars are NEVER emitted

    For daily bars (tf_minutes >= 1440):
      Trading day runs 6:00 PM ET to 6:00 PM ET next day (CME convention).
      DST-aware via ZoneInfo("America/New_York").

    Args:
        one_min_bars: Sorted list of dicts with keys:
                      timestamp (tz-aware ET), open, high, low, close, volume
        tf_minutes:   Timeframe in minutes (5, 15, 30, 60, 240, 1440)

    Returns:
        List of completed HTF bars (dicts with same keys).
    """
    if not one_min_bars:
        return []

    is_daily = tf_minutes >= 1440

    # ── Group bars by their HTF bucket ──
    buckets: Dict = {}

    for bar in one_min_bars:
        ts = bar["timestamp"]

        if is_daily:
            # CME daily: trading day label (date object)
            bucket_key = get_trading_day(ts)
        else:
            # Intraday: floor to tf_minutes boundary in ET
            minutes_since_midnight = ts.hour * 60 + ts.minute
            bucket_start_minutes = (minutes_since_midnight // tf_minutes) * tf_minutes
            bucket_key = ts.replace(
                hour=bucket_start_minutes // 60,
                minute=bucket_start_minutes % 60,
                second=0,
                microsecond=0,
            )

        if bucket_key not in buckets:
            buckets[bucket_key] = []
        buckets[bucket_key].append(bar)

    # ── Sort bucket keys and emit only COMPLETED bars ──
    sorted_keys = sorted(buckets.keys())

    result = []
    for i, key in enumerate(sorted_keys):
        # Last bucket may be incomplete — skip it
        if i == len(sorted_keys) - 1:
            break

        group = buckets[key]
        group.sort(key=lambda b: b["timestamp"])

        if is_daily:
            # Daily bar timestamp: midnight of the trading day in ET
            bucket_ts = datetime(key.year, key.month, key.day, 0, 0, 0, tzinfo=ET)
        else:
            bucket_ts = key

        result.append({
            "timestamp": bucket_ts,
            "open": group[0]["open"],
            "high": max(b["high"] for b in group),
            "low": min(b["low"] for b in group),
            "close": group[-1]["close"],
            "volume": sum(b["volume"] for b in group),
        })

    return result


# =====================================================================
#  CSV I/O
# =====================================================================

def write_csv(bars: List[Dict], filepath: str) -> None:
    """Write bars to CSV with header row. Timestamp in ISO format with TZ."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])

        for bar in bars:
            ts = bar["timestamp"]
            # ISO format preserves timezone and DST info
            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S%z")
            writer.writerow([
                ts_str,
                bar["open"],
                bar["high"],
                bar["low"],
                bar["close"],
                bar["volume"],
            ])


# =====================================================================
#  MAIN
# =====================================================================

def resolve_input_dir(specified: Optional[str]) -> str:
    """Auto-detect the raw data directory if not specified."""
    if specified:
        return specified

    # Try common locations relative to this script
    script_dir = Path(__file__).resolve().parent
    project_dir = script_dir.parent  # nq_bot_vscode/
    repo_root = project_dir.parent   # AI-Trading-Bot/

    candidates = [
        project_dir / "data" / "historical",
        repo_root / "data" / "firstrate" / "raw",
        repo_root / "data" / "historical",
    ]

    for cand in candidates:
        if cand.exists() and list(cand.glob("*.txt")):
            return str(cand)

    print("ERROR: Could not auto-detect raw data directory.")
    print("  Tried:", [str(c) for c in candidates])
    print("  Use --input-dir to specify the path to the .txt files.")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Phase 1 — Historical data preparation and HTF bar builder"
    )
    parser.add_argument(
        "--input-dir", type=str, default=None,
        help="Directory containing raw .txt files (auto-detected if omitted)"
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="Output directory for cleaned data (default: data/historical/)"
    )
    args = parser.parse_args()

    # ── Resolve paths ──
    input_dir = resolve_input_dir(args.input_dir)

    script_dir = Path(__file__).resolve().parent
    project_dir = script_dir.parent
    output_dir = args.output_dir or str(project_dir / "data" / "historical")
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 70)
    print("  PHASE 1 — HISTORICAL DATA PREPARATION")
    print("=" * 70)
    print(f"  Input:  {input_dir}")
    print(f"  Output: {output_dir}")
    print()

    # ══════════════════════════════════════════════════════════════════
    #  STEP 1: Parse each file independently
    # ══════════════════════════════════════════════════════════════════
    txt_files = find_raw_txt_files(input_dir)
    print(f"Found {len(txt_files)} .txt files:")
    print()

    all_bars: List[Dict] = []
    file_reports = []

    for filepath in txt_files:
        filename = os.path.basename(filepath)
        bars = parse_file(filepath)

        if not bars:
            print(f"  {filename}: EMPTY (no valid bars)")
            continue

        first_dt = bars[0]["timestamp"]
        last_dt = bars[-1]["timestamp"]
        report = {
            "file": filename,
            "bars": len(bars),
            "first": first_dt.strftime("%Y-%m-%d"),
            "last": last_dt.strftime("%Y-%m-%d"),
        }
        file_reports.append(report)

        print(f"  {filename}")
        print(f"    Bars: {len(bars):>10,}")
        print(f"    Range: {first_dt.strftime('%Y-%m-%d %H:%M')} -> "
              f"{last_dt.strftime('%Y-%m-%d %H:%M')} ET")
        print()

        all_bars.extend(bars)

    print(f"  Total bars loaded (pre-dedup): {len(all_bars):,}")
    print()

    # ══════════════════════════════════════════════════════════════════
    #  STEP 2: Concatenate chronologically
    # ══════════════════════════════════════════════════════════════════
    print("Concatenating and sorting chronologically...")
    all_bars.sort(key=lambda b: b["timestamp"])
    print(f"  Sorted: {len(all_bars):,} bars")
    print()

    # ══════════════════════════════════════════════════════════════════
    #  STEP 3: Deduplicate (keep first occurrence)
    # ══════════════════════════════════════════════════════════════════
    print("Deduplicating (keeping first occurrence)...")
    clean_bars, dupe_count = deduplicate_bars(all_bars)
    print(f"  Duplicates removed: {dupe_count:,}")
    print(f"  Bars after dedup:   {len(clean_bars):,}")
    print()

    # ══════════════════════════════════════════════════════════════════
    #  STEP 4: Gap analysis
    # ══════════════════════════════════════════════════════════════════
    print("Running gap analysis...")
    gaps = gap_analysis(clean_bars)

    # Separate expected vs unexpected gaps
    missing_gaps = [g for g in gaps if g["gap_type"] == "MISSING DATA"]
    expected_gaps = [g for g in gaps if g["gap_type"] != "MISSING DATA"]

    print(f"  Total gaps detected:    {len(gaps)}")
    print(f"  Weekend/holiday/maint:  {len(expected_gaps)} (expected)")
    print(f"  Missing data gaps:      {len(missing_gaps)}")

    if missing_gaps:
        print()
        print("  *** MISSING DATA GAPS ***")
        for g in missing_gaps:
            print(f"    {g['start'].strftime('%Y-%m-%d %H:%M')} -> "
                  f"{g['end'].strftime('%Y-%m-%d %H:%M')} "
                  f"({g['gap_minutes']:.0f} min)")

    # Log a few weekend gaps for sanity
    if expected_gaps:
        print()
        print(f"  Sample weekend/holiday gaps (first 5):")
        for g in expected_gaps[:5]:
            print(f"    {g['start'].strftime('%Y-%m-%d %H:%M')} -> "
                  f"{g['end'].strftime('%Y-%m-%d %H:%M')} "
                  f"({g['gap_minutes']:.0f} min, {g['gap_type']})")
    print()

    # ══════════════════════════════════════════════════════════════════
    #  STEP 5: Final dataset report
    # ══════════════════════════════════════════════════════════════════
    trading_days = sorted(set(get_trading_day(b["timestamp"]) for b in clean_bars))
    first_ts = clean_bars[0]["timestamp"]
    last_ts = clean_bars[-1]["timestamp"]

    print("=" * 70)
    print("  FINAL DATASET SUMMARY")
    print("=" * 70)
    print(f"  Total bars:    {len(clean_bars):>12,}")
    print(f"  Date range:    {first_ts.strftime('%Y-%m-%d')} -> "
          f"{last_ts.strftime('%Y-%m-%d')}")
    print(f"  Trading days:  {len(trading_days):>12,}")
    print(f"  Price range:   {min(b['low'] for b in clean_bars):.2f} -> "
          f"{max(b['high'] for b in clean_bars):.2f}")
    print(f"  Total volume:  {sum(b['volume'] for b in clean_bars):>12,}")
    print()

    # ══════════════════════════════════════════════════════════════════
    #  STEP 6: Save cleaned 1-min dataset
    # ══════════════════════════════════════════════════════════════════
    combined_path = os.path.join(output_dir, "combined_1min.csv")
    print(f"Saving cleaned 1-min data to {combined_path} ...")
    write_csv(clean_bars, combined_path)
    print(f"  Written: {len(clean_bars):,} bars")
    print()

    # ══════════════════════════════════════════════════════════════════
    #  STEP 7: Build HTF bars from 1-min data (zero look-ahead)
    # ══════════════════════════════════════════════════════════════════
    print("=" * 70)
    print("  BUILDING HTF BARS FROM 1-MIN DATA")
    print("  (no pre-computed files — zero look-ahead bias)")
    print("=" * 70)

    for tf_label, tf_minutes in HTF_CONFIGS:
        htf_bars = build_htf_bars(clean_bars, tf_minutes)
        htf_path = os.path.join(output_dir, f"htf_{tf_label}.csv")
        write_csv(htf_bars, htf_path)
        if htf_bars:
            print(f"  {tf_label:>4s}: {len(htf_bars):>8,} bars  "
                  f"({htf_bars[0]['timestamp'].strftime('%Y-%m-%d')} -> "
                  f"{htf_bars[-1]['timestamp'].strftime('%Y-%m-%d')})")
        else:
            print(f"  {tf_label:>4s}: 0 bars (empty)")

    print()
    print("=" * 70)
    print("  PHASE 1 COMPLETE")
    print("=" * 70)
    print(f"  Output directory: {output_dir}/")
    print(f"  Files written:")
    print(f"    combined_1min.csv   ({len(clean_bars):,} bars)")
    for tf_label, _ in HTF_CONFIGS:
        print(f"    htf_{tf_label}.csv")
    print()
    print("  Next: Phase 2 — run scripts/full_backtest.py")


if __name__ == "__main__":
    main()
