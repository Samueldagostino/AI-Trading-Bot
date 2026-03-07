#!/usr/bin/env python3
"""
Post-Fix Backtest Validation
==============================
Runs full backtest + walk-forward after bug fixes (VWAP session reset,
size multiplier fix, sweep stop validation, MTF iterator sorting) and
compares against the existing baseline.

Usage:
    python scripts/run_post_fix_validation.py --tv
    python scripts/run_post_fix_validation.py --data-dir path/to/data
    python scripts/run_post_fix_validation.py --tv --json results.json
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Project paths
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
PROJECT_DIR = REPO_ROOT / "nq_bot_vscode"
BASELINE_PATH = PROJECT_DIR / "config" / "backtest_baseline.json"

sys.path.insert(0, str(PROJECT_DIR))


# ── Color helpers (ANSI) ──

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def color_delta(value: float, higher_is_better: bool = True) -> str:
    """Color a delta value: green for improvement, red for degradation."""
    if value == 0:
        return f"{YELLOW}{value:+.2f}{RESET}"
    if (value > 0) == higher_is_better:
        return f"{GREEN}{value:+.2f}{RESET}"
    return f"{RED}{value:+.2f}{RESET}"


def color_pct_delta(value: float, higher_is_better: bool = True) -> str:
    """Color a percentage delta."""
    if value == 0:
        return f"{YELLOW}{value:+.1f}%{RESET}"
    if (value > 0) == higher_is_better:
        return f"{GREEN}{value:+.1f}%{RESET}"
    return f"{RED}{value:+.1f}%{RESET}"


# ── Baseline operations ──

def load_baseline(path: Optional[str] = None) -> Dict[str, Any]:
    """Load baseline from config/backtest_baseline.json."""
    p = Path(path) if path else BASELINE_PATH
    if not p.exists():
        raise FileNotFoundError(f"Baseline not found: {p}")
    with open(p) as f:
        return json.load(f)


def extract_metrics(results: Dict[str, Any]) -> Dict[str, float]:
    """Extract comparison metrics from backtest results dict."""
    return {
        "total_trades": results.get("total_trades", 0),
        "win_rate_pct": results.get("win_rate", results.get("win_rate_pct", 0)),
        "profit_factor": results.get("profit_factor", 0),
        "total_pnl": results.get("total_pnl", 0),
        "max_drawdown_pct": results.get("max_drawdown_pct", 0),
        "expectancy_per_trade": results.get("expectancy_per_trade",
                                            results.get("expectancy", 0)),
    }


def extract_baseline_metrics(baseline: Dict[str, Any]) -> Dict[str, float]:
    """Extract comparison metrics from baseline format."""
    return {
        "total_trades": baseline.get("total_trades", 0),
        "win_rate_pct": baseline.get("win_rate_pct", 0),
        "profit_factor": baseline.get("profit_factor", 0),
        "total_pnl": baseline.get("total_pnl", 0),
        "max_drawdown_pct": baseline.get("max_drawdown_pct", 0),
        "expectancy_per_trade": baseline.get("expectancy_per_trade", 0),
    }


# ── Comparison logic ──

METRIC_CONFIG = {
    "total_trades": {"label": "Total Trades", "fmt": "d", "higher_is_better": True},
    "win_rate_pct": {"label": "Win Rate (%)", "fmt": ".1f", "higher_is_better": True},
    "profit_factor": {"label": "Profit Factor", "fmt": ".2f", "higher_is_better": True},
    "total_pnl": {"label": "Total PnL ($)", "fmt": ",.2f", "higher_is_better": True},
    "max_drawdown_pct": {"label": "Max Drawdown (%)", "fmt": ".1f", "higher_is_better": False},
    "expectancy_per_trade": {"label": "Expectancy/Trade ($)", "fmt": ".2f", "higher_is_better": True},
}


def compare_metrics(
    old: Dict[str, float], new: Dict[str, float]
) -> List[Dict[str, Any]]:
    """Compare old vs new metrics, return list of comparison rows."""
    rows = []
    for key, cfg in METRIC_CONFIG.items():
        old_val = old.get(key, 0)
        new_val = new.get(key, 0)
        delta = new_val - old_val
        pct_change = ((new_val - old_val) / abs(old_val) * 100) if old_val != 0 else 0

        # Determine if this is an improvement
        hib = cfg["higher_is_better"]
        if delta == 0:
            status = "unchanged"
        elif (delta > 0) == hib:
            status = "improved"
        else:
            status = "degraded"

        rows.append({
            "metric": key,
            "label": cfg["label"],
            "old_value": old_val,
            "new_value": new_val,
            "delta": delta,
            "pct_change": pct_change,
            "status": status,
            "higher_is_better": hib,
        })
    return rows


def print_comparison_table(rows: List[Dict[str, Any]]) -> None:
    """Print side-by-side comparison table with color coding."""
    print(f"\n{BOLD}{'─' * 90}{RESET}")
    print(f"{BOLD}  {'Metric':<22} {'Baseline':>12} {'New':>12} {'Delta':>14} {'% Change':>12}  Status{RESET}")
    print(f"{BOLD}{'─' * 90}{RESET}")

    for row in rows:
        cfg = METRIC_CONFIG[row["metric"]]
        fmt = cfg["fmt"]
        old_str = f"{row['old_value']:{fmt}}"
        new_str = f"{row['new_value']:{fmt}}"

        delta = row["delta"]
        hib = row["higher_is_better"]
        delta_str = color_delta(delta, hib)
        pct_str = color_pct_delta(row["pct_change"], hib)

        if row["status"] == "improved":
            status_str = f"{GREEN}IMPROVED{RESET}"
        elif row["status"] == "degraded":
            status_str = f"{RED}DEGRADED{RESET}"
        else:
            status_str = f"{YELLOW}UNCHANGED{RESET}"

        print(f"  {row['label']:<22} {old_str:>12} {new_str:>12} {delta_str:>24} {pct_str:>22}  {status_str}")

    print(f"{BOLD}{'─' * 90}{RESET}")


def compute_verdict(rows: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    """Determine overall PASS/FAIL and list reasons."""
    reasons = []
    all_pass = True

    for row in rows:
        if row["status"] == "degraded":
            # Critical thresholds
            if row["metric"] == "profit_factor" and abs(row["pct_change"]) > 10:
                reasons.append(
                    f"FAIL: Profit factor dropped {row['pct_change']:.1f}% "
                    f"(>{10}% threshold)"
                )
                all_pass = False
            elif row["metric"] == "max_drawdown_pct" and abs(row["pct_change"]) > 20:
                reasons.append(
                    f"FAIL: Max drawdown increased {row['pct_change']:.1f}% "
                    f"(>{20}% threshold)"
                )
                all_pass = False
            elif row["metric"] == "total_pnl" and row["delta"] < 0:
                reasons.append(
                    f"WARNING: Total PnL decreased by ${abs(row['delta']):,.2f}"
                )
        elif row["status"] == "improved":
            reasons.append(
                f"PASS: {row['label']} improved by {row['pct_change']:+.1f}%"
            )

    if all_pass and not any("FAIL" in r for r in reasons):
        reasons.insert(0, "PASS: All metrics within acceptable thresholds")

    return all_pass, reasons


# ── Walk-Forward verdict ──

def format_wf_verdict(wf_report) -> Tuple[bool, List[str]]:
    """Extract PASS/FAIL and reasons from WalkForwardReport."""
    s = wf_report.summary
    return s.baseline_pass, s.baseline_reasons


# ── Run backtest ──

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


async def run_walk_forward(data_dir: str) -> Any:
    """Run walk-forward validation."""
    # Import here to avoid circular imports
    sys.path.insert(0, str(PROJECT_DIR))
    from scripts.run_walk_forward import WalkForwardEngine

    engine = WalkForwardEngine(
        train_months=3,
        test_months=1,
        step_months=1,
        min_trades_per_fold=10,
    )
    report = engine.run(data_dir)
    return report


# ── Main ──

async def run_validation(data_dir: str, json_output: Optional[str] = None) -> bool:
    """Run full post-fix validation pipeline."""

    print(f"\n{BOLD}{'=' * 70}{RESET}")
    print(f"{BOLD}  POST-FIX BACKTEST VALIDATION{RESET}")
    print(f"{BOLD}{'=' * 70}{RESET}")
    print(f"  Data directory:  {data_dir}")
    print(f"  Baseline:        {BASELINE_PATH}")
    print(f"  Timestamp:       {datetime.now(timezone.utc).isoformat()}")
    print(f"{BOLD}{'=' * 70}{RESET}")

    # 1. Load baseline
    print(f"\n{CYAN}[1/4] Loading baseline...{RESET}")
    baseline = load_baseline()
    baseline_metrics = extract_baseline_metrics(baseline)
    print(f"  Baseline loaded: {baseline.get('total_trades', '?')} trades, "
          f"PF {baseline.get('profit_factor', '?')}")

    # 2. Run full backtest
    print(f"\n{CYAN}[2/4] Running full multi-TF backtest...{RESET}")
    results = await run_backtest(data_dir)
    new_metrics = extract_metrics(results)
    print(f"  Backtest complete: {new_metrics['total_trades']} trades, "
          f"PF {new_metrics['profit_factor']:.2f}")

    # 3. Compare
    print(f"\n{CYAN}[3/4] Comparing against baseline...{RESET}")
    comparison = compare_metrics(baseline_metrics, new_metrics)
    print_comparison_table(comparison)

    backtest_pass, backtest_reasons = compute_verdict(comparison)

    print(f"\n{BOLD}  Backtest Verdict: "
          f"{GREEN}PASS{RESET}" if backtest_pass else
          f"\n{BOLD}  Backtest Verdict: {RED}FAIL{RESET}")
    for r in backtest_reasons:
        print(f"    {r}")

    # 4. Walk-forward
    print(f"\n{CYAN}[4/4] Running walk-forward validation...{RESET}")
    wf_report = await run_walk_forward(data_dir)
    wf_report.print_summary()
    wf_pass, wf_reasons = format_wf_verdict(wf_report)

    # Overall verdict
    overall_pass = backtest_pass and wf_pass
    print(f"\n{BOLD}{'=' * 70}{RESET}")
    if overall_pass:
        print(f"{BOLD}{GREEN}  OVERALL VERDICT: PASS{RESET}")
        print(f"  Backtest and walk-forward validation both passed.")
    else:
        print(f"{BOLD}{RED}  OVERALL VERDICT: FAIL{RESET}")
        if not backtest_pass:
            print(f"  {RED}Backtest regression detected.{RESET}")
        if not wf_pass:
            print(f"  {RED}Walk-forward validation failed.{RESET}")
    print(f"{BOLD}{'=' * 70}{RESET}\n")

    # Export JSON
    if json_output:
        report = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "baseline": baseline_metrics,
            "new_results": new_metrics,
            "comparison": comparison,
            "backtest_pass": backtest_pass,
            "backtest_reasons": backtest_reasons,
            "walk_forward_pass": wf_pass,
            "walk_forward_reasons": wf_reasons,
            "overall_pass": overall_pass,
        }
        os.makedirs(os.path.dirname(json_output) or ".", exist_ok=True)
        with open(json_output, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"  JSON report saved: {json_output}")

    return overall_pass


def main():
    parser = argparse.ArgumentParser(
        description="Post-Fix Backtest Validation — Compare against baseline"
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
        "--json", type=str, default=None,
        help="Path to export JSON comparison report"
    )
    parser.add_argument(
        "--baseline", type=str, default=None,
        help="Path to custom baseline file (default: config/backtest_baseline.json)"
    )
    args = parser.parse_args()

    if args.baseline:
        global BASELINE_PATH
        BASELINE_PATH = Path(args.baseline)

    if args.data_dir:
        data_dir = args.data_dir
    elif args.tv:
        data_dir = str(PROJECT_DIR / "data" / "tradingview")
    else:
        print("ERROR: Specify --tv or --data-dir")
        sys.exit(1)

    passed = asyncio.run(run_validation(data_dir, args.json))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
