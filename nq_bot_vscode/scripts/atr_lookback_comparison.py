#!/usr/bin/env python3
"""
ATR Lookback Comparison for Trailing Stop Optimization (Research Item R3)
==========================================================================

Research Question: Is ATR(10) better than ATR(14) for trailing stop calculation on 2-minute bars?

This script compares multiple ATR lookback periods (7, 10, 14, 20) across:
  - Avg ATR value at each lookback
  - Trail distance at 2.0x and 2.5x multipliers
  - Responsiveness to regime changes (correlation with future volatility)
  - Effectiveness at protecting winners vs stopping out early

Methodology:
  1. Load 2-minute bar data from CSV or generate synthetic demo data
  2. Compute ATR at periods: 7, 10, 14, 20 using standard Wilder's smoothing
  3. For each ATR lookback, simulate:
     - Trail from entry: ATR(N) × multiplier (2.0x, 2.5x)
     - Measure avg trail distance in points
     - Measure trail volatility (std dev of distance)
     - Count stops of 10pt and 30pt winners
  4. Compute responsiveness metric:
     - Correlation between ATR(N) at time T and realized volatility at T+1 to T+5
  5. Output comparison table with all metrics

Usage:
    # Demo mode with synthetic data
    python scripts/atr_lookback_comparison.py --demo

    # Real data
    python scripts/atr_lookback_comparison.py --data path/to/bars.csv

    # Specify timeframe (default 2 minutes)
    python scripts/atr_lookback_comparison.py --data path/to/bars.csv --timeframe 2

    # Output CSV results
    python scripts/atr_lookback_comparison.py --demo --output results.csv
"""

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ================================================================
# Data Structure
# ================================================================

class Bar:
    """Single OHLCV bar."""
    def __init__(self, timestamp: datetime, open_: float, high: float,
                 low: float, close: float, volume: int):
        self.timestamp = timestamp
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.true_range = None
        self.atr = {}  # keyed by lookback period

    def compute_true_range(self, prev_close: Optional[float] = None) -> float:
        """Compute true range for this bar."""
        if prev_close is None:
            # First bar: use high - low
            tr = self.high - self.low
        else:
            tr = max(
                self.high - self.low,
                abs(self.high - prev_close),
                abs(self.low - prev_close)
            )
        self.true_range = tr
        return tr


# ================================================================
# Data Loading
# ================================================================

def detect_csv_format(filepath: str) -> Dict:
    """Auto-detect CSV format and column indices."""
    with open(filepath, "r", encoding="utf-8-sig") as f:
        sample = f.read(2048)

    # Detect delimiter
    delimiter = "," if sample.count(",") > sample.count("\t") else "\t"

    lines = sample.strip().split("\n")
    if not lines:
        raise ValueError("CSV file is empty")

    first_line = lines[0]
    fields = [h.strip().lower() for h in first_line.split(delimiter)]

    # Check if first row is header (contains non-numeric first field)
    has_header = not fields[0][0].isdigit() if fields[0] else False

    col_map = {"has_header": has_header, "delimiter": delimiter}

    if has_header:
        # Map standard column names
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
        # Headerless: assume positional (time, open, high, low, close, volume)
        col_map["time"] = 0
        col_map["open"] = 1
        col_map["high"] = 2
        col_map["low"] = 3
        col_map["close"] = 4
        col_map["volume"] = 5

    return col_map


