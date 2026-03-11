"""
C2 Breakeven Optimizer — Variant Simulation
============================================
Simulates 4 BE trigger variants against the full viz_data.json trade+bar dataset.

VARIANTS:
  A: No BE      — C2 keeps initial stop throughout; ATR trail handles protection
  B: Delayed    — BE moves to entry+1 only after C2 MFE >= 1.5x stop distance
  C: Partial    — BE at midpoint (entry - stop/2), not full entry+1
  D: Current    — BE at entry+1 immediately on C1 exit (baseline)

HOW IT WORKS:
  For each trade, replays bar-by-bar:
    Phase 1: Simulate C1 Variant C trail-from-profit to find C1 exit bar
    Phase 2: From C1 exit, simulate C2 under each BE variant
             Reads bars beyond the current exit_bar to see full C2 outcome

OUTPUT:
  logs/c2_be_variant_results.json  — full per-variant stats
  logs/c2_be_variant_summary.txt   — human-readable comparison table

Usage:
    python scripts/c2_be_optimizer.py

Requirements:
    docs/viz_data.json must exist (contains trades + bars)
"""

import json
import sys
from pathlib import Path
from collections import defaultdict

# ── Paths ──
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
VIZ_DATA = REPO_ROOT / "docs" / "viz_data.json"
LOGS_DIR = REPO_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = LOGS_DIR / "c2_be_variant_results.json"
OUT_TXT = LOGS_DIR / "c2_be_variant_summary.txt"

# ── Strategy constants (must match scale_out_executor.py + settings.py) ──
C1_PROFIT_THRESH = 3.0      # pts — activate C1 trail when profit >= this
C1_TRAIL_DIST    = 2.5      # pts — C1 trail distance from HWM
C1_MAX_BARS      = 12       # bars — fallback exit if trail never activates
C2_ATR_MULT      = 2.0      # ATR multiplier for C2 trail
C2_MAX_TARGET    = 150.0    # pts — max target for C2
C2_TIME_BARS     = 120      # bars — proxy for 120-min time stop (at 2m bars)
BE_BUFFER        = 1.0      # pts — breakeven buffer (entry + 1pt)
COMMISSION       = 1.29     # $/contract
POINT_VALUE      = 2.0      # $/point (MNQ)
DEFAULT_ATR      = 7.0      # pts — fallback if atr_at_entry missing


# ═══════════════════════════════════════════════════════════
# SIMULATION ENGINE
# ═══════════════════════════════════════════════════════════

