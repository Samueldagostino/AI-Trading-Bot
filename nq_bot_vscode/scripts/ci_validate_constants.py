"""
CI Validation: HC Filter Constants & HTF Gate Assertion
=========================================================

Validates that critical trading constants haven't drifted from
their validated values. These constants define the bot's core
risk and signal quality gates.

Expected Constants:
  HIGH_CONVICTION_MIN_SCORE = 0.75     (signal strength threshold)
  HIGH_CONVICTION_MAX_STOP_PTS = 30.0  (max stop distance in points)
  _EXPECTED_HTF_GATE = 0.3             (HTF bias strength gate)

These were validated in Feb 2026 backtest and calibrated against
6 months of OOS data (Sep 2025 - Feb 2026, FirstRate 1m).

Exit with non-zero if any drift detected to prevent silent
performance degradation.

Usage:
    python scripts/ci_validate_constants.py
"""

import sys
import re
from pathlib import Path

# Get repo root
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
MAIN_PY = REPO_ROOT / "main.py"
FEATURES_HTF = REPO_ROOT / "features" / "htf_engine.py"

def read_main_py():
    """Read main.py and extract HC filter constants."""
    if not MAIN_PY.exists():
        print(f"ERROR: {MAIN_PY} not found")
        return None

    with open(MAIN_PY, 'r') as f:
        content = f.read()

    return content

def extract_constants(content):
    """Parse main.py for constant definitions."""
    constants = {}

    # Extract HIGH_CONVICTION_MIN_SCORE
    match = re.search(r'HIGH_CONVICTION_MIN_SCORE\s*=\s*([\d.]+)', content)
    if match:
        constants['HIGH_CONVICTION_MIN_SCORE'] = float(match.group(1))

    # Extract HIGH_CONVICTION_MAX_STOP_PTS
    match = re.search(r'HIGH_CONVICTION_MAX_STOP_PTS\s*=\s*([\d.]+)', content)
    if match:
        constants['HIGH_CONVICTION_MAX_STOP_PTS'] = float(match.group(1))

    # Extract _EXPECTED_HTF_GATE
    match = re.search(r'_EXPECTED_HTF_GATE\s*=\s*([\d.]+)', content)
    if match:
        constants['_EXPECTED_HTF_GATE'] = float(match.group(1))

    return constants

def validate_constants():
    """Validate constants against expected values."""
    expected = {
        'HIGH_CONVICTION_MIN_SCORE': 0.75,
        'HIGH_CONVICTION_MAX_STOP_PTS': 30.0,
        '_EXPECTED_HTF_GATE': 0.3,
    }

    content = read_main_py()
    if content is None:
        return False

    found = extract_constants(content)

    print("=" * 70)
    print("HC FILTER CONSTANTS VALIDATION")
    print("=" * 70)

    all_valid = True
    for const_name, expected_value in expected.items():
        if const_name in found:
            actual_value = found[const_name]
            status = "✓ OK" if actual_value == expected_value else "✗ DRIFT"
            if actual_value != expected_value:
                all_valid = False
                print(f"\n{const_name}")
                print(f"  Expected: {expected_value}")
                print(f"  Found:    {actual_value}")
                print(f"  Status:   {status} ← VALIDATION FAILED")
            else:
                print(f"\n{const_name}")
                print(f"  Value:  {actual_value}")
                print(f"  Status: {status}")
        else:
            all_valid = False
            print(f"\n{const_name}")
            print(f"  Status: ✗ NOT FOUND")

    print("\n" + "=" * 70)

    if all_valid:
        print("RESULT: All constants validated ✓")
        print("=" * 70)
        return True
    else:
        print("RESULT: Constant drift detected ✗")
        print("=" * 70)
        print("\nCAUTION: Do NOT change constants without full backtest validation")
        print("  - HIGH_CONVICTION_MIN_SCORE drift: silently reduces win rate")
        print("  - HIGH_CONVICTION_MAX_STOP_PTS drift: increases tail risk per trade")
        print("  - HTF gate drift: degrades profit factor (1.29 → 0.79)")
        return False

if __name__ == "__main__":
    success = validate_constants()
    sys.exit(0 if success else 1)
