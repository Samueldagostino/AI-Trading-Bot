"""
Max-Stop Gate Forensic Analysis
================================
Deep forensic analysis of signals blocked by the max stop gate (30pt cap).

Runs the ReplaySimulator on period_4 (~44K bars), captures EVERY signal
blocked by the max stop gate, and performs detailed analysis including:
  - Stop distance distribution
  - Profitability by stop distance bucket
  - Stop formula decomposition with worked examples
  - Structural vs formula stop comparison
  - Root cause analysis

Usage:
    python scripts/max_stop_forensic.py
"""

import asyncio
import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

# ── Paths ──
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
PROJECT_DIR = REPO_ROOT / "nq_bot_vscode"
LOGS_DIR = REPO_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_FILE = LOGS_DIR / "max_stop_forensic.json"

sys.path.insert(0, str(PROJECT_DIR))

from scripts.replay_simulator import ReplaySimulator, load_firstrate_mtf, filter_by_date, bar_to_et

# ── Config ──
DATA_DIR = str(REPO_ROOT / "data" / "firstrate" / "historical" / "aggregated"
               / "period_4_2023-09_to_2024-02")
MAX_EXEC_BARS = 50_000
EXEC_TF = "2m"

# Constants from the system
ATR_MULTIPLIER_STOP = 2.0      # config.risk.atr_multiplier_stop
MAX_STOP_PTS = 30.0            # HIGH_CONVICTION_MAX_STOP_PTS
POINT_VALUE = 2.00             # MNQ $2/point
NUM_CONTRACTS = 2
COMMISSION_PER_SIDE = 1.29
COMMISSION_RT = COMMISSION_PER_SIDE * 2 * NUM_CONTRACTS  # $5.16
SLIPPAGE_RTH = 0.50
SLIPPAGE_ETH = 1.00
STRUCTURAL_BUFFER = 5.0        # pts below/above swept level for structural stop


def estimate_end_date(data_dir: str, start: str, max_bars: int) -> str:
    nq_2m = Path(data_dir) / "NQ_2m.csv"
    if not nq_2m.exists():
        return "2024-03-01"
    line_count = sum(1 for _ in open(str(nq_2m))) - 1
    total_months = 6
    bars_per_month = line_count / total_months if total_months > 0 else 15000
    months_needed = max_bars / bars_per_month
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_month = start_dt.month + int(months_needed)
    end_year = start_dt.year + (end_month - 1) // 12
    end_month = ((end_month - 1) % 12) + 1
    return f"{end_year}-{end_month:02d}-01"


