"""
HTF Gate Rejection Diagnostic
==============================
Runs the replay simulator pipeline, captures EVERY HTF-rejected signal,
determines which specific timeframe(s) caused the rejection, and simulates
what WOULD have happened if the trade was taken.

Output:
  - logs/htf_gate_diagnostic.json   (full rejection analysis)
  - Console summary table

IMPORTANT: This script is READ-ONLY. It does NOT modify:
  - HTF gate logic
  - replay_simulator.py core logic
  - institutional_modifiers.py
"""

import asyncio
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

# Ensure project root is on path
script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))

from config.settings import CONFIG
from config.constants import HTF_TIMEFRAMES, HTF_STRENGTH_GATE
from data_pipeline.pipeline import (
    DataPipeline, BarData, bardata_to_bar, bardata_to_htfbar,
    TradingViewImporter, MINUTES_TO_LABEL,
)
from main import TradingOrchestrator
from signals.aggregator import SignalDirection

LOGS_DIR = project_dir / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH = LOGS_DIR / "htf_gate_diagnostic.json"

ET = ZoneInfo("America/New_York")
EXEC_TF = "2m"

# Session rules (same as replay_simulator)
SESSION_OPEN_HOUR = 18
SESSION_OPEN_MINUTE = 1
SESSION_CLOSE_HOUR = 16
SESSION_CLOSE_MINUTE = 30
MAINTENANCE_START = 17
MAINTENANCE_END = 18

# Shadow simulation constants (same as replay_simulator)
POINT_VALUE = 2.00       # MNQ $2/point
COMMISSION_PER_SIDE = 1.50  # Conservative (real is $1.29)
NUM_CONTRACTS = 2
SLIPPAGE_RTH = 0.75  # Conservative (real avg ~0.50)
SLIPPAGE_ETH = 1.25  # Conservative (real avg ~1.00)
MAX_WALK_BARS = 120  # 4 hours at 2-min bars


def bar_to_et(bar_time: datetime) -> datetime:
    return bar_time.astimezone(ET)


def is_within_session(et_time: datetime) -> bool:
    h, m = et_time.hour, et_time.minute
    if h == MAINTENANCE_START:
        return False
    if h == SESSION_OPEN_HOUR and m < SESSION_OPEN_MINUTE:
        return False
    if h == SESSION_CLOSE_HOUR and m >= SESSION_CLOSE_MINUTE:
        return False
    if SESSION_CLOSE_HOUR < h < MAINTENANCE_START:
        return False
    return True


def load_firstrate_mtf(data_dir: str) -> Dict[str, List[BarData]]:
    """Load aggregated FirstRate CSVs by timeframe."""
    dir_path = Path(data_dir)
    if not dir_path.exists():
        print(f"ERROR: Data directory not found: {data_dir}")
        sys.exit(1)

    tf_map = {
        "NQ_1m.csv": "1m", "NQ_2m.csv": "2m", "NQ_3m.csv": "3m",
        "NQ_5m.csv": "5m", "NQ_15m.csv": "15m", "NQ_30m.csv": "30m",
        "NQ_1H.csv": "1H", "NQ_4H.csv": "4H", "NQ_1D.csv": "1D",
    }

    importer = TradingViewImporter(CONFIG)
    tf_bars: Dict[str, List[BarData]] = {}

    for csv_file in sorted(dir_path.glob("NQ_*.csv")):
        tf_label = tf_map.get(csv_file.name)
        if not tf_label:
            continue
        bars = importer.import_file(str(csv_file))
        if bars:
            for bar in bars:
                bar.source = "firstrate"
            tf_bars[tf_label] = bars
            print(f"  Loaded {tf_label}: {len(bars):,} bars")

    return tf_bars


def filter_by_date(tf_bars, start_date, end_date):
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(
        tzinfo=timezone.utc) if start_date else None
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
        tzinfo=timezone.utc) if end_date else None

    filtered = {}
    for tf, bars in tf_bars.items():
        f = bars
        if start_dt:
            f = [b for b in f if b.timestamp >= start_dt]
        if end_dt:
            f = [b for b in f if b.timestamp < end_dt]
        if f:
            filtered[tf] = f
    return filtered