def parse_timestamp(value: str) -> Optional[datetime]:
    """Parse timestamp from string (handles multiple formats)."""
    value = value.strip()
    if not value:
        return None

    # Try unix timestamp
    try:
        ts = float(value)
        if ts > 1_000_000_000_000:  # milliseconds
            return datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
        else:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, OSError):
        pass

    # Try ISO format with microseconds (e.g., 2026-02-20T06:12:20.424615Z)
    if "." in value and "Z" in value:
        try:
            dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # Try ISO format variants
    for pattern in ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"]:
        try:
            dt = datetime.strptime(value, pattern)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    # Try standard format
    for pattern in ["%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"]:
        try:
            dt = datetime.strptime(value, pattern)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def load_bars_from_csv(filepath: str) -> List[Bar]:
    """Load bars from CSV file."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Data file not found: {filepath}")

    fmt = detect_csv_format(filepath)
    bars = []

    with open(filepath, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f, delimiter=fmt["delimiter"])
        if fmt.get("has_header", True):
            next(reader)

        for row in reader:
            try:
                if len(row) < 5:
                    continue

                time_idx = fmt.get("time", 0)
                open_idx = fmt.get("open", 1)
                high_idx = fmt.get("high", 2)
                low_idx = fmt.get("low", 3)
                close_idx = fmt.get("close", 4)
                vol_idx = fmt.get("volume", 5)

                ts = parse_timestamp(row[time_idx])
                if ts is None:
                    continue

                o = float(row[open_idx])
                h = float(row[high_idx])
                lo = float(row[low_idx])
                c = float(row[close_idx])
                v = int(float(row[vol_idx])) if vol_idx < len(row) else 0

                # Validate
                if not all(math.isfinite(x) for x in [o, h, lo, c]):
                    continue
                if h < lo or o <= 0 or c <= 0:
                    continue

                bar = Bar(ts, o, h, lo, c, v)
                bars.append(bar)
            except (ValueError, IndexError):
                continue

    # Sort by timestamp
    bars.sort(key=lambda b: b.timestamp)

    # Remove duplicates
    if bars:
        deduped = [bars[0]]
        for i in range(1, len(bars)):
            if bars[i].timestamp != deduped[-1].timestamp:
                deduped.append(bars[i])
        bars = deduped

    return bars


def generate_demo_bars(num_bars: int = 500, start_price: float = 20000.0) -> List[Bar]:
    """Generate synthetic 2-minute bars for demo mode."""
    import random

    bars = []
    current_time = datetime(2025, 9, 1, 18, 1, 0, tzinfo=timezone.utc)
    price = start_price

    for i in range(num_bars):
        # Random walk with regime changes
        volatility = 0.5 if (i // 100) % 2 == 0 else 1.5
        change = random.gauss(0, volatility)

        open_price = price
        close_price = price + change
        high_price = max(open_price, close_price) + abs(random.gauss(0, 0.3))
        low_price = min(open_price, close_price) - abs(random.gauss(0, 0.3))

        volume = random.randint(1000, 10000)

        bar = Bar(
            current_time,
            open_price,
            high_price,
            low_price,
            close_price,
            volume
        )
        bars.append(bar)

        price = close_price
        current_time += timedelta(minutes=2)

    return bars


# ================================================================
# ATR Calculation
# ================================================================

def compute_atr_series(bars: List[Bar], lookback: int) -> None:
    """Compute ATR for all bars at given lookback period using Wilder's smoothing."""
    if not bars or lookback < 1:
        return

    # Compute true ranges
    for i, bar in enumerate(bars):
        prev_close = bars[i - 1].close if i > 0 else None
        bar.compute_true_range(prev_close)

    # First ATR = simple average of first lookback TRs
    if len(bars) < lookback:
        return

    atr_value = sum(bar.true_range for bar in bars[:lookback]) / lookback
    bars[lookback - 1].atr[lookback] = atr_value

    # Subsequent ATRs = Wilder's smoothed
    for i in range(lookback, len(bars)):
        atr_value = (atr_value * (lookback - 1) + bars[i].true_range) / lookback
        bars[i].atr[lookback] = atr_value


# ================================================================
# Trail Simulation
# ================================================================

