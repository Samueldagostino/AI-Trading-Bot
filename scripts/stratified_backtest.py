"""
Stratified Backtest — Market Regime Sampling
=============================================
Runs shadow-trade analysis on a representative sample of data
from each market regime (Sep 2021 – Aug 2024).

Instead of running all ~1.4M bars, samples ~131K bars from each
of 7 regimes, reducing runtime from ~2h to ~20min while preserving
edge validation across all market conditions.

Regimes:
  1. Sep 2021 – Dec 2021: Bull run (NQ rallying into year-end)
  2. Jan 2022 – Jun 2022: Bear market (aggressive selloff)
  3. Jul 2022 – Dec 2022: Capitulation/chop (recovery attempts fail)
  4. Jan 2023 – Jun 2023: Recovery (V-shaped bounce)
  5. Jul 2023 – Dec 2023: Bull run (AI boom)
  6. Jan 2024 – Jun 2024: Bull continuation
  7. Jul 2024 – Aug 2024: Continued strength

Usage:
    python scripts/stratified_backtest.py
    python scripts/stratified_backtest.py --seed 123
"""

import argparse
import asyncio
import json
import logging
import random
import shutil
import sys
import tempfile
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

# Project paths
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
PROJECT_DIR = REPO_ROOT / "nq_bot_vscode"
LOGS_DIR = REPO_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Add project source to path
sys.path.insert(0, str(PROJECT_DIR))

from scripts.replay_simulator import ReplaySimulator

# ── Regime definitions ──
REGIMES = OrderedDict([
    ("bull_run_2021", {
        "label": "Sep 2021 – Dec 2021",
        "regime": "Bull run",
        "start": "2021-09-01",
        "end": "2022-01-01",
    }),
    ("bear_market_2022", {
        "label": "Jan 2022 – Jun 2022",
        "regime": "Bear market",
        "start": "2022-01-01",
        "end": "2022-07-01",
    }),
    ("capitulation_chop", {
        "label": "Jul 2022 – Dec 2022",
        "regime": "Capitulation/chop",
        "start": "2022-07-01",
        "end": "2023-01-01",
    }),
    ("recovery_2023", {
        "label": "Jan 2023 – Jun 2023",
        "regime": "Recovery",
        "start": "2023-01-01",
        "end": "2023-07-01",
    }),
    ("ai_boom_2023", {
        "label": "Jul 2023 – Dec 2023",
        "regime": "Bull run (AI boom)",
        "start": "2023-07-01",
        "end": "2024-01-01",
    }),
    ("bull_continuation", {
        "label": "Jan 2024 – Jun 2024",
        "regime": "Bull continuation",
        "start": "2024-01-01",
        "end": "2024-07-01",
    }),
    ("continued_strength", {
        "label": "Jul 2024 – Aug 2024",
        "regime": "Continued strength",
        "start": "2024-07-01",
        "end": "2024-09-01",
    }),
])

# Maximum bars to sample per regime (~6 months of RTH trading)
MAX_BARS_PER_REGIME = 131_000

# Data source directories (all aggregated historical periods)
AGGREGATED_DIR = REPO_ROOT / "data" / "firstrate" / "historical" / "aggregated"

# CSV files to process — exec TF (2m) + all HTF timeframes
CSV_FILENAMES = [
    "NQ_2m.csv", "NQ_5m.csv", "NQ_15m.csv",
    "NQ_30m.csv", "NQ_1H.csv", "NQ_4H.csv", "NQ_1D.csv",
]

CSV_HEADER = "time,open,high,low,close,Volume"


def discover_data_dirs() -> List[Path]:
    """Find all aggregated period directories."""
    if not AGGREGATED_DIR.exists():
        print(f"ERROR: Aggregated data directory not found: {AGGREGATED_DIR}")
        return []

    dirs = sorted(
        d for d in AGGREGATED_DIR.iterdir()
        if d.is_dir() and d.name.startswith("period_")
    )
    return dirs