def simulate_trade(trade: dict, bars: list, variant: str) -> dict | None:
    """
    Bar-by-bar simulation of a single trade under a given BE variant.

    Returns a result dict, or None if the trade can't be simulated
    (e.g., stops in Phase 1 before C1 can exit — same for all variants).
    """
    entry_bar     = trade.get("entry_bar", 0)
    exit_bar      = trade.get("exit_bar", 0)
    direction     = trade.get("direction", "long")
    entry_price   = trade.get("entry_price", 0.0)
    stop_distance = trade.get("stop_distance", 20.0)
    atr           = trade.get("atr_at_entry") or DEFAULT_ATR

    if not entry_price or entry_bar >= len(bars):
        return None

    if direction == "long":
        initial_stop = entry_price - stop_distance
    else:
        initial_stop = entry_price + stop_distance

    trail_atr_dist = atr * C2_ATR_MULT

    # ── PHASE 1: simulate C1 (Variant C trail-from-profit) ──────────────
    c1_bars_elapsed = 0
    c1_best_price   = entry_price
    c1_trailing     = False
    c1_exit_bar     = None
    c1_exit_price   = None
    c1_exit_reason  = None

    phase1_end = min(entry_bar + C1_MAX_BARS + 5, len(bars))

    for i in range(entry_bar, phase1_end):
        bar   = bars[i]
        close = bar.get("close", 0.0)
        high  = bar.get("high", close)
        low   = bar.get("low", close)

        # Both contracts hit initial stop → full loss, same for all variants
        if direction == "long" and low <= initial_stop:
            gross = (initial_stop - entry_price) * POINT_VALUE * 2
            net   = gross - COMMISSION * 2
            return {
                "variant"       : variant,
                "phase_stopped" : "phase1",
                "c1_pnl"        : net / 2,
                "c2_pnl"        : net / 2,
                "total_pnl"     : net,
                "c2_exit_reason": "stop_phase1",
                "c1_bars"       : c1_bars_elapsed,
                "c2_bars"       : 0,
                "c2_mfe"        : 0.0,
            }
        if direction == "short" and high >= initial_stop:
            gross = (entry_price - initial_stop) * POINT_VALUE * 2
            net   = gross - COMMISSION * 2
            return {
                "variant"       : variant,
                "phase_stopped" : "phase1",
                "c1_pnl"        : net / 2,
                "c2_pnl"        : net / 2,
                "total_pnl"     : net,
                "c2_exit_reason": "stop_phase1",
                "c1_bars"       : c1_bars_elapsed,
                "c2_bars"       : 0,
                "c2_mfe"        : 0.0,
            }

        c1_bars_elapsed += 1

        # Update C1 HWM
        if direction == "long":
            c1_best_price = max(c1_best_price, high)
            unrealized    = close - entry_price
        else:
            c1_best_price = min(c1_best_price, low)
            unrealized    = entry_price - close

        # Activate trailing once profit >= threshold
        if unrealized >= C1_PROFIT_THRESH and not c1_trailing:
            c1_trailing = True

        # Check trailing stop
        if c1_trailing:
            if direction == "long":
                trail_stop = c1_best_price - C1_TRAIL_DIST
                if close <= trail_stop:
                    c1_exit_bar    = i
                    c1_exit_price  = round(trail_stop, 2)
                    c1_exit_reason = "c1_trail_from_profit"
                    break
            else:
                trail_stop = c1_best_price + C1_TRAIL_DIST
                if close >= trail_stop:
                    c1_exit_bar    = i
                    c1_exit_price  = round(trail_stop, 2)
                    c1_exit_reason = "c1_trail_from_profit"
                    break

        # Fallback: max bars
        if c1_bars_elapsed >= C1_MAX_BARS and not c1_trailing:
            if unrealized > 0:
                c1_exit_bar    = i
                c1_exit_price  = round(close, 2)
                c1_exit_reason = f"time_{C1_MAX_BARS}bars_fallback"
                break

    if c1_exit_bar is None:
        # C1 never exited (trade stopped in Phase 1 without triggering stop — unusual)
        return None

    # C1 PnL
    if direction == "long":
        c1_gross = (c1_exit_price - entry_price) * POINT_VALUE
    else:
        c1_gross = (entry_price - c1_exit_price) * POINT_VALUE
    c1_net = round(c1_gross - COMMISSION, 2)

    # ── PHASE 2: simulate C2 runner under the given BE variant ──────────
    # Initial C2 stop based on variant
    if variant == "A":
        # No BE — keep initial stop
        c2_stop      = initial_stop
        be_triggered = True   # flag not used for A
        be_threshold = None

    elif variant == "B":
        # Delayed BE — keep initial stop until C2 MFE >= 1.5 × stop_distance
        c2_stop      = initial_stop
        be_triggered = False
        be_threshold = stop_distance * 1.5

    elif variant == "C":
        # Partial BE — midpoint between initial stop and entry
        c2_stop      = round((initial_stop + entry_price) / 2.0, 2)
        be_triggered = True
        be_threshold = None

    else:  # variant == "D" (current baseline)
        if direction == "long":
            c2_stop = round(entry_price + BE_BUFFER, 2)
        else:
            c2_stop = round(entry_price - BE_BUFFER, 2)
        be_triggered = True
        be_threshold = None

    c2_best_price    = c1_exit_price
    c2_trailing_stop = 0.0
    c2_exit_price    = None
    c2_exit_reason   = None
    c2_bars          = 0
    c2_mfe           = 0.0

    # Read up to C2_TIME_BARS bars past C1 exit, or end of bars array
    c2_end = min(c1_exit_bar + C2_TIME_BARS + 1, len(bars))

    for i in range(c1_exit_bar, c2_end):
        bar   = bars[i]
        close = bar.get("close", 0.0)
        high  = bar.get("high", close)
        low   = bar.get("low", close)

        # Update best price (MFE tracker)
        if direction == "long":
            c2_best_price = max(c2_best_price, high)
            c2_mfe        = max(c2_mfe, c2_best_price - entry_price)
        else:
            c2_best_price = min(c2_best_price, low)
            c2_mfe        = max(c2_mfe, entry_price - c2_best_price)

        # Variant B: trigger BE if MFE threshold reached
        if variant == "B" and not be_triggered:
            if c2_mfe >= be_threshold:
                be_triggered = True
                if direction == "long":
                    c2_stop = round(entry_price + BE_BUFFER, 2)
                else:
                    c2_stop = round(entry_price - BE_BUFFER, 2)

        # ATR trailing stop — only ratchets in favorable direction
        new_trail = (c2_best_price - trail_atr_dist if direction == "long"
                     else c2_best_price + trail_atr_dist)
        if direction == "long" and new_trail > c2_stop:
            c2_stop          = round(new_trail, 2)
            c2_trailing_stop = c2_stop
        elif direction == "short" and new_trail < c2_stop:
            c2_stop          = round(new_trail, 2)
            c2_trailing_stop = c2_stop

        # Check stop hit
        stop_hit = (
            (direction == "long"  and low  <= c2_stop) or
            (direction == "short" and high >= c2_stop)
        )

        if stop_hit:
            c2_exit_price = c2_stop
            # Classify exit reason
            if direction == "long":
                pts_from_entry = c2_stop - entry_price
            else:
                pts_from_entry = entry_price - c2_stop

            if c2_trailing_stop > 0 and abs(pts_from_entry) > BE_BUFFER + 0.5:
                c2_exit_reason = "trailing"
            elif pts_from_entry >= -0.5:
                c2_exit_reason = "breakeven"
            else:
                c2_exit_reason = "stop"
            break

        # Check max target
        if direction == "long":
            pts = close - entry_price
        else:
            pts = entry_price - close
        if pts >= C2_MAX_TARGET:
            c2_exit_price  = close
            c2_exit_reason = "max_target"
            break

        c2_bars += 1

    if c2_exit_price is None:
        # Ran out of bars → time stop
        last_bar      = bars[min(c1_exit_bar + c2_bars, len(bars) - 1)]
        c2_exit_price = last_bar.get("close", entry_price)
        c2_exit_reason = "time_stop"

    # C2 PnL
    if direction == "long":
        c2_gross = (c2_exit_price - entry_price) * POINT_VALUE
    else:
        c2_gross = (entry_price - c2_exit_price) * POINT_VALUE
    c2_net = round(c2_gross - COMMISSION, 2)

    total_net = round(c1_net + c2_net, 2)

    return {
        "variant"       : variant,
        "phase_stopped" : "phase2",
        "c1_pnl"        : c1_net,
        "c2_pnl"        : c2_net,
        "total_pnl"     : total_net,
        "c2_exit_reason": c2_exit_reason,
        "c1_bars"       : c1_bars_elapsed,
        "c2_bars"       : c2_bars,
        "c2_mfe"        : round(c2_mfe, 2),
    }


