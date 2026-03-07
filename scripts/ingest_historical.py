"""
Historical NQ Data Ingestion & Normalization
=============================================
Reads Samuel's uploaded .txt files from repo root, normalizes to
standard FirstRate CSV format, deduplicates overlapping ranges,
and outputs clean period-labeled CSVs to data/firstrate/historical/.

Format discovery:
  - All files are headerless CSV (some have a title line at top)
  - Format: DateTime,Open,High,Low,Close,Volume
  - DateTime format: YYYY-MM-DD HH:MM:SS
  - Delimiter: comma
  - This matches FirstRate 1m format exactly (no conversion needed)

Output format (same as aggregate_1m.py):
  time (unix epoch),open,high,low,close,Volume
"""

import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections import OrderedDict

REPO_ROOT = Path(__file__).resolve().parent.parent

# Map: source filename -> (clean_name, start_label, end_label)
FILE_MAP = OrderedDict([
    ("September (2021) - Feb (2022) (6-Months).txt", {
        "clean": "NQ_1m_2021-09_to_2022-02.csv",
        "period": "Period 1: Sep 2021 - Feb 2022",
    }),
    ("March - August (2022) (6-Months).txt", {
        "clean": "NQ_1m_2022-03_to_2022-08.csv",
        "period": "Period 2: Mar 2022 - Aug 2022",
    }),
    ("Feb (2023) - September (2023) (6-Months).txt", {
        "clean": "NQ_1m_2022-09_to_2023-02.csv",
        "period": "Period 3a: Sep 2022 - Feb 2023",
    }),
    ("March - August (2023) (6-Months).txt", {
        "clean": "NQ_1m_2023-03_to_2023-08.csv",
        "period": "Period 3b: Mar 2023 - Aug 2023",
    }),
    ("September (2023) - Feb (2024) (6--Months).txt", {
        "clean": "NQ_1m_2023-09_to_2024-02.csv",
        "period": "Period 4: Sep 2023 - Feb 2024",
    }),
    ("March - August 2024 (6-Months).txt", {
        "clean": "NQ_1m_2024-03_to_2024-08.csv",
        "period": "Period 5: Mar 2024 - Aug 2024",
    }),
])


def parse_txt_file(filepath: str):
    """Parse a historical .txt data file.

    Handles the title line(s) at the top and blank lines.
    Returns list of (datetime, open, high, low, close, volume) tuples.
    """
    bars = []
    skipped = 0

    with open(filepath, 'r', encoding='utf-8-sig') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue

            # Skip title/header lines (don't start with digit)
            if not line[0].isdigit():
                skipped += 1
                continue

            parts = line.split(',')
            if len(parts) < 6:
                skipped += 1
                continue

            try:
                dt = datetime.strptime(parts[0].strip(), "%Y-%m-%d %H:%M:%S")
                dt = dt.replace(tzinfo=timezone.utc)
                o = float(parts[1])
                h = float(parts[2])
                lo = float(parts[3])
                c = float(parts[4])
                v = int(float(parts[5]))

                if h < lo or o <= 0:
                    skipped += 1
                    continue

                bars.append((dt, o, h, lo, c, v))
            except (ValueError, IndexError):
                skipped += 1
                continue

    # Sort chronologically
    bars.sort(key=lambda x: x[0])
    return bars, skipped


