#!/usr/bin/env python3
"""
Institutional Modifier Comparison -- Baseline vs Modified
==========================================================
Runs the replay simulator twice:
  1. Baseline: modifiers DISABLED
  2. Modified: modifiers ENABLED

Outputs results to files and prints comparison table.
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

# Import after path setup
from scripts.replay_simulator import ReplaySimulator


async def run_comparison():
    results_dir = PROJECT_DIR / "logs"
    results_dir.mkdir(parents=True, exist_ok=True)

    # ── Run 1: BASELINE (modifiers disabled) ──
    print("=" * 70)
    print("  BASELINE RUN -- Institutional Modifiers DISABLED")
    print("=" * 70)

    sim_baseline = ReplaySimulator(
        speed="max",
        start_date="2025-09-01",
        end_date="2026-03-01",
        validate=False,
        quiet=True,
        modifiers_enabled=False,
    )
    baseline_results = await sim_baseline.run()

    # Extract key metrics
    baseline_metrics = extract_metrics(sim_baseline, "baseline")

    # Save to file
    with open(results_dir / "backtest_baseline_no_modifiers.json", "w") as f:
        json.dump(baseline_metrics, f, indent=2, default=str)
    print(f"  Baseline results saved to logs/backtest_baseline_no_modifiers.json")

    # ── Run 2: MODIFIED (modifiers enabled) ──
    print()
    print("=" * 70)
    print("  MODIFIED RUN -- Institutional Modifiers ENABLED")
    print("=" * 70)

    sim_modified = ReplaySimulator(
        speed="max",
        start_date="2025-09-01",
        end_date="2026-03-01",
        validate=False,
        quiet=True,
        modifiers_enabled=True,
    )
    modified_results = await sim_modified.run()

    # Extract key metrics
    modified_metrics = extract_metrics(sim_modified, "modified")

    # Save to file
    with open(results_dir / "backtest_modified_with_modifiers.json", "w") as f:
        json.dump(modified_metrics, f, indent=2, default=str)
    print(f"  Modified results saved to logs/backtest_modified_with_modifiers.json")

    # ── Print comparison table ──
    print()
    print_comparison(baseline_metrics, modified_metrics)

    # ── Save comparison to file ──
    comparison = {
        "baseline": baseline_metrics,
        "modified": modified_metrics,
        "timestamp": datetime.now(ZoneInfo("America/New_York")).isoformat(),
    }
    with open(results_dir / "modifier_comparison_results.json", "w") as f:
        json.dump(comparison, f, indent=2, default=str)
    print(f"\n  Full comparison saved to logs/modifier_comparison_results.json")


def extract_metrics(sim, label: str) -> dict:
    """Extract key metrics from a completed ReplaySimulator."""
    state = sim.state

    total_trades = state.total_trades
    wins = state.total_wins
    losses = state.total_losses
    total_pnl = state.total_pnl
    c1_pnl = state.c1_pnl
    c2_pnl = state.c2_pnl
    win_rate = state.win_rate
    profit_factor = state.profit_factor
    max_dd_pct = state.max_drawdown_pct
    expectancy = state.expectancy

    # Compute Sharpe approximation from trade log PnLs
    sharpe = 0
    trade_pnls = [t.get("total_pnl", 0) for t in state.trades_log
                  if "total_pnl" in t]
    if len(trade_pnls) > 1:
        import numpy as np
        pnls = np.array(trade_pnls)
        if pnls.std() > 0:
            sharpe = round(pnls.mean() / pnls.std() * (252 ** 0.5), 2)

    return {
        "label": label,
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "total_pnl": round(total_pnl, 2),
        "c1_pnl": round(c1_pnl, 2),
        "c2_pnl": round(c2_pnl, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "sharpe_approx": sharpe,
        "expectancy": round(expectancy, 2),
    }


def print_comparison(baseline: dict, modified: dict):
    """Print formatted comparison table."""
    print("=" * 70)
    print("  COMPARISON: BASELINE vs INSTITUTIONAL MODIFIERS")
    print("=" * 70)

    header = f"{'Metric':<25} {'Baseline':>15} {'Modified':>15} {'Delta':>12}"
    print(header)
    print("-" * 70)

    metrics = [
        ("Total Trades", "total_trades"),
        ("Win Rate (%)", "win_rate"),
        ("Profit Factor", "profit_factor"),
        ("Total PnL ($)", "total_pnl"),
        ("C1 PnL ($)", "c1_pnl"),
        ("C2 PnL ($)", "c2_pnl"),
        ("Max Drawdown (%)", "max_drawdown_pct"),
        ("Sharpe (approx)", "sharpe_approx"),
        ("Expectancy ($)", "expectancy"),
    ]

    for label, key in metrics:
        b_val = baseline.get(key, "n/a")
        m_val = modified.get(key, "n/a")

        if isinstance(b_val, (int, float)) and isinstance(m_val, (int, float)):
            delta = m_val - b_val
            if isinstance(b_val, int) and isinstance(m_val, int):
                print(f"  {label:<23} {b_val:>15,} {m_val:>15,} {delta:>+12,}")
            else:
                print(f"  {label:<23} {b_val:>15,.2f} {m_val:>15,.2f} {delta:>+12,.2f}")
        else:
            print(f"  {label:<23} {str(b_val):>15} {str(m_val):>15} {'n/a':>12}")

    print("=" * 70)

    # PASS/FAIL assessment
    print()
    print("  ASSESSMENT:")

    # Check if modifiers didn't significantly degrade performance
    b_pnl = baseline.get("total_pnl", 0)
    m_pnl = modified.get("total_pnl", 0)
    b_pf = baseline.get("profit_factor", 0)
    m_pf = modified.get("profit_factor", 0)

    if isinstance(b_pf, str):
        b_pf = 0
    if isinstance(m_pf, str):
        m_pf = 0

    pf_status = "PASS" if m_pf >= 1.3 else "FAIL"
    pnl_status = "PASS" if m_pnl > 0 else "FAIL"
    dd_status = "PASS"
    b_dd = baseline.get("max_drawdown_pct", 0)
    m_dd = modified.get("max_drawdown_pct", 0)
    if isinstance(m_dd, (int, float)) and m_dd > 3.0:
        dd_status = "FAIL"

    print(f"    Profit Factor >= 1.3:    [{pf_status}] (modified PF = {m_pf})")
    print(f"    Positive PnL:            [{pnl_status}] (modified PnL = ${m_pnl:,.2f})")
    print(f"    Max DD <= 3.0%:          [{dd_status}] (modified DD = {m_dd}%)")
    print()


if __name__ == "__main__":
    asyncio.run(run_comparison())
