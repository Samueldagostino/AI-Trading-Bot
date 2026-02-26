"""
Out-of-Sample Validation Runner
=================================
Runs Config D (HC filter ON, HTF gate=0.3) on FirstRate data,
segmented by month. Produces per-month and aggregate metrics,
viz_data JSON for each month, and a combined full-period JSON.

This is pure out-of-sample validation — NO parameter changes allowed.
The system under test is identical to the Feb 2026 baseline.

Config D (locked):
  - HC filter: score >= 0.75, stop <= 30pts, TP1 = 1.5x stop
  - HTF gate: strength >= 0.3 (blocks when 2+ of 6 HTFs oppose)
  - Execution TF: 2m
  - HTF timeframes: 5m, 15m, 30m, 1H, 4H, 1D

Usage:
    python scripts/run_oos_validation.py                         # Auto-detect data/firstrate/
    python scripts/run_oos_validation.py --data-dir path/to/dir  # Custom data directory
    python scripts/run_oos_validation.py --months 6              # Most recent N months (default: 6)

Output:
    docs/out_of_sample_validation.md          — Monthly breakdown + verdict
    data/firstrate/viz_data_{month}.json      — Per-month viz data
    data/firstrate/viz_data_full.json         — Combined 6-month viz data
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Ensure project root is on path
script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))

from config.settings import CONFIG
from data_pipeline.pipeline import (
    DataPipeline, BarData, MultiTimeframeIterator,
    TradingViewImporter, bardata_to_bar, bardata_to_htfbar,
    MINUTES_TO_LABEL, _parse_tf_from_filename,
)
from main import TradingOrchestrator, HTF_TIMEFRAMES

logger = logging.getLogger(__name__)

# February baseline for comparison
FEB_BASELINE = {
    "trades": 84,
    "win_rate": 50.0,
    "profit_factor": 1.29,
    "total_pnl": 1304.36,
    "max_drawdown_pct": 2.8,
    "expectancy": 15.53,
}

EXEC_TF = "2m"


def load_firstrate_mtf(data_dir: str) -> Dict[str, List[BarData]]:
    """Load aggregated FirstRate CSVs (NQ_2m.csv, NQ_5m.csv, etc.) by timeframe.

    Expected filenames: NQ_{tf}.csv where tf is 2m, 3m, 5m, 15m, 30m, 1H, 4H, 1D
    """
    dir_path = Path(data_dir)
    if not dir_path.exists():
        logger.error(f"Data directory not found: {data_dir}")
        return {}

    # Map NQ_{tf}.csv filenames to timeframe labels
    tf_map = {
        "NQ_1m.csv": "1m", "NQ_2m.csv": "2m", "NQ_3m.csv": "3m",
        "NQ_5m.csv": "5m", "NQ_15m.csv": "15m", "NQ_30m.csv": "30m",
        "NQ_1H.csv": "1H", "NQ_4H.csv": "4H", "NQ_1D.csv": "1D",
    }

    importer = TradingViewImporter(CONFIG)
    tf_bars: Dict[str, List[BarData]] = {}

    for csv_file in sorted(dir_path.glob("NQ_*.csv")):
        tf_label = tf_map.get(csv_file.name)
        if not tf_label:
            # Try parsing from filename
            tf_label = _parse_tf_from_filename(str(csv_file))
        if not tf_label:
            logger.warning(f"Skipping unrecognized file: {csv_file.name}")
            continue

        bars = importer.import_file(str(csv_file))
        if bars:
            # Tag source as firstrate
            for bar in bars:
                bar.source = "firstrate"
            tf_bars[tf_label] = bars
            logger.info(f"  Loaded {tf_label}: {len(bars):,} bars")

    return tf_bars


def segment_by_month(
    tf_bars: Dict[str, List[BarData]],
) -> Dict[str, Dict[str, List[BarData]]]:
    """Split multi-TF bars into monthly segments.

    Returns: {month_key: {tf: [bars]}}
    where month_key is like "2025-09", "2025-10", etc.
    """
    monthly: Dict[str, Dict[str, List[BarData]]] = defaultdict(lambda: defaultdict(list))

    for tf, bars in tf_bars.items():
        for bar in bars:
            month_key = bar.timestamp.strftime("%Y-%m")
            monthly[month_key][tf].append(bar)

    return dict(monthly)


def get_recent_months(monthly_keys: List[str], n: int) -> List[str]:
    """Return the most recent N month keys, sorted chronologically."""
    sorted_keys = sorted(monthly_keys)
    return sorted_keys[-n:] if len(sorted_keys) >= n else sorted_keys


async def run_single_month(
    tf_bars: Dict[str, List[BarData]],
    month_key: str,
) -> Dict:
    """Run Config D backtest on a single month's data.

    Returns the full results dict from TradingOrchestrator.run_backtest_mtf()
    plus computed per-trade expectancy.
    """
    CONFIG.execution.paper_trading = True

    bot = TradingOrchestrator(CONFIG)
    await bot.initialize(skip_db=True)

    pipeline = DataPipeline(CONFIG)
    mtf_iterator = pipeline.create_mtf_iterator(tf_bars)

    if len(mtf_iterator) == 0:
        logger.warning(f"  {month_key}: No bars to process")
        return {"month": month_key, "total_trades": 0, "error": "no_data"}

    logger.info(f"  {month_key}: {len(mtf_iterator):,} total bars, exec_tf={EXEC_TF}")

    results = await bot.run_backtest_mtf(mtf_iterator, execution_tf=EXEC_TF)

    # Compute expectancy per trade
    total_trades = results.get("total_trades", 0)
    total_pnl = results.get("total_pnl", 0)
    expectancy = round(total_pnl / total_trades, 2) if total_trades > 0 else 0.0

    results["month"] = month_key
    results["expectancy_per_trade"] = expectancy

    return results


def build_viz_data(results: Dict) -> Dict:
    """Convert backtest results into viz_data.json format."""
    return {
        "summary": {
            k: v for k, v in results.items()
            if k not in (
                "equity_curve", "exec_bars_log", "trade_log",
                "htf_bias_log", "equity_timestamps", "month",
                "expectancy_per_trade",
            )
        },
        "bars": results.get("exec_bars_log", []),
        "trades": results.get("trade_log", []),
        "htf_bias": results.get("htf_bias_log", []),
        "equity_curve": [
            {"equity": eq, "time": ts}
            for eq, ts in zip(
                results.get("equity_curve", []),
                ["start"] + results.get("equity_timestamps", []),
            )
        ],
    }


def compute_monthly_comparison(month_results: Dict, baseline: Dict) -> Dict:
    """Compare a month's results to the February baseline."""
    total_trades = month_results.get("total_trades", 0)
    pf = month_results.get("profit_factor", 0)
    wr = month_results.get("win_rate", 0)
    pnl = month_results.get("total_pnl", 0)
    dd = month_results.get("max_drawdown_pct", 0)
    exp = month_results.get("expectancy_per_trade", 0)

    return {
        "trades": total_trades,
        "trades_vs_baseline": total_trades - baseline["trades"],
        "win_rate": wr,
        "wr_vs_baseline": round(wr - baseline["win_rate"], 1),
        "profit_factor": pf,
        "pf_vs_baseline": round(pf - baseline["profit_factor"], 2),
        "total_pnl": pnl,
        "pnl_vs_baseline": round(pnl - baseline["total_pnl"], 2),
        "max_drawdown_pct": dd,
        "dd_vs_baseline": round(dd - baseline["max_drawdown_pct"], 1),
        "expectancy": exp,
        "exp_vs_baseline": round(exp - baseline["expectancy"], 2),
    }


