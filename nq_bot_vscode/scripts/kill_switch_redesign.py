#!/usr/bin/env python3
"""
INVESTIGATION 2: Kill Switch Redesign Analysis
===============================================
Analyzes current 5-consecutive-loss kill switch impact and simulates
drawdown velocity circuit breaker alternatives.

Output: logs/kill_switch_redesign.json
"""

import json
import statistics
from pathlib import Path
from datetime import datetime
from collections import defaultdict

LOGS = Path(__file__).resolve().parent.parent / "logs"
CHECKPOINT = LOGS / "backtest_checkpoint.json"
TRADES_FILE = LOGS / "backtest_trades_partial.json"
OUTPUT = LOGS / "kill_switch_redesign.json"


def load_data():
    with open(CHECKPOINT) as f:
        checkpoint = json.load(f)

    with open(TRADES_FILE) as f:
        trade_log = json.load(f)

    return checkpoint["executor_trade_history"], trade_log["trades"]


def build_trade_sequence(exec_history, trade_log):
    """Build chronological sequence of trades with PnL and bar indices."""
    # Get bar indices from trade_log exits
    bar_map = {}
    for t in trade_log:
        if t.get("action") == "exit":
            bar_map[t["trade_id"]] = {
                "bar_index": t["bar_index"],
                "adjusted_pnl": t["adjusted_pnl"],
                "timestamp": t["timestamp"],
            }

    # Build sequence from executor history (has richer data)
    sequence = []
    for t in exec_history:
        tid = t["trade_id"]
        pnl = t["total_net_pnl"]
        bar_info = bar_map.get(tid, {})

        sequence.append({
            "trade_id": tid,
            "bar_index": bar_info.get("bar_index", 0),
            "timestamp": bar_info.get("timestamp", t.get("closed_at", "")),
            "pnl": pnl,
            "is_win": pnl > 0,
            "direction": t["direction"],
        })

    # Sort by bar_index to get chronological order
    sequence.sort(key=lambda x: x["bar_index"])
    return sequence


def analyze_current_kill_switch(sequence, max_consec=5):
    """Simulate the current 5-consecutive-loss kill switch."""
    activations = []
    consec_losses = 0
    is_paused = False
    total_missed = 0
    total_missed_pnl = 0.0

    # The current kill switch halts permanently until manual reset.
    # In backtesting, we assume it resets after a cooldown (simulated as
    # the next session or after N trades would have passed).
    # For analysis: we count activations and compute shadow PnL of next
    # 10 trades that would have been taken.
    SHADOW_WINDOW = 10  # trades missed during each activation

    i = 0
    while i < len(sequence):
        t = sequence[i]

        if is_paused:
            # Count missed trades in shadow window
            i += 1
            continue

        if t["pnl"] < 0:
            consec_losses += 1
        else:
            consec_losses = 0

        if consec_losses >= max_consec:
            # Kill switch trips
            shadow_trades = sequence[i + 1: i + 1 + SHADOW_WINDOW]
            shadow_pnl = sum(st["pnl"] for st in shadow_trades)
            shadow_wins = sum(1 for st in shadow_trades if st["pnl"] > 0)

            activations.append({
                "activation_number": len(activations) + 1,
                "triggered_at_trade": i + 1,
                "bar_index": t["bar_index"],
                "timestamp": t["timestamp"],
                "consecutive_losses_pnl": sum(
                    sequence[j]["pnl"] for j in range(max(0, i - max_consec + 1), i + 1)
                ),
                "shadow_window_trades": len(shadow_trades),
                "shadow_window_wins": shadow_wins,
                "shadow_window_pnl": round(shadow_pnl, 2),
                "shadow_window_would_have_recovered": shadow_pnl > 0,
            })

            total_missed += len(shadow_trades)
            total_missed_pnl += shadow_pnl

            # Reset after shadow window
            consec_losses = 0
            i += 1 + SHADOW_WINDOW
            continue

        i += 1

    return {
        "total_activations": len(activations),
        "total_trades_missed": total_missed,
        "total_missed_pnl": round(total_missed_pnl, 2),
        "avg_shadow_pnl_per_activation": round(
            total_missed_pnl / len(activations), 2
        ) if activations else 0,
        "pct_activations_where_recovery_missed": round(
            sum(1 for a in activations if a["shadow_window_would_have_recovered"])
            / len(activations) * 100, 1
        ) if activations else 0,
        "activations": activations,
    }