def simulate_shadow_trade(shadow, exec_bars, total_bars):
    """Simulate a single shadow trade with full forensic detail."""
    bar_idx = shadow["bar_index"]
    entry_idx = bar_idx + 1

    if entry_idx >= total_bars:
        return None

    signal_bar = exec_bars[bar_idx]
    entry_bar = exec_bars[entry_idx]

    # NaN guard
    if (not math.isfinite(entry_bar.open) or not math.isfinite(entry_bar.high)
            or not math.isfinite(entry_bar.low)):
        return None
    if (not math.isfinite(signal_bar.high) or not math.isfinite(signal_bar.low)
            or not math.isfinite(signal_bar.close)):
        return None

    direction = shadow["direction"]
    atr = shadow["atr"]
    stop_dist = shadow["stop_distance"]

    # Slippage model
    et_time = bar_to_et(entry_bar.timestamp)
    h, m = et_time.hour, et_time.minute
    t = h + m / 60.0
    slippage = SLIPPAGE_RTH if 9.5 <= t < 16.0 else SLIPPAGE_ETH

    if direction == "LONG":
        entry_price = entry_bar.open + slippage
    else:
        entry_price = entry_bar.open - slippage

    target_dist = stop_dist * 1.5

    if stop_dist <= 0:
        return None

    # Compute formula stop price
    if direction == "LONG":
        formula_stop_price = entry_price - stop_dist
    else:
        formula_stop_price = entry_price + stop_dist

    # Compute structural stop (using signal bar's extreme as the swept level)
    if direction == "LONG":
        swept_level = signal_bar.low
        structural_stop_price = swept_level - STRUCTURAL_BUFFER
        structural_distance = entry_price - structural_stop_price
    else:
        swept_level = signal_bar.high
        structural_stop_price = swept_level + STRUCTURAL_BUFFER
        structural_distance = structural_stop_price - entry_price

    # Walk forward for MFE/MAE/outcome
    mfe = 0.0
    mae = 0.0
    outcome = "TIMEOUT"
    final_price = entry_price

    max_walk_bars = 120
    walk_end = min(entry_idx + 1 + max_walk_bars, total_bars)

    for j in range(entry_idx + 1, walk_end):
        walk_bar = exec_bars[j]
        if (not math.isfinite(walk_bar.high) or not math.isfinite(walk_bar.low)
                or not math.isfinite(walk_bar.close)):
            continue

        if direction == "LONG":
            favorable = walk_bar.high - entry_price
            adverse = entry_price - walk_bar.low
        else:
            favorable = entry_price - walk_bar.low
            adverse = walk_bar.high - entry_price

        mfe = max(mfe, favorable)
        mae = max(mae, adverse)
        final_price = walk_bar.close

        if mae >= stop_dist:
            outcome = "LOSS"
            break
        if mfe >= target_dist:
            outcome = "WIN"
            break

    # Shadow PnL (using formula stop)
    if outcome == "WIN":
        shadow_pnl = (target_dist * POINT_VALUE * NUM_CONTRACTS) - COMMISSION_RT
    elif outcome == "LOSS":
        shadow_pnl = -(stop_dist * POINT_VALUE * NUM_CONTRACTS) - COMMISSION_RT
    else:
        if direction == "LONG":
            mtm_points = final_price - entry_price
        else:
            mtm_points = entry_price - final_price
        shadow_pnl = (mtm_points * POINT_VALUE * NUM_CONTRACTS) - COMMISSION_RT

    # Also simulate with structural stop
    struct_mfe = 0.0
    struct_mae = 0.0
    struct_outcome = "TIMEOUT"
    struct_final_price = entry_price

    if structural_distance > 0:
        struct_target_dist = structural_distance * 1.5
        walk_end2 = min(entry_idx + 1 + max_walk_bars, total_bars)

        for j in range(entry_idx + 1, walk_end2):
            walk_bar = exec_bars[j]
            if (not math.isfinite(walk_bar.high) or not math.isfinite(walk_bar.low)
                    or not math.isfinite(walk_bar.close)):
                continue

            if direction == "LONG":
                favorable = walk_bar.high - entry_price
                adverse = entry_price - walk_bar.low
            else:
                favorable = entry_price - walk_bar.low
                adverse = walk_bar.high - entry_price

            struct_mfe = max(struct_mfe, favorable)
            struct_mae = max(struct_mae, adverse)
            struct_final_price = walk_bar.close

            if struct_mae >= structural_distance:
                struct_outcome = "LOSS"
                break
            if struct_mfe >= struct_target_dist:
                struct_outcome = "WIN"
                break

        if struct_outcome == "WIN":
            struct_pnl = (struct_target_dist * POINT_VALUE * NUM_CONTRACTS) - COMMISSION_RT
        elif struct_outcome == "LOSS":
            struct_pnl = -(structural_distance * POINT_VALUE * NUM_CONTRACTS) - COMMISSION_RT
        else:
            if direction == "LONG":
                mtm = struct_final_price - entry_price
            else:
                mtm = entry_price - struct_final_price
            struct_pnl = (mtm * POINT_VALUE * NUM_CONTRACTS) - COMMISSION_RT
    else:
        struct_pnl = 0.0
        struct_outcome = "INVALID"
        struct_target_dist = 0.0

    # Stop formula decomposition
    stop_formula = f"ATR({atr:.2f}) * {ATR_MULTIPLIER_STOP} = {atr * ATR_MULTIPLIER_STOP:.2f}"

    return {
        "bar_index": bar_idx,
        "timestamp": shadow["timestamp"],
        "direction": direction,
        "entry_score": round(shadow["score"], 4),
        "raw_stop_distance": round(stop_dist, 2),
        "stop_formula": stop_formula,
        "stop_formula_inputs": {
            "atr_14": round(atr, 4),
            "atr_multiplier": ATR_MULTIPLIER_STOP,
            "computed_stop": round(atr * ATR_MULTIPLIER_STOP, 2),
        },
        "atr_value": round(atr, 4),
        "stop_atr_ratio": round(stop_dist / atr, 4) if atr > 0 else 0,
        "entry_price": round(entry_price, 2),
        "formula_stop_price": round(formula_stop_price, 2),
        "swept_level": round(swept_level, 2),
        "distance_entry_to_swept": round(abs(entry_price - swept_level), 2),
        "structural_stop_price": round(structural_stop_price, 2),
        "structural_distance": round(structural_distance, 2),
        "structural_passes_gate": structural_distance <= MAX_STOP_PTS,
        "mfe": round(mfe, 2),
        "mae": round(mae, 2),
        "outcome": outcome,
        "shadow_pnl": round(shadow_pnl, 2),
        "structural_outcome": struct_outcome,
        "structural_pnl": round(struct_pnl, 2),
        "rejection_reason": shadow["rejection_reason"],
    }


