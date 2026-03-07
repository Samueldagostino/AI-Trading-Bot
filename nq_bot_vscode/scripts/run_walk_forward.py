#!/usr/bin/env python3
"""
Walk-Forward Optimization Engine
==================================
Trains on rolling windows and tests out-of-sample to validate that
strategy parameters hold up across regime changes.

Uses TradingOrchestrator.run_backtest_mtf() as a black box — does NOT
modify the backtest engine, HC filter, risk engine, or signal generation.

Usage:
    # Default: 3-month train, 1-month test, slide by 1 month
    python scripts/run_walk_forward.py --tv

    # Custom windows
    python scripts/run_walk_forward.py --tv --train-months 4 --test-months 2

    # With parameter grid search
    python scripts/run_walk_forward.py --tv --grid

    # Export report
    python scripts/run_walk_forward.py --tv --report reports/wf_report.html
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Project paths
script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))

from config.settings import CONFIG, BotConfig
from data_pipeline.pipeline import (
    DataPipeline, BarData, MultiTimeframeIterator,
    TradingViewImporter, bardata_to_bar, bardata_to_htfbar,
    MINUTES_TO_LABEL, _parse_tf_from_filename,
)
from main import TradingOrchestrator, HTF_TIMEFRAMES
from scripts.walk_forward_report import FoldResult, WFSummary, WalkForwardReport

logger = logging.getLogger(__name__)

EXEC_TF = "2m"
REPO_ROOT = project_dir.parent


class WalkForwardEngine:
    """
    Walk-forward optimization engine with rolling train/test windows.

    Parameters:
        train_months: Training window size in months.
        test_months: Out-of-sample test window size in months.
        step_months: How many months to slide forward each fold.
        min_trades_per_fold: Skip folds with fewer trades than this.
        parameter_grid: Optional dict of parameters to optimize.
    """

    def __init__(
        self,
        train_months: int = 3,
        test_months: int = 1,
        step_months: int = 1,
        min_trades_per_fold: int = 10,
        parameter_grid: Optional[Dict] = None,
    ):
        self.train_months = train_months
        self.test_months = test_months
        self.step_months = step_months
        self.min_trades_per_fold = min_trades_per_fold
        self.parameter_grid = parameter_grid

    def run(self, data_dir: str) -> WalkForwardReport:
        """Run walk-forward optimization on TradingView CSVs.

        Synchronous wrapper around the async engine.
        """
        return asyncio.run(self._run_async(data_dir))

    async def _run_async(self, data_dir: str) -> WalkForwardReport:
        """Core async walk-forward loop."""
        # 1. Load all TradingView CSVs
        tf_bars = self._load_data(data_dir)
        if not tf_bars:
            raise RuntimeError(f"No data loaded from {data_dir}")

        # 2. Get available months sorted chronologically
        months = self._get_available_months(tf_bars)
        if len(months) < self.train_months + self.test_months:
            raise RuntimeError(
                f"Need at least {self.train_months + self.test_months} months of data, "
                f"but only found {len(months)}: {months}"
            )

        # 3. Generate fold definitions
        fold_defs = self._generate_folds(months)
        logger.info(f"Generated {len(fold_defs)} folds from {len(months)} months of data")

        # 4. Segment bars by month
        monthly_bars = self._segment_by_month(tf_bars)

        # 5. Run each fold
        folds: List[FoldResult] = []
        for fold_id, (train_months_list, test_months_list) in enumerate(fold_defs, 1):
            print(f"\n{'─' * 60}")
            print(f"  FOLD {fold_id}/{len(fold_defs)}: "
                  f"Train [{train_months_list[0]}..{train_months_list[-1]}] → "
                  f"Test [{test_months_list[0]}..{test_months_list[-1]}]")
            print(f"{'─' * 60}")

            fold = await self._run_fold(
                fold_id, train_months_list, test_months_list, monthly_bars
            )
            folds.append(fold)

        # 6. Compute summary
        summary = self._compute_summary(folds)

        # 7. Compare against baseline
        self._compare_baseline(summary)

        return WalkForwardReport(folds=folds, summary=summary)

    def _load_data(self, data_dir: str) -> Dict[str, List[BarData]]:
        """Load TradingView CSVs grouped by timeframe."""
        dir_path = Path(data_dir)
        if not dir_path.exists():
            logger.error(f"Data directory not found: {data_dir}")
            return {}

        importer = TradingViewImporter(CONFIG)
        tf_bars: Dict[str, List[BarData]] = {}

        # Try TradingView txt/csv files
        patterns = ["*.txt", "*.csv"]
        all_files = []
        for pattern in patterns:
            all_files.extend(sorted(dir_path.glob(pattern)))

        if not all_files:
            logger.error(f"No CSV/TXT files found in {data_dir}")
            return {}

        for csv_file in all_files:
            tf_label = _parse_tf_from_filename(str(csv_file))
            if not tf_label:
                # For TradingView exports without TF in filename, try NQ_ prefix
                name = csv_file.stem
                tf_map = {
                    "NQ_1m": "1m", "NQ_2m": "2m", "NQ_3m": "3m",
                    "NQ_5m": "5m", "NQ_15m": "15m", "NQ_30m": "30m",
                    "NQ_1H": "1H", "NQ_4H": "4H", "NQ_1D": "1D",
                }
                tf_label = tf_map.get(name)

            if not tf_label:
                logger.warning(f"Could not determine timeframe for: {csv_file.name}, skipping")
                continue

            bars = importer.import_file(str(csv_file))
            if bars:
                if tf_label in tf_bars:
                    tf_bars[tf_label].extend(bars)
                    tf_bars[tf_label].sort(key=lambda b: b.timestamp)
                else:
                    tf_bars[tf_label] = bars
                logger.info(f"  Loaded {tf_label}: {len(bars):,} bars from {csv_file.name}")

        return tf_bars

    def _get_available_months(self, tf_bars: Dict[str, List[BarData]]) -> List[str]:
        """Get sorted list of month keys from execution TF data."""
        # Use the finest available TF for month detection
        target_tf = None
        for tf in [EXEC_TF, "2m", "1m", "3m", "5m"]:
            if tf in tf_bars:
                target_tf = tf
                break
        if not target_tf:
            # Use whatever is available
            target_tf = list(tf_bars.keys())[0]

        months = set()
        for bar in tf_bars[target_tf]:
            months.add(bar.timestamp.strftime("%Y-%m"))
        return sorted(months)

    def _generate_folds(
        self, months: List[str]
    ) -> List[Tuple[List[str], List[str]]]:
        """Generate (train_months, test_months) tuples for each fold.

        Ensures train window never overlaps with test window.
        """
        folds = []
        i = 0
        while i + self.train_months + self.test_months <= len(months):
            train = months[i : i + self.train_months]
            test = months[i + self.train_months : i + self.train_months + self.test_months]
            folds.append((train, test))
            i += self.step_months
        return folds

    def _segment_by_month(
        self, tf_bars: Dict[str, List[BarData]]
    ) -> Dict[str, Dict[str, List[BarData]]]:
        """Split multi-TF bars into {month_key: {tf: [bars]}}."""
        monthly: Dict[str, Dict[str, List[BarData]]] = defaultdict(lambda: defaultdict(list))
        for tf, bars in tf_bars.items():
            for bar in bars:
                month_key = bar.timestamp.strftime("%Y-%m")
                monthly[month_key][tf].append(bar)
        return dict(monthly)

    def _collect_window_bars(
        self, month_keys: List[str], monthly_bars: Dict[str, Dict[str, List[BarData]]]
    ) -> Dict[str, List[BarData]]:
        """Merge bars from multiple months into a single {tf: [bars]} dict."""
        merged: Dict[str, List[BarData]] = defaultdict(list)
        for mk in month_keys:
            if mk in monthly_bars:
                for tf, bars in monthly_bars[mk].items():
                    merged[tf].extend(bars)
        # Sort each TF
        for tf in merged:
            merged[tf].sort(key=lambda b: b.timestamp)
        return dict(merged)

    async def _run_fold(
        self,
        fold_id: int,
        train_months: List[str],
        test_months: List[str],
        monthly_bars: Dict[str, Dict[str, List[BarData]]],
    ) -> FoldResult:
        """Run a single walk-forward fold: backtest on train, then test."""
        fold = FoldResult(
            fold_id=fold_id,
            train_start=train_months[0],
            train_end=train_months[-1],
            test_start=test_months[0],
            test_end=test_months[-1],
        )

        # Collect train/test bars
        train_bars = self._collect_window_bars(train_months, monthly_bars)
        test_bars = self._collect_window_bars(test_months, monthly_bars)

        if not train_bars or EXEC_TF not in train_bars:
            fold.skipped = True
            fold.skip_reason = f"No {EXEC_TF} data in train window"
            print(f"  SKIPPED: {fold.skip_reason}")
            return fold

        if not test_bars or EXEC_TF not in test_bars:
            fold.skipped = True
            fold.skip_reason = f"No {EXEC_TF} data in test window"
            print(f"  SKIPPED: {fold.skip_reason}")
            return fold

        # Run train backtest
        train_results = await self._run_backtest(train_bars)
        fold.train_trades = train_results.get("total_trades", 0)
        fold.train_pf = train_results.get("profit_factor", 0)
        fold.train_wr = train_results.get("win_rate", 0)
        fold.train_pnl = train_results.get("total_pnl", 0)
        fold.train_dd = train_results.get("max_drawdown_pct", 0)

        print(f"  Train: {fold.train_trades} trades | PF {fold.train_pf:.2f} | "
              f"WR {fold.train_wr:.1f}% | PnL ${fold.train_pnl:,.2f} | DD {fold.train_dd:.1f}%")

        # Check min trades in train
        if fold.train_trades < self.min_trades_per_fold:
            fold.skipped = True
            fold.skip_reason = f"Train had only {fold.train_trades} trades (min: {self.min_trades_per_fold})"
            print(f"  SKIPPED: {fold.skip_reason}")
            return fold

        # Run test backtest (same config — no parameter changes)
        test_results = await self._run_backtest(test_bars)
        fold.test_trades = test_results.get("total_trades", 0)
        fold.test_pf = test_results.get("profit_factor", 0)
        fold.test_wr = test_results.get("win_rate", 0)
        fold.test_pnl = test_results.get("total_pnl", 0)
        fold.test_dd = test_results.get("max_drawdown_pct", 0)

        print(f"  Test:  {fold.test_trades} trades | PF {fold.test_pf:.2f} | "
              f"WR {fold.test_wr:.1f}% | PnL ${fold.test_pnl:,.2f} | DD {fold.test_dd:.1f}%")

        # Check min trades in test
        if fold.test_trades < self.min_trades_per_fold:
            fold.skipped = True
            fold.skip_reason = f"Test had only {fold.test_trades} trades (min: {self.min_trades_per_fold})"
            print(f"  SKIPPED: {fold.skip_reason}")
            return fold

        # Compute degradation
        if fold.train_pf > 0:
            fold.degradation = round(fold.test_pf / fold.train_pf, 4)
        else:
            fold.degradation = 0.0

        print(f"  Degradation: {fold.degradation:.2f} (OOS PF / IS PF)")

        return fold

    async def _run_backtest(self, tf_bars: Dict[str, List[BarData]]) -> Dict:
        """Run a single backtest using TradingOrchestrator as a black box."""
        CONFIG.execution.paper_trading = True
        bot = TradingOrchestrator(CONFIG)
        await bot.initialize(skip_db=True)

        pipeline = DataPipeline(CONFIG)
        mtf_iterator = pipeline.create_mtf_iterator(tf_bars)

        if len(mtf_iterator) == 0:
            return {"total_trades": 0, "profit_factor": 0, "win_rate": 0,
                    "total_pnl": 0, "max_drawdown_pct": 0}

        results = await bot.run_backtest_mtf(mtf_iterator, execution_tf=EXEC_TF)
        return results

    def _compute_summary(self, folds: List[FoldResult]) -> WFSummary:
        """Aggregate metrics across all folds."""
        valid = [f for f in folds if not f.skipped]
        skipped = [f for f in folds if f.skipped]

        summary = WFSummary(
            total_folds=len(folds),
            valid_folds=len(valid),
            skipped_folds=len(skipped),
        )

        if not valid:
            return summary

        summary.avg_train_pf = round(sum(f.train_pf for f in valid) / len(valid), 2)
        summary.avg_test_pf = round(sum(f.test_pf for f in valid) / len(valid), 2)
        summary.avg_train_wr = round(sum(f.train_wr for f in valid) / len(valid), 1)
        summary.avg_test_wr = round(sum(f.test_wr for f in valid) / len(valid), 1)
        summary.avg_degradation = round(sum(f.degradation for f in valid) / len(valid), 2)
        summary.max_test_dd = round(max(f.test_dd for f in valid), 1)
        summary.total_test_pnl = round(sum(f.test_pnl for f in valid), 2)
        summary.total_test_trades = sum(f.test_trades for f in valid)

        # Consistency: % of folds where OOS is profitable (PF > 1.0)
        profitable_folds = sum(1 for f in valid if f.test_pf > 1.0)
        summary.consistency_pct = round(profitable_folds / len(valid) * 100, 1)

        # Regime breaks: folds where OOS PF < 1.0
        summary.regime_breaks = sum(1 for f in valid if f.test_pf < 1.0)

        return summary

    def _compare_baseline(self, summary: WFSummary) -> None:
        """Compare aggregate OOS metrics against config/backtest_baseline.json."""
        baseline_path = project_dir / "config" / "backtest_baseline.json"
        if not baseline_path.exists():
            summary.baseline_reasons.append("No baseline file found — skipping comparison")
            return

        with open(baseline_path) as f:
            baseline = json.load(f)

        reasons = []
        all_pass = True

        # 1. Aggregate OOS profit factor >= 1.5
        if summary.avg_test_pf >= 1.5:
            reasons.append(f"OOS PF {summary.avg_test_pf:.2f} >= 1.5 threshold: PASS")
        else:
            reasons.append(f"OOS PF {summary.avg_test_pf:.2f} < 1.5 threshold: FAIL")
            all_pass = False

        # 2. OOS consistency >= 60%
        if summary.consistency_pct >= 60.0:
            reasons.append(f"Consistency {summary.consistency_pct:.0f}% >= 60%: PASS")
        else:
            reasons.append(f"Consistency {summary.consistency_pct:.0f}% < 60%: FAIL")
            all_pass = False

        # 3. Max OOS drawdown per fold <= 5%
        if summary.max_test_dd <= 5.0:
            reasons.append(f"Max OOS DD {summary.max_test_dd:.1f}% <= 5.0%: PASS")
        else:
            reasons.append(f"Max OOS DD {summary.max_test_dd:.1f}% > 5.0%: FAIL")
            all_pass = False

        # 4. Degradation ratio >= 0.6
        if summary.avg_degradation >= 0.6:
            reasons.append(f"Degradation ratio {summary.avg_degradation:.2f} >= 0.6: PASS")
        else:
            reasons.append(f"Degradation ratio {summary.avg_degradation:.2f} < 0.6: FAIL")
            all_pass = False

        summary.baseline_pass = all_pass
        summary.baseline_reasons = reasons

    # ── Analysis Methods ──

    def compute_degradation(self, folds: List[FoldResult]) -> List[Dict]:
        """Ratio of OOS profit factor to in-sample PF per fold."""
        results = []
        for f in folds:
            if f.skipped:
                continue
            results.append({
                "fold_id": f.fold_id,
                "train_pf": f.train_pf,
                "test_pf": f.test_pf,
                "degradation": f.degradation,
                "edge_retained": f"{f.degradation * 100:.0f}%",
            })
        return results

    def compute_consistency(self, folds: List[FoldResult]) -> float:
        """Percentage of folds where OOS is profitable (PF > 1.0)."""
        valid = [f for f in folds if not f.skipped]
        if not valid:
            return 0.0
        profitable = sum(1 for f in valid if f.test_pf > 1.0)
        return round(profitable / len(valid) * 100, 1)

    def detect_regime_breaks(self, folds: List[FoldResult]) -> List[Dict]:
        """Flag folds where OOS PF drops below 1.0."""
        breaks = []
        for f in folds:
            if f.skipped:
                continue
            if f.test_pf < 1.0:
                breaks.append({
                    "fold_id": f.fold_id,
                    "test_period": f"{f.test_start} to {f.test_end}",
                    "test_pf": f.test_pf,
                    "test_pnl": f.test_pnl,
                    "degradation": f.degradation,
                })
        return breaks

    def parameter_stability(self, folds: List[FoldResult]) -> List[Dict]:
        """Check if optimal params drift across folds (grid search only)."""
        if not self.parameter_grid:
            return []
        results = []
        for f in folds:
            if f.skipped or not f.best_params:
                continue
            results.append({
                "fold_id": f.fold_id,
                "best_params": f.best_params,
            })
        return results


def main():
    parser = argparse.ArgumentParser(
        description="Walk-Forward Optimization — Rolling Train/Test Validation"
    )
    parser.add_argument(
        "--tv", action="store_true",
        help="Use TradingView data from data/tradingview/"
    )
    parser.add_argument(
        "--data-dir", type=str, default=None,
        help="Custom data directory (overrides --tv)"
    )
    parser.add_argument(
        "--train-months", type=int, default=3,
        help="Training window size in months (default: 3)"
    )
    parser.add_argument(
        "--test-months", type=int, default=1,
        help="Out-of-sample test window in months (default: 1)"
    )
    parser.add_argument(
        "--step-months", type=int, default=1,
        help="Slide forward by this many months each fold (default: 1)"
    )
    parser.add_argument(
        "--min-trades", type=int, default=10,
        help="Minimum trades per fold (default: 10)"
    )
    parser.add_argument(
        "--grid", action="store_true",
        help="Enable parameter grid search (not yet implemented)"
    )
    parser.add_argument(
        "--report", type=str, default=None,
        help="Path to export HTML report (e.g., reports/wf_report.html)"
    )
    parser.add_argument(
        "--json", type=str, default=None,
        help="Path to export JSON report"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # Determine data directory
    if args.data_dir:
        data_dir = args.data_dir
    elif args.tv:
        data_dir = str(project_dir / "data" / "tradingview")
    else:
        print("ERROR: Specify --tv or --data-dir")
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"  WALK-FORWARD OPTIMIZATION")
    print(f"{'=' * 60}")
    print(f"  Data dir:       {data_dir}")
    print(f"  Train window:   {args.train_months} months")
    print(f"  Test window:    {args.test_months} months")
    print(f"  Step:           {args.step_months} months")
    print(f"  Min trades:     {args.min_trades}")
    print(f"  Grid search:    {'ON' if args.grid else 'OFF'}")
    print(f"{'=' * 60}\n")

    # Build engine
    engine = WalkForwardEngine(
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.step_months,
        min_trades_per_fold=args.min_trades,
        parameter_grid={} if args.grid else None,
    )

    # Run
    report = engine.run(data_dir)

    # Print summary
    report.print_summary()

    # Export JSON
    if args.json:
        report.to_json(args.json)
        print(f"  JSON report: {args.json}")

    # Export HTML
    if args.report:
        report.to_html(args.report)
        print(f"  HTML report: {args.report}")

    # Default: also save to reports/ dir
    default_report_dir = REPO_ROOT / "reports"
    default_report_dir.mkdir(parents=True, exist_ok=True)
    default_json = str(default_report_dir / "walk_forward_results.json")
    report.to_json(default_json)
    print(f"  Default JSON: {default_json}")


if __name__ == "__main__":
    main()
