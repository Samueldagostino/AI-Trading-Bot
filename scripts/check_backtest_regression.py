"""
Backtest Regression Check
==========================

Ensures that backtest results don't regress below acceptable thresholds.
Fails the CI pipeline if:
  - Profit factor drops below 90% of baseline
  - Max drawdown exceeds 110% of baseline
  - Win rate drops below 90% of baseline

Usage:
    python check_backtest_regression.py \\
        --baseline config/backtest_baseline.json \\
        --results /tmp/backtest_results.json

Exit Code:
  0 = All checks passed
  1 = Regression detected
"""

import json
import sys
from pathlib import Path
from argparse import ArgumentParser

def load_json(filepath):
    """Load JSON file safely."""
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"ERROR: Cannot load {filepath}: {e}")
        return None

def check_regression(baseline, results):
    """Check for performance regression."""
    print("=" * 70)
    print("BACKTEST REGRESSION CHECK")
    print("=" * 70)

    if not baseline:
        print("\nWARNING: No baseline available, skipping regression check")
        return True

    if not results:
        print("\nERROR: No results to check")
        return False

    # Define regression thresholds
    checks = [
        {
            'name': 'Profit Factor',
            'baseline_key': 'profit_factor',
            'actual_key': 'profit_factor',
            'threshold_pct': 90,  # Actual must be >= 90% of baseline
            'direction': 'higher_better',
        },
        {
            'name': 'Max Drawdown',
            'baseline_key': 'max_drawdown_pct',
            'actual_key': 'max_drawdown_pct',
            'threshold_pct': 110,  # Actual must be <= 110% of baseline
            'direction': 'lower_better',
        },
        {
            'name': 'Win Rate',
            'baseline_key': 'win_rate_pct',
            'actual_key': 'win_rate_pct',
            'threshold_pct': 90,  # Actual must be >= 90% of baseline
            'direction': 'higher_better',
        },
    ]

    all_passed = True
    baseline_pf = baseline.get('profit_factor', 1.73)
    baseline_dd = baseline.get('max_drawdown_pct', 1.4)
    baseline_wr = baseline.get('win_rate_pct', 61.9)

    print(f"\nBaseline Configuration (Config D + Variant C):")
    print(f"  Profit Factor:   {baseline_pf:.2f}")
    print(f"  Max Drawdown:    {baseline_dd:.2f}%")
    print(f"  Win Rate:        {baseline_wr:.2f}%")

    print(f"\nActual Results:")

    for check in checks:
        baseline_val = baseline.get(check['baseline_key'])
        actual_val = results.get(check['actual_key'])

        if baseline_val is None or actual_val is None:
            continue

        if check['direction'] == 'higher_better':
            # For metrics like PF and WR, higher is better
            threshold_val = baseline_val * (check['threshold_pct'] / 100.0)
            passed = actual_val >= threshold_val
            print(f"\n{check['name']}:")
            print(f"  Baseline: {baseline_val:.2f}")
            print(f"  Actual:   {actual_val:.2f}")
            print(f"  Minimum:  {threshold_val:.2f} ({check['threshold_pct']}% of baseline)")
            print(f"  Status:   {'✓ PASS' if passed else '✗ FAIL'}")
        else:
            # For metrics like drawdown, lower is better
            threshold_val = baseline_val * (check['threshold_pct'] / 100.0)
            passed = actual_val <= threshold_val
            print(f"\n{check['name']}:")
            print(f"  Baseline: {baseline_val:.2f}%")
            print(f"  Actual:   {actual_val:.2f}%")
            print(f"  Maximum:  {threshold_val:.2f}% ({check['threshold_pct']}% of baseline)")
            print(f"  Status:   {'✓ PASS' if passed else '✗ FAIL'}")

        if not passed:
            all_passed = False

    print("\n" + "=" * 70)

    if all_passed:
        print("REGRESSION CHECK: PASSED ✓")
        print("Performance within acceptable thresholds.")
        print("=" * 70)
        return True
    else:
        print("REGRESSION CHECK: FAILED ✗")
        print("Performance has degraded below acceptable thresholds.")
        print("=" * 70)
        print("\nInvestigate:")
        print("  1. Did you change any HC filter constants?")
        print("  2. Did you modify the HTF gate (STRENGTH_GATE)?")
        print("  3. Did you alter the scale-out exit logic?")
        print("  4. Are there data/environment differences?")
        return False

def main():
    parser = ArgumentParser()
    parser.add_argument('--baseline', required=True, help='Path to baseline JSON')
    parser.add_argument('--results', required=True, help='Path to results JSON')
    args = parser.parse_args()

    baseline = load_json(args.baseline)
    results = load_json(args.results)

    success = check_regression(baseline, results)
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
