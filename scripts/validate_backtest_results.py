"""
Backtest Results Validator
===========================

Compares actual backtest results against baseline metrics.
Reports on profit factor, win rate, max drawdown, and trade count.

Usage:
    python validate_backtest_results.py \\
        --baseline config/backtest_baseline.json \\
        --results /tmp/backtest_results.json
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

def validate_results(baseline, results):
    """Compare results against baseline."""
    print("=" * 70)
    print("BACKTEST VALIDATION REPORT")
    print("=" * 70)

    if not baseline:
        print("\nWARNING: No baseline available, skipping validation")
        return True

    if not results:
        print("\nERROR: No results to validate")
        return False

    # Key metrics to validate
    metrics = [
        ('profit_factor', 'Profit Factor', 'min', 1.5),  # Should not drop below 1.5
        ('win_rate_pct', 'Win Rate %', 'min', 55.0),
        ('max_drawdown_pct', 'Max Drawdown %', 'max', 2.0),
        ('trades_per_month', 'Trades/Month', 'min', 200),
    ]

    all_valid = True
    baseline_pf = baseline.get('profit_factor', 1.73)
    baseline_dd = baseline.get('max_drawdown_pct', 1.4)
    baseline_wr = baseline.get('win_rate_pct', 61.9)

    print(f"\nBaseline (Config D + Variant C, 6-month OOS):")
    print(f"  Profit Factor:   {baseline_pf}")
    print(f"  Win Rate:        {baseline_wr}%")
    print(f"  Max Drawdown:    {baseline_dd}%")

    print(f"\nActual Results:")

    for metric_key, metric_name, direction, threshold in metrics:
        if metric_key in results:
            actual = results[metric_key]
            baseline_val = baseline.get(metric_key)

            # Check against threshold
            if direction == 'min' and actual < threshold:
                print(f"  {metric_name:20} {actual:>10.2f} ✗ BELOW {threshold}")
                all_valid = False
            elif direction == 'max' and actual > threshold:
                print(f"  {metric_name:20} {actual:>10.2f} ✗ EXCEEDS {threshold}")
                all_valid = False
            else:
                print(f"  {metric_name:20} {actual:>10.2f} ✓")

    print("\n" + "=" * 70)

    if all_valid:
        print("VALIDATION: PASSED ✓")
        print("=" * 70)
        return True
    else:
        print("VALIDATION: FAILED ✗")
        print("=" * 70)
        print("\nFailed metrics exceed acceptable thresholds.")
        print("Review configuration and backtest results.")
        return False

def main():
    parser = ArgumentParser()
    parser.add_argument('--baseline', required=True, help='Path to baseline JSON')
    parser.add_argument('--results', required=True, help='Path to results JSON')
    args = parser.parse_args()

    baseline = load_json(args.baseline)
    results = load_json(args.results)

    success = validate_results(baseline, results)
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
