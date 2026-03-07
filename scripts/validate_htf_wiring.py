#!/usr/bin/env python3
"""
50K-bar validation: confirms HTF wiring in full_backtest pipeline.

Prints diagnostic at bar 1000, 5000, 10000, 25000, 50000:
  - HTF bars completed per TF
  - HTF bias state (direction, strength, allows_long/short)
  - Aggregator htf_blocked count
  - Whether "HTF FAIL-SAFE" warnings have stopped
"""

import asyncio
import sys
import logging
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent / "nq_bot_vscode"
sys.path.insert(0, str(PROJECT_DIR))

# Count HTF FAIL-SAFE warnings
htf_failsafe_count = 0
original_warning = None

def counting_warning(self, msg, *args, **kwargs):
    global htf_failsafe_count
    formatted = msg % args if args else msg
    if "HTF FAIL-SAFE" in str(formatted):
        htf_failsafe_count += 1
    original_warning(self, msg, *args, **kwargs)

# Patch logger to count warnings
import logging as _logging
original_warning = _logging.Logger.warning
_logging.Logger.warning = counting_warning

_logging.basicConfig(level=_logging.WARNING, format="%(name)s %(levelname)s %(message)s")

from scripts.full_backtest import (
    load_1min_csv, aggregate_to_2m, aggregate_1m_to_htf,
    HTFScheduler, CausalReplayEngine,
)
from config.settings import BotConfig

DATA_PATH = str(PROJECT_DIR / "data" / "historical" / "combined_1min.csv")
CHECKPOINTS = [1000, 5000, 10000, 25000, 50000]


async def main():
    global htf_failsafe_count

    print("=" * 72)
    print("  HTF WIRING VALIDATION — 50K Bars")
    print("=" * 72)
    print()

    # Load enough 1m bars to produce ~50K 2m bars (need ~100K 1m bars)
    print("Loading 1m data...")
    bars_1m = load_1min_csv(DATA_PATH)[:110_000]
    print(f"  Loaded: {len(bars_1m):,} bars")
    print()

    print("Aggregating to 2m execution bars...")
    bars_2m = aggregate_to_2m(bars_1m)
    print(f"  Result: {len(bars_2m):,} bars")
    print()

    print("Building HTF bars from 1m data...")
    htf_data = aggregate_1m_to_htf(bars_1m)
    print()

    del bars_1m  # free memory

    config = BotConfig()
    engine = CausalReplayEngine(config)
    scheduler = HTFScheduler(htf_data)

    max_bars = min(50_000, len(bars_2m))
    print(f"Processing {max_bars:,} 2m bars...\n")

    last_failsafe = 0
    results = []

    for i in range(max_bars):
        bar = bars_2m[i]
        await engine.process_bar(bar, scheduler)

        bar_num = i + 1
        if bar_num in CHECKPOINTS:
            bias = engine._htf_bias
            htf_completed = dict(engine._htf_bars_completed)
            htf_blocked = engine.signal_aggregator._htf_blocked_count
            failsafe_delta = htf_failsafe_count - last_failsafe
            last_failsafe = htf_failsafe_count

            bias_str = "None" if bias is None else (
                f"{bias.consensus_direction}({bias.consensus_strength:.2f})"
            )
            allows_L = getattr(bias, 'htf_allows_long', 'N/A')
            allows_S = getattr(bias, 'htf_allows_short', 'N/A')

            result = {
                "bar": bar_num,
                "ts": bar["timestamp"],
                "htf_bias": bias_str,
                "allows_long": allows_L,
                "allows_short": allows_S,
                "htf_completed": htf_completed,
                "htf_blocked_agg": htf_blocked,
                "failsafe_warnings_total": htf_failsafe_count,
                "failsafe_warnings_delta": failsafe_delta,
                "trades": engine._entry_count,
                "signals_with_dir": engine._signals_with_direction,
            }
            results.append(result)

            print(f"  ── Bar {bar_num:>6,} | {bar['timestamp']} ──")
            print(f"    HTF bias:           {bias_str}")
            print(f"    allows_long:        {allows_L}")
            print(f"    allows_short:       {allows_S}")
            print(f"    HTF bars completed: 1H={htf_completed.get('1H',0)} "
                  f"4H={htf_completed.get('4H',0)} "
                  f"1D={htf_completed.get('1D',0)}")
            print(f"    HTF blocked (agg):  {htf_blocked}")
            print(f"    FAIL-SAFE warnings: {htf_failsafe_count} total "
                  f"(+{failsafe_delta} since last)")
            print(f"    Trades:             {engine._entry_count}")
            print(f"    Signals w/ dir:     {engine._signals_with_direction}")
            print()

    # Final summary
    print("=" * 72)
    print("  VALIDATION SUMMARY")
    print("=" * 72)

    # Check pass criteria
    warmup_cutoff = 2000  # ~2000 bars for 1H warmup
    failsafe_after_warmup = 0
    # Count fail-safe warnings that occurred after bar 2000
    # We approximate: if total at bar 5000 minus total at bar 1000 > 0, that's bad
    pass_failsafe = True
    if len(results) >= 2:
        # Fail-safe warnings should stop after warmup
        # At bar 5000, there should be 0 new failsafe warnings
        for r in results:
            if r["bar"] >= 5000 and r["failsafe_warnings_delta"] > 0:
                pass_failsafe = False
                break

    pass_htf_gate = False
    for r in results:
        if r["bar"] >= 50000 and r["signals_with_dir"] > 0:
            pass_htf_gate = True

    pass_trades = engine._entry_count > 0

    bias_is_set = engine._htf_bias is not None

    print(f"\n  htf_bias set after warmup:     {'PASS' if bias_is_set else 'FAIL'}")
    print(f"  FAIL-SAFE warnings stop:       {'PASS' if pass_failsafe else 'FAIL'} "
          f"(total: {htf_failsafe_count})")
    print(f"  Signals pass HTF gate:         {'PASS' if pass_htf_gate else 'FAIL'} "
          f"({engine._signals_with_direction} signals with direction)")
    print(f"  Trades produced:               {'PASS' if pass_trades else 'FAIL'} "
          f"({engine._entry_count} trades)")
    print()

    all_pass = bias_is_set and pass_failsafe and pass_htf_gate and pass_trades
    if all_pass:
        print("  *** ALL CHECKS PASS — HTF WIRING IS CORRECT ***")
    else:
        print("  *** SOME CHECKS FAILED — SEE ABOVE ***")

    print()
    return all_pass


if __name__ == "__main__":
    result = asyncio.run(main())
    sys.exit(0 if result else 1)
