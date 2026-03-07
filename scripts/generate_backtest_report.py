"""
Backtest Report Generator
==========================

Generates a formatted markdown report of backtest results
for posting to GitHub PR comments.

Usage:
    python generate_backtest_report.py \\
        --baseline config/backtest_baseline.json \\
        --results /tmp/backtest_results.json \\
        --output /tmp/backtest_report.md
"""

import json
import sys
from pathlib import Path
from argparse import ArgumentParser
from datetime import datetime

def load_json(filepath):
    """Load JSON file safely."""
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"WARNING: Cannot load {filepath}: {e}")
        return None

def format_metric(name, value, baseline_value=None, direction='higher_better'):
    """Format a metric with comparison to baseline."""
    if baseline_value is None:
        return f"| {name} | {value:.2f} | — |"

    if direction == 'higher_better':
        diff = value - baseline_value
        indicator = "📈" if diff > 0 else "📉" if diff < 0 else "➡️"
    else:
        diff = baseline_value - value
        indicator = "📈" if diff > 0 else "📉" if diff < 0 else "➡️"

    return f"| {name} | {value:.2f} | {baseline_value:.2f} | {indicator} {abs(diff):+.2f} |"

def generate_report(baseline, results):
    """Generate markdown report."""
    report = "## Backtest Validation Results\n\n"

    if not results:
        report += "❌ **Backtest Failed** — No results available\n"
        return report

    report += f"**Timestamp**: {datetime.now().isoformat()}\n\n"

    # Summary section
    report += "### Summary\n\n"
    report += f"- **Strategy**: 2-Contract Scale-Out (HC Filtered)\n"
    report += f"- **Account**: $50,000\n"
    report += f"- **HTF Gate**: Config D (0.3)\n"
    report += f"- **HC Filter**: Score >= 0.75, Stop <= 30pts\n\n"

    # Performance metrics
    report += "### Performance Metrics\n\n"
    report += "| Metric | Actual | Baseline | Change |\n"
    report += "|--------|--------|----------|--------|\n"

    metrics_to_show = [
        ('total_trades', 'Total Trades', 'count'),
        ('win_rate_pct', 'Win Rate (%)', 'percent'),
        ('profit_factor', 'Profit Factor', 'float'),
        ('expectancy_per_trade', 'Expectancy/Trade ($)', 'float'),
        ('total_pnl', 'Total P&L ($)', 'currency'),
        ('max_drawdown_pct', 'Max Drawdown (%)', 'drawdown'),
        ('avg_winner', 'Avg Winner ($)', 'currency'),
        ('avg_loser', 'Avg Loser ($)', 'currency'),
    ]

    for key, display_name, metric_type in metrics_to_show:
        if key in results:
            actual = results[key]
            baseline_val = baseline.get(key) if baseline else None

            if metric_type == 'count':
                baseline_str = str(int(baseline_val)) if baseline_val else '—'
                report += f"| {display_name} | {int(actual)} | {baseline_str} |\n"
            elif metric_type == 'percent':
                direction = 'higher_better'
                report += format_metric(display_name, actual, baseline_val, direction) + "\n"
            elif metric_type == 'currency':
                baseline_str = f"${baseline_val:,.2f}" if baseline_val else "—"
                report += f"| {display_name} | ${actual:,.2f} | {baseline_str} |\n"
            elif metric_type == 'float':
                direction = 'higher_better'
                report += format_metric(display_name, actual, baseline_val, direction) + "\n"
            elif metric_type == 'drawdown':
                direction = 'lower_better'
                report += format_metric(display_name, actual, baseline_val, direction) + "\n"

    # Scale-out breakdown
    if 'c1_total_pnl' in results and 'c2_total_pnl' in results:
        report += "\n### Scale-Out Breakdown\n\n"
        c1_pnl = results['c1_total_pnl']
        c2_pnl = results['c2_total_pnl']
        total = c1_pnl + c2_pnl

        report += f"- **Contract 1 (Trail)**: ${c1_pnl:,.2f} ({100*c1_pnl/total:.1f}%)\n"
        report += f"- **Contract 2 (Runner)**: ${c2_pnl:,.2f} ({100*c2_pnl/total:.1f}%)\n"

        if 'c2_outperformed_c1_pct' in results:
            report += f"- **C2 Outperformance**: {results['c2_outperformed_c1_pct']:.1f}% of trades\n"

    # Regime & blocking
    if 'htf_blocked_signals' in results and 'htf_block_rate' in results:
        report += "\n### HTF Filtering\n\n"
        report += f"- **Signals Blocked by HTF Gate**: {results['htf_blocked_signals']:,}\n"
        report += f"- **Block Rate**: {results['htf_block_rate']:.1f}%\n"
        report += f"- **Insight**: HTF filter prevented {results['htf_block_rate']:.0f}% of signals (tail risk reduction)\n"

    # Validation status
    report += "\n### Validation Status\n\n"

    if baseline:
        baseline_pf = baseline.get('profit_factor', 1.73)
        baseline_dd = baseline.get('max_drawdown_pct', 1.4)

        actual_pf = results.get('profit_factor', 0)
        actual_dd = results.get('max_drawdown_pct', 100)

        pf_ok = actual_pf >= baseline_pf * 0.95  # Allow 5% degradation
        dd_ok = actual_dd <= baseline_dd * 1.1  # Allow 10% increase

        status = "✅ PASSED" if (pf_ok and dd_ok) else "⚠️ REVIEW"

        report += f"- Profit Factor: {actual_pf:.2f} vs baseline {baseline_pf:.2f} {('✓' if pf_ok else '⚠️')}\n"
        report += f"- Max Drawdown: {actual_dd:.2f}% vs baseline {baseline_dd:.2f}% {('✓' if dd_ok else '⚠️')}\n"
        report += f"\n**Overall**: {status}\n"
    else:
        report += "⚠️ No baseline for comparison\n"

    report += "\n---\n"
    report += "*Generated by GitHub Actions CI*\n"

    return report

def main():
    parser = ArgumentParser()
    parser.add_argument('--baseline', required=True, help='Path to baseline JSON')
    parser.add_argument('--results', required=True, help='Path to results JSON')
    parser.add_argument('--output', required=True, help='Output markdown file')
    args = parser.parse_args()

    baseline = load_json(args.baseline)
    results = load_json(args.results)

    report = generate_report(baseline, results)

    # Write report
    Path(args.output).write_text(report)
    print(f"Report written to: {args.output}")
    print("\n" + report)

if __name__ == "__main__":
    main()