def compute_equity_curve(sequence):
    """Compute running equity from trade sequence."""
    equity = []
    cumulative = 0.0
    for t in sequence:
        cumulative += t["pnl"]
        equity.append({
            "bar_index": t["bar_index"],
            "pnl": t["pnl"],
            "cumulative": cumulative,
        })
    return equity


def simulate_drawdown_velocity_breaker(sequence, params):
    """
    Drawdown velocity circuit breaker:
    If equity drops > pct_threshold within lookback_trades, pause for pause_trades.
    """
    pct_threshold = params["pct_threshold"]
    lookback_trades = params["lookback_trades"]
    pause_trades = params["pause_trades"]
    account_size = 50000.0  # Starting account

    activations = []
    paused_until_idx = -1
    equity = 0.0
    equity_history = []
    total_pnl = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    trades_taken = 0
    peak_equity = 0.0
    max_dd = 0.0

    for i, t in enumerate(sequence):
        equity_history.append(equity)

        # Check if paused
        if i < paused_until_idx:
            continue

        # Take the trade
        equity += t["pnl"]
        total_pnl += t["pnl"]
        trades_taken += 1

        if t["pnl"] > 0:
            gross_profit += t["pnl"]
        else:
            gross_loss += abs(t["pnl"])

        if equity > peak_equity:
            peak_equity = equity

        dd = (peak_equity - equity) / (account_size + peak_equity) * 100 if (account_size + peak_equity) > 0 else 0
        if dd > max_dd:
            max_dd = dd

        # Check drawdown velocity: how much equity dropped in last N trades
        if len(equity_history) >= lookback_trades:
            lookback_equity = equity_history[-lookback_trades]
            drop = lookback_equity - equity  # positive means equity dropped
            drop_pct = drop / (account_size + max(0, lookback_equity)) * 100

            if drop_pct > pct_threshold:
                # Trip the breaker
                shadow_trades = sequence[i + 1: i + 1 + pause_trades]
                shadow_pnl = sum(st["pnl"] for st in shadow_trades)

                activations.append({
                    "activation_number": len(activations) + 1,
                    "triggered_at_trade": i + 1,
                    "bar_index": t["bar_index"],
                    "equity_at_trip": round(equity, 2),
                    "lookback_equity": round(lookback_equity, 2),
                    "drop_pct": round(drop_pct, 3),
                    "shadow_pnl": round(shadow_pnl, 2),
                })

                paused_until_idx = i + 1 + pause_trades

    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    return {
        "params": params,
        "total_pnl": round(total_pnl, 2),
        "trades_taken": trades_taken,
        "trades_skipped": len(sequence) - trades_taken,
        "profit_factor": round(pf, 3),
        "max_drawdown_pct": round(max_dd, 3),
        "activations": len(activations),
        "total_shadow_pnl": round(sum(a["shadow_pnl"] for a in activations), 2),
        "activation_details": activations[:10],  # First 10 for reference
    }


