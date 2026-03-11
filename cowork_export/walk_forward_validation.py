#!/usr/bin/env python3
"""
Walk-Forward Validation Framework for MNQ Futures Trading Bot

This module implements an anchored walk-forward validation methodology to assess
the out-of-sample (OOS) performance and robustness of trading strategies.

METHODOLOGY:
-----------
Walk-forward validation uses an expanding training window with a fixed test window:

  Year 1: Train [Jan-Jun] → Test [Jul]
  Year 2: Train [Jan-Dec] → Test [Jan]  (anchored from year 1)
  Year 3: Train [Jan-Feb] → Test [Mar]  (anchored from year 1)

This approach:
  1. Prevents look-ahead bias (training data never sees test data)
  2. Tests strategy robustness with multiple out-of-sample periods
  3. Measures performance degradation (in-sample vs out-of-sample)
  4. Allows parameter optimization on expanding historical data

KEY METRICS:
-----------
Per-Fold:
  - IS PF (In-Sample Profit Factor)
  - OOS PF (Out-Of-Sample Profit Factor)
  - Degradation: (IS PF - OOS PF) / IS PF
  - Win Rate, Total P&L, Max Drawdown, Sharpe Ratio
  - Number of Trades

Aggregate:
  - Mean/Std OOS PF across folds
  - Consistency: percentage of profitable folds
  - Average degradation
  - Pass/Fail verdict based on thresholds

VALIDATION THRESHOLDS:
---------------------
- OOS PF: Must be > 1.5 for PASS, 1.0-1.5 for CAUTION, < 1.0 for FAIL
- Consistency: Must be >= 60% for PASS
- Degradation: Must be <= 20% for PASS
"""

import argparse
import json
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import List, Tuple, Dict, Optional
import statistics
import sys
from pathlib import Path


@dataclass
class FoldResult:
    """Results from a single walk-forward fold."""
    fold_number: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime

    # In-Sample metrics
    is_pf: float  # Profit Factor
    is_win_rate: float
    is_total_pnl: float
    is_max_dd: float
    is_sharpe: float
    is_num_trades: int
    is_c1_pnl: float  # Contract 1 P&L
    is_c2_pnl: float  # Contract 2 P&L

    # Out-Of-Sample metrics
    oos_pf: float
    oos_win_rate: float
    oos_total_pnl: float
    oos_max_dd: float
    oos_sharpe: float
    oos_num_trades: int
    oos_c1_pnl: float
    oos_c2_pnl: float

    @property
    def degradation(self) -> float:
        """Calculate performance degradation: (IS PF - OOS PF) / IS PF."""
        if self.is_pf <= 0:
            return float('nan')
        return max(0, (self.is_pf - self.oos_pf) / self.is_pf)

    @property
    def oos_profitable(self) -> bool:
        """Check if fold was profitable out-of-sample."""
        return self.oos_pf > 1.0


@dataclass
class AggregateMetrics:
    """Aggregate statistics across all folds."""
    total_folds: int
    mean_oos_pf: float
    std_oos_pf: float
    min_oos_pf: float
    max_oos_pf: float
    profitable_folds: int
    consistency_ratio: float  # profitable_folds / total_folds
    mean_degradation: float
    std_degradation: float
    mean_oos_trades: int
    mean_oos_win_rate: float

    @property
    def verdict(self) -> str:
        """
        Determine pass/fail/caution based on validation thresholds.

        PASS:    OOS PF >= 1.5 AND Consistency >= 60% AND Degradation <= 20%
        CAUTION: OOS PF >= 1.0 AND Consistency >= 40%
        FAIL:    Everything else
        """
        pf_check = self.mean_oos_pf >= 1.5
        consistency_check = self.consistency_ratio >= 0.6
        degradation_check = self.mean_degradation <= 0.2

        if pf_check and consistency_check and degradation_check:
            return "PASS"

        caution_pf = self.mean_oos_pf >= 1.0
        caution_consistency = self.consistency_ratio >= 0.4

        if caution_pf and caution_consistency:
            return "CAUTION"

        return "FAIL"


