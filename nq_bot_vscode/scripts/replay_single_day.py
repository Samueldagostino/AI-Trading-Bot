"""
Single-Week Replay: Feb 23-27, 2026
=====================================
Replays one week of FirstRate 1-minute MNQ data through the FULL
validated pipeline:

  NQFeatureEngine → HTFBiasEngine → RegimeDetector → SweepDetector
  → SignalAggregator → HC Filter (≥0.75) → HTF Gate (≥0.3)
  → RiskEngine (stop ≤30pts) → ScaleOutExecutor (2-contract, Variant C)

Uses the REAL modules from the codebase — no stubs, no reimplementation.

Data:
  Source: data/firstrate/ (aggregated NQ_*.csv files)
  1m bars aggregated to 2m execution bars + all HTF timeframes
  Filtered to Feb 23-27, 2026 window

Output:
  logs/replay_feb23-27.json        — trade-by-trade JSON
  logs/replay_feb23-27_summary.txt — human-readable summary
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# Ensure project root is on path
script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))

from config.settings import CONFIG
from data_pipeline.pipeline import (
    DataPipeline, BarData, MultiTimeframeIterator,
    TradingViewImporter, bardata_to_bar, bardata_to_htfbar,
    MINUTES_TO_LABEL,
)
from main import TradingOrchestrator, HTF_TIMEFRAMES

# Reuse infrastructure from replay_simulator
from scripts.replay_simulator import (
    ReplaySimulator, ReplayState, DynamicSlippageEngine,
    load_firstrate_mtf, filter_by_date,
    bar_to_et, is_within_session, should_be_flat,
    EXEC_TF, DAILY_LOSS_LIMIT,
)

import logging

logger = logging.getLogger(__name__)

# ── Paths ──
LOGS_DIR = project_dir / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
JSON_LOG = LOGS_DIR / "replay_feb23-27.json"
SUMMARY_LOG = LOGS_DIR / "replay_feb23-27_summary.txt"

# ── Replay window ──
REPLAY_START = "2026-02-23"
REPLAY_END = "2026-02-28"  # exclusive upper bound

# ── Baseline (from config/backtest_baseline.json, scaled to 1 week) ──
BASELINE_WEEKLY = {
    "trades_per_week": 254 / 4.33,  # ~58.7 trades/week
    "pnl_per_week": 25581.0 / 26,   # ~983.88/week
    "win_rate": 61.9,
    "profit_factor": 1.73,
    "expectancy": 16.79,
    "max_drawdown_pct": 1.4,
}


async def run_week_replay() -> Dict:
    """Run the single-week replay and return results."""
    t0 = time.time()

    print(f"\n{'=' * 65}")
    print(f"  SINGLE-WEEK REPLAY: Feb 23-27, 2026")
    print(f"  Pipeline: Full validated (Config D + Variant C + Sweep)")
    print(f"  HC Filter: score >= 0.75, stop <= 30pts")
    print(f"  HTF Gate:  strength >= 0.3")
    print(f"  C1 Exit:   Trail from +3pts (2.5pt trail, 12-bar fallback)")
    print(f"  Slippage:  Calibrated v2")
    print(f"{'=' * 65}\n")

    # ── Load data ──
    data_dir = str(project_dir / "data" / "firstrate")
    print("Loading FirstRate multi-timeframe data...")
    tf_bars = load_firstrate_mtf(data_dir)

    if not tf_bars or EXEC_TF not in tf_bars:
        print(f"ERROR: No {EXEC_TF} data found in {data_dir}")
        sys.exit(1)

    # Show raw data summary
    for tf in sorted(tf_bars.keys()):
        bars = tf_bars[tf]
        print(f"  {tf:>4s}: {len(bars):>7,} bars  "
              f"({bars[0].timestamp.strftime('%Y-%m-%d')} → "
              f"{bars[-1].timestamp.strftime('%Y-%m-%d')})")

    # ── Filter to replay window ──
    tf_bars = filter_by_date(tf_bars, REPLAY_START, REPLAY_END)

    if EXEC_TF not in tf_bars:
        print(f"\nERROR: No {EXEC_TF} data in {REPLAY_START} → {REPLAY_END}")
        sys.exit(1)

    exec_count = len(tf_bars.get(EXEC_TF, []))
    exec_bars = tf_bars[EXEC_TF]
    actual_start = exec_bars[0].timestamp.strftime("%Y-%m-%d")
    actual_end = exec_bars[-1].timestamp.strftime("%Y-%m-%d")

    print(f"\nReplay window: {actual_start} → {actual_end}")
    print(f"Execution bars ({EXEC_TF}): {exec_count:,}")

    # Collect days present
    days_present = sorted(set(b.timestamp.strftime("%Y-%m-%d") for b in exec_bars))
    print(f"Trading days: {len(days_present)} ({', '.join(days_present)})")

    if len(days_present) < 5:
        print(f"  NOTE: Data available for {len(days_present)} of 5 requested days")

    # ── Build MTF iterator ──
    pipeline = DataPipeline(CONFIG)
    mtf_iterator = pipeline.create_mtf_iterator(tf_bars)
    print(f"Total bars (all TFs): {len(mtf_iterator):,}")

    # ── Initialize the ReplaySimulator ──
    # Use existing ReplaySimulator with date range
    sim = ReplaySimulator(
        speed="max",
        start_date=REPLAY_START,
        end_date=REPLAY_END,
        validate=False,
        data_dir=data_dir,
        c1_variant="C",
        quiet=True,
        sweep_enabled=True,
    )
    sim.state = ReplayState()
    sim.bot = TradingOrchestrator(CONFIG)
    sim.bot._sweep_enabled = True
    await sim.bot.initialize(skip_db=True)
    sim._patch_executor_slippage()

    print(f"\nStarting replay...\n")

    # ── Per-day tracking ──
    daily_results: Dict[str, Dict] = {}
    last_date = ""

    for i, (timeframe, bar_data) in enumerate(mtf_iterator):
        sim.state.bars_processed += 1

        if timeframe in HTF_TIMEFRAMES:
            sim.bot.process_htf_bar(timeframe, bar_data)
            sim.state.htf_bars_processed += 1
            continue

        if timeframe != EXEC_TF:
            continue

        # ── Execution bar ──
        sim.state.exec_bars_processed += 1
        sim.state.current_price = bar_data.close
        sim.state.current_time = bar_data.timestamp.isoformat()

        # Feed volume to slippage engine
        sim.slippage_engine.feed_volume(bar_data.volume)
        sim._current_bar_volume = bar_data.volume
        sim._current_bar_time = bar_data.timestamp
        sim._current_et_time = bar_to_et(bar_data.timestamp)

        # Daily reset
        date_str = bar_data.timestamp.strftime("%Y-%m-%d")
        if date_str != last_date:
            # Save previous day's stats
            if last_date and last_date in daily_results:
                daily_results[last_date]["end_equity"] = sim.state.equity

            # Flatten at day boundary
            if sim.bot.executor.has_active_trade and last_date:
                result = await sim.bot.executor.emergency_flatten(bar_data.close)
                if result:
                    sim._record_trade_result(result, bar_data.timestamp)
                    sim.state.session_flattens += 1

            sim.state.reset_daily(date_str)
            last_date = date_str

            # Reset risk engine daily state
            risk_state = sim.bot.risk_engine.state
            risk_state.daily_pnl = 0.0
            risk_state.daily_trades = 0
            risk_state.daily_wins = 0
            risk_state.daily_losses = 0
            risk_state.daily_limit_hit = False
            risk_state.consecutive_losses = 0
            risk_state.consecutive_wins = 0
            risk_state.kill_switch_active = False
            risk_state.kill_switch_reason = ""
            risk_state.kill_switch_resume_at = None

            # Initialize daily tracking
            daily_results[date_str] = {
                "date": date_str,
                "day_of_week": bar_data.timestamp.strftime("%A"),
                "start_equity": sim.state.equity,
                "end_equity": sim.state.equity,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "pnl": 0.0,
                "c1_pnl": 0.0,
                "c2_pnl": 0.0,
                "trade_details": [],
            }

        # Session rules
        et_time = bar_to_et(bar_data.timestamp)
        if not is_within_session(et_time):
            sim.state.session_blocks += 1
            continue

        if sim.state.daily_loss_limit_hit:
            continue

        if should_be_flat(et_time):
            if sim.bot.executor.has_active_trade:
                result = await sim.bot.executor.emergency_flatten(bar_data.close)
                if result:
                    sim._record_trade_result(result, bar_data.timestamp)
                    sim.state.session_flattens += 1
            continue

        # ── Process through full pipeline ──
        exec_bar = bardata_to_bar(bar_data)
        result = await sim.bot.process_bar(exec_bar)

        if result:
            sim._handle_result(result, bar_data.timestamp)

    # ── Final flatten ──
    if sim.bot.executor.has_active_trade:
        last_bar = tf_bars[EXEC_TF][-1]
        result = await sim.bot.executor.emergency_flatten(last_bar.close)
        if result:
            sim._record_trade_result(result, last_bar.timestamp)

    # Update last day's end equity
    if last_date and last_date in daily_results:
        daily_results[last_date]["end_equity"] = sim.state.equity

    elapsed = time.time() - t0

    # ── Assign trades to days ──
    for trade in sim.state.trades_log:
        ts = trade.get("timestamp", "")
        trade_date = ts[:10] if ts else ""
        if trade_date in daily_results:
            day = daily_results[trade_date]
            day["trades"] += 1
            pnl = trade.get("total_pnl", 0)
            day["pnl"] += pnl
            day["c1_pnl"] += trade.get("c1_pnl", 0)
            day["c2_pnl"] += trade.get("c2_pnl", 0)
            if pnl > 0:
                day["wins"] += 1
            elif pnl < 0:
                day["losses"] += 1
            day["trade_details"].append(trade)

    # ── Build comprehensive results ──
    results = {
        "replay_window": {
            "requested": f"{REPLAY_START} → 2026-02-27",
            "actual": f"{actual_start} → {actual_end}",
            "days_available": len(days_present),
            "days": days_present,
        },
        "weekly_summary": {
            "total_trades": sim.state.total_trades,
            "wins": sim.state.total_wins,
            "losses": sim.state.total_losses,
            "win_rate": round(sim.state.win_rate, 1),
            "profit_factor": round(sim.state.profit_factor, 2),
            "total_pnl": round(sim.state.total_pnl, 2),
            "expectancy": round(sim.state.expectancy, 2),
            "c1_pnl": round(sim.state.c1_pnl, 2),
            "c2_pnl": round(sim.state.c2_pnl, 2),
            "max_drawdown_pct": round(sim.state.max_drawdown_pct, 2),
            "final_equity": round(sim.state.equity, 2),
        },
        "pipeline_stats": {
            "exec_bars": sim.state.exec_bars_processed,
            "htf_bars": sim.state.htf_bars_processed,
            "session_blocks": sim.state.session_blocks,
            "session_flattens": sim.state.session_flattens,
            "elapsed_seconds": round(elapsed, 1),
        },
        "slippage": sim.slippage_engine.summary(),
        "slippage_cost_total": round(sim.state.total_slippage_cost, 2),
        "sweep_stats": {
            "sweep_trades": sim.state.sweep_trades,
            "sweep_wins": sim.state.sweep_wins,
            "sweep_pnl": round(sim.state.sweep_pnl, 2),
            "confluence_trades": sim.state.confluence_trades,
            "confluence_wins": sim.state.confluence_wins,
            "confluence_pnl": round(sim.state.confluence_pnl, 2),
            "signal_only_trades": sim.state.signal_only_trades,
            "signal_only_wins": sim.state.signal_only_wins,
            "signal_only_pnl": round(sim.state.signal_only_pnl, 2),
        },
        "daily_summaries": {d: {k: v for k, v in day.items() if k != "trade_details"}
                           for d, day in sorted(daily_results.items())},
        "trades": sim.state.trades_log,
    }

    # ── Save JSON ──
    with open(str(JSON_LOG), "w") as f:
        json.dump(results, f, indent=2, default=str)

    # ── Generate text summary ──
    summary_text = generate_summary(results, daily_results, sim)

    with open(str(SUMMARY_LOG), "w") as f:
        f.write(summary_text)

    # ── Print to console ──
    print(summary_text)

    print(f"\n  Logs saved:")
    print(f"    {JSON_LOG}")
    print(f"    {SUMMARY_LOG}")

    return results


def generate_summary(results: Dict, daily_results: Dict, sim) -> str:
    """Generate the human-readable summary report."""
    lines = []
    ws = results["weekly_summary"]
    ps = results["pipeline_stats"]
    ss = results["sweep_stats"]

    lines.append("=" * 65)
    lines.append("  SINGLE-WEEK REPLAY RESULTS: Feb 23-27, 2026")
    lines.append("  Config D + Variant C (Trail from Profit) + Sweep Detector")
    lines.append("  Slippage: Calibrated v2")
    lines.append("=" * 65)

    lines.append(f"\n  Replay Window: {results['replay_window']['actual']}")
    lines.append(f"  Trading Days:  {results['replay_window']['days_available']}")

    # ── Trade-by-Trade Report ──
    lines.append(f"\n{'─' * 65}")
    lines.append("  TRADE-BY-TRADE REPORT")
    lines.append(f"{'─' * 65}")

    trades = results["trades"]
    if trades:
        lines.append(f"  {'#':>3}  {'Time':^19}  {'Dir':^5}  {'Entry':>10}  "
                      f"{'PnL':>9}  {'C1':>8}  {'C2':>8}  {'Source':^10}")
        lines.append(f"  {'─' * 3}  {'─' * 19}  {'─' * 5}  {'─' * 10}  "
                      f"{'─' * 9}  {'─' * 8}  {'─' * 8}  {'─' * 10}")

        for i, t in enumerate(trades, 1):
            ts = t.get("timestamp", "")[:19]
            direction = t.get("direction", "?")
            entry = t.get("entry_price", 0)
            pnl = t.get("total_pnl", 0)
            c1 = t.get("c1_pnl", 0)
            c2 = t.get("c2_pnl", 0)
            source = t.get("signal_source", "signal")

            pnl_str = f"${pnl:+.2f}"
            c1_str = f"${c1:+.2f}"
            c2_str = f"${c2:+.2f}"

            lines.append(f"  {i:>3}  {ts}  {direction:^5}  "
                          f"{entry:>10.2f}  {pnl_str:>9}  "
                          f"{c1_str:>8}  {c2_str:>8}  {source:^10}")
    else:
        lines.append("  No trades executed.")

    # ── Daily Summaries ──
    lines.append(f"\n{'─' * 65}")
    lines.append("  DAILY SUMMARIES")
    lines.append(f"{'─' * 65}")

    for date_str in sorted(daily_results.keys()):
        day = daily_results[date_str]
        day_trades = day["trades"]
        day_wr = (day["wins"] / day_trades * 100) if day_trades > 0 else 0
        day_pnl = day["pnl"]

        lines.append(f"\n  {date_str} ({day['day_of_week']})")
        lines.append(f"    Trades: {day_trades}  |  W/L: {day['wins']}/{day['losses']}  "
                      f"|  WR: {day_wr:.0f}%")
        lines.append(f"    PnL: ${day_pnl:+.2f}  |  C1: ${day['c1_pnl']:+.2f}  "
                      f"|  C2: ${day['c2_pnl']:+.2f}")

    # ── Weekly Summary ──
    lines.append(f"\n{'─' * 65}")
    lines.append("  WEEKLY SUMMARY")
    lines.append(f"{'─' * 65}")
    lines.append(f"  Total Trades:    {ws['total_trades']}")
    lines.append(f"  Win Rate:        {ws['win_rate']}%")
    pf = ws['profit_factor']
    pf_str = f"{pf:.2f}" if pf < 100 else "inf"
    lines.append(f"  Profit Factor:   {pf_str}")
    lines.append(f"  Total PnL:       ${ws['total_pnl']:+,.2f}")
    lines.append(f"  Expectancy:      ${ws['expectancy']:+.2f}/trade")
    lines.append(f"  C1 PnL:          ${ws['c1_pnl']:+,.2f}")
    lines.append(f"  C2 PnL:          ${ws['c2_pnl']:+,.2f}")
    lines.append(f"  Max Drawdown:    {ws['max_drawdown_pct']}%")
    lines.append(f"  Final Equity:    ${ws['final_equity']:,.2f}")

    # ── Sweep Breakdown ──
    lines.append(f"\n  Signal Source Breakdown:")
    lines.append(f"    Signal-only: {ss['signal_only_trades']} trades, "
                  f"${ss['signal_only_pnl']:+.2f}")
    lines.append(f"    Sweep-only:  {ss['sweep_trades']} trades, "
                  f"${ss['sweep_pnl']:+.2f}")
    lines.append(f"    Confluence:  {ss['confluence_trades']} trades, "
                  f"${ss['confluence_pnl']:+.2f}")

    # ── Slippage ──
    slip = results["slippage"]
    lines.append(f"\n  Slippage:")
    lines.append(f"    Total fills:   {slip['total_fills']}")
    lines.append(f"    Avg slippage:  {slip['avg_slippage_per_fill']:.2f} pts/fill")
    lines.append(f"    Total cost:    ${results['slippage_cost_total']:,.2f}")

    # ── Pipeline Stats ──
    lines.append(f"\n  Pipeline:")
    lines.append(f"    Exec bars:     {ps['exec_bars']:,}")
    lines.append(f"    HTF bars:      {ps['htf_bars']:,}")
    lines.append(f"    Session blocks:{ps['session_blocks']:,}")
    lines.append(f"    Flattens:      {ps['session_flattens']}")
    lines.append(f"    Elapsed:       {ps['elapsed_seconds']:.1f}s")

    # ── Comparison to Baseline ──
    lines.append(f"\n{'─' * 65}")
    lines.append("  COMPARISON TO 6-MONTH BASELINE (weekly pro-rata)")
    lines.append(f"{'─' * 65}")

    baseline_trades = BASELINE_WEEKLY["trades_per_week"]
    baseline_pnl = BASELINE_WEEKLY["pnl_per_week"]

    lines.append(f"  {'Metric':<25} {'Replay':>12} {'Baseline':>12} {'Delta':>12}")
    lines.append(f"  {'─' * 25} {'─' * 12} {'─' * 12} {'─' * 12}")

    # Trades
    lines.append(f"  {'Trades':<25} {ws['total_trades']:>12} "
                  f"{baseline_trades:>12.1f} "
                  f"{ws['total_trades'] - baseline_trades:>+12.1f}")

    # PnL
    lines.append(f"  {'PnL':<25} ${ws['total_pnl']:>10,.2f} "
                  f"${baseline_pnl:>10,.2f} "
                  f"${ws['total_pnl'] - baseline_pnl:>+10,.2f}")

    # Win Rate
    lines.append(f"  {'Win Rate':<25} {ws['win_rate']:>11.1f}% "
                  f"{BASELINE_WEEKLY['win_rate']:>11.1f}% "
                  f"{ws['win_rate'] - BASELINE_WEEKLY['win_rate']:>+11.1f}%")

    # PF
    lines.append(f"  {'Profit Factor':<25} {pf_str:>12} "
                  f"{BASELINE_WEEKLY['profit_factor']:>12.2f} "
                  f"{pf - BASELINE_WEEKLY['profit_factor']:>+12.2f}")

    # Expectancy
    lines.append(f"  {'Expectancy':<25} ${ws['expectancy']:>10.2f} "
                  f"${BASELINE_WEEKLY['expectancy']:>10.2f} "
                  f"${ws['expectancy'] - BASELINE_WEEKLY['expectancy']:>+10.2f}")

    lines.append(f"\n{'=' * 65}")

    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    results = asyncio.run(run_week_replay())
