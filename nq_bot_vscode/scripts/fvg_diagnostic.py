"""
FVG Diagnostic — Understand why FVG confluence never fires.

Runs a partial replay (first 10K 2m bars) and counts:
1. How many FVGs are detected by feature engine
2. How many bars have active FVGs
3. How many sweep bars have price inside/near an aligned FVG
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
from datetime import datetime, timezone
from features.engine import NQFeatureEngine, Bar
from config.settings import BotConfig
from scripts.full_backtest import load_1min_csv, aggregate_to_2m

# ─── CONFIG ───
MAX_BARS = 10_000
FVG_PROXIMITY_ATR_FACTOR = 0.5
PROJECT_DIR = Path(__file__).resolve().parent.parent

print("=" * 60)
print("  FVG DIAGNOSTIC — Phase 3 Confluence Analysis")
print("=" * 60)

config = BotConfig()
print(f"\nFVG settings:")
print(f"  fvg_min_gap_ticks: {config.features.fvg_min_gap_ticks}")
print(f"  fvg_min_gap in NQ pts: {config.features.fvg_min_gap_ticks * 0.25}")
print(f"  fvg_max_age_bars: {config.features.fvg_max_age_bars}")

# Load and aggregate data
data_path = str(PROJECT_DIR / "data" / "historical" / "combined_1min.csv")
print(f"\nLoading 1m data from {data_path}...")
bars_1m = load_1min_csv(data_path)
print(f"  Loaded {len(bars_1m):,} 1m bars")

print("Aggregating to 2m...")
bars_2m = aggregate_to_2m(bars_1m)
print(f"  Result: {len(bars_2m):,} 2m bars")

# Filter to start date
cutoff = datetime(2025, 3, 1, tzinfo=timezone.utc)
bars_2m = [b for b in bars_2m if b["timestamp"] >= cutoff]
print(f"  After start-date filter: {len(bars_2m):,} bars")

# Init
engine = NQFeatureEngine(config)

# Counters
total_bars = 0
bars_with_active_fvgs = 0
bars_inside_bullish_fvg = 0
bars_inside_bearish_fvg = 0
max_active_fvgs_seen = 0
fvg_sizes = []
proximity_samples = []

limit = min(len(bars_2m), MAX_BARS)
print(f"\nProcessing {limit:,} bars...\n")

for i in range(limit):
    bar_data = bars_2m[i]

    # Create Bar for feature engine
    bar = Bar(
        timestamp=bar_data["timestamp"],
        open=bar_data["open"],
        high=bar_data["high"],
        low=bar_data["low"],
        close=bar_data["close"],
        volume=bar_data.get("volume", 0),
    )

    fvg_count_before = len(engine._fvgs)
    features = engine.update(bar)
    fvg_count_after = len(engine._fvgs)

    new_fvgs = fvg_count_after - fvg_count_before
    if new_fvgs > 0:
        for fvg in engine._fvgs[-new_fvgs:]:
            fvg_sizes.append(fvg.gap_size)

    total_bars += 1

    if not features:
        continue

    active_fvgs = getattr(features, 'active_fvgs', [])
    n_active = len(active_fvgs)
    if n_active > 0:
        bars_with_active_fvgs += 1
        max_active_fvgs_seen = max(max_active_fvgs_seen, n_active)

    if getattr(features, 'inside_bullish_fvg', False):
        bars_inside_bullish_fvg += 1
    if getattr(features, 'inside_bearish_fvg', False):
        bars_inside_bearish_fvg += 1

    # Sample proximity data on every bar with active FVGs (no sweep needed)
    if n_active > 0:
        atr = getattr(features, 'atr_14', 20.0) or 20.0
        current_price = bar_data["close"]
        for fvg in active_fvgs:
            dist_high = abs(current_price - fvg.gap_high)
            dist_low = abs(current_price - fvg.gap_low)
            min_dist = min(dist_high, dist_low)
            # Is price inside this FVG?
            is_inside_this = (fvg.gap_low <= current_price <= fvg.gap_high)
            proximity_samples.append({
                "bar": i,
                "distance": round(min_dist, 2),
                "atr": round(atr, 2),
                "inside": is_inside_this,
                "fvg_type": fvg.gap_type,
                "fvg_size": fvg.gap_size,
            })

    if i % 2500 == 0 and i > 0:
        print(f"  [{i:>6,}] active_fvgs={n_active}, total_created={len(fvg_sizes)}")

# ─── REPORT ───
print()
print("=" * 60)
print("  RESULTS")
print("=" * 60)

print(f"\n--- FVG Detection ---")
print(f"  Total bars processed:       {total_bars:,}")
print(f"  Total FVGs ever created:    {len(fvg_sizes)}")
print(f"  Current in _fvgs list:      {len(engine._fvgs)}")
print(f"  Max active at once:         {max_active_fvgs_seen}")
print(f"  Bars with active FVGs:      {bars_with_active_fvgs:,} ({100*bars_with_active_fvgs/max(total_bars,1):.1f}%)")
print(f"  Bars inside bullish FVG:    {bars_inside_bullish_fvg:,} ({100*bars_inside_bullish_fvg/max(total_bars,1):.1f}%)")
print(f"  Bars inside bearish FVG:    {bars_inside_bearish_fvg:,} ({100*bars_inside_bearish_fvg/max(total_bars,1):.1f}%)")

if fvg_sizes:
    sorted_sizes = sorted(fvg_sizes)
    print(f"\n--- FVG Size Distribution (NQ points) ---")
    print(f"  Min: {sorted_sizes[0]:.2f}")
    print(f"  Max: {sorted_sizes[-1]:.2f}")
    print(f"  Avg: {sum(fvg_sizes)/len(fvg_sizes):.2f}")
    print(f"  Median: {sorted_sizes[len(sorted_sizes)//2]:.2f}")

if proximity_samples:
    distances = [s["distance"] for s in proximity_samples]
    inside_count = sum(1 for s in proximity_samples if s["inside"])
    print(f"\n--- Proximity Analysis (all bars with active FVGs) ---")
    print(f"  Total samples:                 {len(proximity_samples)}")
    print(f"  Samples where price INSIDE:    {inside_count}")
    print(f"  Min distance to FVG boundary:  {min(distances):.2f} pts")
    print(f"  Max distance:                  {max(distances):.2f} pts")
    print(f"  Avg distance:                  {sum(distances)/len(distances):.2f} pts")
    print(f"  Median distance:               {sorted(distances)[len(distances)//2]:.2f} pts")

    # What threshold would catch how many?
    print(f"\n--- Proximity Threshold Analysis ---")
    for factor in [0.5, 1.0, 1.5, 2.0, 3.0, 5.0]:
        matches = sum(1 for s in proximity_samples if s["distance"] <= s["atr"] * factor)
        pct = 100 * matches / len(proximity_samples)
        print(f"  Within {factor}x ATR:  {matches}/{len(proximity_samples)} ({pct:.0f}%)")

print(f"\n{'='*60}")
print(f"  DIAGNOSIS")
print(f"{'='*60}")
if len(fvg_sizes) == 0:
    print("  ROOT CAUSE: Zero FVGs detected. Feature engine not creating FVGs.")
    print("  FIX: Check fvg_min_gap_ticks or FVG detection logic.")
elif bars_with_active_fvgs < total_bars * 0.01:
    print(f"  ROOT CAUSE: Only {bars_with_active_fvgs} bars ({100*bars_with_active_fvgs/total_bars:.1f}%) have active FVGs.")
    print("  FVGs are getting filled/invalidated too quickly on 2m timeframe.")
    print("  FIX: Increase fvg_max_age_bars or use HTF FVGs.")
elif bars_inside_bullish_fvg + bars_inside_bearish_fvg == 0:
    print(f"  ROOT CAUSE: {bars_with_active_fvgs} bars have active FVGs but price is NEVER inside one.")
    print("  2m FVGs are too small — price passes through instantly.")
    print("  FIX: Widen proximity threshold or detect FVGs on higher timeframes.")
else:
    inside_pct = 100 * (bars_inside_bullish_fvg + bars_inside_bearish_fvg) / total_bars
    print(f"  FVGs exist and price is inside them {inside_pct:.1f}% of the time.")
    print(f"  The multiplier SHOULD be firing. Check if sweep timing aligns.")
    print(f"  Possible issue: sweeps fire on bars where FVG is not aligned directionally.")
