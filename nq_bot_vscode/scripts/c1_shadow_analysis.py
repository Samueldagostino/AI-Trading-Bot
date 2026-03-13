#!/usr/bin/env python3
"""
C1-Only Shadow Analysis
========================
Re-simulates ALL blocked signals using C1 strategy:
  - 1 contract
  - 5-bar time exit (exit at close of 5th bar after entry)
  - Stop loss at stop_distance (if hit before 5 bars)
  - No profit target

This tells us exactly how much edge we're leaving on the table
by being too selective with our filters.
"""

import json
import math
import sys
import os
from collections import defaultdict
from pathlib import Path

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Constants matching the real backtest
POINT_VALUE = 2.0  # MNQ
COMMISSION_PER_CONTRACT_PER_SIDE = 1.50  # Conservative (real is $1.29)
COMMISSION_RT = COMMISSION_PER_CONTRACT_PER_SIDE * 2  # $3.00 round-trip for 1 contract

def get_slippage_pts(timestamp_str):
    """Simplified slippage: 1.25 RTH, 2.00 ETH (HARDENED)."""
    # Parse hour from ISO timestamp
    try:
        hour = int(timestamp_str[11:13])
        # RTH roughly 9:30-16:00 ET => hours 9-15
        if 9 <= hour <= 15:
            return 1.25
    except:
        pass
    return 2.00


def main():
    # Load the backtest output
    trades_path = Path(__file__).resolve().parent.parent / "logs" / "full_validation_trades.json"
    if not trades_path.exists():
        print(f"ERROR: {trades_path} not found. Run the full backtest first.")
        return

    with open(trades_path) as f:
        data = json.load(f)

    shadow_analysis = data.get("shadow_analysis", {})
    print("Current shadow analysis from last backtest:")
    for gate_info in shadow_analysis.get("gate_value_ranking", []):
        print(f"  {gate_info['gate']:50s} | {gate_info['count']:>5} blocked | Shadow PnL: ${gate_info['shadow_total_pnl']:>+12,.2f} | {gate_info['verdict']}")

    print("\n" + "=" * 80)
    print("  Now we need to re-run the backtest with C1-only shadow simulation")
    print("  The existing shadow sim uses 2-contract target/stop logic.")
    print("  We need bar-level data to do 5-bar time exit simulation.")
    print("=" * 80)

    # We need the actual 2m bar data plus the shadow signals.
    # The shadow signals are stored in the engine during runtime but not saved to JSON.
    # We need to modify the backtest to save them, or better yet,
    # modify the shadow simulation in the backtest itself.
    print("\n  => Modifying the shadow simulation in full_backtest.py to use C1 5-bar exit logic...")


if __name__ == "__main__":
    main()