def generate_walk_forward_splits(
    start_date: datetime,
    end_date: datetime,
    train_months: int = 3,
    test_months: int = 1,
    n_folds: int = 3,
) -> List[Tuple[datetime, datetime, datetime, datetime]]:
    """
    Generate anchored walk-forward splits.

    Anchored means the training window grows from the initial start_date,
    while the test window always stays fixed size and follows the training window.

    Args:
        start_date: Beginning of the data range
        end_date: End of the data range
        train_months: Number of months in the INITIAL training window
        test_months: Number of months in each test window (fixed)
        n_folds: Number of walk-forward folds to generate (minimum 3)

    Returns:
        List of (train_start, train_end, test_start, test_end) tuples

    Example:
        If start_date=2023-01-01, train_months=3, test_months=1, n_folds=3:

        Fold 1: Train [Jan-Mar] → Test [Apr]
        Fold 2: Train [Jan-Jun] → Test [Jul]      (expanded training)
        Fold 3: Train [Jan-Sep] → Test [Oct]      (expanded training)
    """
    if n_folds < 3:
        raise ValueError("Minimum 3 folds required for walk-forward validation")

    splits = []
    current_date = start_date

    for fold_idx in range(n_folds):
        # Training window: always starts from original start_date, expands
        train_start = start_date
        train_end = add_months(current_date, train_months)

        # Test window: fixed size, immediately after training
        test_start = train_end + timedelta(days=1)
        test_end = add_months(test_start, test_months)

        # Check if test period is within data range
        if test_end > end_date:
            break

        splits.append((train_start, train_end, test_start, test_end))

        # Move forward for next fold
        current_date = add_months(current_date, train_months + test_months)

    if len(splits) < 3:
        raise ValueError(
            f"Could not generate minimum 3 folds. Generated {len(splits)} folds. "
            f"Increase date range or reduce train_months/test_months."
        )

    return splits[:n_folds]


