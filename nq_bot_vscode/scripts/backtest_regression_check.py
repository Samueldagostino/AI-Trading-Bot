#!/usr/bin/env python3
"""
Backtest Regression Check Tool

Compares before/after backtest results to verify that code changes didn't introduce
regressions. Reads baseline expectations and applies clear pass/fail criteria.

Usage:
    python backtest_regression_check.py --before before.json --after after.json
    python backtest_regression_check.py --demo
    python backtest_regression_check.py --before before.json --after after.json --baseline custom_baseline.json
"""

import json
import sys
import argparse
from pathlib import Path
from typing import Dict, List, Any, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime


@dataclass
class RegressionResult:
    """Result of regression check."""
    verdict: str  # PASS, FAIL, WARN
    overall_status: str  # Summary of overall status
    metrics: Dict[str, Any]  # Individual metric results
    failures: List[str]  # Detailed failure messages
    warnings: List[str]  # Warning messages
    improvements: List[str]  # Improvements detected
    timestamp: str  # When check was run


def load_json(filepath: str) -> dict:
    """Load JSON file with error handling."""
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"File not found: {filepath}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {filepath}: {e}")


def save_json(data: dict, filepath: str) -> None:
    """Save data to JSON file."""
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)


def normalize_metrics(data: dict) -> dict:
    """Normalize metric names and values from backtest results."""
    normalized = {
        'profit_factor': data.get('profit_factor', data.get('pf')),
        'win_rate_pct': data.get('win_rate_pct', data.get('wr')),
        'max_drawdown_pct': data.get('max_drawdown_pct', data.get('max_dd')),
        'total_trades': data.get('total_trades', data.get('trades')),
        'c1_pnl': data.get('c1_pnl'),
        'c2_pnl': data.get('c2_pnl'),
        'total_pnl': data.get('total_pnl'),
        'monthly': data.get('monthly', []),
        'trades_per_month': data.get('trades_per_month'),
    }
    return normalized


def extract_monthly_profitability(monthly_data: List[dict]) -> Dict[str, float]:
    """Extract monthly PnL from monthly array."""
    monthly_pnl = {}
    if monthly_data:
        for month_entry in monthly_data:
            month_key = month_entry.get('month', '')
            pnl = month_entry.get('pnl', 0)
            monthly_pnl[month_key] = pnl
    return monthly_pnl


def compare_profit_factor(before: float, after: float, baseline: float, failures: List[str], metrics_out: dict) -> str:
    """Compare profit factor. FAIL if dropped >15% from baseline."""
    if before is None or after is None or baseline is None:
        metrics_out['profit_factor'] = {'status': 'SKIP', 'reason': 'Missing data'}
        return None

    change_pct = ((after - baseline) / baseline * 100) if baseline else 0
    before_change_pct = ((before - baseline) / baseline * 100) if baseline else 0

    metrics_out['profit_factor'] = {
        'status': 'CHECK',
        'baseline': baseline,
        'before': before,
        'after': after,
        'change_from_baseline_pct': round(change_pct, 2),
        'before_change_from_baseline_pct': round(before_change_pct, 2),
    }

    if after < baseline * 0.85:
        verdict = 'FAIL'
        failures.append(f"Profit Factor: {after:.2f} dropped >15% from baseline {baseline:.2f} "
                       f"({change_pct:.1f}% change)")
    else:
        verdict = None

    return verdict


def compare_win_rate(before: float, after: float, baseline: float, failures: List[str], metrics_out: dict) -> str:
    """Compare win rate. FAIL if dropped >5 percentage points."""
    if before is None or after is None or baseline is None:
        metrics_out['win_rate_pct'] = {'status': 'SKIP', 'reason': 'Missing data'}
        return None

    change_pts = after - baseline
    before_change_pts = before - baseline

    metrics_out['win_rate_pct'] = {
        'status': 'CHECK',
        'baseline': baseline,
        'before': before,
        'after': after,
        'change_pts': round(change_pts, 2),
        'before_change_pts': round(before_change_pts, 2),
    }

    if after < baseline - 5.0:
        verdict = 'FAIL'
        failures.append(f"Win Rate: {after:.1f}% dropped >5 pts from baseline {baseline:.1f}% "
                       f"({change_pts:+.1f} pts)")
    else:
        verdict = None

    return verdict


