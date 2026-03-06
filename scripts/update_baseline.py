#!/usr/bin/env python3
"""
Update Backtest Baseline
==========================
Runs the full backtest, extracts metrics, and updates
config/backtest_baseline.json with new numbers.

Creates a timestamped backup before overwriting.

Usage:
    python scripts/update_baseline.py --tv
    python scripts/update_baseline.py --data-dir path/to/data
    python scripts/update_baseline.py --tv --dry-run
"""

import argparse
import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Project paths
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
PROJECT_DIR = REPO_ROOT / "nq_bot_vscode"
BASELINE_PATH = PROJECT_DIR / "config" / "backtest_baseline.json"

sys.path.insert(0, str(PROJECT_DIR))


def load_baseline(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Load existing baseline if it exists."""
    p = path or BASELINE_PATH
    if not p.exists():
        return None
    with open(p) as f:
        return json.load(f)


def create_backup(path: Optional[Path] = None) -> Optional[str]:
    """Create timestamped backup of current baseline.

    Returns the backup path, or None if no baseline exists.
    """
    p = path or BASELINE_PATH
    if not p.exists():
        return None

    timestamp = datetime.now().strftime("%Y%m%d")
    backup_name = f"backtest_baseline_{timestamp}.json"
    backup_path = p.parent / backup_name

    # Avoid overwriting existing backup from same day
    if backup_path.exists():
        counter = 1
        while backup_path.exists():
            backup_name = f"backtest_baseline_{timestamp}_{counter}.json"
            backup_path = p.parent / backup_name
            counter += 1

    shutil.copy2(str(p), str(backup_path))
    return str(backup_path)


async def run_backtest(data_dir: str) -> Dict[str, Any]:
    """Run full multi-TF backtest via TradingOrchestrator."""
    from config.settings import CONFIG
    from data_pipeline.pipeline import DataPipeline, TradingViewImporter, _parse_tf_from_filename
    from main import TradingOrchestrator

    EXEC_TF = "2m"

    CONFIG.execution.paper_trading = True
    bot = TradingOrchestrator(CONFIG)
    await bot.initialize(skip_db=True)

    # Load data
    importer = TradingViewImporter(CONFIG)
    tf_bars = {}
    dir_path = Path(data_dir)
    for pattern in ["*.txt", "*.csv"]:
        for csv_file in sorted(dir_path.glob(pattern)):
            tf_label = _parse_tf_from_filename(str(csv_file))
            if not tf_label:
                name = csv_file.stem
                tf_map = {
                    "NQ_1m": "1m", "NQ_2m": "2m", "NQ_3m": "3m",
                    "NQ_5m": "5m", "NQ_15m": "15m", "NQ_30m": "30m",
                    "NQ_1H": "1H", "NQ_4H": "4H", "NQ_1D": "1D",
                }
                tf_label = tf_map.get(name)
            if not tf_label:
                continue
            bars = importer.import_file(str(csv_file))
            if bars:
                if tf_label in tf_bars:
                    tf_bars[tf_label].extend(bars)
                    tf_bars[tf_label].sort(key=lambda b: b.timestamp)
                else:
                    tf_bars[tf_label] = bars

    if not tf_bars:
        raise RuntimeError(f"No data loaded from {data_dir}")

    pipeline = DataPipeline(CONFIG)
    mtf_iterator = pipeline.create_mtf_iterator(tf_bars)
    if len(mtf_iterator) == 0:
        raise RuntimeError("MTF iterator empty — no bars to process")

    results = await bot.run_backtest_mtf(mtf_iterator, execution_tf=EXEC_TF)
    return results


def build_baseline_from_results(
    results: Dict[str, Any],
    trades_log: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """Build baseline JSON from backtest results."""
    total_trades = results.get("total_trades", 0)
    total_pnl = results.get("total_pnl", 0)
    months_count = results.get("months", 0)

    # Build monthly breakdown if trades_log available
    monthly = []
    if trades_log:
        monthly_data = {}
        for t in trades_log:
            ts = t.get("timestamp", "")
            month = ts[:7]
            if not month:
                continue
            if month not in monthly_data:
                monthly_data[month] = {
                    "trades": 0, "wins": 0, "pnl": 0.0,
                    "gross_profit": 0.0, "gross_loss": 0.0,
                }
            m = monthly_data[month]
            pnl = t.get("total_pnl", 0)
            m["trades"] += 1
            m["pnl"] += pnl
            if pnl > 0:
                m["wins"] += 1
                m["gross_profit"] += pnl
            else:
                m["gross_loss"] += abs(pnl)

        for month_key in sorted(monthly_data.keys()):
            m = monthly_data[month_key]
            wr = (m["wins"] / m["trades"] * 100) if m["trades"] > 0 else 0
            pf = (m["gross_profit"] / m["gross_loss"]) if m["gross_loss"] > 0 else 0
            monthly.append({
                "month": month_key,
                "trades": m["trades"],
                "wr": round(wr, 1),
                "pf": round(pf, 2),
                "pnl": round(m["pnl"]),
            })
        months_count = len(monthly_data)

    baseline = {
        "_comment": (
            f"Backtest baseline — updated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. "
            f"Config D + Variant C + Sweep Detector + Calibrated Slippage."
        ),
        "profit_factor": results.get("profit_factor", 0),
        "win_rate_pct": results.get("win_rate", results.get("win_rate_pct", 0)),
        "trades_per_month": round(total_trades / max(months_count, 1)),
        "expectancy_per_trade": results.get("expectancy_per_trade",
                                            results.get("expectancy", 0)),
        "max_drawdown_pct": results.get("max_drawdown_pct", 0),
        "total_pnl": round(total_pnl, 2),
        "c1_pnl": round(results.get("c1_pnl", 0), 2),
        "c2_pnl": round(results.get("c2_pnl", 0), 2),
        "total_trades": total_trades,
        "months": months_count,
        "account_size": 50000,
        "avg_slippage_pts": results.get("avg_slippage_pts",
                                        results.get("avg_slippage_per_fill", 0)),
        "hc_filter": {
            "min_score": 0.75,
            "max_stop_pts": 30.0,
        },
    }

    if monthly:
        baseline["monthly"] = monthly

    return baseline


def print_diff(old: Optional[Dict], new: Dict) -> None:
    """Print git-friendly diff between old and new baseline."""
    if old is None:
        print("  No previous baseline — creating new baseline.")
        return

    print(f"\n  {'Metric':<25} {'Old':>12} {'New':>12} {'Delta':>12}")
    print(f"  {'─' * 63}")

    compare_keys = [
        ("profit_factor", ".2f"),
        ("win_rate_pct", ".1f"),
        ("total_trades", "d"),
        ("trades_per_month", "d"),
        ("expectancy_per_trade", ".2f"),
        ("max_drawdown_pct", ".1f"),
        ("total_pnl", ",.2f"),
        ("c1_pnl", ",.2f"),
        ("c2_pnl", ",.2f"),
    ]

    for key, fmt in compare_keys:
        old_val = old.get(key, 0)
        new_val = new.get(key, 0)
        delta = new_val - old_val
        print(f"  {key:<25} {old_val:>{12}{fmt}} {new_val:>{12}{fmt}} {delta:>{12}{fmt}}")


async def update_baseline(
    data_dir: str, dry_run: bool = False
) -> Dict[str, Any]:
    """Run backtest and update baseline."""

    print(f"\n{'=' * 60}")
    print(f"  BASELINE UPDATE")
    print(f"{'=' * 60}")
    print(f"  Data directory:  {data_dir}")
    print(f"  Baseline:        {BASELINE_PATH}")
    print(f"  Dry run:         {dry_run}")
    print(f"{'=' * 60}")

    # 1. Run backtest
    print(f"\n  [1/4] Running full backtest...")
    results = await run_backtest(data_dir)
    print(f"  Backtest complete: {results.get('total_trades', 0)} trades")

    # 2. Build new baseline
    print(f"\n  [2/4] Building new baseline...")
    new_baseline = build_baseline_from_results(results)

    # 3. Load old baseline and show diff
    print(f"\n  [3/4] Comparing with existing baseline...")
    old_baseline = load_baseline()
    print_diff(old_baseline, new_baseline)

    if dry_run:
        print(f"\n  DRY RUN — no changes written.")
        print(f"\n  New baseline would be:")
        print(json.dumps(new_baseline, indent=4))
        return new_baseline

    # 4. Backup and write
    print(f"\n  [4/4] Writing new baseline...")
    backup_path = create_backup()
    if backup_path:
        print(f"  Backup created: {backup_path}")

    with open(BASELINE_PATH, "w") as f:
        json.dump(new_baseline, f, indent=4)
        f.write("\n")

    print(f"  Baseline updated: {BASELINE_PATH}")
    print(f"{'=' * 60}\n")

    return new_baseline


def main():
    parser = argparse.ArgumentParser(
        description="Update Backtest Baseline — Run backtest and save new baseline"
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
        "--dry-run", action="store_true",
        help="Show diff without writing changes"
    )
    args = parser.parse_args()

    if args.data_dir:
        data_dir = args.data_dir
    elif args.tv:
        data_dir = str(PROJECT_DIR / "data" / "tradingview")
    else:
        print("ERROR: Specify --tv or --data-dir")
        sys.exit(1)

    asyncio.run(update_baseline(data_dir, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