def write_normalized_csv(bars, filepath: str):
    """Write bars to normalized CSV format (unix epoch timestamps)."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    with open(filepath, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["time", "open", "high", "low", "close", "Volume"])

        for dt, o, h, lo, c, v in bars:
            ts = int(dt.timestamp())
            writer.writerow([ts, o, h, lo, c, v])


def detect_gaps(bars, max_gap_minutes=10):
    """Detect gaps larger than max_gap_minutes in the data.

    Only checks during trading hours (Sun 6PM - Fri 5PM ET).
    """
    gaps = []
    for i in range(1, len(bars)):
        dt_prev = bars[i-1][0]
        dt_curr = bars[i][0]
        diff_minutes = (dt_curr - dt_prev).total_seconds() / 60

        # Skip weekend gaps and daily maintenance gaps
        if diff_minutes > max_gap_minutes:
            # Check if it's a weekend (Fri->Sun gap)
            if dt_prev.weekday() == 4 and dt_curr.weekday() == 6:
                continue
            # Daily maintenance (about 60 min gap)
            if 55 <= diff_minutes <= 65:
                continue
            gaps.append({
                "from": dt_prev.strftime("%Y-%m-%d %H:%M"),
                "to": dt_curr.strftime("%Y-%m-%d %H:%M"),
                "gap_minutes": int(diff_minutes),
            })

    return gaps


def check_overlap(bars1, bars2):
    """Check for overlapping timestamps between two bar sets."""
    if not bars1 or not bars2:
        return []

    set1_start, set1_end = bars1[0][0], bars1[-1][0]
    set2_start, set2_end = bars2[0][0], bars2[-1][0]

    # Check if ranges overlap
    if set1_end < set2_start or set2_end < set1_start:
        return []

    # Find overlapping timestamps
    timestamps1 = {b[0] for b in bars1}
    timestamps2 = {b[0] for b in bars2}
    overlap = timestamps1 & timestamps2

    return sorted(overlap)


def merge_and_deduplicate(bars1, bars2):
    """Merge two bar sets, deduplicating by timestamp (keep first occurrence)."""
    seen = {}
    for bar in bars1:
        if bar[0] not in seen:
            seen[bar[0]] = bar
    for bar in bars2:
        if bar[0] not in seen:
            seen[bar[0]] = bar

    merged = sorted(seen.values(), key=lambda x: x[0])
    return merged


def main():
    print("=" * 70)
    print("  HISTORICAL NQ DATA INGESTION")
    print("=" * 70)

    hist_dir = REPO_ROOT / "data" / "firstrate" / "historical"
    raw_dir = REPO_ROOT / "data" / "firstrate" / "raw"
    os.makedirs(hist_dir, exist_ok=True)
    os.makedirs(raw_dir, exist_ok=True)

    all_results = []
    all_bars_by_file = {}

    # Source files live in data/tradingview/ (moved from repo root)
    tv_dir = REPO_ROOT / "data" / "tradingview"

    for filename, info in FILE_MAP.items():
        # Check data/tradingview/ first, fall back to repo root
        src_path = tv_dir / filename
        if not src_path.exists():
            src_path = REPO_ROOT / filename
        if not src_path.exists():
            print(f"\n  WARNING: File not found: {filename}")
            print(f"           Looked in: {tv_dir}")
            continue

        print(f"\n{'─' * 70}")
        print(f"  FILE: {filename}")
        print(f"  {info['period']}")
        print(f"{'─' * 70}")

        # Parse
        bars, skipped = parse_txt_file(str(src_path))

        if not bars:
            print(f"  ERROR: No valid bars parsed!")
            continue

        all_bars_by_file[filename] = bars

        # Report
        first_dt = bars[0][0]
        last_dt = bars[-1][0]
        days = (last_dt - first_dt).days
        price_low = min(b[3] for b in bars)
        price_high = max(b[2] for b in bars)
        total_vol = sum(b[5] for b in bars)

        print(f"  Bars:        {len(bars):,}")
        print(f"  Skipped:     {skipped}")
        print(f"  Date range:  {first_dt.strftime('%Y-%m-%d %H:%M')} → {last_dt.strftime('%Y-%m-%d %H:%M')}")
        print(f"  Span:        {days} days")
        print(f"  Price range: {price_low:.2f} – {price_high:.2f}")
        print(f"  Total volume: {total_vol:,}")

        # Detect gaps
        gaps = detect_gaps(bars)
        if gaps:
            print(f"  Gaps (>{10}min): {len(gaps)}")
            for g in gaps[:5]:
                print(f"    {g['from']} → {g['to']} ({g['gap_minutes']}min)")
            if len(gaps) > 5:
                print(f"    ... and {len(gaps) - 5} more")
        else:
            print(f"  Gaps: None detected")

        # Write normalized CSV
        out_path = hist_dir / info["clean"]
        write_normalized_csv(bars, str(out_path))
        print(f"  Output:      {out_path}")

        all_results.append({
            "filename": filename,
            "clean_name": info["clean"],
            "period": info["period"],
            "bar_count": len(bars),
            "start": first_dt.strftime("%Y-%m-%d"),
            "end": last_dt.strftime("%Y-%m-%d"),
            "days": days,
            "price_low": price_low,
            "price_high": price_high,
            "gaps": len(gaps),
        })

    # Check overlaps between adjacent files
    print(f"\n{'=' * 70}")
    print(f"  OVERLAP DETECTION")
    print(f"{'=' * 70}")

    filenames = list(all_bars_by_file.keys())
    for i in range(len(filenames)):
        for j in range(i + 1, len(filenames)):
            f1, f2 = filenames[i], filenames[j]
            overlap = check_overlap(all_bars_by_file[f1], all_bars_by_file[f2])
            if overlap:
                print(f"\n  OVERLAP: {f1}")
                print(f"       vs: {f2}")
                print(f"  Overlapping bars: {len(overlap)}")
                print(f"  Range: {overlap[0].strftime('%Y-%m-%d %H:%M')} → {overlap[-1].strftime('%Y-%m-%d %H:%M')}")
            else:
                # Only print non-overlap for adjacent periods
                pass

    # Create merged Period 3 (Feb 2023 - Aug 2023 = files 3a + 3b)
    f3a = "Feb (2023) - September (2023) (6-Months).txt"
    f3b = "March - August (2023) (6-Months).txt"
    if f3a in all_bars_by_file and f3b in all_bars_by_file:
        print(f"\n{'─' * 70}")
        print(f"  CREATING MERGED PERIOD 3: Feb 2023 - Aug 2023")
        print(f"{'─' * 70}")

        merged = merge_and_deduplicate(all_bars_by_file[f3a], all_bars_by_file[f3b])
        print(f"  3a bars: {len(all_bars_by_file[f3a]):,}")
        print(f"  3b bars: {len(all_bars_by_file[f3b]):,}")
        print(f"  Merged:  {len(merged):,} (after dedup)")
        print(f"  Range:   {merged[0][0].strftime('%Y-%m-%d')} → {merged[-1][0].strftime('%Y-%m-%d')}")

        merged_path = hist_dir / "NQ_1m_2022-09_to_2023-08_merged.csv"
        write_normalized_csv(merged, str(merged_path))
        print(f"  Output:  {merged_path}")

    # Summary
    print(f"\n{'=' * 70}")
    print(f"  SUMMARY")
    print(f"{'=' * 70}")
    for r in all_results:
        print(f"  {r['period']:<35s} | {r['bar_count']:>8,} bars | "
              f"{r['start']} → {r['end']} | Gaps: {r['gaps']}")

    total_bars = sum(r["bar_count"] for r in all_results)
    print(f"\n  Total bars across all files: {total_bars:,}")
    print(f"  Files written to: {hist_dir}")

    return all_results


if __name__ == "__main__":
    main()