class TrailSimulation:
    """Simulate trailing stop for a single entry."""
    def __init__(self, entry_bar_idx: int, entry_price: float, lookback: int,
                 multiplier: float, bars: List[Bar]):
        self.entry_bar_idx = entry_bar_idx
        self.entry_price = entry_price
        self.lookback = lookback
        self.multiplier = multiplier
        self.bars = bars
        self.trail_distances = []  # Distance from high-water mark at each bar
        self.exit_bar_idx = None
        self.exit_reason = None

    def run(self) -> None:
        """Simulate the trail from entry through exit."""
        hwm = self.entry_price  # High-water mark
        trail_price = self.entry_price

        for i in range(self.entry_bar_idx + 1, len(self.bars)):
            bar = self.bars[i]

            # Skip if ATR not computed
            if self.lookback not in bar.atr:
                continue

            atr = bar.atr[self.lookback]
            trail_distance = atr * self.multiplier

            # Update HWM
            if bar.high > hwm:
                hwm = bar.high
                trail_price = hwm - trail_distance
            else:
                # Tight trail distance
                trail_price = max(trail_price, hwm - trail_distance)

            self.trail_distances.append(trail_distance)

            # Check if stopped out
            if bar.low <= trail_price:
                self.exit_bar_idx = i
                self.exit_reason = "trail"
                return

            # Hard stop at 12 bars
            if i - self.entry_bar_idx >= 12:
                self.exit_bar_idx = i
                self.exit_reason = "timeout"
                return

    def profit_at_exit(self) -> Optional[float]:
        """Return profit in points if exited, else None."""
        if self.exit_bar_idx is None:
            return None
        exit_price = self.bars[self.exit_bar_idx].close
        return exit_price - self.entry_price


def simulate_trails_for_lookback(bars: List[Bar], lookback: int,
                                  multiplier: float) -> Dict:
    """Simulate trailing stops for all entry points at a given lookback and multiplier."""
    results = {
        "trail_distances": [],
        "profits": [],
        "stopped_10pt_winner": 0,
        "stopped_30pt_winner": 0,
        "total_simulations": 0,
    }

    # Enter at each bar starting from lookback+1
    for entry_idx in range(lookback + 1, len(bars) - 13):  # -13 to allow 12 bars ahead
        entry_bar = bars[entry_idx]
        if lookback not in entry_bar.atr:
            continue

        entry_price = entry_bar.close

        sim = TrailSimulation(entry_idx, entry_price, lookback, multiplier, bars)
        sim.run()

        if sim.exit_bar_idx is not None:
            # Record metrics
            results["total_simulations"] += 1

            # Trail distances
            if sim.trail_distances:
                results["trail_distances"].extend(sim.trail_distances)

            # Profit
            profit = sim.profit_at_exit()
            if profit is not None:
                results["profits"].append(profit)

                # Track stops of winners
                if 9 <= profit < 11:  # Approximate 10pt winner
                    results["stopped_10pt_winner"] += 1
                elif 29 <= profit < 31:  # Approximate 30pt winner
                    results["stopped_30pt_winner"] += 1

    return results


# ================================================================
# Responsiveness Metric
# ================================================================

def compute_responsiveness(bars: List[Bar], lookback: int) -> float:
    """
    Responsiveness = correlation between ATR(N) at time T and
    realized volatility at T+1 to T+5.

    Higher correlation = more responsive to future regime changes.
    """
    atr_values = []
    realized_vols = []

    # For each bar, compute ATR and realized volatility over next 5 bars
    for i in range(lookback, len(bars) - 5):
        bar = bars[i]
        if lookback not in bar.atr:
            continue

        atr = bar.atr[lookback]

        # Realized volatility: std dev of close returns over next 5 bars
        returns = []
        for j in range(i + 1, i + 6):
            ret = (bars[j].close - bars[j - 1].close) / bars[j - 1].close
            returns.append(ret)

        realized_vol = math.sqrt(sum(r ** 2 for r in returns) / len(returns))

        atr_values.append(atr)
        realized_vols.append(realized_vol)

    if len(atr_values) < 2:
        return 0.0

    # Compute correlation
    mean_atr = sum(atr_values) / len(atr_values)
    mean_vol = sum(realized_vols) / len(realized_vols)

    cov = sum((atr_values[i] - mean_atr) * (realized_vols[i] - mean_vol)
              for i in range(len(atr_values))) / len(atr_values)

    std_atr = math.sqrt(sum((x - mean_atr) ** 2 for x in atr_values) / len(atr_values))
    std_vol = math.sqrt(sum((x - mean_vol) ** 2 for x in realized_vols) / len(realized_vols))

    if std_atr == 0 or std_vol == 0:
        return 0.0

    correlation = cov / (std_atr * std_vol)
    return max(-1.0, min(1.0, correlation))  # Clamp to [-1, 1]