def compare_max_drawdown(before: float, after: float, baseline: float, failures: List[str], metrics_out: dict) -> str:
    """Compare max drawdown. FAIL if increased >50% from baseline."""
    if before is None or after is None or baseline is None:
        metrics_out['max_drawdown_pct'] = {'status': 'SKIP', 'reason': 'Missing data'}
        return None

    change_pct = ((after - baseline) / baseline * 100) if baseline else 0
    before_change_pct = ((before - baseline) / baseline * 100) if baseline else 0

    metrics_out['max_drawdown_pct'] = {
        'status': 'CHECK',
        'baseline': baseline,
        'before': before,
        'after': after,
        'change_pct': round(change_pct, 2),
        'before_change_pct': round(before_change_pct, 2),
    }

    if after > baseline * 1.5:
        verdict = 'FAIL'
        failures.append(f"Max Drawdown: {after:.2f}% increased >50% from baseline {baseline:.2f}% "
                       f"({change_pct:+.1f}% change)")
    else:
        verdict = None

    return verdict


def compare_trade_count(before: float, after: float, baseline: float, warnings: List[str],
                        improvements: List[str], metrics_out: dict) -> str:
    """Compare trade count. WARN if changed >20% (may indicate entry logic affected)."""
    if before is None or after is None or baseline is None:
        metrics_out['trade_count'] = {'status': 'SKIP', 'reason': 'Missing data'}
        return None

    change_pct = ((after - baseline) / baseline * 100) if baseline else 0
    before_change_pct = ((before - baseline) / baseline * 100) if baseline else 0

    metrics_out['trade_count'] = {
        'status': 'CHECK',
        'baseline': baseline,
        'before': before,
        'after': after,
        'change_pct': round(change_pct, 2),
        'before_change_pct': round(before_change_pct, 2),
    }

    verdict = None
    if abs(change_pct) > 20:
        verdict = 'WARN'
        warnings.append(f"Trade Count: Changed {change_pct:+.1f}% from baseline {baseline:.0f} "
                       f"(now {after:.0f}). Entry logic may be affected.")

    return verdict


def report_pnl_changes(before: dict, after: dict, improvements: List[str], metrics_out: dict) -> None:
    """Report C1 and C2 PnL changes."""
    # C1 PnL
    before_c1 = before.get('c1_pnl')
    after_c1 = after.get('c1_pnl')

    if before_c1 is not None and after_c1 is not None:
        c1_change = after_c1 - before_c1
        c1_pct = (c1_change / before_c1 * 100) if before_c1 else 0
        metrics_out['c1_pnl'] = {
            'status': 'REPORT',
            'before': before_c1,
            'after': after_c1,
            'change': round(c1_change, 2),
            'change_pct': round(c1_pct, 2),
        }
        if c1_change > 0:
            improvements.append(f"C1 PnL: +${c1_change:.2f} ({c1_pct:+.1f}%)")
        elif c1_change < 0:
            improvements.append(f"C1 PnL: ${c1_change:.2f} ({c1_pct:+.1f}%)")

    # C2 PnL
    before_c2 = before.get('c2_pnl')
    after_c2 = after.get('c2_pnl')

    if before_c2 is not None and after_c2 is not None:
        c2_change = after_c2 - before_c2
        c2_pct = (c2_change / before_c2 * 100) if before_c2 else 0
        metrics_out['c2_pnl'] = {
            'status': 'REPORT',
            'before': before_c2,
            'after': after_c2,
            'change': round(c2_change, 2),
            'change_pct': round(c2_pct, 2),
        }
        if c2_change > 0:
            improvements.append(f"C2 PnL: +${c2_change:.2f} ({c2_pct:+.1f}%)")
        elif c2_change < 0:
            improvements.append(f"C2 PnL: ${c2_change:.2f} ({c2_pct:+.1f}%)")


def compare_monthly_consistency(before: dict, after: dict, failures: List[str], metrics_out: dict) -> str:
    """Check for months that flipped from profitable to losing (or vice versa)."""
    before_monthly = extract_monthly_profitability(before.get('monthly', []))
    after_monthly = extract_monthly_profitability(after.get('monthly', []))

    flips = []
    for month_key in before_monthly:
        if month_key in after_monthly:
            before_pnl = before_monthly[month_key]
            after_pnl = after_monthly[month_key]

            before_profitable = before_pnl > 0
            after_profitable = after_pnl > 0

            if before_profitable != after_profitable:
                direction = "profitable to losing" if before_profitable else "losing to profitable"
                flips.append(f"{month_key}: flipped {direction} (${before_pnl:.0f} → ${after_pnl:.0f})")

    if flips:
        metrics_out['monthly_consistency'] = {
            'status': 'FAIL',
            'flips': flips,
        }
        verdict = 'FAIL'
        for flip in flips:
            failures.append(f"Monthly Flip: {flip}")
    else:
        metrics_out['monthly_consistency'] = {
            'status': 'PASS',
            'flips': [],
        }
        verdict = None

    return verdict