# ═══════════════════════════════════════════════════════════
# AGGREGATION
# ═══════════════════════════════════════════════════════════

def aggregate(results: list[dict]) -> dict:
    """Compute summary stats from a list of trade result dicts."""
    if not results:
        return {}

    pnls    = [r["total_pnl"] for r in results]
    c2_pnls = [r["c2_pnl"]   for r in results]
    winners = [p for p in pnls if p > 0]
    losers  = [p for p in pnls if p < 0]

    exit_counts = defaultdict(int)
    for r in results:
        exit_counts[r["c2_exit_reason"]] += 1

    gross_wins  = sum(winners)
    gross_loss  = abs(sum(losers)) if losers else 0
    pf          = round(gross_wins / gross_loss, 3) if gross_loss > 0 else float("inf")
    win_rate    = round(len(winners) / len(results) * 100, 1)
    avg_c2      = round(sum(c2_pnls) / len(c2_pnls), 2)
    avg_mfe     = round(sum(r["c2_mfe"] for r in results) / len(results), 2)

    return {
        "trades"           : len(results),
        "win_rate"         : win_rate,
        "profit_factor"    : pf,
        "total_pnl"        : round(sum(pnls), 2),
        "c2_total_pnl"     : round(sum(c2_pnls), 2),
        "avg_c2_pnl"       : avg_c2,
        "avg_c2_mfe"       : avg_mfe,
        "c2_exit_breakdown": dict(exit_counts),
    }


# ═══════════════════════════════════════════════════════════
# REPORTING
# ═══════════════════════════════════════════════════════════

VARIANT_LABELS = {
    "A": "No BE (initial stop)",
    "B": "Delayed BE (MFE >= 1.5x stop)",
    "C": "Partial BE (midpoint)",
    "D": "Current (BE+1 on C1 exit)",
}