def print_distribution(forensic_signals):
    """Print stop distance distribution."""
    buckets = {
        "30-35 pts": (30, 35),
        "35-40 pts": (35, 40),
        "40-50 pts": (40, 50),
        "50-60 pts": (50, 60),
        "60+ pts":   (60, 9999),
    }

    total = len(forensic_signals)

    print(f"\n{'=' * 72}")
    print(f"  DISTRIBUTION OF STOP DISTANCES")
    print(f"{'=' * 72}")

    for label, (lo, hi) in buckets.items():
        count = sum(1 for s in forensic_signals if lo <= s["raw_stop_distance"] < hi)
        pct = count / total * 100 if total > 0 else 0
        print(f"  {label:<12}  {count:>4} signals ({pct:>5.1f}%)")


def print_profitability_by_bucket(forensic_signals):
    """Print profitability analysis by stop distance bucket."""
    buckets = {
        "30-35 pts": (30, 35),
        "35-40 pts": (35, 40),
        "40-50 pts": (40, 50),
        "50-60 pts": (50, 60),
        "60+ pts":   (60, 9999),
    }

    print(f"\n{'=' * 72}")
    print(f"  PROFITABILITY BY STOP DISTANCE BUCKET")
    print(f"{'=' * 72}")
    print(f"  {'Bucket':<12} {'Count':>6} {'WR':>6} {'PF':>7} {'Avg MFE':>9} {'Avg MAE':>9} {'Shadow PnL':>12}")
    print(f"  {'─' * 66}")

    for label, (lo, hi) in buckets.items():
        sigs = [s for s in forensic_signals if lo <= s["raw_stop_distance"] < hi]
        count = len(sigs)
        if count == 0:
            print(f"  {label:<12} {0:>6} {'---':>6} {'---':>7} {'---':>9} {'---':>9} {'---':>12}")
            continue

        wins = sum(1 for s in sigs if s["outcome"] == "WIN")
        wr = wins / count * 100

        gross_wins = sum(s["shadow_pnl"] + COMMISSION_RT for s in sigs
                         if s["shadow_pnl"] + COMMISSION_RT > 0)
        gross_losses = sum(abs(s["shadow_pnl"] + COMMISSION_RT) for s in sigs
                           if s["shadow_pnl"] + COMMISSION_RT < 0)
        pf = gross_wins / gross_losses if gross_losses > 0 else float('inf')

        avg_mfe = sum(s["mfe"] for s in sigs) / count
        avg_mae = sum(s["mae"] for s in sigs) / count
        total_pnl = sum(s["shadow_pnl"] for s in sigs)

        pf_str = f"{pf:.2f}" if pf != float('inf') else "inf"
        print(f"  {label:<12} {count:>6} {wr:>5.1f}% {pf_str:>7} {avg_mfe:>8.1f} {avg_mae:>8.1f} ${total_pnl:>+10,.2f}")


