"""
1-Minute OHLCV Aggregator for FirstRate NQ Data
=================================================
Aggregates 1-minute bars into standard higher timeframes:
  2m, 3m, 5m, 15m, 30m, 1H, 4H, 1D

Uses proper OHLCV aggregation:
  - Open  = first open in window
  - High  = max high in window
  - Low   = min low in window
  - Close = last close in window
  - Volume = sum of all volumes in window

Bars are aligned to standard CME NQ market boundaries:
  - Intraday: aligned to midnight UTC (00:00)
  - Daily:    full calendar day (00:00–23:59 UTC)
  - 4H:      00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC

Usage:
    python scripts/aggregate_1m.py                    # Auto-detect 1m CSV in data/firstrate/
    python scripts/aggregate_1m.py --input path.csv   # Specific file
    python scripts/aggregate_1m.py --output-dir dir/  # Custom output directory

Output:
    data/firstrate/NQ_2m.csv
    data/firstrate/NQ_3m.csv
    data/firstrate/NQ_5m.csv
    data/firstrate/NQ_15m.csv
    data/firstrate/NQ_30m.csv
    data/firstrate/NQ_1H.csv
    data/firstrate/NQ_4H.csv
    data/firstrate/NQ_1D.csv
"""

import argparse
import csv
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

# Timeframes to generate: (label, minutes_per_bar)
TIMEFRAMES = [
    ("2m", 2),
    ("3m", 3),
    ("5m", 5),
    ("15m", 15),
    ("30m", 30),
    ("1H", 60),
    ("4H", 240),
    ("1D", 1440),
]


def detect_1m_csv(directory: str) -> Optional[str]:
    """Find the 1-minute CSV in the given directory.

    Looks for common FirstRate naming patterns:
      - NQ_1m.csv, NQ_1min.csv
      - NQ_full_1min.csv
      - Any CSV with '1m' or '1min' in the name
      - Falls back to the single/largest CSV if only one exists
    """
    dir_path = Path(directory)
    if not dir_path.exists():
        return None

    csvs = sorted(dir_path.glob("*.csv"))
    if not csvs:
        return None

    # Priority: files with '1m' or '1min' in name
    for f in csvs:
        name_lower = f.name.lower()
        if "1m" in name_lower or "1min" in name_lower:
            return str(f)

    # Fallback: if there's exactly one CSV, assume it's the 1m data
    if len(csvs) == 1:
        return str(csvs[0])

    # Fallback: largest CSV is likely the 1m data
    largest = max(csvs, key=lambda f: f.stat().st_size)
    return str(largest)


def _looks_like_timestamp(value: str) -> bool:
    """Check if a string looks like a timestamp rather than a column name."""
    v = value.strip()
    # Starts with a digit → likely data, not a header
    if v and v[0].isdigit():
        return True
    return False


def detect_csv_format(filepath: str) -> Dict:
    """Auto-detect CSV format: column names, delimiter, timestamp format.

    Handles both files with headers and headerless files (like FirstRate).

    Returns dict with keys:
      delimiter, time (col index), open, high, low, close, volume,
      ts_format ('unix', 'iso', 'us', 'standard'), has_header (bool)
    """
    with open(filepath, "r", encoding="utf-8-sig") as f:
        sample = f.read(4096)

    # Detect delimiter
    delimiter = ","
    if "\t" in sample and sample.count("\t") > sample.count(","):
        delimiter = "\t"

    lines = sample.strip().split("\n")
    first_line = lines[0]
    fields = [h.strip().lower() for h in first_line.split(delimiter)]

    # Detect whether first row is a header or data
    # If the first field looks like a timestamp/number, there's no header
    has_header = not _looks_like_timestamp(fields[0])

    col_map = {"has_header": has_header}

    if has_header:
        time_names = ["time", "date", "datetime", "timestamp", "date_time"]
        open_names = ["open", "o"]
        high_names = ["high", "h"]
        low_names = ["low", "l"]
        close_names = ["close", "c", "last"]
        vol_names = ["volume", "vol", "v"]

        for std, candidates in [
            ("time", time_names),
            ("open", open_names),
            ("high", high_names),
            ("low", low_names),
            ("close", close_names),
            ("volume", vol_names),
        ]:
            for c in candidates:
                if c in fields:
                    col_map[std] = fields.index(c)
                    break
    else:
        # Headerless: assume positional — datetime,open,high,low,close,volume
        col_map["time"] = 0
        col_map["open"] = 1
        col_map["high"] = 2
        col_map["low"] = 3
        col_map["close"] = 4
        col_map["volume"] = 5

    # Detect timestamp format from first data row
    data_row_idx = 1 if has_header else 0
    if len(lines) > data_row_idx:
        first_row = lines[data_row_idx].split(delimiter)
        time_val = first_row[col_map.get("time", 0)].strip()

        # Unix timestamp (integer or float)
        try:
            ts = float(time_val)
            if ts > 1_000_000_000_000:  # milliseconds
                col_map["ts_format"] = "unix_ms"
            else:
                col_map["ts_format"] = "unix"
        except ValueError:
            if "T" in time_val:
                col_map["ts_format"] = "iso"
            elif "/" in time_val:
                col_map["ts_format"] = "us"
            else:
                col_map["ts_format"] = "standard"

    col_map["delimiter"] = delimiter
    return col_map