def build_report(stats: dict[str, dict]) -> str:
    lines = [
        "=" * 72,
        "C2 BREAKEVEN VARIANT COMPARISON",
        "=" * 72,
        "",
        f"{'Metric':<30} {'A:No BE':>10} {'B:Delayed':>10} {'C:Partial':>10} {'D:Current':>10}",
        "-" * 72,
    ]

    def row(label, key, fmt=".2f"):
        vals = [stats.get(v, {}).get(key, 0) for v in ["A", "B", "C", "D"]]
        formatted = [f"{v:{fmt}}" for v in vals]
        lines.append(f"  {label:<28} {formatted[0]:>10} {formatted[1]:>10} {formatted[2]:>10} {formatted[3]:>10}")

    row("Trades simulated",   "trades",        ".0f")
    row("Win rate (%)",        "win_rate",      ".1f")
    row("Profit factor",       "profit_factor", ".3f")
    row("Total PnL ($)",       "total_pnl",     ".2f")
    row("C2 total PnL ($)",    "c2_total_pnl",  ".2f")
    row("Avg C2 PnL/trade ($)","avg_c2_pnl",    ".2f")
    row("Avg C2 MFE (pts)",    "avg_c2_mfe",    ".2f")

    lines += ["", "C2 Exit Reason Breakdown:", "-" * 72]
    all_reasons = sorted(set(
        k for s in stats.values() for k in s.get("c2_exit_breakdown", {})
    ))
    for reason in all_reasons:
        vals = [stats.get(v, {}).get("c2_exit_breakdown", {}).get(reason, 0)
                for v in ["A", "B", "C", "D"]]
        lines.append(f"  {reason:<28} {vals[0]:>10} {vals[1]:>10} {vals[2]:>10} {vals[3]:>10}")

    lines += [
        "",
        "=" * 72,
        "RECOMMENDATION",
        "=" * 72,
    ]

    # Pick winner by highest total PnL
    best = max(["A", "B", "C", "D"],
               key=lambda v: stats.get(v, {}).get("total_pnl", float("-inf")))
    d_pnl    = stats.get("D", {}).get("total_pnl", 0)
    best_pnl = stats.get(best, {}).get("total_pnl", 0)
    delta    = best_pnl - d_pnl

    if best == "D" or delta <= 0:
        lines.append("  Current (D) is optimal. No change recommended.")
    else:
        lines.append(f"  Best variant: {best} — {VARIANT_LABELS[best]}")
        lines.append(f"  vs Current:   +${delta:,.2f} PnL improvement")
        lines.append(f"  PF:           {stats[best]['profit_factor']:.3f} vs {stats['D']['profit_factor']:.3f}")
        lines.append(f"  Win rate:     {stats[best]['win_rate']:.1f}% vs {stats['D']['win_rate']:.1f}%")

    lines.append("")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    if not VIZ_DATA.exists():
        print(f"[ERROR] viz_data.json not found at: {VIZ_DATA}")
        sys.exit(1)

    print(f"Loading {VIZ_DATA} ...")
    with open(VIZ_DATA, "r") as f:
        data = json.load(f)

    trades = data.get("trades", [])
    bars   = data.get("bars", [])

    if not trades or not bars:
        print("[ERROR] viz_data.json has no trades or bars.")
        sys.exit(1)

    print(f"Loaded {len(trades)} trades, {len(bars)} bars.")
    print("Running simulation for variants A, B, C, D ...\n")

    variant_results: dict[str, list[dict]] = {"A": [], "B": [], "C": [], "D": []}
    skipped = 0

    for idx, trade in enumerate(trades):
        # Run all 4 variants on this trade
        for v in ["A", "B", "C", "D"]:
            result = simulate_trade(trade, bars, v)
            if result is None:
                if v == "A":
                    skipped += 1
                continue
            variant_results[v].append(result)

        if (idx + 1) % 200 == 0:
            print(f"  ... {idx + 1}/{len(trades)} trades processed")

    print(f"\nSimulation complete. Skipped {skipped} trades (insufficient bar data).")

    # Aggregate stats per variant
    stats = {v: aggregate(variant_results[v]) for v in ["A", "B", "C", "D"]}

    # Build report
    report = build_report(stats)
    print(report)

    # Write JSON output
    output = {
        "variant_stats"  : stats,
        "variant_labels" : VARIANT_LABELS,
        "trades_total"   : len(trades),
        "bars_total"     : len(bars),
        "skipped"        : skipped,
        "constants"      : {
            "c1_profit_thresh_pts": C1_PROFIT_THRESH,
            "c1_trail_dist_pts"   : C1_TRAIL_DIST,
            "c1_max_bars_fallback": C1_MAX_BARS,
            "c2_atr_multiplier"   : C2_ATR_MULT,
            "c2_max_target_pts"   : C2_MAX_TARGET,
            "be_buffer_pts"       : BE_BUFFER,
            "default_atr"         : DEFAULT_ATR,
        },
    }
    with open(OUT_JSON, "w") as f:
        json.dump(output, f, indent=2)

    with open(OUT_TXT, "w") as f:
        f.write(report)

    print(f"Results saved:")
    print(f"  {OUT_JSON}")
    print(f"  {OUT_TXT}")

    # Return best variant for use by caller
    best = max(["A", "B", "C", "D"],
               key=lambda v: stats.get(v, {}).get("total_pnl", float("-inf")))
    return best, stats


if __name__ == "__main__":
    best, stats = main()
    print(f"\nBest variant: {best} — {VARIANT_LABELS[best]}")
    print(f"Total PnL: ${stats[best]['total_pnl']:,.2f}  (vs D: ${stats['D']['total_pnl']:,.2f})")
