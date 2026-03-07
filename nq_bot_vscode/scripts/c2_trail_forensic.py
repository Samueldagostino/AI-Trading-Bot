#!/usr/bin/env python3
"""
INVESTIGATION 1: C2 Trailing Stop Forensic Analysis
====================================================
Analyzes C2 capture ratios and simulates wider trail widths.
Uses executor_trade_history from backtest_checkpoint.json.

Output: logs/c2_trail_forensic.json
"""

import json
import statistics
from pathlib import Path
from datetime import datetime

LOGS = Path(__file__).resolve().parent.parent / "logs"
CHECKPOINT = LOGS / "backtest_checkpoint.json"
OUTPUT = LOGS / "c2_trail_forensic.json"

# MNQ point value: $0.50 per tick, 4 ticks per point = $2.00/point
MNQ_POINT_VALUE = 2.0


def load_trades():
    with open(CHECKPOINT) as f:
        data = json.load(f)
    return data["executor_trade_history"]


def analyze_capture_ratios(trades):
    """For each closed C2 trade, compute capture ratio = captured_move / available_move."""
    results = []
    for t in trades:
        c2 = t["c2"]
        if c2["is_open"] or not c2["is_filled"]:
            continue

        direction = t["direction"]
        entry = c2["entry_price"]
        exit_price = c2["exit_price"]
        best_price = t["c2_best_price"]
        trail_stop = t["c2_trailing_stop"]
        atr = t["atr_at_entry"]
        exit_reason = c2["exit_reason"]

        if direction == "long":
            available_move = best_price - entry
            captured_move = exit_price - entry
        else:
            available_move = entry - best_price
            captured_move = entry - exit_price

        # Skip trades where price never moved favorably
        if available_move <= 0:
            capture_ratio = 0.0
        else:
            capture_ratio = captured_move / available_move

        # Clamp to [0, 1] — exit can be worse than entry (negative capture)
        # but we keep raw for analysis
        results.append({
            "trade_id": t["trade_id"],
            "direction": direction,
            "entry_price": entry,
            "exit_price": exit_price,
            "best_price": best_price,
            "trail_stop": trail_stop,
            "atr_at_entry": atr,
            "trail_width_points": atr * 2.0,  # current: ATR × 2.0
            "available_move": round(available_move, 2),
            "captured_move": round(captured_move, 2),
            "capture_ratio": round(capture_ratio, 4),
            "c2_pnl": round(c2["net_pnl"], 2),
            "exit_reason": exit_reason,
        })

    return results


def print_distribution(results):
    """Print capture ratio distribution."""
    ratios = [r["capture_ratio"] for r in results]
    total = len(ratios)

    buckets = {
        "<0% (gave back gains)": len([r for r in ratios if r < 0]),
        "0-25%": len([r for r in ratios if 0 <= r < 0.25]),
        "25-50%": len([r for r in ratios if 0.25 <= r < 0.50]),
        "50-75%": len([r for r in ratios if 0.50 <= r < 0.75]),
        "75-100%": len([r for r in ratios if 0.75 <= r <= 1.0]),
        ">100% (exited beyond best?)": len([r for r in ratios if r > 1.0]),
    }

    print("\n" + "=" * 60)
    print("C2 CAPTURE RATIO DISTRIBUTION")
    print("=" * 60)
    for label, count in buckets.items():
        pct = count / total * 100 if total > 0 else 0
        bar = "█" * int(pct / 2)
        print(f"  {label:30s}  {count:4d}  ({pct:5.1f}%)  {bar}")

    print(f"\n  Total C2 trades analyzed: {total}")
    print(f"  Median capture ratio:     {statistics.median(ratios):.2%}")
    print(f"  Mean capture ratio:       {statistics.mean(ratios):.2%}")
    print(f"  Stdev:                    {statistics.stdev(ratios):.2%}")

    # By exit reason
    print("\n  Capture ratio by exit reason:")
    by_reason = {}
    for r in results:
        reason = r["exit_reason"]
        by_reason.setdefault(reason, []).append(r["capture_ratio"])
    for reason, rats in sorted(by_reason.items()):
        med = statistics.median(rats)
        print(f"    {reason:25s}  n={len(rats):4d}  median={med:.2%}  mean={statistics.mean(rats):.2%}")

    return buckets, ratios