def parse_timestamp(value: str, fmt: str) -> Optional[datetime]:
    """Parse a timestamp string into a UTC datetime."""
    value = value.strip()
    if not value:
        return None

    if fmt == "unix":
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif fmt == "unix_ms":
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    elif fmt == "iso":
        for pattern in ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"]:
            try:
                dt = datetime.strptime(value, pattern)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
    elif fmt == "us":
        for pattern in ["%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"]:
            try:
                dt = datetime.strptime(value, pattern)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    else:
        for pattern in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]:
            try:
                dt = datetime.strptime(value, pattern)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

    # Last resort: try as unix
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


def load_1m_bars(filepath: str) -> List[Dict]:
    """Load 1-minute bars from CSV. Returns list of dicts with standard keys."""
    fmt = detect_csv_format(filepath)
    bars = []

    with open(filepath, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f, delimiter=fmt["delimiter"])
        if fmt.get("has_header", True):
            next(reader)  # skip header only if present

        time_idx = fmt.get("time", 0)
        open_idx = fmt.get("open", 1)
        high_idx = fmt.get("high", 2)
        low_idx = fmt.get("low", 3)
        close_idx = fmt.get("close", 4)
        vol_idx = fmt.get("volume", 5)
        ts_format = fmt.get("ts_format", "unix")

        for row_num, row in enumerate(reader, start=2):
            try:
                if len(row) < 5:
                    continue

                ts = parse_timestamp(row[time_idx], ts_format)
                if ts is None:
                    continue

                o = float(row[open_idx])
                h = float(row[high_idx])
                lo = float(row[low_idx])
                c = float(row[close_idx])
                v = int(float(row[vol_idx])) if vol_idx < len(row) else 0

                if h < lo or o <= 0:
                    continue

                bars.append({
                    "timestamp": ts,
                    "open": o,
                    "high": h,
                    "low": lo,
                    "close": c,
                    "volume": v,
                })
            except (ValueError, IndexError):
                continue

    # Sort chronologically
    bars.sort(key=lambda b: b["timestamp"])
    return bars