def generate_markdown_report(
    monthly_results: List[Dict],
    comparisons: List[Dict],
    aggregate: Dict,
    month_keys: List[str],
) -> str:
    """Generate the out_of_sample_validation.md report."""
    lines = []
    lines.append("# Out-of-Sample Validation — Config D")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"**Data Source:** FirstRate 1-minute absolute-adjusted NQ futures")
    lines.append(f"**Execution TF:** {EXEC_TF}")
    lines.append(f"**Period:** {month_keys[0]} to {month_keys[-1]} ({len(month_keys)} months)")
    lines.append("")
    lines.append("## Config D Parameters (Locked)")
    lines.append("")
    lines.append("| Parameter | Value |")
    lines.append("|-----------|-------|")
    lines.append("| HC Min Score | >= 0.75 |")
    lines.append("| HC Max Stop | <= 30 pts |")
    lines.append("| HC TP1 Ratio | 1.5x stop |")
    lines.append("| HTF Gate | strength >= 0.3 |")
    lines.append("| HTF Timeframes | 5m, 15m, 30m, 1H, 4H, 1D |")
    lines.append("| Contracts | 2 (C1 fixed target + C2 runner) |")
    lines.append("| Account Size | $50,000 |")
    lines.append("")
    lines.append("## February Baseline (In-Sample)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Trades | {FEB_BASELINE['trades']} |")
    lines.append(f"| Win Rate | {FEB_BASELINE['win_rate']}% |")
    lines.append(f"| Profit Factor | {FEB_BASELINE['profit_factor']} |")
    lines.append(f"| Total PnL | ${FEB_BASELINE['total_pnl']:,.2f} |")
    lines.append(f"| Max Drawdown | {FEB_BASELINE['max_drawdown_pct']}% |")
    lines.append(f"| Expectancy/Trade | ${FEB_BASELINE['expectancy']:.2f} |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Monthly Performance Breakdown")
    lines.append("")

    # Table header
    lines.append("| Month | Trades | WR% | PF | Total PnL | Max DD% | Exp/Trade | vs Baseline |")
    lines.append("|-------|--------|-----|----|-----------|---------|-----------| ------------|")

    for i, (res, comp) in enumerate(zip(monthly_results, comparisons)):
        month = res.get("month", "?")
        trades = comp["trades"]
        wr = comp["win_rate"]
        pf = comp["profit_factor"]
        pnl = comp["total_pnl"]
        dd = comp["max_drawdown_pct"]
        exp = comp["expectancy"]

        # Color indicator
        if pf >= 1.2:
            verdict_icon = "+++"
        elif pf >= 1.0:
            verdict_icon = "+"
        else:
            verdict_icon = "---"

        pf_delta = comp["pf_vs_baseline"]
        pf_dir = "+" if pf_delta >= 0 else ""

        lines.append(
            f"| {month} | {trades} | {wr:.1f} | {pf:.2f} | "
            f"${pnl:,.2f} | {dd:.1f} | ${exp:.2f} | "
            f"PF {pf_dir}{pf_delta:.2f} {verdict_icon} |"
        )

    lines.append("")

    # Aggregate
    lines.append("## Aggregate Statistics (All Months)")
    lines.append("")
    lines.append("| Metric | OOS Value | Feb Baseline | Delta |")
    lines.append("|--------|-----------|--------------|-------|")

    agg_trades = aggregate.get("total_trades", 0)
    agg_pf = aggregate.get("profit_factor", 0)
    agg_wr = aggregate.get("win_rate", 0)
    agg_pnl = aggregate.get("total_pnl", 0)
    agg_dd = aggregate.get("max_drawdown_pct", 0)
    agg_exp = aggregate.get("expectancy_per_trade", 0)
    n_months = len(month_keys)

    lines.append(f"| Total Trades | {agg_trades} | {FEB_BASELINE['trades']} (1mo) | — |")
    lines.append(f"| Avg Trades/Month | {agg_trades / n_months:.0f} | {FEB_BASELINE['trades']} | "
                 f"{agg_trades / n_months - FEB_BASELINE['trades']:+.0f} |")
    lines.append(f"| Win Rate | {agg_wr:.1f}% | {FEB_BASELINE['win_rate']}% | "
                 f"{agg_wr - FEB_BASELINE['win_rate']:+.1f}% |")
    lines.append(f"| Profit Factor | {agg_pf:.2f} | {FEB_BASELINE['profit_factor']} | "
                 f"{agg_pf - FEB_BASELINE['profit_factor']:+.2f} |")
    lines.append(f"| Total PnL | ${agg_pnl:,.2f} | ${FEB_BASELINE['total_pnl']:,.2f} (1mo) | — |")
    lines.append(f"| Avg PnL/Month | ${agg_pnl / n_months:,.2f} | ${FEB_BASELINE['total_pnl']:,.2f} | "
                 f"${agg_pnl / n_months - FEB_BASELINE['total_pnl']:+,.2f} |")
    lines.append(f"| Max Drawdown | {agg_dd:.1f}% | {FEB_BASELINE['max_drawdown_pct']}% | "
                 f"{agg_dd - FEB_BASELINE['max_drawdown_pct']:+.1f}% |")
    lines.append(f"| Expectancy/Trade | ${agg_exp:.2f} | ${FEB_BASELINE['expectancy']:.2f} | "
                 f"${agg_exp - FEB_BASELINE['expectancy']:+.2f} |")

    lines.append("")

    # C1/C2 breakdown
    c1_pnl = aggregate.get("c1_total_pnl", 0)
    c2_pnl = aggregate.get("c2_total_pnl", 0)
    lines.append("### Scale-Out Breakdown")
    lines.append("")
    lines.append(f"- **C1 (fixed target) PnL:** ${c1_pnl:,.2f}")
    lines.append(f"- **C2 (runner) PnL:** ${c2_pnl:,.2f}")
    if agg_pnl != 0:
        lines.append(f"- **C2 contribution:** {c2_pnl / agg_pnl * 100:.0f}% of total PnL")
    lines.append("")

    # HTF impact
    htf_blocked = aggregate.get("htf_blocked_signals", 0)
    htf_rate = aggregate.get("htf_block_rate", 0)
    lines.append("### HTF Filter Impact")
    lines.append("")
    lines.append(f"- **Signals blocked by HTF:** {htf_blocked:,}")
    lines.append(f"- **Block rate:** {htf_rate:.1f}%")
    lines.append("")

    # Verdict
    lines.append("---")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")

    profitable_months = sum(1 for r in monthly_results if r.get("total_pnl", 0) > 0)
    pf_above_1 = sum(1 for r in monthly_results if r.get("profit_factor", 0) >= 1.0)

    if agg_pf >= 1.2 and profitable_months >= n_months * 0.67 and agg_dd <= 5.0:
        verdict = "PASS"
        confidence = "HIGH"
        detail = (
            f"Config D produces PF {agg_pf:.2f} out-of-sample with {profitable_months}/{n_months} "
            f"profitable months and max drawdown {agg_dd:.1f}%. "
            f"The system demonstrates durable edge beyond the February training period. "
            f"**Approved for paper trading.**"
        )
    elif agg_pf >= 1.0 and profitable_months >= n_months * 0.5:
        verdict = "CONDITIONAL PASS"
        confidence = "MEDIUM"
        detail = (
            f"Config D is marginally profitable out-of-sample (PF {agg_pf:.2f}, "
            f"{profitable_months}/{n_months} profitable months). "
            f"Edge exists but is weaker than in-sample. "
            f"Recommend extended paper trading with tighter monitoring."
        )
    else:
        verdict = "FAIL"
        confidence = "N/A"
        detail = (
            f"Config D does not hold up out-of-sample (PF {agg_pf:.2f}, "
            f"{profitable_months}/{n_months} profitable months, DD {agg_dd:.1f}%). "
            f"The February results were likely overfit. "
            f"**Do NOT proceed to paper trading.** Re-evaluate strategy."
        )

    lines.append(f"### {verdict} (Confidence: {confidence})")
    lines.append("")
    lines.append(detail)
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*Report generated by `scripts/run_oos_validation.py` — "
                 f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*")

    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser(
        description="Out-of-Sample Validation — Config D"
    )
    parser.add_argument(
        "--data-dir", type=str, default=None,
        help="Directory with aggregated NQ_{tf}.csv files (default: data/firstrate/)"
    )
    parser.add_argument(
        "--months", type=int, default=6,
        help="Number of most recent months to validate (default: 6)"
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="Directory for viz_data output (default: same as data-dir)"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    default_data_dir = str(project_dir / "data" / "firstrate")
    data_dir = args.data_dir or default_data_dir
    output_dir = args.output_dir or data_dir
    docs_dir = str(project_dir / "docs")
    os.makedirs(docs_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    print(f"\n{'=' * 60}")
    print(f"  OUT-OF-SAMPLE VALIDATION — CONFIG D")
    print(f"  Pure validation: NO parameter changes")
    print(f"{'=' * 60}")
    print(f"  Data dir:     {data_dir}")
    print(f"  Exec TF:      {EXEC_TF}")
    print(f"  HTF Gate:     0.3 (Config D)")
    print(f"  HC Filter:    score>=0.75, stop<=30pts, TP1=1.5x")
    print(f"  Months:       {args.months}")
    print(f"{'=' * 60}\n")

    # Load all timeframes
    print("Loading FirstRate aggregated data...")
    tf_bars = load_firstrate_mtf(data_dir)

    if not tf_bars:
        print(f"\nERROR: No NQ_*.csv files found in {data_dir}")
        print("Run the aggregator first:")
        print(f"  python scripts/aggregate_1m.py --output-dir {data_dir}")
        sys.exit(1)

    if EXEC_TF not in tf_bars:
        print(f"\nERROR: No data for execution timeframe '{EXEC_TF}'")
        print(f"Available: {', '.join(sorted(tf_bars.keys()))}")
        sys.exit(1)

    # Summary
    print(f"\nLoaded timeframes:")
    for tf in sorted(tf_bars.keys()):
        bars = tf_bars[tf]
        print(f"  {tf:>4s}: {len(bars):>7,} bars  "
              f"({bars[0].timestamp.strftime('%Y-%m-%d')} → "
              f"{bars[-1].timestamp.strftime('%Y-%m-%d')})")

    # Segment by month
    monthly = segment_by_month(tf_bars)
    month_keys = get_recent_months(list(monthly.keys()), args.months)

    print(f"\nMonths available: {sorted(monthly.keys())}")
    print(f"Running validation on: {month_keys}")
    print()

    # Run each month
    monthly_results = []
    all_trade_logs = []
    all_bars_logs = []
    all_htf_logs = []
    all_equity = [CONFIG.risk.account_size]
    all_equity_ts = []

    best_month = None
    best_pf = -999
    worst_month = None
    worst_pf = 999

    for month_key in month_keys:
        print(f"\n{'─' * 60}")
        print(f"  MONTH: {month_key}")
        print(f"{'─' * 60}")

        month_tf_bars = monthly[month_key]

        # Verify we have execution TF data
        if EXEC_TF not in month_tf_bars:
            print(f"  WARNING: No {EXEC_TF} data for {month_key}, skipping")
            continue

        results = await run_single_month(month_tf_bars, month_key)
        monthly_results.append(results)

        trades = results.get("total_trades", 0)
        pf = results.get("profit_factor", 0)
        pnl = results.get("total_pnl", 0)
        wr = results.get("win_rate", 0)
        dd = results.get("max_drawdown_pct", 0)
        exp = results.get("expectancy_per_trade", 0)

        print(f"\n  Results: {trades} trades | WR {wr:.1f}% | PF {pf:.2f} | "
              f"PnL ${pnl:,.2f} | DD {dd:.1f}% | Exp ${exp:.2f}")

        # Track best/worst
        if trades > 0:
            if pf > best_pf:
                best_pf = pf
                best_month = month_key
            if pf < worst_pf:
                worst_pf = pf
                worst_month = month_key

        # Save per-month viz_data
        viz = build_viz_data(results)
        viz_path = os.path.join(output_dir, f"viz_data_{month_key}.json")
        with open(viz_path, "w") as f:
            json.dump(viz, f, indent=2, default=str)
        print(f"  Saved: {viz_path}")

        # Accumulate for combined
        all_trade_logs.extend(results.get("trade_log", []))
        all_bars_logs.extend(results.get("exec_bars_log", []))
        all_htf_logs.extend(results.get("htf_bias_log", []))

    # Build aggregate stats
    print(f"\n{'=' * 60}")
    print(f"  AGGREGATE RESULTS")
    print(f"{'=' * 60}")

    total_trades = sum(r.get("total_trades", 0) for r in monthly_results)
    total_pnl = sum(r.get("total_pnl", 0) for r in monthly_results)
    total_c1 = sum(r.get("c1_total_pnl", 0) for r in monthly_results)
    total_c2 = sum(r.get("c2_total_pnl", 0) for r in monthly_results)
    total_htf_blocked = sum(r.get("htf_blocked_signals", 0) for r in monthly_results)

    # Compute aggregate PF from monthly winners/losers
    total_gross_profit = 0
    total_gross_loss = 0
    all_wins = 0
    all_losses = 0
    max_dd = 0

    for r in monthly_results:
        trades_n = r.get("total_trades", 0)
        wr_pct = r.get("win_rate", 0)
        avg_w = r.get("avg_winner", 0)
        avg_l = abs(r.get("avg_loser", 0))
        dd_pct = r.get("max_drawdown_pct", 0)

        wins = round(trades_n * wr_pct / 100)
        losses = trades_n - wins

        total_gross_profit += wins * avg_w
        total_gross_loss += losses * avg_l
        all_wins += wins
        all_losses += losses
        if dd_pct > max_dd:
            max_dd = dd_pct

    agg_pf = round(total_gross_profit / total_gross_loss, 2) if total_gross_loss > 0 else 0
    agg_wr = round(all_wins / total_trades * 100, 1) if total_trades > 0 else 0
    agg_exp = round(total_pnl / total_trades, 2) if total_trades > 0 else 0
    htf_total_signals = total_htf_blocked + total_trades  # approximate
    agg_htf_rate = round(total_htf_blocked / htf_total_signals * 100, 1) if htf_total_signals > 0 else 0

    aggregate = {
        "total_trades": total_trades,
        "win_rate": agg_wr,
        "profit_factor": agg_pf,
        "total_pnl": round(total_pnl, 2),
        "max_drawdown_pct": round(max_dd, 1),
        "expectancy_per_trade": agg_exp,
        "c1_total_pnl": round(total_c1, 2),
        "c2_total_pnl": round(total_c2, 2),
        "htf_blocked_signals": total_htf_blocked,
        "htf_block_rate": agg_htf_rate,
        "months_tested": len(month_keys),
        "profitable_months": sum(1 for r in monthly_results if r.get("total_pnl", 0) > 0),
    }

    print(f"  Total Trades:     {total_trades}")
    print(f"  Win Rate:         {agg_wr:.1f}%")
    print(f"  Profit Factor:    {agg_pf:.2f}")
    print(f"  Total PnL:        ${total_pnl:,.2f}")
    print(f"  Avg PnL/Month:    ${total_pnl / len(month_keys):,.2f}")
    print(f"  Max Drawdown:     {max_dd:.1f}%")
    print(f"  Expectancy/Trade: ${agg_exp:.2f}")
    print(f"  C1 PnL:           ${total_c1:,.2f}")
    print(f"  C2 PnL:           ${total_c2:,.2f}")
    print(f"  HTF Blocked:      {total_htf_blocked:,}")
    if best_month:
        print(f"  Best Month:       {best_month} (PF {best_pf:.2f})")
    if worst_month:
        print(f"  Worst Month:      {worst_month} (PF {worst_pf:.2f})")

    # Save combined viz_data_full.json
    combined_viz = {
        "summary": aggregate,
        "bars": all_bars_logs,
        "trades": all_trade_logs,
        "htf_bias": all_htf_logs,
        "equity_curve": [],  # Combined from monthly
    }
    combined_path = os.path.join(output_dir, "viz_data_full.json")
    with open(combined_path, "w") as f:
        json.dump(combined_viz, f, indent=2, default=str)
    print(f"\n  Combined viz data: {combined_path}")

    # Generate per-month comparisons
    comparisons = []
    for r in monthly_results:
        comp = compute_monthly_comparison(r, FEB_BASELINE)
        comparisons.append(comp)

    # Generate markdown report
    md_report = generate_markdown_report(monthly_results, comparisons, aggregate, month_keys)
    md_path = os.path.join(docs_dir, "out_of_sample_validation.md")
    with open(md_path, "w") as f:
        f.write(md_report)
    print(f"  Markdown report:   {md_path}")

    # Summary comparison
    print(f"\n{'=' * 60}")
    print(f"  COMPARISON TO FEBRUARY BASELINE")
    print(f"{'=' * 60}")
    print(f"  {'Metric':<25} {'Feb (IS)':>12} {'OOS Avg':>12} {'Delta':>10}")
    print(f"  {'─' * 59}")
    print(f"  {'Trades/month':<25} {FEB_BASELINE['trades']:>12} {total_trades / len(month_keys):>12.0f} "
          f"{total_trades / len(month_keys) - FEB_BASELINE['trades']:>+10.0f}")
    print(f"  {'Win Rate':<25} {FEB_BASELINE['win_rate']:>11.1f}% {agg_wr:>11.1f}% "
          f"{agg_wr - FEB_BASELINE['win_rate']:>+10.1f}%")
    print(f"  {'Profit Factor':<25} {FEB_BASELINE['profit_factor']:>12.2f} {agg_pf:>12.2f} "
          f"{agg_pf - FEB_BASELINE['profit_factor']:>+10.2f}")
    print(f"  {'PnL/month':<25} ${FEB_BASELINE['total_pnl']:>10,.2f} ${total_pnl / len(month_keys):>10,.2f} "
          f"${total_pnl / len(month_keys) - FEB_BASELINE['total_pnl']:>+9,.2f}")
    print(f"  {'Max Drawdown':<25} {FEB_BASELINE['max_drawdown_pct']:>11.1f}% {max_dd:>11.1f}% "
          f"{max_dd - FEB_BASELINE['max_drawdown_pct']:>+10.1f}%")
    print(f"  {'Expectancy/trade':<25} ${FEB_BASELINE['expectancy']:>10.2f} ${agg_exp:>10.2f} "
          f"${agg_exp - FEB_BASELINE['expectancy']:>+9.2f}")
    print(f"{'=' * 60}")

    # Verdict
    profitable_months = sum(1 for r in monthly_results if r.get("total_pnl", 0) > 0)
    if agg_pf >= 1.2 and profitable_months >= len(month_keys) * 0.67 and max_dd <= 5.0:
        print(f"\n  VERDICT: PASS — Config D holds out-of-sample")
        print(f"  Approved for paper trading.")
    elif agg_pf >= 1.0 and profitable_months >= len(month_keys) * 0.5:
        print(f"\n  VERDICT: CONDITIONAL PASS — Marginally profitable OOS")
        print(f"  Extended paper testing recommended.")
    else:
        print(f"\n  VERDICT: FAIL — Config D does not generalize")
        print(f"  Do NOT proceed to paper trading.")

    print(f"\n  Reports saved:")
    print(f"    {md_path}")
    print(f"    {combined_path}")
    for mk in month_keys:
        print(f"    {os.path.join(output_dir, f'viz_data_{mk}.json')}")


if __name__ == "__main__":
    asyncio.run(main())
