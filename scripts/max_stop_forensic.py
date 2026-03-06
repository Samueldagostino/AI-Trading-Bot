#!/usr/bin/env python3
"""
Max Stop Gate Forensic Analysis
================================
Analyzes trades blocked by the 30pt HC stop cap to determine whether
the formula is producing unnecessarily wide stops vs. genuinely wide setups.

Classifies blocked trades into three buckets:
  A — "Formula Too Wide": structural stop < 30pt but formula stop > 30pt
  B — "Legitimately Wide": both structural and formula stop > 30pt
  C — "No Clear Structure": structural stop can't be cleanly identified

DO NOT change the 30pt cap — this is analysis only.

Usage:
    python scripts/max_stop_forensic.py
"""

import asyncio
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# ── Paths ──
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
PROJECT_DIR = REPO_ROOT / "nq_bot_vscode"
LOGS_DIR = REPO_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PROJECT_DIR))

from scripts.replay_simulator import ReplaySimulator

# ── Config ──
DATA_DIR = str(REPO_ROOT / "data" / "firstrate" / "historical" / "aggregated"
               / "period_4_2023-09_to_2024-02")
EXEC_TF = "2m"
MAX_STOP_CAP = 30.0


async def run_forensic():
    """Run forensic analysis on max-stop blocked trades."""
    t0 = time.time()

    print("=" * 72)
    print("  MAX STOP GATE FORENSIC ANALYSIS")
    print("  DO NOT CHANGE THE 30pt CAP — analysis only")
    print("=" * 72)

    # ── Load existing shadow results ──
    shadow_path = LOGS_DIR / "fast_shadow_results.json"
    if not shadow_path.exists():
        print(f"\n  ERROR: {shadow_path} not found.")
        print("  Run scripts/fast_shadow_diagnostic.py first.")
        sys.exit(1)

    with open(str(shadow_path)) as f:
        shadow_data = json.load(f)

    # ── Extract max-stop blocked trades from shadow analysis ──
    by_gate = shadow_data.get("shadow_analysis", {}).get("by_gate", {})

    max_stop_direct = by_gate.get("Max stop exceeded", {})
    max_stop_ucl = by_gate.get("Max stop exceeded \u2014 routed to UCL watch", {})

    direct_count = max_stop_direct.get("count", 0)
    ucl_count = max_stop_ucl.get("count", 0)
    total_blocked = direct_count + ucl_count

    print(f"\n  Blocked trades found:")
    print(f"    Max stop exceeded (direct):    {direct_count}")
    print(f"    Max stop exceeded (UCL route):  {ucl_count}")
    print(f"    Total:                          {total_blocked}")

    if total_blocked == 0:
        print("\n  No max-stop blocked trades to analyze.")
        sys.exit(0)

    # ── Run replay to capture detailed shadow signals ──
    print("\n  Running replay to capture detailed shadow signals...")

    sim = ReplaySimulator(
        speed="max",
        start_date="2023-09-01",
        end_date="2023-12-01",
        validate=True,
        data_dir=DATA_DIR,
        c1_variant="C",
        quiet=True,
        sweep_enabled=True,
    )

    results = await sim.run()
    shadow_analysis = sim._simulate_shadow_trades()

    # ── Extract individual shadow signals for max-stop gates ──
    max_stop_signals = []
    for shadow in sim._iter_all_shadow_signals():
        reason = shadow.get("rejection_reason", "")
        if "Max stop exceeded" in reason or "max stop" in reason.lower():
            max_stop_signals.append(shadow)

    print(f"  Found {len(max_stop_signals)} individual max-stop shadow signals")

    # ── Stop Formula Documentation ──
    print(f"\n{'=' * 72}")
    print("  STOP FORMULA DOCUMENTATION")
    print("=" * 72)
    print("""
  Formula: stop_distance = min(structural_stop, ATR_14 x 1.5)

  Where:
    - ATR_14 = 14-period Average True Range (NQFeatureEngine)
    - atr_multiplier_stop = 1.5 (from RiskConfig)
    - structural_stop = distance to nearest OB/FVG invalidation level
    - If no structural stop: stop_distance = ATR_14 x 1.5

  For sweep entries, sweep_stop_override = |close - sweep_level|
    - Used ONLY if tighter than formula stop
    - UCL confirmed entries always use confirmed stop

  HC Cap: stop_distance > 30.0 -> REJECTED (or routed to UCL for sweeps)
""")

    # ── Analyze each blocked signal ──
    bucket_a = []  # Formula too wide (structural < 30pt)
    bucket_b = []  # Legitimately wide (both > 30pt)
    bucket_c = []  # No clear structure

    comparison_table = []

    for shadow in max_stop_signals:
        formula_stop = shadow.get("stop_distance", 0)
        atr = shadow.get("atr", 0)
        direction = shadow.get("direction", "UNKNOWN")
        score = shadow.get("score", 0)
        bar_idx = shadow.get("bar_index", 0)

        # Compute what the ATR-based stop would be
        atr_stop = atr * 1.5 if atr > 0 else 0

        # Determine if a structural stop was used (tighter than ATR)
        structural_stop = None
        if formula_stop > 0 and atr_stop > 0:
            if formula_stop < atr_stop - 0.5:
                # Formula used structural stop (it was tighter than ATR ceiling)
                structural_stop = formula_stop
            # else: formula = ATR-based, no structural override

        row = {
            "bar_index": bar_idx,
            "direction": direction,
            "score": round(score, 3),
            "formula_stop": round(formula_stop, 1),
            "atr_stop": round(atr_stop, 1),
            "structural_stop": round(structural_stop, 1) if structural_stop is not None else None,
            "atr": round(atr, 2),
        }

        if structural_stop is not None and structural_stop <= MAX_STOP_CAP:
            row["bucket"] = "A"
            row["would_fit"] = True
            bucket_a.append(row)
        elif structural_stop is not None and structural_stop > MAX_STOP_CAP:
            row["bucket"] = "B"
            row["would_fit"] = False
            bucket_b.append(row)
        else:
            # No structural stop identified — ATR-based rejection
            row["bucket"] = "C"
            row["would_fit"] = None
            bucket_c.append(row)

        comparison_table.append(row)

    # ── Shadow PnL from aggregate data ──
    direct_pnl = max_stop_direct.get("shadow_total_pnl", 0)
    ucl_pnl = max_stop_ucl.get("shadow_total_pnl", 0)
    total_shadow_pnl = direct_pnl + ucl_pnl

    direct_wr = max_stop_direct.get("shadow_win_rate", 0)
    ucl_wr = max_stop_ucl.get("shadow_win_rate", 0)
    direct_pf = max_stop_direct.get("shadow_profit_factor", 0)
    ucl_pf = max_stop_ucl.get("shadow_profit_factor", 0)

    # ── Stop distribution stats ──
    all_stops = [s.get("stop_distance", 0) for s in max_stop_signals if s.get("stop_distance", 0) > 0]

    if all_stops:
        stops_sorted = sorted(all_stops)
        n = len(stops_sorted)
        stop_stats = {
            "min": round(stops_sorted[0], 1),
            "max": round(stops_sorted[-1], 1),
            "mean": round(sum(stops_sorted) / n, 1),
            "median": round(stops_sorted[n // 2], 1),
            "p75": round(stops_sorted[int(n * 0.75)], 1) if n > 3 else round(stops_sorted[-1], 1),
            "p90": round(stops_sorted[int(n * 0.90)], 1) if n > 9 else round(stops_sorted[-1], 1),
        }
    else:
        stop_stats = {"min": 0, "max": 0, "mean": 0, "median": 0, "p75": 0, "p90": 0}

    # ── Print comparison table ──
    print("=" * 72)
    print("  STRUCTURAL vs FORMULA STOP COMPARISON")
    print("=" * 72)
    print(f"  {'#':<4} {'Dir':<6} {'Score':<6} {'Formula':<10} {'ATR Stop':<10} {'Struct':<10} {'Fits?':<6} {'Bucket'}")
    print(f"  {'-' * 66}")

    for i, row in enumerate(comparison_table):
        struct_str = f"{row['structural_stop']:.1f}" if row['structural_stop'] is not None else "---"
        fits_str = "YES" if row['would_fit'] is True else ("NO" if row['would_fit'] is False else "?")
        print(f"  {i+1:<4} {row['direction']:<6} {row['score']:<6} "
              f"{row['formula_stop']:<10.1f} {row['atr_stop']:<10.1f} "
              f"{struct_str:<10} {fits_str:<6} {row['bucket']}")

    # ── Bucket classification ──
    bucket_a_pct = len(bucket_a) / len(comparison_table) * 100 if comparison_table else 0
    bucket_b_pct = len(bucket_b) / len(comparison_table) * 100 if comparison_table else 0
    bucket_c_pct = len(bucket_c) / len(comparison_table) * 100 if comparison_table else 0

    print(f"\n{'=' * 72}")
    print(f"  BUCKET CLASSIFICATION")
    print(f"{'=' * 72}")
    print(f"  Bucket A (Formula Too Wide):     {len(bucket_a)} trades ({bucket_a_pct:.0f}%)")
    print(f"  Bucket B (Legitimately Wide):    {len(bucket_b)} trades ({bucket_b_pct:.0f}%)")
    print(f"  Bucket C (No Clear Structure):   {len(bucket_c)} trades ({bucket_c_pct:.0f}%)")
    print(f"  Total:                           {len(comparison_table)} trades")
    print()
    print(f"  Aggregate Shadow PnL (all max-stop blocked):")
    print(f"    Direct rejections:  ${direct_pnl:+,.2f} (WR={direct_wr:.1f}%, PF={direct_pf})")
    print(f"    UCL-routed:         ${ucl_pnl:+,.2f} (WR={ucl_wr:.1f}%, PF={ucl_pf})")
    print(f"    Total:              ${total_shadow_pnl:+,.2f}")
    print()
    print(f"  Stop Distance Distribution (blocked trades):")
    for k, v in stop_stats.items():
        print(f"    {k:<8} {v:.1f} pts")

    # ── Generate recommendation ──
    print(f"\n{'=' * 72}")
    print(f"  RECOMMENDATION")
    print(f"{'=' * 72}")

    if bucket_a_pct > 60:
        recommendation = "USE_STRUCTURAL_STOPS"
        confidence = "HIGH"
        detail = (
            f"Bucket A ({len(bucket_a)} trades, {bucket_a_pct:.0f}%) dominates. "
            f"The formula is producing unnecessarily wide stops. "
            f"Recommend: use structural stop when available and < 30pt."
        )
    elif bucket_b_pct > 50 and total_shadow_pnl > 0:
        recommendation = "CONSIDER_RAISING_CAP"
        confidence = "MEDIUM"
        cap_suggestion = stop_stats.get("p75", 35)
        detail = (
            f"Bucket B ({len(bucket_b)} trades, {bucket_b_pct:.0f}%) majority "
            f"with positive shadow PnL (${total_shadow_pnl:+,.2f}). "
            f"Consider raising cap to {cap_suggestion}pt (p75). "
            f"Requires full backtest validation."
        )
    elif bucket_c_pct > 50:
        recommendation = "KEEP_30PT_CAP"
        confidence = "MEDIUM"
        detail = (
            f"Bucket C ({len(bucket_c)} trades, {bucket_c_pct:.0f}%) dominant — "
            f"most blocked trades use ATR-based stops with no structural override. "
            f"Shadow data lacks structural stop info for definitive classification. "
            f"Small sample ({len(comparison_table)} trades), shadow PnL ${total_shadow_pnl:+,.2f}. "
            f"The 30pt cap blocks a small number of trades with marginal PnL impact. "
            f"Recommend: keep current 30pt cap. The UCL watch mechanism already "
            f"recovers the best wide-stop setups through post-sweep confirmation."
        )
    else:
        recommendation = "KEEP_30PT_CAP"
        confidence = "MEDIUM"
        detail = (
            f"No clear dominant bucket. Small sample ({len(comparison_table)} trades). "
            f"Shadow PnL ${total_shadow_pnl:+,.2f}. Keep current cap."
        )

    print(f"\n  RECOMMENDATION: {recommendation}")
    print(f"  Confidence: {confidence}")
    print(f"  Detail: {detail}")

    # ── Write forensic report JSON ──
    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "analysis": "max_stop_forensic",
        "dataset": shadow_data.get("dataset", {}),
        "stop_formula": {
            "formula": "min(structural_stop, ATR_14 x 1.5)",
            "atr_multiplier_stop": 1.5,
            "hc_max_stop_pts": MAX_STOP_CAP,
            "inputs": ["ATR_14", "structural_stop_distance (OB/FVG invalidation)"],
        },
        "blocked_trades": {
            "total": len(comparison_table),
            "direct_rejections": direct_count,
            "ucl_routed": ucl_count,
        },
        "stop_distribution": stop_stats,
        "bucket_classification": {
            "bucket_a_formula_too_wide": {
                "count": len(bucket_a),
                "pct": round(bucket_a_pct, 1),
                "trades": bucket_a,
            },
            "bucket_b_legitimately_wide": {
                "count": len(bucket_b),
                "pct": round(bucket_b_pct, 1),
                "trades": bucket_b,
            },
            "bucket_c_no_clear_structure": {
                "count": len(bucket_c),
                "pct": round(bucket_c_pct, 1),
                "trades": bucket_c,
            },
        },
        "shadow_pnl": {
            "direct_pnl": direct_pnl,
            "ucl_pnl": ucl_pnl,
            "total": total_shadow_pnl,
            "direct_wr": direct_wr,
            "ucl_wr": ucl_wr,
            "direct_pf": direct_pf,
            "ucl_pf": ucl_pf,
        },
        "recommendation": {
            "action": recommendation,
            "confidence": confidence,
            "detail": detail,
        },
    }

    report_path = LOGS_DIR / "max_stop_forensic_report.json"
    with open(str(report_path), "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n  Report saved to: {report_path}")

    # ── Write recommendation text file ──
    rec_path = LOGS_DIR / "max_stop_forensic_recommendation.txt"
    with open(str(rec_path), "w") as f:
        f.write("MAX STOP GATE FORENSIC — RECOMMENDATION\n")
        f.write("=" * 50 + "\n\n")
        f.write(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"Dataset: period_4 (Sep 2023 - Feb 2024)\n\n")
        f.write(f"Stop Formula: min(structural_stop, ATR_14 x 1.5)\n")
        f.write(f"HC Cap: {MAX_STOP_CAP} pts\n\n")
        f.write(f"Blocked Trades Analyzed: {len(comparison_table)}\n")
        f.write(f"  Direct rejections: {direct_count}\n")
        f.write(f"  UCL-routed: {ucl_count}\n\n")
        f.write(f"Stop Distribution:\n")
        for k, v in stop_stats.items():
            f.write(f"  {k:<8} {v:.1f} pts\n")
        f.write(f"\nBucket Classification:\n")
        f.write(f"  A (Formula Too Wide):   {len(bucket_a)} trades ({bucket_a_pct:.0f}%)\n")
        f.write(f"  B (Legitimately Wide):  {len(bucket_b)} trades ({bucket_b_pct:.0f}%)\n")
        f.write(f"  C (No Clear Structure): {len(bucket_c)} trades ({bucket_c_pct:.0f}%)\n\n")
        f.write(f"Shadow PnL (if these trades were executed):\n")
        f.write(f"  Direct: ${direct_pnl:+,.2f} (WR={direct_wr:.1f}%, PF={direct_pf})\n")
        f.write(f"  UCL:    ${ucl_pnl:+,.2f} (WR={ucl_wr:.1f}%, PF={ucl_pf})\n")
        f.write(f"  Total:  ${total_shadow_pnl:+,.2f}\n\n")
        f.write(f"RECOMMENDATION: {recommendation}\n")
        f.write(f"Confidence: {confidence}\n\n")
        f.write(f"{detail}\n")
    print(f"  Recommendation saved to: {rec_path}")

    print(f"\n  Total time: {time.time() - t0:.1f}s")
    return report


if __name__ == "__main__":
    asyncio.run(run_forensic())