def simulate_current_kill_switch_as_variant(sequence, max_consec=5, pause_trades=10):
    """Simulate the current kill switch with same metrics as velocity breaker for comparison."""
    activations_count = 0
    paused_until_idx = -1
    consec_losses = 0
    equity = 0.0
    total_pnl = 0.0
    gross_profit = 0.0
    gross_loss = 0.0
    trades_taken = 0
    peak_equity = 0.0
    max_dd = 0.0
    account_size = 50000.0

    for i, t in enumerate(sequence):
        if i < paused_until_idx:
            continue

        # Take the trade
        equity += t["pnl"]
        total_pnl += t["pnl"]
        trades_taken += 1

        if t["pnl"] > 0:
            gross_profit += t["pnl"]
        else:
            gross_loss += abs(t["pnl"])

        if equity > peak_equity:
            peak_equity = equity

        dd = (peak_equity - equity) / (account_size + peak_equity) * 100 if (account_size + peak_equity) > 0 else 0
        if dd > max_dd:
            max_dd = dd

        if t["pnl"] < 0:
            consec_losses += 1
        else:
            consec_losses = 0

        if consec_losses >= max_consec:
            activations_count += 1
            consec_losses = 0
            paused_until_idx = i + 1 + pause_trades

    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    return {
        "params": {"type": "consecutive_losses", "max_consecutive": max_consec, "pause_trades": pause_trades},
        "total_pnl": round(total_pnl, 2),
        "trades_taken": trades_taken,
        "trades_skipped": len(sequence) - trades_taken,
        "profit_factor": round(pf, 3),
        "max_drawdown_pct": round(max_dd, 3),
        "activations": activations_count,
    }


