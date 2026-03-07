#!/usr/bin/env python3
"""
Multi-Instrument Runner
========================
Launches trading pipelines for multiple CME Micro futures instruments.

On startup, prints the validation status of each configured instrument
and warns if any are unvalidated (blocked from order execution unless
ALLOW_UNVALIDATED=true is set).

Usage:
    python scripts/run_multi_instrument.py
    ALLOW_UNVALIDATED=true python scripts/run_multi_instrument.py  # paper testing override
"""

import os
import sys

# Ensure project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.instruments import INSTRUMENT_SPECS, InstrumentSpec


def print_validation_status() -> None:
    """Print validation status of all configured instruments on startup."""
    print()
    print("=" * 60)
    print("  MULTI-INSTRUMENT VALIDATION STATUS")
    print("=" * 60)

    has_unvalidated = False
    for sym in sorted(INSTRUMENT_SPECS):
        spec = INSTRUMENT_SPECS[sym]
        if spec.validated:
            print(f"  {spec.symbol:<6} VALIDATED   — cleared for trading")
        else:
            has_unvalidated = True
            print(f"  {spec.symbol:<6} UNVALIDATED — blocked from order execution")

    print()

    if has_unvalidated:
        override = os.getenv("ALLOW_UNVALIDATED", "false").lower() == "true"
        print("  " + "!" * 56)
        print("  !! WARNING: Unvalidated instruments detected.           !!")
        print("  !! These instruments have NOT been backtested with      !!")
        print("  !! 200+ trades. Orders will be BLOCKED at execution.    !!")
        if override:
            print("  !!                                                      !!")
            print("  !! ALLOW_UNVALIDATED=true is SET — override active.     !!")
            print("  !! Use for PAPER TESTING ONLY.                          !!")
        else:
            print("  !!                                                      !!")
            print("  !! Set ALLOW_UNVALIDATED=true to override (PAPER ONLY). !!")
        print("  " + "!" * 56)
        print()
    else:
        print("  All instruments validated. Ready for deployment.")
        print()

    print("=" * 60)
    print()


def main() -> None:
    print_validation_status()

    # Only proceed with validated instruments (unless override is set)
    override = os.getenv("ALLOW_UNVALIDATED", "false").lower() == "true"
    active = []
    for sym in sorted(INSTRUMENT_SPECS):
        spec = INSTRUMENT_SPECS[sym]
        if spec.validated or override:
            active.append(sym)

    if not active:
        print("No instruments available for trading. Exiting.")
        sys.exit(1)

    print(f"Active instruments: {', '.join(active)}")
    print("Multi-instrument pipeline ready.")
    # Future: launch per-instrument pipelines here


if __name__ == "__main__":
    main()