def add_months(date: datetime, months: int) -> datetime:
    """Add a number of months to a datetime, handling month-end edge cases."""
    month = date.month - 1 + months
    year = date.year + month // 12
    month = month % 12 + 1
    day = min(date.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                          31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return datetime(year, month, day, date.hour, date.minute, date.second)


def run_backtest(
    config: Dict,
    start_date: datetime,
    end_date: datetime,
    fold_number: int = 0,
) -> Dict:
    """
    PLACEHOLDER: Replace with actual backtest engine call.

    This function should:
    1. Load market data for the specified date range
    2. Optimize strategy parameters (if it's an in-sample backtest)
    3. Run the strategy and collect metrics

    Args:
        config: Strategy configuration dict
        start_date: Backtest period start
        end_date: Backtest period end
        fold_number: Current fold number (for logging)

    Returns:
        Dictionary with keys:
        {
            "pf": float,                # Profit Factor (gross profit / gross loss)
            "win_rate": float,          # Win rate as decimal (0-1)
            "total_pnl": float,         # Total P&L in currency
            "max_dd": float,            # Maximum drawdown as decimal
            "sharpe": float,            # Sharpe ratio
            "num_trades": int,          # Total number of trades
            "c1_pnl": float,            # Contract 1 P&L
            "c2_pnl": float,            # Contract 2 P&L
        }

    Raises:
        NotImplementedError: This is a placeholder
    """
    raise NotImplementedError(
        "Connect run_backtest() to your actual backtest engine.\n"
        "This should call your backtester and return metrics dict."
    )


def run_walk_forward_validation(
    config: Dict,
    start_date: datetime,
    end_date: datetime,
    train_months: int = 3,
    test_months: int = 1,
    n_folds: int = 3,
    verbose: bool = True,
) -> Tuple[List[FoldResult], AggregateMetrics]:
    """
    Execute walk-forward validation.

    Args:
        config: Strategy configuration dictionary
        start_date: Start of validation period
        end_date: End of validation period
        train_months: Months per training window
        test_months: Months per test window
        n_folds: Number of folds
        verbose: Print progress messages

    Returns:
        (fold_results, aggregate_metrics) tuple
    """
    if verbose:
        print(f"Generating {n_folds} walk-forward splits...")

    splits = generate_walk_forward_splits(
        start_date, end_date, train_months, test_months, n_folds
    )

    fold_results: List[FoldResult] = []

    for fold_idx, (train_start, train_end, test_start, test_end) in enumerate(splits, 1):
        if verbose:
            print(f"\nFold {fold_idx}/{len(splits)}")
            print(f"  Training: {train_start.strftime('%Y-%m-%d')} to {train_end.strftime('%Y-%m-%d')}")
            print(f"  Testing:  {test_start.strftime('%Y-%m-%d')} to {test_end.strftime('%Y-%m-%d')}")

        # Run in-sample backtest (optimization period)
        if verbose:
            print(f"  Running in-sample backtest...")
        is_metrics = run_backtest(config, train_start, train_end, fold_idx)

        # Run out-of-sample backtest (test period, no optimization)
        if verbose:
            print(f"  Running out-of-sample backtest...")
        oos_metrics = run_backtest(config, test_start, test_end, fold_idx)

        fold_result = FoldResult(
            fold_number=fold_idx,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            is_pf=is_metrics["pf"],
            is_win_rate=is_metrics["win_rate"],
            is_total_pnl=is_metrics["total_pnl"],
            is_max_dd=is_metrics["max_dd"],
            is_sharpe=is_metrics["sharpe"],
            is_num_trades=is_metrics["num_trades"],
            is_c1_pnl=is_metrics.get("c1_pnl", 0.0),
            is_c2_pnl=is_metrics.get("c2_pnl", 0.0),
            oos_pf=oos_metrics["pf"],
            oos_win_rate=oos_metrics["win_rate"],
            oos_total_pnl=oos_metrics["total_pnl"],
            oos_max_dd=oos_metrics["max_dd"],
            oos_sharpe=oos_metrics["sharpe"],
            oos_num_trades=oos_metrics["num_trades"],
            oos_c1_pnl=oos_metrics.get("c1_pnl", 0.0),
            oos_c2_pnl=oos_metrics.get("c2_pnl", 0.0),
        )
        fold_results.append(fold_result)

    # Compute aggregate metrics
    oos_pfs = [f.oos_pf for f in fold_results]
    degradations = [f.degradation for f in fold_results if not (f.degradation != f.degradation)]  # Filter NaN
    profitable_count = sum(1 for f in fold_results if f.oos_profitable)

    aggregate = AggregateMetrics(
        total_folds=len(fold_results),
        mean_oos_pf=statistics.mean(oos_pfs),
        std_oos_pf=statistics.stdev(oos_pfs) if len(oos_pfs) > 1 else 0.0,
        min_oos_pf=min(oos_pfs),
        max_oos_pf=max(oos_pfs),
        profitable_folds=profitable_count,
        consistency_ratio=profitable_count / len(fold_results),
        mean_degradation=statistics.mean(degradations) if degradations else 0.0,
        std_degradation=statistics.stdev(degradations) if len(degradations) > 1 else 0.0,
        mean_oos_trades=int(statistics.mean([f.oos_num_trades for f in fold_results])),
        mean_oos_win_rate=statistics.mean([f.oos_win_rate for f in fold_results]),
    )

    return fold_results, aggregate


def print_results(fold_results: List[FoldResult], aggregate: AggregateMetrics) -> None:
    """
    Print walk-forward validation results in a formatted table.

    Args:
        fold_results: List of FoldResult objects
        aggregate: AggregateMetrics object
    """
    print("\n" + "=" * 90)
    print("WALK-FORWARD VALIDATION RESULTS - MNQ FUTURES TRADING BOT".center(90))
    print("=" * 90)

    # Per-fold results
    print("\nPER-FOLD RESULTS:")
    print("-" * 90)
    print(f"{'Fold':<5} {'Train Period':<20} {'Test Period':<20} {'IS PF':<8} {'OOS PF':<8} {'Degrad.':<8} {'OOS WR':<8}")
    print("-" * 90)

    for fold in fold_results:
        train_period = f"{fold.train_start.strftime('%b %y')}-{fold.train_end.strftime('%b %y')}"
        test_period = f"{fold.test_start.strftime('%b %y')}-{fold.test_end.strftime('%b %y')}"
        degradation_pct = fold.degradation * 100 if fold.degradation == fold.degradation else float('nan')
        oos_wr_pct = fold.oos_win_rate * 100

        print(
            f"{fold.fold_number:<5} {train_period:<20} {test_period:<20} "
            f"{fold.is_pf:<8.2f} {fold.oos_pf:<8.2f} {degradation_pct:<8.1f}% {oos_wr_pct:<8.1f}%"
        )

    # Detailed fold information
    print("\nDETAILED FOLD ANALYSIS:")
    print("-" * 90)

    for fold in fold_results:
        degradation_pct = fold.degradation * 100 if fold.degradation == fold.degradation else float('nan')
        print(
            f"\nFold {fold.fold_number}: "
            f"Train [{fold.train_start.strftime('%b %y')}-{fold.train_end.strftime('%b %y')}] "
            f"→ Test [{fold.test_start.strftime('%b %y')}-{fold.test_end.strftime('%b %y')}]"
        )
        print(f"  IN-SAMPLE   | PF: {fold.is_pf:6.2f} | Trades: {fold.is_num_trades:4d} | "
              f"WR: {fold.is_win_rate*100:5.1f}% | P&L: ${fold.is_total_pnl:10,.0f} | "
              f"MaxDD: {fold.is_max_dd*100:5.1f}% | Sharpe: {fold.is_sharpe:6.2f}")
        print(f"  OUT-OF-SAMPLE | PF: {fold.oos_pf:6.2f} | Trades: {fold.oos_num_trades:4d} | "
              f"WR: {fold.oos_win_rate*100:5.1f}% | P&L: ${fold.oos_total_pnl:10,.0f} | "
              f"MaxDD: {fold.oos_max_dd*100:5.1f}% | Sharpe: {fold.oos_sharpe:6.2f}")
        print(f"  Degradation: {degradation_pct:5.1f}% | C1 P&L: ${fold.oos_c1_pnl:10,.0f} | "
              f"C2 P&L: ${fold.oos_c2_pnl:10,.0f}")

    # Aggregate statistics
    print("\n" + "=" * 90)
    print("AGGREGATE STATISTICS".center(90))
    print("=" * 90)

    print(f"\nOut-of-Sample Profit Factor:")
    print(f"  Mean:        {aggregate.mean_oos_pf:6.2f}")
    print(f"  Std Dev:     {aggregate.std_oos_pf:6.2f}")
    print(f"  Min/Max:     {aggregate.min_oos_pf:6.2f} / {aggregate.max_oos_pf:6.2f}")

    print(f"\nConsistency:")
    print(f"  Profitable Folds:  {aggregate.profitable_folds}/{aggregate.total_folds} "
          f"({aggregate.consistency_ratio*100:.1f}%)")

    print(f"\nPerformance Degradation (IS vs OOS):")
    print(f"  Mean:        {aggregate.mean_degradation*100:6.1f}%")
    print(f"  Std Dev:     {aggregate.std_degradation*100:6.1f}%")

    print(f"\nAverage Out-of-Sample Metrics:")
    print(f"  Avg Trades:  {aggregate.mean_oos_trades}")
    print(f"  Avg Win Rate: {aggregate.mean_oos_win_rate*100:.1f}%")

    # Verdict
    print("\n" + "=" * 90)
    verdict_color = {
        "PASS": "✓",
        "CAUTION": "⚠",
        "FAIL": "✗"
    }
    print(f"VERDICT: [{verdict_color.get(aggregate.verdict, '?')}] {aggregate.verdict}".center(90))
    print("=" * 90)

    # Explain verdict
    print("\nVERDICT CRITERIA:")
    print(f"  PASS:    Mean OOS PF ≥ 1.50 AND Consistency ≥ 60% AND Degradation ≤ 20%")
    print(f"  CAUTION: Mean OOS PF ≥ 1.00 AND Consistency ≥ 40%")
    print(f"  FAIL:    Does not meet PASS or CAUTION thresholds")

    print(f"\nYour Strategy:")
    print(f"  Mean OOS PF:       {aggregate.mean_oos_pf:.2f} {'✓' if aggregate.mean_oos_pf >= 1.5 else '✗ (need ≥1.50)'}")
    print(f"  Consistency:       {aggregate.consistency_ratio*100:.1f}% {'✓' if aggregate.consistency_ratio >= 0.6 else '✗ (need ≥60%)'}")
    print(f"  Degradation:       {aggregate.mean_degradation*100:.1f}% {'✓' if aggregate.mean_degradation <= 0.2 else '✗ (need ≤20%)'}")

    print("\n")


def generate_demo_results() -> Tuple[List[FoldResult], AggregateMetrics]:
    """
    Generate synthetic walk-forward results for demonstration.

    This shows what output looks like when the framework is fully connected
    to a backtest engine.

    Returns:
        (fold_results, aggregate_metrics) tuple with realistic demo data
    """
    demo_folds = [
        FoldResult(
            fold_number=1,
            train_start=datetime(2023, 1, 1),
            train_end=datetime(2023, 3, 31),
            test_start=datetime(2023, 4, 1),
            test_end=datetime(2023, 4, 30),
            is_pf=2.15, is_win_rate=0.58, is_total_pnl=4250, is_max_dd=0.12, is_sharpe=1.45,
            is_num_trades=84, is_c1_pnl=2150, is_c2_pnl=2100,
            oos_pf=1.68, oos_win_rate=0.54, oos_total_pnl=2890, oos_max_dd=0.15, oos_sharpe=1.22,
            oos_num_trades=52, oos_c1_pnl=1480, oos_c2_pnl=1410,
        ),
        FoldResult(
            fold_number=2,
            train_start=datetime(2023, 1, 1),
            train_end=datetime(2023, 6, 30),
            test_start=datetime(2023, 7, 1),
            test_end=datetime(2023, 7, 31),
            is_pf=2.32, is_win_rate=0.60, is_total_pnl=5120, is_max_dd=0.11, is_sharpe=1.68,
            is_num_trades=156, is_c1_pnl=2580, is_c2_pnl=2540,
            oos_pf=1.82, oos_win_rate=0.56, oos_total_pnl=3210, oos_max_dd=0.14, oos_sharpe=1.35,
            oos_num_trades=61, oos_c1_pnl=1620, oos_c2_pnl=1590,
        ),
        FoldResult(
            fold_number=3,
            train_start=datetime(2023, 1, 1),
            train_end=datetime(2023, 9, 30),
            test_start=datetime(2023, 10, 1),
            test_end=datetime(2023, 10, 31),
            is_pf=2.08, is_win_rate=0.57, is_total_pnl=4890, is_max_dd=0.13, is_sharpe=1.52,
            is_num_trades=142, is_c1_pnl=2450, is_c2_pnl=2440,
            oos_pf=1.55, oos_win_rate=0.52, oos_total_pnl=2340, oos_max_dd=0.18, oos_sharpe=1.08,
            oos_num_trades=48, oos_c1_pnl=1190, oos_c2_pnl=1150,
        ),
    ]

    oos_pfs = [f.oos_pf for f in demo_folds]
    degradations = [f.degradation for f in demo_folds]
    profitable = sum(1 for f in demo_folds if f.oos_pf > 1.0)

    aggregate = AggregateMetrics(
        total_folds=3,
        mean_oos_pf=statistics.mean(oos_pfs),
        std_oos_pf=statistics.stdev(oos_pfs),
        min_oos_pf=min(oos_pfs),
        max_oos_pf=max(oos_pfs),
        profitable_folds=profitable,
        consistency_ratio=profitable / 3,
        mean_degradation=statistics.mean(degradations),
        std_degradation=statistics.stdev(degradations),
        mean_oos_trades=int(statistics.mean([f.oos_num_trades for f in demo_folds])),
        mean_oos_win_rate=statistics.mean([f.oos_win_rate for f in demo_folds]),
    )

    return demo_folds, aggregate


def save_results_to_json(
    fold_results: List[FoldResult],
    aggregate: AggregateMetrics,
    output_path: str,
) -> None:
    """
    Save walk-forward results to JSON for further analysis.

    Args:
        fold_results: List of FoldResult objects
        aggregate: AggregateMetrics object
        output_path: Path to output JSON file
    """
    output = {
        "timestamp": datetime.now().isoformat(),
        "fold_results": [asdict(f) for f in fold_results],
        "aggregate_metrics": asdict(aggregate),
    }

    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2, default=str)

    print(f"Results saved to {output_path}")


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Walk-Forward Validation Framework for MNQ Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run demo mode (shows expected output format)
  python walk_forward_validation.py --demo

  # Run actual validation (requires backtest engine implementation)
  python walk_forward_validation.py --start 2023-01-01 --end 2023-12-31 --folds 5

  # Adjust training/test window sizes
  python walk_forward_validation.py --demo --train-months 6 --test-months 2
        """
    )

    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run in demo mode with synthetic results",
    )
    parser.add_argument(
        "--start",
        type=str,
        default="2023-01-01",
        help="Start date (YYYY-MM-DD, default: 2023-01-01)",
    )
    parser.add_argument(
        "--end",
        type=str,
        default="2023-12-31",
        help="End date (YYYY-MM-DD, default: 2023-12-31)",
    )
    parser.add_argument(
        "--train-months",
        type=int,
        default=3,
        help="Initial training window in months (default: 3)",
    )
    parser.add_argument(
        "--test-months",
        type=int,
        default=1,
        help="Test window in months (default: 1)",
    )
    parser.add_argument(
        "--folds",
        type=int,
        default=3,
        help="Number of walk-forward folds (default: 3)",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Save results to JSON file",
    )

    args = parser.parse_args()

    if args.demo:
        print("\n[DEMO MODE] Generating synthetic results...\n")
        fold_results, aggregate = generate_demo_results()
    else:
        try:
            start_date = datetime.strptime(args.start, "%Y-%m-%d")
            end_date = datetime.strptime(args.end, "%Y-%m-%d")
        except ValueError:
            print("Error: Invalid date format. Use YYYY-MM-DD")
            sys.exit(1)

        config = {}  # Empty config - would be populated with strategy parameters

        try:
            fold_results, aggregate = run_walk_forward_validation(
                config=config,
                start_date=start_date,
                end_date=end_date,
                train_months=args.train_months,
                test_months=args.test_months,
                n_folds=args.folds,
            )
        except NotImplementedError as e:
            print(f"\nError: {e}")
            print("\nTo run actual validation, implement the run_backtest() function:")
            print("  1. Load market data for the date range")
            print("  2. Optimize strategy parameters (in-sample)")
            print("  3. Run strategy and collect metrics")
            print("  4. Return dict with pf, win_rate, total_pnl, max_dd, sharpe, etc.")
            sys.exit(1)

    print_results(fold_results, aggregate)

    if args.output:
        save_results_to_json(fold_results, aggregate, args.output)


if __name__ == "__main__":
    main()