def get_bar_bucket(ts: datetime, minutes: int) -> datetime:
    """Compute the aligned bucket start time for a given timestamp.

    Alignment rules:
      - Intraday (< 1D): Align to midnight UTC boundaries.
        e.g. for 15m: 00:00, 00:15, 00:30, ...
      - Daily: Align to date boundary (00:00 UTC).
    """
    if minutes >= 1440:
        # Daily: align to start of day
        return ts.replace(hour=0, minute=0, second=0, microsecond=0)

    # Minutes since midnight UTC
    minutes_since_midnight = ts.hour * 60 + ts.minute
    bucket_start_minutes = (minutes_since_midnight // minutes) * minutes

    return ts.replace(
        hour=bucket_start_minutes // 60,
        minute=bucket_start_minutes % 60,
        second=0,
        microsecond=0,
    )


def aggregate_bars(bars_1m: List[Dict], minutes: int) -> List[Dict]:
    """Aggregate 1-minute bars into a higher timeframe.

    OHLCV rules:
      O = first open, H = max high, L = min low, C = last close, V = sum volume
    """
    if not bars_1m:
        return []

    buckets: Dict[datetime, List[Dict]] = {}

    for bar in bars_1m:
        bucket = get_bar_bucket(bar["timestamp"], minutes)
        if bucket not in buckets:
            buckets[bucket] = []
        buckets[bucket].append(bar)

    result = []
    for bucket_time in sorted(buckets.keys()):
        group = buckets[bucket_time]
        # Sort group by timestamp to ensure correct first/last
        group.sort(key=lambda b: b["timestamp"])

        result.append({
            "timestamp": bucket_time,
            "open": group[0]["open"],
            "high": max(b["high"] for b in group),
            "low": min(b["low"] for b in group),
            "close": group[-1]["close"],
            "volume": sum(b["volume"] for b in group),
        })

    return result


def write_csv(bars: List[Dict], filepath: str) -> None:
    """Write aggregated bars to CSV in TradingView-compatible format.

    Format: time (unix epoch), open, high, low, close, Volume
    This matches the existing TradingView CSV format used by the pipeline.
    """
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time", "open", "high", "low", "close", "Volume"])

        for bar in bars:
            ts = int(bar["timestamp"].timestamp())
            writer.writerow([
                ts,
                bar["open"],
                bar["high"],
                bar["low"],
                bar["close"],
                bar["volume"],
            ])


def main():
    parser = argparse.ArgumentParser(
        description="Aggregate 1-minute NQ bars into higher timeframes"
    )
    parser.add_argument(
        "--input", type=str, default=None,
        help="Path to 1-minute CSV (auto-detects in data/firstrate/ if omitted)"
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="Output directory (defaults to same as input file)"
    )
    args = parser.parse_args()

    # Find the project root (nq_bot_vscode/)
    script_dir = Path(__file__).resolve().parent
    project_dir = script_dir.parent
    default_data_dir = project_dir / "data" / "firstrate"

    # Find input file
    input_path = args.input
    if not input_path:
        input_path = detect_1m_csv(str(default_data_dir))
        if not input_path:
            print(f"ERROR: No 1-minute CSV found in {default_data_dir}")
            print("Place your FirstRate 1m data there, or use --input <path>")
            sys.exit(1)

    if not os.path.exists(input_path):
        print(f"ERROR: File not found: {input_path}")
        sys.exit(1)

    output_dir = args.output_dir or str(Path(input_path).parent)

    print(f"{'=' * 60}")
    print(f"  FIRSTRATE 1M AGGREGATOR")
    print(f"{'=' * 60}")
    print(f"  Input:  {input_path}")
    print(f"  Output: {output_dir}/")
    print()

    # Load 1m data
    print("Loading 1-minute data...")
    bars_1m = load_1m_bars(input_path)

    if not bars_1m:
        print("ERROR: No valid bars loaded from input file.")
        sys.exit(1)

    # Report what we have
    first_ts = bars_1m[0]["timestamp"]
    last_ts = bars_1m[-1]["timestamp"]
    days = (last_ts - first_ts).days
    price_low = min(b["low"] for b in bars_1m)
    price_high = max(b["high"] for b in bars_1m)
    total_vol = sum(b["volume"] for b in bars_1m)

    print(f"  Rows:        {len(bars_1m):,}")
    print(f"  Date range:  {first_ts.strftime('%Y-%m-%d %H:%M')} → {last_ts.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"  Span:        {days} days")
    print(f"  Price range: {price_low:.2f} – {price_high:.2f}")
    print(f"  Total volume: {total_vol:,}")
    print()

    # First 5 rows
    print("  First 5 bars:")
    for b in bars_1m[:5]:
        print(f"    {b['timestamp'].strftime('%Y-%m-%d %H:%M')}  "
              f"O={b['open']:.2f}  H={b['high']:.2f}  L={b['low']:.2f}  "
              f"C={b['close']:.2f}  V={b['volume']}")

    print()
    print("  Last 5 bars:")
    for b in bars_1m[-5:]:
        print(f"    {b['timestamp'].strftime('%Y-%m-%d %H:%M')}  "
              f"O={b['open']:.2f}  H={b['high']:.2f}  L={b['low']:.2f}  "
              f"C={b['close']:.2f}  V={b['volume']}")
    print()

    # Aggregate each timeframe
    print(f"{'=' * 60}")
    print(f"  AGGREGATING")
    print(f"{'=' * 60}")

    for tf_label, tf_minutes in TIMEFRAMES:
        agg = aggregate_bars(bars_1m, tf_minutes)
        outfile = os.path.join(output_dir, f"NQ_{tf_label}.csv")
        write_csv(agg, outfile)
        print(f"  {tf_label:>4s}: {len(agg):>7,} bars → {outfile}")

    # Also save a copy of the 1m data in standard format
    outfile_1m = os.path.join(output_dir, "NQ_1m.csv")
    write_csv(bars_1m, outfile_1m)
    print(f"  {'1m':>4s}: {len(bars_1m):>7,} bars → {outfile_1m} (standardized)")

    print(f"\n{'=' * 60}")
    print(f"  DONE — {len(TIMEFRAMES) + 1} files written to {output_dir}/")
    print(f"{'=' * 60}")
    print()
    print("Next step: run out-of-sample validation:")
    print(f"  python scripts/run_oos_validation.py --data-dir {output_dir}")


if __name__ == "__main__":
    main()