def compare_results(before: dict, after: dict, baseline: dict = None) -> RegressionResult:
    """
    Main comparison function. Can be imported and called from other scripts.

    Args:
        before: Dict with backtest results before changes
        after: Dict with backtest results after changes
        baseline: Dict with baseline expectations (optional, uses defaults if None)

    Returns:
        RegressionResult with verdict and detailed metrics
    """
    # Normalize inputs
    before_norm = normalize_metrics(before)
    after_norm = normalize_metrics(after)

    # Default baseline if not provided
    if baseline is None:
        baseline = {
            'profit_factor': 1.73,
            'win_rate_pct': 61.9,
            'max_drawdown_pct': 1.4,
            'total_trades': 1524,
        }
    baseline_norm = normalize_metrics(baseline)

    # Tracking
    failures = []
    warnings = []
    improvements = []
    metrics_out = {}
    verdicts = []

    # Run comparisons
    v = compare_profit_factor(
        before_norm['profit_factor'],
        after_norm['profit_factor'],
        baseline_norm['profit_factor'],
        failures,
        metrics_out
    )
    if v:
        verdicts.append(v)

    v = compare_win_rate(
        before_norm['win_rate_pct'],
        after_norm['win_rate_pct'],
        baseline_norm['win_rate_pct'],
        failures,
        metrics_out
    )
    if v:
        verdicts.append(v)

    v = compare_max_drawdown(
        before_norm['max_drawdown_pct'],
        after_norm['max_drawdown_pct'],
        baseline_norm['max_drawdown_pct'],
        failures,
        metrics_out
    )
    if v:
        verdicts.append(v)

    v = compare_trade_count(
        before_norm['total_trades'],
        after_norm['total_trades'],
        baseline_norm['total_trades'],
        warnings,
        improvements,
        metrics_out
    )
    if v:
        verdicts.append(v)

    report_pnl_changes(before_norm, after_norm, improvements, metrics_out)

    v = compare_monthly_consistency(before_norm, after_norm, failures, metrics_out)
    if v:
        verdicts.append(v)

    # Determine overall verdict
    if 'FAIL' in verdicts:
        overall_verdict = 'FAIL'
        overall_status = 'REGRESSION DETECTED - Changes must be reviewed'
    elif 'WARN' in verdicts:
        overall_verdict = 'WARN'
        overall_status = 'Warnings present - Review carefully'
    else:
        overall_verdict = 'PASS'
        overall_status = 'No regressions detected'

    return RegressionResult(
        verdict=overall_verdict,
        overall_status=overall_status,
        metrics=metrics_out,
        failures=failures,
        warnings=warnings,
        improvements=improvements,
        timestamp=datetime.now().isoformat()
    )


def print_result(result: RegressionResult) -> None:
    """Print regression check result in human-readable format."""
    print("\n" + "=" * 80)
    print(f"BACKTEST REGRESSION CHECK REPORT")
    print(f"Timestamp: {result.timestamp}")
    print("=" * 80)

    # Overall verdict
    status_symbol = {
        'PASS': '✓ PASS',
        'FAIL': '✗ FAIL',
        'WARN': '⚠ WARN',
    }
    print(f"\n{status_symbol.get(result.verdict, result.verdict)}")
    print(f"{result.overall_status}\n")

    # Detailed metrics
    print("DETAILED METRICS:")
    print("-" * 80)
    for metric_name, metric_data in result.metrics.items():
        status = metric_data.get('status', 'UNKNOWN')
        if status == 'SKIP':
            print(f"  {metric_name:20s} SKIPPED ({metric_data.get('reason', '')})")
        elif status == 'CHECK':
            baseline = metric_data.get('baseline')
            before = metric_data.get('before')
            after = metric_data.get('after')
            change = metric_data.get('change_pct') or metric_data.get('change_pts', 0)
            change_unit = '%' if metric_data.get('change_pct') is not None else 'pts'
            print(f"  {metric_name:20s} | Baseline: {baseline:>8} | Before: {before:>8} | After: {after:>8} | Change: {change:+>7}{change_unit}")
        elif status == 'REPORT':
            before = metric_data.get('before')
            after = metric_data.get('after')
            change = metric_data.get('change')
            change_pct = metric_data.get('change_pct', 0)
            print(f"  {metric_name:20s} | Before: ${before:>10.2f} | After: ${after:>10.2f} | Change: ${change:+>10.2f} ({change_pct:+.1f}%)")
        elif status == 'PASS':
            print(f"  {metric_name:20s} PASS - No consistency flips detected")
        elif status == 'FAIL':
            print(f"  {metric_name:20s} FAIL - Monthly consistency issues:")
            for flip in metric_data.get('flips', []):
                print(f"    - {flip}")

    # Failures
    if result.failures:
        print("\nFAILURES:")
        print("-" * 80)
        for failure in result.failures:
            print(f"  ✗ {failure}")

    # Warnings
    if result.warnings:
        print("\nWARNINGS:")
        print("-" * 80)
        for warning in result.warnings:
            print(f"  ⚠ {warning}")

    # Improvements
    if result.improvements:
        print("\nCHANGES:")
        print("-" * 80)
        for improvement in result.improvements:
            print(f"  → {improvement}")

    print("\n" + "=" * 80 + "\n")