# ================================================================
# Analysis
# ================================================================

def analyze_lookback(bars: List[Bar], lookback: int) -> Dict:
    """Comprehensive analysis for a single ATR lookback period."""
    # Compute ATR series
    compute_atr_series(bars, lookback)

    # Gather ATR values
    atr_values = []
    for bar in bars:
        if lookback in bar.atr:
            atr_values.append(bar.atr[lookback])

    avg_atr = sum(atr_values) / len(atr_values) if atr_values else 0.0
    std_atr = math.sqrt(sum((x - avg_atr) ** 2 for x in atr_values) / len(atr_values)) if atr_values else 0.0

    # Simulate trails at 2.0x and 2.5x
    trail_2x = simulate_trails_for_lookback(bars, lookback, 2.0)
    trail_2_5x = simulate_trails_for_lookback(bars, lookback, 2.5)

    # Compute average trail distances
    avg_trail_2x = sum(trail_2x["trail_distances"]) / len(trail_2x["trail_distances"]) \
        if trail_2x["trail_distances"] else 0.0
    avg_trail_2_5x = sum(trail_2_5x["trail_distances"]) / len(trail_2_5x["trail_distances"]) \
        if trail_2_5x["trail_distances"] else 0.0

    # Compute responsiveness
    responsiveness = compute_responsiveness(bars, lookback)

    return {
        "lookback": lookback,
        "avg_atr": avg_atr,
        "std_atr": std_atr,
        "trail_2x_avg_distance": avg_trail_2x,
        "trail_2_5x_avg_distance": avg_trail_2_5x,
        "trail_2x_simulations": trail_2x["total_simulations"],
        "trail_2_5x_simulations": trail_2_5x["total_simulations"],
        "trail_2x_stopped_10pt": trail_2x["stopped_10pt_winner"],
        "trail_2_5x_stopped_10pt": trail_2_5x["stopped_10pt_winner"],
        "trail_2x_stopped_30pt": trail_2x["stopped_30pt_winner"],
        "trail_2_5x_stopped_30pt": trail_2_5x["stopped_30pt_winner"],
        "responsiveness": responsiveness,
    }


# ================================================================
# Reporting
# ================================================================

def print_comparison_table(results: List[Dict]) -> None:
    """Print formatted comparison table."""
    print("\n" + "=" * 140)
    print("ATR LOOKBACK COMPARISON - TRAILING STOP ANALYSIS (R3)")
    print("=" * 140)
    print()

    # Header
    print(f"{'Lookback':<10} {'Avg ATR':<12} {'Trail 2.0x':<12} {'Trail 2.5x':<12} "
          f"{'Stop 10pt':<20} {'Stop 30pt':<20} {'Responsive':<12}")
    print(f"{'':.<10} {'(pts)':<12} {'(pts)':<12} {'(pts)':<12} "
          f"{'(2.0x / 2.5x)':<20} {'(2.0x / 2.5x)':<20} {'(corr)':<12}")
    print("-" * 140)

    for result in results:
        lb = result["lookback"]
        avg_atr = result["avg_atr"]
        trail_2x = result["trail_2x_avg_distance"]
        trail_2_5x = result["trail_2_5x_avg_distance"]
        stop_10 = f"{result['trail_2x_stopped_10pt']} / {result['trail_2_5x_stopped_10pt']}"
        stop_30 = f"{result['trail_2x_stopped_30pt']} / {result['trail_2_5x_stopped_30pt']}"
        resp = result["responsiveness"]

        print(f"{lb:<10} {avg_atr:<12.2f} {trail_2x:<12.2f} {trail_2_5x:<12.2f} "
              f"{stop_10:<20} {stop_30:<20} {resp:<12.3f}")

    print("-" * 140)
    print()
    print("Interpretation:")
    print("  Avg ATR      : Higher = more volatile, lower = calmer market")
    print("  Trail 2.0x   : Tighter stop, more likely to get stopped early")
    print("  Trail 2.5x   : Looser stop, allows more room to breathe")
    print("  Stop 10/30pt : Count of approximate winners stopped by trail (2.0x / 2.5x)")
    print("  Responsive   : Correlation with future volatility (1.0 = perfect)")
    print("                 Higher = ATR adapts quickly to regime changes")
    print()