def classify_htf_conflict(direction: str, tf_biases: Dict[str, str]) -> Dict[str, bool]:
    """Determine which timeframes are conflicting with the signal direction.

    A timeframe 'conflicts' when its bias opposes the signal direction:
    - LONG signal + bearish TF bias = conflict
    - SHORT signal + bullish TF bias = conflict
    """
    conflicts = {}
    opposing = "bearish" if direction == "LONG" else "bullish"
    for tf, bias in tf_biases.items():
        conflicts[tf] = (bias == opposing)
    return conflicts


def simulate_shadow_trade(
    direction: str,
    entry_idx: int,
    stop_distance: float,
    exec_bars: List[BarData],
) -> Dict:
    """Simulate what would have happened if a rejected trade was taken.

    Returns C1 and C2 simulation results.
    """
    total_bars = len(exec_bars)
    if entry_idx >= total_bars:
        return None

    entry_bar = exec_bars[entry_idx]
    if not math.isfinite(entry_bar.open):
        return None

    et_time = bar_to_et(entry_bar.timestamp)
    h, m = et_time.hour, et_time.minute
    t = h + m / 60.0
    slippage = SLIPPAGE_RTH if 9.5 <= t < 16.0 else SLIPPAGE_ETH

    if direction == "LONG":
        entry_price = entry_bar.open + slippage
    else:
        entry_price = entry_bar.open - slippage

    if stop_distance <= 0:
        return None

    target_c1 = stop_distance * 1.5  # C1: 1.5x ATR target
    commission_rt = COMMISSION_PER_SIDE * 2 * NUM_CONTRACTS

    # Simulate the trade walk-forward
    mfe = 0.0
    mae = 0.0
    c1_outcome = "TIMEOUT"
    c2_outcome = "TIMEOUT"
    c1_pnl = 0.0
    c2_pnl = 0.0
    final_price = entry_price

    # C1 simulation: target = 1.5x stop, or stop hit
    walk_end = min(entry_idx + 1 + MAX_WALK_BARS, total_bars)
    c1_exit_bar = walk_end - 1

    for j in range(entry_idx + 1, walk_end):
        bar = exec_bars[j]
        if not math.isfinite(bar.high) or not math.isfinite(bar.low):
            continue

        if direction == "LONG":
            favorable = bar.high - entry_price
            adverse = entry_price - bar.low
        else:
            favorable = entry_price - bar.low
            adverse = bar.high - entry_price

        mfe = max(mfe, favorable)
        mae = max(mae, adverse)
        final_price = bar.close

        if mae >= stop_distance:
            c1_outcome = "LOSS"
            c1_exit_bar = j
            break
        if mfe >= target_c1:
            c1_outcome = "WIN"
            c1_exit_bar = j
            break

    # C1 PnL
    c1_commission = COMMISSION_PER_SIDE * 2  # 1 contract
    if c1_outcome == "WIN":
        c1_pnl = (target_c1 * POINT_VALUE) - c1_commission
    elif c1_outcome == "LOSS":
        c1_pnl = -(stop_distance * POINT_VALUE) - c1_commission
    else:
        if direction == "LONG":
            mtm = final_price - entry_price
        else:
            mtm = entry_price - final_price
        c1_pnl = (mtm * POINT_VALUE) - c1_commission

    # C2 simulation: trailing stop after C1 exits
    c2_mfe = 0.0
    c2_final = entry_price
    trail_distance = stop_distance  # ATR-based trail

    if c1_outcome == "LOSS":
        # Both contracts stopped -- same loss
        c2_outcome = "LOSS"
        c2_pnl = -(stop_distance * POINT_VALUE) - c1_commission
    else:
        # C2 continues with trailing stop from C1 exit point
        hwm = 0.0
        c2_stop = stop_distance  # Initial stop

        for j in range(c1_exit_bar + 1, min(c1_exit_bar + MAX_WALK_BARS, total_bars)):
            bar = exec_bars[j]
            if not math.isfinite(bar.high) or not math.isfinite(bar.low):
                continue

            if direction == "LONG":
                unrealized = bar.high - entry_price
                adverse = entry_price - bar.low
            else:
                unrealized = entry_price - bar.low
                adverse = bar.high - entry_price

            hwm = max(hwm, unrealized)
            c2_mfe = max(c2_mfe, unrealized)
            c2_final = bar.close

            # Trail stop from HWM
            if hwm > 0:
                c2_stop = min(c2_stop, hwm - trail_distance) if trail_distance < hwm else c2_stop

            if adverse >= c2_stop:
                c2_outcome = "STOPPED"
                break

        if direction == "LONG":
            c2_mtm = c2_final - entry_price
        else:
            c2_mtm = entry_price - c2_final

        c2_pnl = (c2_mtm * POINT_VALUE) - c1_commission

        if c2_pnl > 0:
            c2_outcome = "WIN"
        elif c2_outcome != "STOPPED":
            c2_outcome = "TIMEOUT"

    total_pnl = c1_pnl + c2_pnl

    return {
        "entry_price": round(entry_price, 2),
        "stop_distance": round(stop_distance, 2),
        "c1_outcome": c1_outcome,
        "c1_pnl": round(c1_pnl, 2),
        "c2_outcome": c2_outcome,
        "c2_pnl": round(c2_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "mfe": round(mfe, 2),
        "mae": round(mae, 2),
    }


async def run_diagnostic():
    """Main diagnostic: replay with HTF gate logging."""

    start_date = "2025-09-01"
    end_date = "2026-03-01"
    data_dir = str(project_dir / "data" / "firstrate")

    print(f"\n{'=' * 62}")
    print(f"  HTF GATE REJECTION DIAGNOSTIC")
    print(f"  Period: {start_date} to {end_date}")
    print(f"{'=' * 62}\n")

    # ── Load data ──
    print("Loading FirstRate data...")
    tf_bars = load_firstrate_mtf(data_dir)

    if not tf_bars or EXEC_TF not in tf_bars:
        print(f"ERROR: No {EXEC_TF} data found in {data_dir}")
        print("Run: python scripts/aggregate_1m.py --output-dir data/firstrate/")
        sys.exit(1)

    tf_bars = filter_by_date(tf_bars, start_date, end_date)
    if EXEC_TF not in tf_bars:
        print(f"\nERROR: No {EXEC_TF} data in date range")
        sys.exit(1)

    exec_bars = tf_bars[EXEC_TF]
    print(f"\nReplay window: {len(exec_bars):,} exec bars")

    # ── Build MTF iterator ──
    pipeline = DataPipeline(CONFIG)
    mtf_iterator = pipeline.create_mtf_iterator(tf_bars)
    print(f"Total bars (all TFs): {len(mtf_iterator):,}")

    # ── Initialize orchestrator ──
    CONFIG.execution.paper_trading = True
    bot = TradingOrchestrator(CONFIG)
    bot._sweep_enabled = True
    bot._modifiers_enabled = True
    await bot.initialize(skip_db=True)

    # ── Replay loop with HTF gate capture ──
    print("\nRunning replay with HTF gate logging...\n")
    t0 = time.time()

    all_signals = []  # All signals (approved + rejected)
    htf_rejections = []  # HTF-specific rejections with per-TF detail
    approved_count = 0
    last_date = ""
    exec_bar_index = 0

    for i, (timeframe, bar_data) in enumerate(mtf_iterator):
        if timeframe in HTF_TIMEFRAMES:
            bot.process_htf_bar(timeframe, bar_data)
            continue

        if timeframe != EXEC_TF:
            continue

        current_exec_idx = exec_bar_index
        exec_bar_index += 1

        # Session rules
        et_time = bar_to_et(bar_data.timestamp)
        if not is_within_session(et_time):
            continue

        # Daily reset
        date_str = bar_data.timestamp.strftime("%Y-%m-%d")
        if date_str != last_date:
            if bot.executor.has_active_trade and last_date:
                await bot.executor.emergency_flatten(bar_data.close)
            # Reset risk state
            risk_state = bot.risk_engine.state
            risk_state.daily_pnl = 0.0
            risk_state.daily_trades = 0
            risk_state.daily_wins = 0
            risk_state.daily_losses = 0
            risk_state.daily_limit_hit = False
            risk_state.consecutive_losses = 0
            risk_state.consecutive_wins = 0
            risk_state.kill_switch_active = False
            risk_state.kill_switch_reason = ""
            risk_state.kill_switch_resume_at = None
            last_date = date_str

        # Process bar
        exec_bar = bardata_to_bar(bar_data)
        result = await bot.process_bar(exec_bar)

        # ── Capture rejection data ──
        if bot._last_rejection is not None:
            rejection = bot._last_rejection
            gate = rejection.get("gate")
            reason = rejection.get("rejection_reason", "")

            # We care about ALL rejections, but specifically capture HTF gate details
            is_htf_rejection = (gate == 1 or "HTF" in reason)

            # Get current HTF bias state
            htf_bias = bot._htf_bias
            tf_biases = {}
            consensus_dir = "n/a"
            consensus_str = 0.0

            if htf_bias is not None:
                tf_biases = dict(htf_bias.tf_biases)
                consensus_dir = htf_bias.consensus_direction
                consensus_str = htf_bias.consensus_strength

            signal_dir = rejection.get("direction", "UNKNOWN")

            # Determine which TFs caused the conflict
            conflicts = classify_htf_conflict(signal_dir, tf_biases)
            conflicting_tfs = [tf for tf, is_conflict in conflicts.items() if is_conflict]

            # Estimate stop distance
            stop_distance = rejection.get("stop_distance")
            atr = rejection.get("atr", 0.0)
            if stop_distance is None or not math.isfinite(stop_distance) or stop_distance <= 0:
                est = atr * CONFIG.risk.atr_multiplier_stop if math.isfinite(atr) else 10.0
                stop_distance = est if (math.isfinite(est) and est > 0) else 10.0

            record = {
                "bar_index": current_exec_idx,
                "timestamp": bar_data.timestamp.isoformat(),
                "direction": signal_dir,
                "score": round(rejection.get("score", 0.0), 4),
                "stop_distance": round(stop_distance, 2),
                "atr": round(atr if math.isfinite(atr) else 0.0, 4),
                "rejection_reason": reason,
                "rejected_at_gate": gate,
                "is_htf_rejection": is_htf_rejection,
                "htf_consensus_direction": consensus_dir,
                "htf_consensus_strength": round(consensus_str, 3),
                "tf_biases": tf_biases,
                "conflicting_tfs": conflicting_tfs,
            }

            all_signals.append(record)
            if is_htf_rejection:
                htf_rejections.append(record)

        elif result and result.get("action") == "entry":
            approved_count += 1

        # Progress update
        if exec_bar_index % 10000 == 0:
            print(f"  Processed {exec_bar_index:,} exec bars... "
                  f"({len(htf_rejections):,} HTF rejections so far)")

    # Flatten any remaining position
    if bot.executor.has_active_trade:
        last_bar = exec_bars[-1]
        await bot.executor.emergency_flatten(last_bar.close)

    elapsed = time.time() - t0
    print(f"\nReplay complete in {elapsed:.1f}s")

    total_rejected = len(all_signals)
    total_htf_rejected = len(htf_rejections)
    total_signals = approved_count + total_rejected

    # ── Simulate shadow trades for HTF rejections ──
    print(f"\nSimulating {total_htf_rejected:,} HTF-rejected signals...")

    shadow_results = []
    for rej in htf_rejections:
        entry_idx = rej["bar_index"] + 1
        shadow = simulate_shadow_trade(
            direction=rej["direction"],
            entry_idx=entry_idx,
            stop_distance=rej["stop_distance"],
            exec_bars=exec_bars,
        )
        if shadow:
            shadow["bar_index"] = rej["bar_index"]
            shadow["timestamp"] = rej["timestamp"]
            shadow["direction"] = rej["direction"]
            shadow["score"] = rej["score"]
            shadow["conflicting_tfs"] = rej["conflicting_tfs"]
            shadow["tf_biases"] = rej["tf_biases"]
            shadow_results.append(shadow)

    # ── Build analysis ──
    # 1. Rejection reasons breakdown
    rejection_reasons = defaultdict(int)
    for rej in htf_rejections:
        tfs = rej["conflicting_tfs"]
        if len(tfs) == 0:
            # HTF data unavailable or consensus-based
            rejection_reasons["consensus_block"] += 1
        elif len(tfs) == 1:
            rejection_reasons[f"{tfs[0]}_conflict"] += 1
        else:
            rejection_reasons["multiple_conflict"] += 1
            # Also count individual TFs
            for tf in tfs:
                rejection_reasons[f"{tf}_conflict"] += 1

    # 2. Shadow PnL analysis
    winners = [s for s in shadow_results if s["total_pnl"] > 0]
    losers = [s for s in shadow_results if s["total_pnl"] <= 0]
    total_shadow_pnl = sum(s["total_pnl"] for s in shadow_results)
    gross_wins = sum(s["total_pnl"] for s in winners)
    gross_losses = abs(sum(s["total_pnl"] for s in losers)) if losers else 0.0
    shadow_pf = round(gross_wins / gross_losses, 2) if gross_losses > 0 else float("inf")

    # 3. Top 10 most profitable rejected signals
    sorted_by_pnl = sorted(shadow_results, key=lambda x: x["total_pnl"], reverse=True)
    top_10 = []
    for s in sorted_by_pnl[:10]:
        top_10.append({
            "timestamp": s["timestamp"],
            "direction": s["direction"],
            "score": s["score"],
            "total_pnl": s["total_pnl"],
            "conflicting_tfs": s["conflicting_tfs"],
            "mfe": s["mfe"],
        })

    # 4. Per-TF conflict analysis
    tf_conflict_analysis = {}
    htf_tfs = ["1D", "4H", "1H", "30m", "15m", "5m"]
    for tf in htf_tfs:
        tf_shadows = [s for s in shadow_results if tf in s["conflicting_tfs"]]
        if not tf_shadows:
            tf_conflict_analysis[tf] = {
                "rejection_count": 0,
                "shadow_winners": 0,
                "shadow_losers": 0,
                "shadow_pnl": 0.0,
            }
            continue
        tf_winners = [s for s in tf_shadows if s["total_pnl"] > 0]
        tf_losers = [s for s in tf_shadows if s["total_pnl"] <= 0]
        tf_gross_wins = sum(s["total_pnl"] for s in tf_winners)
        tf_gross_losses = abs(sum(s["total_pnl"] for s in tf_losers))
        tf_pf = round(tf_gross_wins / tf_gross_losses, 2) if tf_gross_losses > 0 else float("inf")
        tf_conflict_analysis[tf] = {
            "rejection_count": len(tf_shadows),
            "shadow_winners": len(tf_winners),
            "shadow_losers": len(tf_losers),
            "shadow_pnl": round(sum(s["total_pnl"] for s in tf_shadows), 2),
            "shadow_win_rate": round(len(tf_winners) / len(tf_shadows) * 100, 1),
            "shadow_pf": tf_pf if tf_pf != float("inf") else "inf",
            "avg_mfe": round(sum(s["mfe"] for s in tf_shadows) / len(tf_shadows), 2),
            "avg_mae": round(sum(s["mae"] for s in tf_shadows) / len(tf_shadows), 2),
        }

    # 5. Gate value assessment
    # Compare system PF with HTF gate vs hypothetical PF without it
    # If shadow PnL is negative, gate is protecting edge
    gate_verdict = "ABOUT_RIGHT"
    if total_shadow_pnl < -1000:
        gate_verdict = "PROTECTING_EDGE"
    elif total_shadow_pnl > 5000:
        gate_verdict = "TOO_AGGRESSIVE"
    elif total_shadow_pnl > 1000:
        gate_verdict = "SLIGHTLY_AGGRESSIVE"

    rejection_rate = round(total_htf_rejected / total_signals * 100, 1) if total_signals > 0 else 0

    # ── Build output JSON ──
    output = {
        "diagnostic_run": {
            "period": f"{start_date} to {end_date}",
            "elapsed_seconds": round(elapsed, 1),
            "exec_bars_processed": exec_bar_index,
        },
        "total_signals": total_signals,
        "approved": approved_count,
        "rejected": total_rejected,
        "htf_rejected": total_htf_rejected,
        "rejection_rate": f"{rejection_rate}%",
        "rejection_reasons": dict(rejection_reasons),
        "shadow_pnl_of_rejected": {
            "would_have_been_winners": len(winners),
            "would_have_been_losers": len(losers),
            "hypothetical_pnl": round(total_shadow_pnl, 2),
            "hypothetical_pf": shadow_pf if shadow_pf != float("inf") else "inf",
        },
        "per_timeframe_analysis": tf_conflict_analysis,
        "gate_value_analysis": {
            "gate_improved_pf_by": gate_verdict,
            "htf_strength_gate": HTF_STRENGTH_GATE,
            "signals_worth_reconsidering": top_10,
        },
    }

    # ── Save to JSON ──
    with open(str(OUTPUT_PATH), "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nResults saved to {OUTPUT_PATH}")

    # ── Print summary table ──
    print(f"\n{'=' * 70}")
    print(f"  HTF GATE DIAGNOSTIC SUMMARY")
    print(f"  Period: {start_date} → {end_date}")
    print(f"{'=' * 70}")
    print(f"\n  Total signals evaluated:  {total_signals:,}")
    print(f"  Approved (traded):        {approved_count:,}")
    print(f"  Total rejected:           {total_rejected:,}")
    print(f"  HTF gate rejections:      {total_htf_rejected:,}")
    print(f"  HTF rejection rate:       {rejection_rate}%")

    print(f"\n  {'─' * 66}")
    print(f"  REJECTION REASONS (HTF gate)")
    print(f"  {'─' * 66}")
    print(f"  {'Reason':<30} {'Count':>8}")
    print(f"  {'─' * 66}")
    for reason, count in sorted(rejection_reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason:<30} {count:>8,}")

    print(f"\n  {'─' * 66}")
    print(f"  PER-TIMEFRAME CONFLICT ANALYSIS")
    print(f"  {'─' * 66}")
    print(f"  {'TF':<6} {'Rejections':>10} {'Winners':>8} {'Losers':>8} "
          f"{'WR':>6} {'PF':>6} {'Shadow PnL':>12}")
    print(f"  {'─' * 66}")
    for tf in htf_tfs:
        a = tf_conflict_analysis[tf]
        if a["rejection_count"] == 0:
            print(f"  {tf:<6} {'0':>10}")
            continue
        pf = a["shadow_pf"]
        pf_str = f"{pf:.2f}" if isinstance(pf, (int, float)) else pf
        print(f"  {tf:<6} {a['rejection_count']:>10,} {a['shadow_winners']:>8,} "
              f"{a['shadow_losers']:>8,} {a['shadow_win_rate']:>5.1f}% "
              f"{pf_str:>6} ${a['shadow_pnl']:>+10,.2f}")

    print(f"\n  {'─' * 66}")
    print(f"  SHADOW PnL OF HTF-REJECTED SIGNALS")
    print(f"  {'─' * 66}")
    print(f"  Would-have-been winners:  {len(winners):,}")
    print(f"  Would-have-been losers:   {len(losers):,}")
    print(f"  Hypothetical total PnL:   ${total_shadow_pnl:>+,.2f}")
    shadow_pf_str = f"{shadow_pf:.2f}" if shadow_pf != float("inf") else "inf"
    print(f"  Hypothetical PF:          {shadow_pf_str}")

    print(f"\n  {'─' * 66}")
    print(f"  GATE VALUE ASSESSMENT")
    print(f"  {'─' * 66}")
    print(f"  HTF Strength Gate:  {HTF_STRENGTH_GATE}")
    print(f"  Verdict:            {gate_verdict}")

    if gate_verdict == "PROTECTING_EDGE":
        print(f"  Recommendation:     Gate is PROTECTING the system's edge.")
        print(f"                      Rejected signals would have LOST ${abs(total_shadow_pnl):,.2f}.")
        print(f"                      Keep the gate as-is.")
    elif gate_verdict == "TOO_AGGRESSIVE":
        print(f"  Recommendation:     Gate may be TOO AGGRESSIVE.")
        print(f"                      Rejected signals would have EARNED ${total_shadow_pnl:,.2f}.")
        print(f"                      Consider loosening the strength gate (currently {HTF_STRENGTH_GATE}).")
    elif gate_verdict == "SLIGHTLY_AGGRESSIVE":
        print(f"  Recommendation:     Gate is slightly aggressive.")
        print(f"                      Rejected signals show modest edge (${total_shadow_pnl:,.2f}).")
        print(f"                      Consider targeted relaxation for specific TFs.")
    else:
        print(f"  Recommendation:     Gate is ABOUT RIGHT.")
        print(f"                      Shadow PnL near neutral. Gate is well-calibrated.")

    if top_10:
        print(f"\n  {'─' * 66}")
        print(f"  TOP 10 MOST PROFITABLE REJECTED SIGNALS")
        print(f"  {'─' * 66}")
        print(f"  {'Timestamp':<22} {'Dir':<6} {'Score':>6} {'PnL':>10} {'Conflicting TFs'}")
        print(f"  {'─' * 66}")
        for s in top_10:
            ts = s["timestamp"][:19]
            tfs = ", ".join(s["conflicting_tfs"]) if s["conflicting_tfs"] else "consensus"
            print(f"  {ts:<22} {s['direction']:<6} {s['score']:>6.3f} "
                  f"${s['total_pnl']:>+8,.2f}  {tfs}")

    print(f"\n{'=' * 70}\n")

    return output


if __name__ == "__main__":
    result = asyncio.run(run_diagnostic())