def load_all_bars(data_dirs: List[Path]) -> Dict[str, List[Tuple[int, str]]]:
    """Load all CSV data from all period directories, merge by filename.

    Returns: {csv_filename: [(unix_timestamp, raw_csv_line), ...]} sorted by time.
    """
    all_bars: Dict[str, List[Tuple[int, str]]] = {}

    for data_dir in data_dirs:
        for csv_name in CSV_FILENAMES:
            csv_path = data_dir / csv_name
            if not csv_path.exists():
                continue

            if csv_name not in all_bars:
                all_bars[csv_name] = []

            with open(csv_path, "r") as f:
                header = f.readline()  # skip header
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ts = int(line.split(",", 1)[0])
                        all_bars[csv_name].append((ts, line))
                    except (ValueError, IndexError):
                        continue

    # Sort by timestamp and deduplicate
    for csv_name in all_bars:
        all_bars[csv_name].sort(key=lambda x: x[0])
        seen = set()
        deduped = []
        for ts, line in all_bars[csv_name]:
            if ts not in seen:
                seen.add(ts)
                deduped.append((ts, line))
        all_bars[csv_name] = deduped

    return all_bars


def filter_by_regime(
    all_bars: Dict[str, List[Tuple[int, str]]],
    start_str: str,
    end_str: str,
) -> Dict[str, List[Tuple[int, str]]]:
    """Filter bars to a regime's date range."""
    start_ts = int(
        datetime.strptime(start_str, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )
    end_ts = int(
        datetime.strptime(end_str, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )

    filtered = {}
    for csv_name, bars in all_bars.items():
        regime_bars = [
            (ts, line) for ts, line in bars if start_ts <= ts < end_ts
        ]
        if regime_bars:
            filtered[csv_name] = regime_bars

    return filtered


def sample_exec_bars(
    bars: List[Tuple[int, str]],
    max_bars: int,
    seed: int = 42,
) -> List[Tuple[int, str]]:
    """Randomly sample bars, preserving chronological order.

    If the regime has fewer than max_bars, returns all bars.
    """
    if len(bars) <= max_bars:
        return bars

    rng = random.Random(seed)
    indices = sorted(rng.sample(range(len(bars)), max_bars))
    return [bars[i] for i in indices]


def write_csv_file(
    bars: List[Tuple[int, str]],
    filepath: Path,
) -> None:
    """Write bars to a CSV file with the standard header."""
    with open(filepath, "w") as f:
        f.write(CSV_HEADER + "\n")
        for _, line in bars:
            f.write(line + "\n")


def build_stratified_dataset(
    all_bars: Dict[str, List[Tuple[int, str]]],
    seed: int = 42,
) -> Tuple[Dict[str, List[Tuple[int, str]]], Dict]:
    """Build the stratified sample from all regimes.

    Returns:
        combined_bars: {csv_filename: [(ts, line), ...]} — the full sample
        composition: regime composition metadata
    """
    combined: Dict[str, List[Tuple[int, str]]] = {}
    composition = {}

    exec_file = "NQ_2m.csv"

    for regime_id, regime in REGIMES.items():
        regime_bars = filter_by_regime(all_bars, regime["start"], regime["end"])

        if not regime_bars or exec_file not in regime_bars:
            print(f"  SKIP: No data for {regime_id} ({regime['label']})")
            composition[regime_id] = {
                "bars": 0,
                "date_range": f"{regime['start']} to {regime['end']}",
                "status": "no_data",
            }
            continue

        exec_bars = regime_bars[exec_file]
        original_count = len(exec_bars)

        # Sample exec bars
        sampled_exec = sample_exec_bars(exec_bars, MAX_BARS_PER_REGIME, seed)
        sampled_count = len(sampled_exec)

        # Build set of sampled timestamps for filtering other TFs
        # For HTF bars, keep ALL bars within the regime date range
        # (they provide necessary context and are far fewer)
        for csv_name, bars in regime_bars.items():
            if csv_name not in combined:
                combined[csv_name] = []

            if csv_name == exec_file:
                combined[csv_name].extend(sampled_exec)
            else:
                # Keep all HTF bars for the regime
                combined[csv_name].extend(bars)

        # Determine actual date range from sampled data
        first_ts = sampled_exec[0][0]
        last_ts = sampled_exec[-1][0]
        first_date = datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        last_date = datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d")

        sampled_str = (
            f"(sampled from {original_count:,})"
            if sampled_count < original_count
            else "(all)"
        )

        print(
            f"  {regime_id:<25s} {sampled_count:>7,} bars {sampled_str:>25s}  "
            f"{first_date} → {last_date}"
        )

        composition[regime_id] = {
            "bars": sampled_count,
            "original_bars": original_count,
            "date_range": f"{first_date} to {last_date}",
            "regime": regime["regime"],
            "label": regime["label"],
        }

    # Sort combined bars by timestamp (regimes are sequential so mostly sorted)
    for csv_name in combined:
        combined[csv_name].sort(key=lambda x: x[0])

    return combined, composition


def write_dataset_to_dir(
    combined: Dict[str, List[Tuple[int, str]]],
    output_dir: Path,
) -> None:
    """Write the combined stratified dataset to CSV files."""
    output_dir.mkdir(parents=True, exist_ok=True)

    for csv_name, bars in combined.items():
        if not bars:
            continue
        filepath = output_dir / csv_name
        write_csv_file(bars, filepath)


async def run_stratified_backtest(seed: int = 42) -> Dict:
    """Main stratified backtest runner."""
    t0 = time.time()

    print(f"\n{'#' * 70}")
    print(f"  STRATIFIED BACKTEST — Market Regime Sampling")
    print(f"  Config: Variant C + Calibrated Slippage + Sweep Detector")
    print(f"  Regimes: {len(REGIMES)} | Max bars/regime: {MAX_BARS_PER_REGIME:,}")
    print(f"  Seed: {seed}")
    print(f"{'#' * 70}\n")

    # ── Step 1: Discover and load data ──
    data_dirs = discover_data_dirs()
    if not data_dirs:
        print("ERROR: No data directories found")
        sys.exit(1)

    print(f"Loading data from {len(data_dirs)} period directories...")
    for d in data_dirs:
        print(f"  {d.name}")

    all_bars = load_all_bars(data_dirs)

    total_2m = len(all_bars.get("NQ_2m.csv", []))
    print(f"\nTotal 2m bars loaded: {total_2m:,}")

    if total_2m == 0:
        print("ERROR: No 2m data found")
        sys.exit(1)

    # ── Step 2: Build stratified sample ──
    print(f"\nBuilding stratified sample...")
    print(f"  {'Regime':<25s} {'Bars':>7} {'Source':>25s}  {'Date Range'}")
    print(f"  {'─' * 80}")

    combined, composition = build_stratified_dataset(all_bars, seed)

    total_sampled = len(combined.get("NQ_2m.csv", []))
    regimes_sampled = sum(1 for c in composition.values() if c.get("bars", 0) > 0)

    print(f"\n  Total sample: {total_sampled:,} exec bars from {regimes_sampled} regimes")

    if total_sampled == 0:
        print("ERROR: No bars in sample")
        sys.exit(1)

    # ── Step 3: Write to temp directory ──
    tmp_dir = Path(tempfile.mkdtemp(prefix="stratified_bt_"))
    print(f"\nWriting stratified dataset to: {tmp_dir}")
    write_dataset_to_dir(combined, tmp_dir)

    for csv_name in sorted(combined.keys()):
        count = len(combined[csv_name])
        print(f"  {csv_name:<12s}: {count:>8,} bars")

    # ── Step 4: Run ReplaySimulator ──
    print(f"\n{'=' * 70}")
    print(f"  Running ReplaySimulator on stratified sample...")
    print(f"{'=' * 70}\n")

    sim = ReplaySimulator(
        speed="max",
        start_date=None,
        end_date=None,
        validate=False,
        data_dir=str(tmp_dir),
        c1_variant="C",
        quiet=True,
        sweep_enabled=True,
    )

    results = await sim.run()

    # ── Step 5: Shadow-trade analysis ──
    shadow_analysis = sim._simulate_shadow_trades()

    elapsed = time.time() - t0

    # ── Step 6: Build output ──
    output = {
        "stratified_sample": {
            "total_bars": total_sampled,
            "regimes_sampled": regimes_sampled,
            "max_bars_per_regime": MAX_BARS_PER_REGIME,
            "seed": seed,
            "sample_composition": composition,
        },
        "backtest_results": {
            "total_trades": results["total_trades"],
            "total_pnl": results["total_pnl"],
            "win_rate": results["win_rate"],
            "profit_factor": results["profit_factor"],
            "max_drawdown": results["max_drawdown_pct"],
            "c1_pnl": results["c1_pnl"],
            "c2_pnl": results["c2_pnl"],
            "expectancy": results["expectancy"],
        },
        "shadow_analysis": shadow_analysis,
        "elapsed_seconds": round(elapsed, 1),
        "exec_bars": results.get("exec_bars", 0),
        "htf_bars": results.get("htf_bars", 0),
    }

    # ── Step 7: Save results ──
    output_path = LOGS_DIR / "stratified_analysis.json"
    with open(str(output_path), "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nResults saved: {output_path}")

    # ── Cleanup temp directory ──
    try:
        shutil.rmtree(tmp_dir)
    except OSError:
        pass

    return output


def print_summary(output: Dict) -> None:
    """Print the stratified backtest summary."""
    sample = output["stratified_sample"]
    results = output["backtest_results"]
    shadow = output["shadow_analysis"]

    pf = results["profit_factor"]
    pf_str = f"{pf:.2f}" if pf < 100 else "inf"

    print(f"\n{'=' * 70}")
    print(f"  === STRATIFIED BACKTEST SUMMARY ===")
    print(f"{'=' * 70}")
    print(f"  Sample: {sample['regimes_sampled']} regimes, "
          f"{sample['total_bars']:,} bars total")
    print(f"  Duration: Sep 2021 – Aug 2024 (representative sample)")
    print(f"  Elapsed: {output['elapsed_seconds']:.1f}s")
    print()
    print(f"  Results:")
    print(f"    Trades: {results['total_trades']:,}")
    print(f"    PnL: ${results['total_pnl']:+,.2f}")
    print(f"    Win Rate: {results['win_rate']:.1f}%")
    print(f"    Profit Factor: {pf_str}")
    print(f"    Max DD: {results['max_drawdown']:.1f}%")
    print(f"    C1 PnL: ${results['c1_pnl']:+,.2f}")
    print(f"    C2 PnL: ${results['c2_pnl']:+,.2f}")
    print(f"    Expectancy: ${results['expectancy']:.2f}/trade")

    # Regime composition
    print(f"\n  Sample Composition:")
    comp = sample["sample_composition"]
    for regime_id, info in comp.items():
        bars = info.get("bars", 0)
        if bars == 0:
            continue
        regime_label = info.get("label", regime_id)
        date_range = info.get("date_range", "?")
        print(f"    {regime_label:<30s} {bars:>7,} bars  ({date_range})")

    # Shadow analysis
    ranking = shadow.get("gate_value_ranking", [])
    total_shadow = shadow.get("total_shadow_signals", 0)

    if ranking:
        print(f"\n  Shadow Analysis:")
        print(f"    Total shadow signals: {total_shadow:,}")
        print(f"    {'Gate':<25} {'Count':>6} {'Shadow PnL':>12} {'Verdict':>12}")
        print(f"    {'─' * 58}")
        for g in ranking:
            print(
                f"    {g['gate']:<25} {g['count']:>6} "
                f"${g['shadow_pnl']:>+10,.2f} {g['verdict']:>12}"
            )

    print(f"\n{'=' * 70}")


async def main():
    parser = argparse.ArgumentParser(
        description="Stratified Backtest — Market Regime Sampling"
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for sampling (default: 42)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    output = await run_stratified_backtest(seed=args.seed)
    print_summary(output)


if __name__ == "__main__":
    asyncio.run(main())