def save_results_csv(results: List[Dict], output_path: str) -> None:
    """Save results to CSV file."""
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".",
                exist_ok=True)

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "lookback", "avg_atr", "std_atr",
            "trail_2x_avg_distance", "trail_2_5x_avg_distance",
            "trail_2x_simulations", "trail_2_5x_simulations",
            "trail_2x_stopped_10pt", "trail_2_5x_stopped_10pt",
            "trail_2x_stopped_30pt", "trail_2_5x_stopped_30pt",
            "responsiveness"
        ])
        writer.writeheader()
        writer.writerows(results)

    print(f"Results saved to: {output_path}")


# ================================================================
# Main
# ================================================================

def main():
    parser = argparse.ArgumentParser(
        description="ATR Lookback Comparison for Trailing Stop Optimization (R3)"
    )
    parser.add_argument(
        "--data", type=str, default=None,
        help="Path to OHLCV bar data CSV"
    )
    parser.add_argument(
        "--timeframe", type=int, default=2,
        help="Bar size in minutes (default: 2)"
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="Use synthetic demo data instead of CSV"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output CSV file for results (optional)"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print detailed debug info"
    )

    args = parser.parse_args()

    # Load data
    if args.demo:
        print("Generating synthetic 2-minute bars (500 bars)...")
        bars = generate_demo_bars(num_bars=500, start_price=20000.0)
    else:
        if not args.data:
            print("ERROR: Must provide --data path or use --demo")
            sys.exit(1)
        print(f"Loading bars from: {args.data}")
        bars = load_bars_from_csv(args.data)

    if not bars:
        print("ERROR: No bars loaded")
        sys.exit(1)

    print(f"Loaded {len(bars)} bars")
    print(f"Date range: {bars[0].timestamp} to {bars[-1].timestamp}")
    print()

    # Run analysis for each lookback
    lookbacks = [7, 10, 14, 20]
    results = []

    for lookback in lookbacks:
        if args.verbose:
            print(f"Analyzing ATR({lookback})...")
        result = analyze_lookback(bars, lookback)
        results.append(result)

        if args.verbose:
            print(f"  Avg ATR: {result['avg_atr']:.2f}")
            print(f"  Responsiveness: {result['responsiveness']:.3f}")

    # Print results
    print_comparison_table(results)

    # Save to CSV if requested
    if args.output:
        save_results_csv(results, args.output)

    # Print recommendation
    print("RECOMMENDATION FOR R3:")
    print("-" * 60)
    best_responsive = max(results, key=lambda r: r["responsiveness"])
    print(f"Most responsive to regime change: ATR({best_responsive['lookback']})")
    print(f"  Correlation with future vol: {best_responsive['responsiveness']:.3f}")
    print()

    # Compare ATR(10) vs ATR(14)
    atr10 = next((r for r in results if r["lookback"] == 10), None)
    atr14 = next((r for r in results if r["lookback"] == 14), None)
    if atr10 and atr14:
        print("ATR(10) vs ATR(14) comparison:")
        print(f"  ATR(10) responsiveness: {atr10['responsiveness']:.3f}")
        print(f"  ATR(14) responsiveness: {atr14['responsiveness']:.3f}")
        if atr10["responsiveness"] > atr14["responsiveness"]:
            diff = (atr10["responsiveness"] - atr14["responsiveness"]) / atr14["responsiveness"] * 100
            print(f"  -> ATR(10) is {diff:.1f}% MORE responsive (favors tighter tracking)")
        else:
            diff = (atr14["responsiveness"] - atr10["responsiveness"]) / atr10["responsiveness"] * 100
            print(f"  -> ATR(14) is {diff:.1f}% MORE responsive (favors smoother adaptation)")
    print()


if __name__ == "__main__":
    main()