def main():
    print("Loading trade data...")
    exec_history, trade_log = load_data()
    sequence = build_trade_sequence(exec_history, trade_log)
    print(f"Built trade sequence: {len(sequence)} trades")

    # Basic stats
    wins = sum(1 for t in sequence if t["pnl"] > 0)
    total = len(sequence)
    print(f"Win rate: {wins}/{total} = {wins/total*100:.1f}%")

    # Count longest losing streaks
    streaks = []
    current_streak = 0
    for t in sequence:
        if t["pnl"] < 0:
            current_streak += 1
        else:
            if current_streak > 0:
                streaks.append(current_streak)
            current_streak = 0
    if current_streak > 0:
        streaks.append(current_streak)

    print(f"Losing streaks ≥5: {sum(1 for s in streaks if s >= 5)}")
    print(f"Losing streaks ≥7: {sum(1 for s in streaks if s >= 7)}")
    print(f"Longest losing streak: {max(streaks) if streaks else 0}")

    # ── PART 1: Current kill switch analysis ──
    print("\n" + "=" * 60)
    print("CURRENT KILL SWITCH ANALYSIS (5 consecutive losses)")
    print("=" * 60)

    ks_analysis = analyze_current_kill_switch(sequence, max_consec=5)

    print(f"  Total activations:     {ks_analysis['total_activations']}")
    print(f"  Total trades missed:   {ks_analysis['total_trades_missed']}")
    print(f"  Total missed PnL:      ${ks_analysis['total_missed_pnl']:,.2f}")
    print(f"  Avg shadow PnL/trip:   ${ks_analysis['avg_shadow_pnl_per_activation']:,.2f}")
    print(f"  Recovery missed:       {ks_analysis['pct_activations_where_recovery_missed']:.1f}%")

    if ks_analysis["activations"]:
        print(f"\n  First 5 activations:")
        for a in ks_analysis["activations"][:5]:
            print(f"    #{a['activation_number']}: trade {a['triggered_at_trade']}, "
                  f"shadow PnL=${a['shadow_window_pnl']:+,.2f}, "
                  f"recovery={'YES' if a['shadow_window_would_have_recovered'] else 'NO'}")

    # ── PART 2: Drawdown velocity circuit breaker variants ──
    print("\n" + "=" * 60)
    print("DRAWDOWN VELOCITY CIRCUIT BREAKER SIMULATION")
    print("=" * 60)

    breaker_params = [
        {
            "label": "Conservative",
            "pct_threshold": 1.0,    # 1% equity drop
            "lookback_trades": 10,   # within 10 trades
            "pause_trades": 15,      # pause for 15 trades
        },
        {
            "label": "Moderate",
            "pct_threshold": 0.75,   # 0.75% equity drop
            "lookback_trades": 8,    # within 8 trades
            "pause_trades": 10,      # pause for 10 trades
        },
        {
            "label": "Aggressive",
            "pct_threshold": 0.5,    # 0.5% equity drop
            "lookback_trades": 6,    # within 6 trades
            "pause_trades": 8,       # pause for 8 trades
        },
    ]

    # Simulate current kill switch for apples-to-apples comparison
    current_ks = simulate_current_kill_switch_as_variant(sequence, max_consec=5, pause_trades=10)

    breaker_results = {}
    for params in breaker_params:
        label = params.pop("label")
        result = simulate_drawdown_velocity_breaker(sequence, params)
        params["label"] = label
        breaker_results[label] = result

    # Comparison table
    print(f"\n  {'Variant':<25s}  {'PnL':>10s}  {'PF':>6s}  {'MaxDD':>7s}  {'Trips':>6s}  {'Skipped':>8s}")
    print("  " + "-" * 66)

    # Current baseline
    print(f"  {'Current (5-loss halt)':<25s}  ${current_ks['total_pnl']:>9,.2f}  "
          f"{current_ks['profit_factor']:>5.3f}  {current_ks['max_drawdown_pct']:>6.2f}%  "
          f"{current_ks['activations']:>5d}  {current_ks['trades_skipped']:>7d}")

    for label, r in breaker_results.items():
        print(f"  {label:<25s}  ${r['total_pnl']:>9,.2f}  "
              f"{r['profit_factor']:>5.3f}  {r['max_drawdown_pct']:>6.2f}%  "
              f"{r['activations']:>5d}  {r['trades_skipped']:>7d}")

    # No-kill-switch baseline
    no_ks_pnl = sum(t["pnl"] for t in sequence)
    no_ks_gp = sum(t["pnl"] for t in sequence if t["pnl"] > 0)
    no_ks_gl = sum(abs(t["pnl"]) for t in sequence if t["pnl"] < 0)
    no_ks_pf = no_ks_gp / no_ks_gl if no_ks_gl > 0 else float("inf")
    print(f"  {'NO kill switch':<25s}  ${no_ks_pnl:>9,.2f}  "
          f"{no_ks_pf:>5.3f}  {'N/A':>7s}  {'0':>5s}  {'0':>7s}")

    # Write results
    output = {
        "investigation": "Kill Switch Redesign",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "data_source": "backtest_checkpoint.json + backtest_trades_partial.json",
        "trades_in_sequence": len(sequence),
        "win_rate": round(wins / total * 100, 1),
        "losing_streak_stats": {
            "total_streaks_gte_5": sum(1 for s in streaks if s >= 5),
            "total_streaks_gte_7": sum(1 for s in streaks if s >= 7),
            "longest_streak": max(streaks) if streaks else 0,
            "all_streaks_gte_5": [s for s in streaks if s >= 5],
        },
        "current_kill_switch": {
            "type": "consecutive_losses",
            "threshold": 5,
            "analysis": ks_analysis,
        },
        "current_kill_switch_simulation": current_ks,
        "no_kill_switch_baseline": {
            "total_pnl": round(no_ks_pnl, 2),
            "profit_factor": round(no_ks_pf, 3),
            "trades": len(sequence),
        },
        "drawdown_velocity_variants": {
            label: {
                "params": params,
                "results": breaker_results[label],
            }
            for label, params in zip(
                ["Conservative", "Moderate", "Aggressive"],
                breaker_params
            )
        },
        "comparison_summary": {
            "current_ks_pnl": current_ks["total_pnl"],
            "best_variant": max(breaker_results.items(), key=lambda x: x[1]["total_pnl"])[0],
            "best_variant_pnl": max(r["total_pnl"] for r in breaker_results.values()),
            "no_ks_pnl": round(no_ks_pnl, 2),
        },
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Results written to: {OUTPUT}")


if __name__ == "__main__":
    main()