def create_demo_data() -> Tuple[dict, dict, dict]:
    """Create synthetic demo data showing before/after changes."""
    baseline = {
        "profit_factor": 1.73,
        "win_rate_pct": 61.9,
        "max_drawdown_pct": 1.4,
        "total_trades": 1524,
        "c1_pnl": 10008.00,
        "c2_pnl": 15573.00,
        "monthly": [
            {"month": "2025-09", "pnl": 3608},
            {"month": "2025-10", "pnl": 2798},
            {"month": "2025-11", "pnl": 6496},
            {"month": "2025-12", "pnl": 4385},
            {"month": "2026-01", "pnl": 4702},
            {"month": "2026-02", "pnl": 3592},
        ]
    }

    # Before: slightly above baseline
    before = {
        "profit_factor": 1.75,
        "win_rate_pct": 62.1,
        "max_drawdown_pct": 1.35,
        "total_trades": 1530,
        "c1_pnl": 10050.00,
        "c2_pnl": 15610.00,
        "monthly": [
            {"month": "2025-09", "pnl": 3650},
            {"month": "2025-10", "pnl": 2820},
            {"month": "2025-11", "pnl": 6520},
            {"month": "2025-12", "pnl": 4410},
            {"month": "2026-01", "pnl": 4750},
            {"month": "2026-02", "pnl": 3620},
        ]
    }

    # After: good improvement (B:5 bars optimization)
    after = {
        "profit_factor": 1.82,
        "win_rate_pct": 63.2,
        "max_drawdown_pct": 1.28,
        "total_trades": 1545,
        "c1_pnl": 10750.00,  # +700 improvement
        "c2_pnl": 16280.00,   # +670 improvement
        "monthly": [
            {"month": "2025-09", "pnl": 3850},
            {"month": "2025-10", "pnl": 3020},
            {"month": "2025-11", "pnl": 6850},
            {"month": "2025-12", "pnl": 4650},
            {"month": "2026-01", "pnl": 5050},
            {"month": "2026-02", "pnl": 3860},
        ]
    }

    return before, after, baseline


def main():
    parser = argparse.ArgumentParser(
        description='Backtest Regression Check Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Compare two backtest results
  python backtest_regression_check.py --before before.json --after after.json

  # Use custom baseline
  python backtest_regression_check.py --before before.json --after after.json --baseline custom_baseline.json

  # Demo mode with synthetic data
  python backtest_regression_check.py --demo
        '''
    )

    parser.add_argument('--before', type=str, help='Path to before backtest results JSON')
    parser.add_argument('--after', type=str, help='Path to after backtest results JSON')
    parser.add_argument('--baseline', type=str, default='config/backtest_baseline.json',
                       help='Path to baseline expectations JSON (default: config/backtest_baseline.json)')
    parser.add_argument('--demo', action='store_true', help='Run in demo mode with synthetic data')
    parser.add_argument('--output', type=str, help='Save detailed results to JSON file')

    args = parser.parse_args()

    try:
        if args.demo:
            print("\n[DEMO MODE] Using synthetic before/after data...\n")
            before, after, baseline = create_demo_data()
        else:
            if not args.before or not args.after:
                parser.print_help()
                sys.exit(1)

            before = load_json(args.before)
            after = load_json(args.after)
            baseline = load_json(args.baseline)

        # Run comparison
        result = compare_results(before, after, baseline)

        # Print result
        print_result(result)

        # Save to file if requested
        if args.output:
            output_data = asdict(result)
            save_json(output_data, args.output)
            print(f"Results saved to: {args.output}")

        # Exit with appropriate code
        if result.verdict == 'FAIL':
            sys.exit(1)
        elif result.verdict == 'WARN':
            sys.exit(0)  # Still allows CI to pass but flags warnings
        else:
            sys.exit(0)

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