def print_worked_examples(forensic_signals):
    """Print 5 worked examples showing formula vs structural stop gap."""
    # Pick 5 diverse examples: 2 profitable, 2 losers, 1 timeout
    wins = [s for s in forensic_signals if s["outcome"] == "WIN"]
    losses = [s for s in forensic_signals if s["outcome"] == "LOSS"]
    timeouts = [s for s in forensic_signals if s["outcome"] == "TIMEOUT"]

    examples = []
    # Pick from different stop distance ranges for diversity
    for pool in [wins, losses, timeouts]:
        if pool:
            pool_sorted = sorted(pool, key=lambda x: x["raw_stop_distance"])
            examples.append(pool_sorted[0])
            if len(pool_sorted) > 1:
                examples.append(pool_sorted[len(pool_sorted) // 2])

    examples = examples[:5]
    if len(examples) < 5:
        remaining = [s for s in forensic_signals if s not in examples]
        remaining.sort(key=lambda x: x["raw_stop_distance"])
        for s in remaining:
            if len(examples) >= 5:
                break
            if s not in examples:
                examples.append(s)

    print(f"\n{'=' * 72}")
    print(f"  STOP FORMULA FORENSIC — 5 WORKED EXAMPLES")
    print(f"{'=' * 72}")

    for i, ex in enumerate(examples, 1):
        atr = ex["atr_value"]
        stop_dist = ex["raw_stop_distance"]
        struct_dist = ex["structural_distance"]
        gap = stop_dist - struct_dist

        print(f"\n  EXAMPLE {i}:")
        print(f"    Entry: {ex['entry_price']:,.2f} ({ex['direction']})")
        print(f"    ATR: {atr:.2f}")
        print(f"    Stop formula: entry {'−' if ex['direction']=='LONG' else '+'} "
              f"(ATR × {ATR_MULTIPLIER_STOP}) = "
              f"{ex['entry_price']:,.2f} {'−' if ex['direction']=='LONG' else '+'} "
              f"{atr * ATR_MULTIPLIER_STOP:.1f} = {ex['formula_stop_price']:,.2f}")
        print(f"    Stop distance: {stop_dist:.1f} pts (BLOCKED by {MAX_STOP_PTS:.0f}pt cap)")
        print(f"    Swept level: {ex['swept_level']:,.2f}")
        print(f"    Distance to swept level: {ex['distance_entry_to_swept']:.1f} pts")
        print(f"    Structural stop: {ex['structural_stop_price']:,.2f} "
              f"(swept level {'−' if ex['direction']=='LONG' else '+'} {STRUCTURAL_BUFFER:.0f}pts)")
        print(f"    Structural distance: {struct_dist:.1f} pts "
              f"({'PASSES' if ex['structural_passes_gate'] else 'BLOCKED'} {MAX_STOP_PTS:.0f}pt gate)")
        print(f"    MFE: {ex['mfe']:.1f} pts | MAE: {ex['mae']:.1f} pts | Outcome: {ex['outcome']}")
        print(f"    Shadow PnL (formula): ${ex['shadow_pnl']:+,.2f}")
        print(f"    Shadow PnL (structural): ${ex['structural_pnl']:+,.2f}")
        print(f"    ")
        print(f"    KEY INSIGHT: ATR-based stop is {stop_dist:.1f}pt but structural "
              f"stop is {struct_dist:.1f}pt. "
              f"Gap: {gap:+.1f}pts. "
              f"{'Formula overprotects by ' + f'{gap:.0f}pts' if gap > 0 else 'Structural is wider'}.")


def print_structural_comparison(forensic_signals):
    """Print structural vs formula stop comparison for all blocked signals."""
    formula_dists = [s["raw_stop_distance"] for s in forensic_signals]
    struct_dists = [s["structural_distance"] for s in forensic_signals]

    struct_under_30 = [s for s in forensic_signals if s["structural_passes_gate"]]
    struct_under_25 = [s for s in forensic_signals if s["structural_distance"] <= 25]

    total = len(forensic_signals)

    print(f"\n{'=' * 72}")
    print(f"  STOP DISTANCE COMPARISON: FORMULA vs STRUCTURAL")
    print(f"{'=' * 72}")
    print(f"  {'Metric':<30} {'Formula Stop':>14} {'Structural Stop':>16}")
    print(f"  {'─' * 62}")

    mean_formula = sum(formula_dists) / total if total else 0
    mean_struct = sum(struct_dists) / total if total else 0
    sorted_formula = sorted(formula_dists)
    sorted_struct = sorted(struct_dists)
    median_formula = sorted_formula[total // 2] if total else 0
    median_struct = sorted_struct[total // 2] if total else 0

    print(f"  {'Mean distance':<30} {mean_formula:>13.1f} {mean_struct:>15.1f}")
    print(f"  {'Median distance':<30} {median_formula:>13.1f} {median_struct:>15.1f}")
    print(f"  {'Min distance':<30} {min(formula_dists):>13.1f} {min(struct_dists):>15.1f}")
    print(f"  {'Max distance':<30} {max(formula_dists):>13.1f} {max(struct_dists):>15.1f}")
    print(f"  {'% under 30pt cap':<30} {'0%':>14} "
          f"{len(struct_under_30)/total*100:>14.1f}%")
    print(f"  {'% under 25pt':<30} {'0%':>14} "
          f"{len(struct_under_25)/total*100:>14.1f}%")

    # Profitability of signals recoverable with structural stops
    if struct_under_30:
        wins = sum(1 for s in struct_under_30 if s["structural_outcome"] == "WIN")
        wr = wins / len(struct_under_30) * 100
        total_pnl = sum(s["structural_pnl"] for s in struct_under_30)

        gross_wins = sum(s["structural_pnl"] + COMMISSION_RT for s in struct_under_30
                         if s["structural_pnl"] + COMMISSION_RT > 0)
        gross_losses = sum(abs(s["structural_pnl"] + COMMISSION_RT) for s in struct_under_30
                           if s["structural_pnl"] + COMMISSION_RT < 0)
        pf = gross_wins / gross_losses if gross_losses > 0 else float('inf')
        pf_str = f"{pf:.2f}" if pf != float('inf') else "inf"

        print(f"\n  If structural stops pass the {MAX_STOP_PTS:.0f}pt gate:")
        print(f"    {len(struct_under_30)} of {total} signals ({len(struct_under_30)/total*100:.1f}%) "
              f"would pass with structural stops")
        print(f"    These {len(struct_under_30)} signals have: "
              f"WR {wr:.1f}%, PF {pf_str}, shadow PnL ${total_pnl:+,.2f}")


def print_root_cause(forensic_signals):
    """Print root cause analysis and recommendation."""
    total = len(forensic_signals)
    formula_dists = [s["raw_stop_distance"] for s in forensic_signals]
    struct_dists = [s["structural_distance"] for s in forensic_signals]
    struct_under_30 = [s for s in forensic_signals if s["structural_passes_gate"]]
    total_formula_pnl = sum(s["shadow_pnl"] for s in forensic_signals)
    total_struct_pnl = sum(s["structural_pnl"] for s in struct_under_30)

    mean_formula = sum(formula_dists) / total if total else 0
    mean_struct = sum(struct_dists) / total if total else 0
    gap = mean_formula - mean_struct

    # Determine stop method
    # Check if all stops are exactly ATR * multiplier
    all_atr_based = all(
        abs(s["raw_stop_distance"] - s["atr_value"] * ATR_MULTIPLIER_STOP) < 0.1
        for s in forensic_signals
    )

    recoverable_pct = len(struct_under_30) / total * 100 if total else 0

    print(f"\n{'=' * 72}")
    print(f"  ROOT CAUSE ANALYSIS")
    print(f"{'═' * 72}")
    print(f"")
    print(f"  1. Stop calculation method: {'ATR-based (ATR × ' + str(ATR_MULTIPLIER_STOP) + ')' if all_atr_based else 'hybrid (ATR-based with sweep override)'}")
    print(f"  2. Why stops exceed {MAX_STOP_PTS:.0f}pt: ATR too wide "
          f"(mean ATR = {sum(s['atr_value'] for s in forensic_signals)/total:.1f}pts, "
          f"× {ATR_MULTIPLIER_STOP} = {sum(s['atr_value'] for s in forensic_signals)/total * ATR_MULTIPLIER_STOP:.1f}pts)")
    print(f"  3. Gap between formula stop and structural stop: {gap:.1f} pts avg")
    print(f"  4. Signals recoverable with structural stops: "
          f"{len(struct_under_30)} of {total} ({recoverable_pct:.1f}%)")
    print(f"  5. Estimated PnL recovery: ${total_struct_pnl:+,.2f} of ${total_formula_pnl:+,.2f}")
    print(f"")
    print(f"  RECOMMENDED FIX:")
    print(f"  {'─' * 64}")
    if recoverable_pct > 50:
        print(f"  The problem is the stop FORMULA, not the stop GATE.")
        print(f"  {len(struct_under_30)} of {total} signals ({recoverable_pct:.0f}%) have structural")
        print(f"  stops under {MAX_STOP_PTS:.0f}pt. The ATR × {ATR_MULTIPLIER_STOP} formula produces stops")
        print(f"  averaging {mean_formula:.1f}pts when structural stops average only {mean_struct:.1f}pts.")
        print(f"")
        print(f"  SPECIFIC RECOMMENDATION:")
        print(f"  For sweep-originated signals, use structural stop = swept_level ± {STRUCTURAL_BUFFER:.0f}pts")
        print(f"  instead of ATR × {ATR_MULTIPLIER_STOP}. This already exists in UCL watch logic")
        print(f"  (_create_wide_stop_watch) but only for sweep-sourced signals. Extend")
        print(f"  to ALL signal sources by computing min(ATR-stop, structural-stop).")
        print(f"  Expected recovery: ${total_struct_pnl:+,.2f} in edge.")
    else:
        print(f"  Both the formula AND structural stops tend to exceed {MAX_STOP_PTS:.0f}pt.")
        print(f"  Only {len(struct_under_30)} of {total} ({recoverable_pct:.0f}%) would pass.")
        print(f"  Consider either raising the gate threshold or implementing a")
        print(f"  tiered approach with reduced position size for wider stops.")
    print(f"{'=' * 72}")


async def main():
    t0 = time.time()
    start_date = "2023-09-01"

    print("=" * 72)
    print("  MAX-STOP GATE FORENSIC ANALYSIS")
    print(f"  Dataset: period_4 (Sep 2023 – Feb 2024, chop regime)")
    print(f"  Max exec bars: {MAX_EXEC_BARS:,}")
    print("=" * 72)

    end_date = estimate_end_date(DATA_DIR, start_date, MAX_EXEC_BARS)
    print(f"\n  Estimated end date for {MAX_EXEC_BARS:,} bars: {end_date}")

    # ── Create simulator ──
    sim = ReplaySimulator(
        speed="max",
        start_date=start_date,
        end_date=end_date,
        validate=True,
        data_dir=DATA_DIR,
        c1_variant="C",
        quiet=True,
        sweep_enabled=True,
    )

    # ── Run replay ──
    print("\n  Running replay (this takes ~3 minutes)...")
    results = await sim.run()
    elapsed_replay = time.time() - t0
    print(f"  Replay completed in {elapsed_replay:.1f}s")
    print(f"  Total shadow signals recorded: {sim._shadow_signal_count:,}")

    # ── Extract max-stop blocked signals ──
    exec_bars = sim._exec_bars
    total_bars = len(exec_bars)
    max_stop_signals = []

    for shadow in sim._iter_all_shadow_signals():
        reason = shadow.get("rejection_reason", "")
        if "Max stop exceeded" in reason:
            max_stop_signals.append(shadow)

    print(f"  Max-stop blocked signals: {len(max_stop_signals)}")

    # ── Forensic simulation for each blocked signal ──
    print(f"\n  Running forensic simulation on {len(max_stop_signals)} blocked signals...")
    forensic_signals = []
    for shadow in max_stop_signals:
        result = simulate_shadow_trade(shadow, exec_bars, total_bars)
        if result is not None:
            forensic_signals.append(result)

    print(f"  Forensic results: {len(forensic_signals)} signals analyzed")

    # ── Separate by sub-gate ──
    plain_blocked = [s for s in forensic_signals
                     if s["rejection_reason"] == "Max stop exceeded"]
    ucl_routed = [s for s in forensic_signals
                  if "UCL watch" in s["rejection_reason"]]
    print(f"    Plain max-stop blocked: {len(plain_blocked)}")
    print(f"    UCL watch routed:       {len(ucl_routed)}")

    # ── STEP 2: Statistical profile ──
    print_distribution(forensic_signals)
    print_profitability_by_bucket(forensic_signals)

    # ── STEP 3: Worked examples ──
    print_worked_examples(forensic_signals)

    # ── STEP 4: Structural vs formula comparison ──
    print_structural_comparison(forensic_signals)

    # ── STEP 5: Root cause ──
    print_root_cause(forensic_signals)

    # ── Write JSON output ──
    output = {
        "forensic": "max_stop_gate_analysis",
        "generated": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "period": "period_4",
            "label": "Sep 2023 – Feb 2024 (chop regime)",
            "start_date": start_date,
            "end_date": end_date,
            "exec_bars": total_bars,
        },
        "summary": {
            "total_max_stop_blocked": len(forensic_signals),
            "plain_blocked": len(plain_blocked),
            "ucl_routed": len(ucl_routed),
            "total_shadow_pnl": round(sum(s["shadow_pnl"] for s in forensic_signals), 2),
            "total_structural_pnl": round(
                sum(s["structural_pnl"] for s in forensic_signals
                    if s["structural_passes_gate"]), 2),
            "recoverable_with_structural": len(
                [s for s in forensic_signals if s["structural_passes_gate"]]),
            "mean_formula_stop": round(
                sum(s["raw_stop_distance"] for s in forensic_signals) / len(forensic_signals), 2)
                if forensic_signals else 0,
            "mean_structural_stop": round(
                sum(s["structural_distance"] for s in forensic_signals) / len(forensic_signals), 2)
                if forensic_signals else 0,
        },
        "signals": forensic_signals,
    }

    with open(str(OUTPUT_FILE), "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n  Results written to: {OUTPUT_FILE}")

    # ── Print deliverable checklist ──
    print(f"\n{'=' * 72}")
    print(f"  DELIVERABLE CHECKLIST")
    print(f"{'=' * 72}")
    checks = [
        ("scripts/max_stop_forensic.py created and run", True),
        ("logs/max_stop_forensic.json created", OUTPUT_FILE.exists()),
        ("Stop distance distribution printed", True),
        ("Profitability by stop bucket printed", True),
        ("Stop formula identified with 5 worked examples", len(forensic_signals) >= 5),
        ("Structural vs formula stop comparison printed", True),
        ("Root cause analysis with specific recommendation", True),
    ]
    for label, passed in checks:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {label}")

    total_time = time.time() - t0
    print(f"\n  Total time: {total_time:.1f}s")

    return output


if __name__ == "__main__":
    asyncio.run(main())
