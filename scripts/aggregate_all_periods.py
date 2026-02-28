"""
Aggregate All Historical Periods
==================================
Runs aggregate_1m.py logic on each historical period's 1m CSV,
producing NQ_2m.csv, NQ_5m.csv, etc. in per-period subdirectories
under data/firstrate/historical/aggregated/.

Period layout:
  data/firstrate/historical/aggregated/
    period_1_2021-09_to_2022-02/
      NQ_1m.csv, NQ_2m.csv, NQ_5m.csv, ... NQ_1D.csv
    period_2_2022-03_to_2022-08/
      ...
    period_3_2022-09_to_2023-08/   (merged)
      ...
    period_4_2023-09_to_2024-02/
      ...
    period_5_2024-03_to_2024-08/
      ...
"""

import os
import sys
from pathlib import Path

# Add project root for imports
script_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(script_dir.parent / "nq_bot_vscode"))

from scripts.aggregate_1m import load_1m_bars, aggregate_bars, write_csv, TIMEFRAMES

REPO_ROOT = script_dir.parent
HIST_DIR = REPO_ROOT / "data" / "firstrate" / "historical"
AGG_DIR = HIST_DIR / "aggregated"

# Define the 5 non-overlapping periods + their source 1m files
PERIODS = [
    {
        "name": "period_1_2021-09_to_2022-02",
        "source": "NQ_1m_2021-09_to_2022-02.csv",
        "label": "Period 1: Sep 2021 - Feb 2022",
    },
    {
        "name": "period_2_2022-03_to_2022-08",
        "source": "NQ_1m_2022-03_to_2022-08.csv",
        "label": "Period 2: Mar 2022 - Aug 2022",
    },
    {
        "name": "period_3_2022-09_to_2023-08",
        "source": "NQ_1m_2022-09_to_2023-08_merged.csv",
        "label": "Period 3: Sep 2022 - Aug 2023 (merged)",
    },
    {
        "name": "period_4_2023-09_to_2024-02",
        "source": "NQ_1m_2023-09_to_2024-02.csv",
        "label": "Period 4: Sep 2023 - Feb 2024",
    },
    {
        "name": "period_5_2024-03_to_2024-08",
        "source": "NQ_1m_2024-03_to_2024-08.csv",
        "label": "Period 5: Mar 2024 - Aug 2024",
    },
    {
        "name": "period_5b_2024-09_to_2025-08",
        "source": "NQ_1m_2024-09_to_2025-08.csv",
        "label": "Period 5b: Sep 2024 - Aug 2025",
    },
]


def aggregate_period(period: dict) -> dict:
    """Aggregate a single period's 1m data into all timeframes."""
    source_path = HIST_DIR / period["source"]
    output_dir = AGG_DIR / period["name"]
    os.makedirs(output_dir, exist_ok=True)

    print(f"\n{'─' * 60}")
    print(f"  {period['label']}")
    print(f"  Source: {source_path}")
    print(f"  Output: {output_dir}/")
    print(f"{'─' * 60}")

    if not source_path.exists():
        print(f"  ERROR: Source file not found!")
        return {"error": "file_not_found"}

    # Load 1m bars
    print("  Loading 1m data...")
    bars_1m = load_1m_bars(str(source_path))

    if not bars_1m:
        print("  ERROR: No valid bars loaded!")
        return {"error": "no_bars"}

    first_ts = bars_1m[0]["timestamp"]
    last_ts = bars_1m[-1]["timestamp"]
    print(f"  Rows: {len(bars_1m):,}")
    print(f"  Range: {first_ts.strftime('%Y-%m-%d %H:%M')} → {last_ts.strftime('%Y-%m-%d %H:%M')}")

    # Write standardized 1m file
    out_1m = str(output_dir / "NQ_1m.csv")
    write_csv(bars_1m, out_1m)
    print(f"  {'1m':>4s}: {len(bars_1m):>8,} bars → NQ_1m.csv")

    # Aggregate each timeframe
    results = {"1m": len(bars_1m)}
    for tf_label, tf_minutes in TIMEFRAMES:
        agg = aggregate_bars(bars_1m, tf_minutes)
        outfile = str(output_dir / f"NQ_{tf_label}.csv")
        write_csv(agg, outfile)
        results[tf_label] = len(agg)
        print(f"  {tf_label:>4s}: {len(agg):>8,} bars → NQ_{tf_label}.csv")

    return results


def main():
    print("=" * 60)
    print("  AGGREGATE ALL HISTORICAL PERIODS")
    print("=" * 60)

    all_results = {}
    for period in PERIODS:
        result = aggregate_period(period)
        all_results[period["name"]] = result

    print(f"\n{'=' * 60}")
    print(f"  ALL PERIODS AGGREGATED")
    print(f"{'=' * 60}")
    for name, result in all_results.items():
        if "error" in result:
            print(f"  {name}: ERROR - {result['error']}")
        else:
            print(f"  {name}: {result.get('1m', 0):,} bars → "
                  f"{sum(v for k, v in result.items() if k != '1m'):,} aggregated bars")

    print(f"\n  Output directory: {AGG_DIR}")


if __name__ == "__main__":
    main()