def simulate_trail_variants(trades):
    """Simulate 3 trail width variants using the same trade entries."""
    # Current ATR multiplier is 2.0
    variants = [
        {"label": "Current (ATR×2.0)", "multiplier": 2.0},
        {"label": "Wider 25% (ATR×2.5)", "multiplier": 2.5},
        {"label": "Wider 50% (ATR×3.0)", "multiplier": 3.0},
    ]

    results = {}

    for variant in variants:
        mult = variant["multiplier"]
        label = variant["label"]

        total_pnl = 0.0
        gross_profit = 0.0
        gross_loss = 0.0
        capture_ratios = []
        trade_count = 0

        for t in trades:
            c2 = t["c2"]
            if c2["is_open"] or not c2["is_filled"]:
                continue

            direction = t["direction"]
            entry = c2["entry_price"]
            best_price = t["c2_best_price"]
            atr = t["atr_at_entry"]
            commission = c2["commission"]

            trail_width = atr * mult

            # Simulate: the trail stop would be placed at best_price - trail_width (long)
            # or best_price + trail_width (short)
            if direction == "long":
                available_move = best_price - entry
                sim_trail_stop = best_price - trail_width

                # The exit price is the trail stop, but it can't be worse than the
                # worst case (entry - initial_stop_distance) or better than best_price
                # With a wider trail, if best_price - trail_width < entry (i.e., trail
                # never locked in profit), exit at entry - initial stop
                # But we're simulating the trail only: exit = max(sim_trail_stop, initial_stop)
                initial_stop = t.get("initial_stop", entry - t.get("atr_at_entry", 10) * 2.0)

                # For wider trails: exit is at trail stop if it's above initial stop
                if sim_trail_stop > initial_stop:
                    sim_exit = sim_trail_stop
                else:
                    # Trail never locked in enough, would have hit initial stop
                    # Use original exit as fallback (stop/breakeven case)
                    sim_exit = c2["exit_price"]

                sim_captured = sim_exit - entry
            else:
                available_move = entry - best_price
                sim_trail_stop = best_price + trail_width

                initial_stop = t.get("initial_stop", entry + t.get("atr_at_entry", 10) * 2.0)

                if sim_trail_stop < initial_stop:
                    sim_exit = sim_trail_stop
                else:
                    sim_exit = c2["exit_price"]

                sim_captured = entry - sim_exit

            # For trades that exited via non-trailing reasons (stop, breakeven, time_stop),
            # the trail width change doesn't affect them unless the wider trail would have
            # kept the trade alive longer. Without bar-by-bar data we can't simulate that.
            # We only re-simulate trades that exited via "trailing"
            exit_reason = c2["exit_reason"]
            if exit_reason != "trailing":
                # Use original PnL for non-trailing exits
                pnl = c2["net_pnl"]
                if available_move > 0:
                    capture_ratios.append(c2["exit_price"] - entry if direction == "long" else entry - c2["exit_price"])
                    capture_ratios[-1] = capture_ratios[-1] / available_move if available_move > 0 else 0
            else:
                # Trailing exit: simulate with new trail width
                gross_pnl = sim_captured * MNQ_POINT_VALUE
                pnl = gross_pnl - commission

                if available_move > 0:
                    capture_ratios.append(sim_captured / available_move if available_move > 0 else 0)

            total_pnl += pnl
            if pnl > 0:
                gross_profit += pnl
            else:
                gross_loss += abs(pnl)
            trade_count += 1

        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        avg_capture = statistics.mean(capture_ratios) if capture_ratios else 0

        results[label] = {
            "multiplier": mult,
            "trail_width_formula": f"ATR × {mult}",
            "trades": trade_count,
            "total_pnl": round(total_pnl, 2),
            "profit_factor": round(pf, 3),
            "avg_capture_ratio": round(avg_capture, 4),
            "median_capture_ratio": round(statistics.median(capture_ratios), 4) if capture_ratios else 0,
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
        }

    return results


def main():
    print("Loading trade data...")
    trades = load_trades()
    print(f"Loaded {len(trades)} trades from backtest checkpoint")

    # Step 1: Capture ratio analysis
    c2_analysis = analyze_capture_ratios(trades)
    buckets, ratios = print_distribution(c2_analysis)

    # Step 2: Trail width simulation
    print("\n" + "=" * 60)
    print("TRAIL WIDTH VARIANT SIMULATION")
    print("=" * 60)
    variants = simulate_trail_variants(trades)

    print(f"\n  {'Variant':<30s}  {'PnL':>10s}  {'PF':>6s}  {'Avg Cap':>8s}  {'Med Cap':>8s}")
    print("  " + "-" * 66)
    for label, v in variants.items():
        print(f"  {label:<30s}  ${v['total_pnl']:>9,.2f}  {v['profit_factor']:>5.3f}  "
              f"{v['avg_capture_ratio']:>7.2%}  {v['median_capture_ratio']:>7.2%}")

    # Diagnosis
    median_capture = statistics.median(ratios)
    print(f"\n  DIAGNOSIS: Median capture ratio = {median_capture:.2%}")
    if median_capture < 0.50:
        print("  ⚠ CONFIRMED: Trail is too tight — median capture < 50%")
        print("  → C2 is giving back >50% of available favorable movement")
    else:
        print("  ✓ Trail width appears adequate (median capture ≥ 50%)")

    # Write results
    output = {
        "investigation": "C2 Trailing Stop Forensic",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "data_source": "backtest_checkpoint.json (executor_trade_history)",
        "trades_analyzed": len(c2_analysis),
        "current_trail_config": {
            "type": "atr",
            "multiplier": 2.0,
            "formula": "trail_width = atr_at_entry × 2.0"
        },
        "capture_ratio_distribution": {
            "below_0pct": buckets["<0% (gave back gains)"],
            "0_to_25pct": buckets["0-25%"],
            "25_to_50pct": buckets["25-50%"],
            "50_to_75pct": buckets["50-75%"],
            "75_to_100pct": buckets["75-100%"],
        },
        "capture_ratio_stats": {
            "median": round(statistics.median(ratios), 4),
            "mean": round(statistics.mean(ratios), 4),
            "stdev": round(statistics.stdev(ratios), 4),
        },
        "trail_width_variants": variants,
        "diagnosis": {
            "median_capture_below_50pct": median_capture < 0.50,
            "recommendation": (
                "Widen C2 trail — median capture ratio indicates premature exits"
                if median_capture < 0.50
                else "Trail width adequate — look elsewhere for PnL improvement"
            ),
        },
        "trade_details": c2_analysis[:20],  # First 20 for reference
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Results written to: {OUTPUT}")


if __name__ == "__main__":
    main()
