"""
C1 Exit Strategy Experiments -- Optimized Split-Phase Runner
=============================================================
Research script -- does NOT modify production code.

Tests 5 alternate C1 exit strategies across the full 6-month FirstRate
dataset (Sep 2025 - Feb 2026) using Config D (HC ON, HTF gate=0.3).

Architecture:
  Phase 1 -- Run the full backtest ONCE, intercepting every trade entry
            and caching all 2m bar data from entry forward.
  Phase 2 -- For each experiment config, replay exits using cached data.
            No feature computation needed -> ~1000x faster.

Experiments:
  A -- Vary C1 target ratio: 1.0x, 1.25x, 1.5x (current), 1.75x, 2.0x, 2.5x stop
  B -- Time-based C1 exit: 5, 10, 15, 20, 30 bars after entry
  C -- No C1 target: both contracts trail as runners
  D -- Aggressive C1 scalp: 0.5x stop
  E -- Breakeven C1: move stop to BE at 1.0x, exit at 2.0x

Output: docs/c1_exit_research.md
"""

import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))

from config.settings import CONFIG
from data_pipeline.pipeline import (
    DataPipeline, BarData, MultiTimeframeIterator,
    TradingViewImporter, bardata_to_bar, bardata_to_htfbar,
)

logger = logging.getLogger(__name__)

DATA_DIR = str(project_dir / "data" / "firstrate")
DOCS_DIR = str(project_dir / "docs")

# MNQ constants
POINT_VALUE = 2.0          # $2 per point per contract
COMMISSION = 1.50          # Per contract (conservative — real is $1.29)

# C2 trailing config (from settings.py -- do NOT change)
C2_TRAILING_TYPE = "atr"
C2_TRAILING_ATR_MULT = 2.0
C2_TRAILING_FIXED_PTS = 30.0
C2_BE_BUFFER = 1.0        # Entry + 1pt
C2_MAX_TARGET_PTS = 150.0
C2_TIME_STOP_MIN = 120

# February baseline for comparison
FEB_BASELINE = {
    "c1_pnl": -903.78,
    "c2_pnl": 6682.08,
    "total_pnl": 5778.30,
    "pf": 1.15,
    "trades": 748,
    "wr": 46.7,
    "exp": 7.72,
}


# ═══════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════

class TradeCapture:
    """Captured trade entry + subsequent bar data for offline replay."""
    __slots__ = (
        "trade_id", "direction", "entry_price", "entry_time",
        "stop_distance", "initial_stop", "atr_at_entry",
        "c1_target_pts", "signal_score", "regime", "month",
        "bars_after_entry",
    )

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
        if not hasattr(self, "bars_after_entry"):
            self.bars_after_entry = []

    def __repr__(self):
        return (
            f"Trade({self.direction} @{self.entry_price:.2f} "
            f"stop={self.stop_distance:.1f}pts atr={self.atr_at_entry:.1f} "
            f"bars={len(self.bars_after_entry)})"
        )


class BarSnapshot:
    """Minimal bar data for replay."""
    __slots__ = ("timestamp", "open", "high", "low", "close", "volume")

    def __init__(self, timestamp, open_, high, low, close, volume=0):
        self.timestamp = timestamp
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


class ReplayResult:
    """Result of replaying one trade with experimental C1 logic."""
    __slots__ = (
        "c1_pnl", "c2_pnl", "total_pnl", "c1_exit_reason", "c2_exit_reason",
        "direction", "entry_price", "month", "is_win",
    )

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


# ═══════════════════════════════════════════════════════════════════
# PHASE 1: Capture all trade entries from full backtest
# ═══════════════════════════════════════════════════════════════════

def load_data() -> Dict[str, List[BarData]]:
    """Load FirstRate aggregated CSVs."""
    tf_map = {
        "NQ_2m.csv": "2m", "NQ_5m.csv": "5m", "NQ_15m.csv": "15m",
        "NQ_30m.csv": "30m", "NQ_1H.csv": "1H", "NQ_4H.csv": "4H",
        "NQ_1D.csv": "1D",
    }
    importer = TradingViewImporter(CONFIG)
    tf_bars = {}

    for csv_file in sorted(Path(DATA_DIR).glob("NQ_*.csv")):
        tf_label = tf_map.get(csv_file.name)
        if not tf_label:
            continue
        bars = importer.import_file(str(csv_file))
        if bars:
            tf_bars[tf_label] = bars
    return tf_bars


def filter_to_months(tf_bars: Dict[str, List[BarData]], target_months: set) -> Dict[str, List[BarData]]:
    """Filter bar data to only target months (e.g., Sep 2025 - Feb 2026)."""
    filtered = {}
    for tf, bars in tf_bars.items():
        filtered[tf] = [b for b in bars if b.timestamp.strftime("%Y-%m") in target_months]
    return filtered


def build_2m_index(bars_2m: List[BarData]) -> Dict[datetime, int]:
    """Build timestamp -> index lookup for 2m bars."""
    return {b.timestamp: i for i, b in enumerate(bars_2m)}


async def run_baseline_and_capture(
    tf_bars_full: Dict[str, List[BarData]],
    month_keys: List[str],
) -> Tuple[List[TradeCapture], List[BarSnapshot], Dict]:
    """
    Run the full backtest with production Config D, month by month.
    Each month gets a fresh orchestrator (matching OOS validation behavior).
    Intercept every trade entry and capture metadata + subsequent bars.
    """
    from main import TradingOrchestrator

    CONFIG.execution.paper_trading = True

    all_captures: List[TradeCapture] = []
    all_2m_bars: List[BarSnapshot] = []
    bar_index: Dict[datetime, int] = {}
    baseline_stats = {}

    for month_key in month_keys:
        # Filter data to this month
        month_tf_bars = filter_to_months(tf_bars_full, {month_key})
        if "2m" not in month_tf_bars or not month_tf_bars["2m"]:
            print(f"    {month_key}: no 2m data, skipping")
            continue

        # Fresh orchestrator per month (risk engine resets)
        bot = TradingOrchestrator(CONFIG)
        await bot.initialize(skip_db=True)

        pipeline = DataPipeline(CONFIG)
        mtf_iterator = pipeline.create_mtf_iterator(month_tf_bars)

        # Track current bar timestamp
        current_bar_ts = [None]
        month_captures: List[TradeCapture] = []

        # Monkey-patch to capture entries
        original_enter = bot.executor.enter_trade

        async def capturing_enter(direction, entry_price, stop_distance, atr,
                                  signal_score=0.0, regime="unknown",
                                  c1_target_override=0.0,
                                  _captures=month_captures, _ts=current_bar_ts):
            trade = await original_enter(
                direction=direction, entry_price=entry_price,
                stop_distance=stop_distance, atr=atr,
                signal_score=signal_score, regime=regime,
                c1_target_override=c1_target_override,
            )
            if trade:
                bar_ts = _ts[0]
                cap = TradeCapture(
                    trade_id=trade.trade_id,
                    direction=direction,
                    entry_price=entry_price,
                    entry_time=bar_ts,
                    stop_distance=stop_distance,
                    initial_stop=trade.initial_stop,
                    atr_at_entry=atr,
                    c1_target_pts=c1_target_override if c1_target_override > 0 else stop_distance * 1.5,
                    signal_score=signal_score,
                    regime=regime,
                    month=bar_ts.strftime("%Y-%m") if bar_ts else month_key,
                    bars_after_entry=[],
                )
                _captures.append(cap)
            return trade

        bot.executor.enter_trade = capturing_enter

        # Process all bars for this month
        execution_tf = "2m"
        htf_tfs = {"5m", "15m", "30m", "1H", "4H", "1D"}
        month_bars: List[BarSnapshot] = []
        month_bar_index: Dict[datetime, int] = {}

        for timeframe, bar_data in mtf_iterator:
            if timeframe in htf_tfs:
                bot.process_htf_bar(timeframe, bar_data)
            elif timeframe == execution_tf:
                exec_bar = bardata_to_bar(bar_data)
                snap = BarSnapshot(
                    exec_bar.timestamp, exec_bar.open, exec_bar.high,
                    exec_bar.low, exec_bar.close,
                    getattr(exec_bar, 'volume', 0),
                )
                local_idx = len(month_bars)
                month_bar_index[exec_bar.timestamp] = local_idx
                month_bars.append(snap)

                current_bar_ts[0] = exec_bar.timestamp
                await bot.process_bar(exec_bar)

        # Link captures to bar slices (within this month's bars)
        for cap in month_captures:
            entry_ts = cap.entry_time
            if entry_ts in month_bar_index:
                start_idx = month_bar_index[entry_ts]
                cap.bars_after_entry = month_bars[start_idx:start_idx + 300]
            else:
                for i, b in enumerate(month_bars):
                    if b.timestamp >= entry_ts:
                        cap.bars_after_entry = month_bars[i:i + 300]
                        break

        # Also add to global index (offset by existing bars)
        offset = len(all_2m_bars)
        for ts, idx in month_bar_index.items():
            bar_index[ts] = offset + idx
        all_2m_bars.extend(month_bars)
        all_captures.extend(month_captures)

        # Collect baseline stats
        stats = bot.executor.get_stats()
        baseline_stats[month_key] = stats

        linked = sum(1 for c in month_captures if len(c.bars_after_entry) > 0)
        print(f"    {month_key}: {len(month_bars):>6,} bars, "
              f"{len(month_captures):>3} trades captured ({linked} linked)")

    return all_captures, all_2m_bars, baseline_stats


# ═══════════════════════════════════════════════════════════════════
# PHASE 2: Replay exits with different C1 strategies
# ═══════════════════════════════════════════════════════════════════

def compute_leg_pnl(entry_price: float, exit_price: float,
                    direction: str, contracts: int = 1) -> float:
    """Compute gross PnL for one leg in dollars."""
    if direction == "long":
        points = exit_price - entry_price
    else:
        points = entry_price - exit_price
    return round(points * POINT_VALUE * contracts, 2)


def replay_trade_standard(cap: TradeCapture, c1_ratio: float) -> Optional[ReplayResult]:
    """
    Replay a trade with a fixed C1 target ratio.
    C1 target = stop_distance × c1_ratio.
    """
    direction = cap.direction
    entry = cap.entry_price
    stop_dist = cap.stop_distance
    atr = cap.atr_at_entry

    # Compute stops and targets
    if direction == "long":
        initial_stop = entry - stop_dist
        c1_target = entry + (stop_dist * c1_ratio)
    else:
        initial_stop = entry + stop_dist
        c1_target = entry - (stop_dist * c1_ratio)

    # State
    c1_open = True
    c2_stop = initial_stop
    c2_best = entry
    c2_trailing = 0.0
    phase = "phase_1"

    c1_exit_price = 0.0
    c1_exit_reason = ""
    c2_exit_price = 0.0
    c2_exit_reason = ""
    entry_time = cap.entry_time

    for bar in cap.bars_after_entry:
        price = bar.close

        if phase == "phase_1":
            # Check stop (both contracts) -- exit at bar close (may gap past stop)
            if direction == "long" and price <= initial_stop:
                c1_exit_price = price
                c2_exit_price = price
                c1_exit_reason = c2_exit_reason = "stop"
                c1_open = False
                break
            elif direction == "short" and price >= initial_stop:
                c1_exit_price = price
                c2_exit_price = price
                c1_exit_reason = c2_exit_reason = "stop"
                c1_open = False
                break

            # Check C1 target
            c1_hit = False
            if direction == "long" and price >= c1_target:
                c1_hit = True
            elif direction == "short" and price <= c1_target:
                c1_hit = True

            if c1_hit:
                c1_exit_price = c1_target
                c1_exit_reason = "target"
                c1_open = False

                # Move C2 to breakeven
                if direction == "long":
                    c2_stop = entry + C2_BE_BUFFER
                else:
                    c2_stop = entry - C2_BE_BUFFER

                c2_best = price
                phase = "running"
                continue

            # Update best
            if direction == "long":
                c2_best = max(c2_best, price)
            else:
                c2_best = min(c2_best, price) if c2_best > 0 else price

        if phase == "running":
            # Update best
            if direction == "long":
                c2_best = max(c2_best, price)
            else:
                c2_best = min(c2_best, price) if c2_best == 0 else min(c2_best, price)

            # Update trailing stop
            if C2_TRAILING_TYPE == "atr":
                trail_dist = atr * C2_TRAILING_ATR_MULT
            else:
                trail_dist = C2_TRAILING_FIXED_PTS

            if direction == "long":
                new_trail = c2_best - trail_dist
                if new_trail > c2_stop:
                    c2_stop = round(new_trail, 2)
                    c2_trailing = c2_stop
            else:
                new_trail = c2_best + trail_dist
                if new_trail < c2_stop or c2_stop == 0:
                    c2_stop = round(new_trail, 2)
                    c2_trailing = c2_stop

            # Check C2 stop
            if direction == "long" and price <= c2_stop:
                c2_exit_price = c2_stop
                c2_exit_reason = "trailing" if c2_trailing > 0 else "breakeven"
                break
            elif direction == "short" and price >= c2_stop:
                c2_exit_price = c2_stop
                c2_exit_reason = "trailing" if c2_trailing > 0 else "breakeven"
                break

            # Check max target
            pts_from_entry = abs(price - entry)
            if pts_from_entry >= C2_MAX_TARGET_PTS:
                c2_exit_price = price
                c2_exit_reason = "max_target"
                break

            # Check time stop
            if entry_time and bar.timestamp:
                elapsed = (bar.timestamp - entry_time).total_seconds() / 60
                if elapsed >= C2_TIME_STOP_MIN:
                    c2_exit_price = price
                    c2_exit_reason = "time_stop"
                    break

    if not c2_exit_reason:
        # Trade didn't close within captured bars -- close at last bar
        last_bar = cap.bars_after_entry[-1] if cap.bars_after_entry else None
        if last_bar:
            if c1_open:
                c1_exit_price = last_bar.close
                c1_exit_reason = "eod"
            c2_exit_price = last_bar.close
            c2_exit_reason = "eod"
        else:
            return None

    c1_gross = compute_leg_pnl(entry, c1_exit_price, direction)
    c2_gross = compute_leg_pnl(entry, c2_exit_price, direction)
    c1_net = c1_gross - COMMISSION
    c2_net = c2_gross - COMMISSION
    total = c1_net + c2_net

    return ReplayResult(
        c1_pnl=c1_net, c2_pnl=c2_net, total_pnl=total,
        c1_exit_reason=c1_exit_reason, c2_exit_reason=c2_exit_reason,
        direction=direction, entry_price=entry,
        month=cap.month, is_win=total > 0,
    )


def replay_trade_time_exit(cap: TradeCapture, exit_bars: int) -> Optional[ReplayResult]:
    """
    Replay with time-based C1 exit.
    After N bars, if C1 is profitable, exit at market.
    Otherwise let normal target/stop handle it.
    """
    direction = cap.direction
    entry = cap.entry_price
    stop_dist = cap.stop_distance
    atr = cap.atr_at_entry

    if direction == "long":
        initial_stop = entry - stop_dist
        c1_target = entry + (stop_dist * 1.5)  # Fallback normal target
    else:
        initial_stop = entry + stop_dist
        c1_target = entry - (stop_dist * 1.5)

    c1_open = True
    c2_stop = initial_stop
    c2_best = entry
    c2_trailing = 0.0
    phase = "phase_1"
    bars_count = 0

    c1_exit_price = 0.0
    c1_exit_reason = ""
    c2_exit_price = 0.0
    c2_exit_reason = ""
    entry_time = cap.entry_time

    for bar in cap.bars_after_entry:
        price = bar.close

        if phase == "phase_1":
            # Check stop
            if direction == "long" and price <= initial_stop:
                c1_exit_price = price
                c2_exit_price = price
                c1_exit_reason = c2_exit_reason = "stop"
                break
            elif direction == "short" and price >= initial_stop:
                c1_exit_price = price
                c2_exit_price = price
                c1_exit_reason = c2_exit_reason = "stop"
                break

            bars_count += 1

            # Time exit: after N bars, if profitable, exit C1
            if bars_count >= exit_bars:
                in_profit = (direction == "long" and price > entry) or \
                            (direction == "short" and price < entry)
                if in_profit:
                    c1_exit_price = price
                    c1_exit_reason = f"time_{exit_bars}bars"
                    c1_open = False
                    if direction == "long":
                        c2_stop = entry + C2_BE_BUFFER
                    else:
                        c2_stop = entry - C2_BE_BUFFER
                    c2_best = price
                    phase = "running"
                    bars_count = 0
                    continue

            # Also check normal C1 target as fallback
            c1_hit = (direction == "long" and price >= c1_target) or \
                     (direction == "short" and price <= c1_target)
            if c1_hit:
                c1_exit_price = c1_target
                c1_exit_reason = "target"
                c1_open = False
                if direction == "long":
                    c2_stop = entry + C2_BE_BUFFER
                else:
                    c2_stop = entry - C2_BE_BUFFER
                c2_best = price
                phase = "running"
                continue

            if direction == "long":
                c2_best = max(c2_best, price)
            else:
                c2_best = min(c2_best, price) if c2_best > 0 else price

        if phase == "running":
            # C2 trailing logic (same as standard)
            if direction == "long":
                c2_best = max(c2_best, price)
            else:
                c2_best = min(c2_best, price) if c2_best == 0 else min(c2_best, price)

            if C2_TRAILING_TYPE == "atr":
                trail_dist = atr * C2_TRAILING_ATR_MULT
            else:
                trail_dist = C2_TRAILING_FIXED_PTS

            if direction == "long":
                new_trail = c2_best - trail_dist
                if new_trail > c2_stop:
                    c2_stop = round(new_trail, 2)
                    c2_trailing = c2_stop
            else:
                new_trail = c2_best + trail_dist
                if new_trail < c2_stop or c2_stop == 0:
                    c2_stop = round(new_trail, 2)
                    c2_trailing = c2_stop

            if direction == "long" and price <= c2_stop:
                c2_exit_price = c2_stop
                c2_exit_reason = "trailing" if c2_trailing > 0 else "breakeven"
                break
            elif direction == "short" and price >= c2_stop:
                c2_exit_price = c2_stop
                c2_exit_reason = "trailing" if c2_trailing > 0 else "breakeven"
                break

            pts_from_entry = abs(price - entry)
            if pts_from_entry >= C2_MAX_TARGET_PTS:
                c2_exit_price = price
                c2_exit_reason = "max_target"
                break

            if entry_time and bar.timestamp:
                elapsed = (bar.timestamp - entry_time).total_seconds() / 60
                if elapsed >= C2_TIME_STOP_MIN:
                    c2_exit_price = price
                    c2_exit_reason = "time_stop"
                    break

    if not c2_exit_reason:
        last_bar = cap.bars_after_entry[-1] if cap.bars_after_entry else None
        if last_bar:
            if c1_open:
                c1_exit_price = last_bar.close
                c1_exit_reason = "eod"
            c2_exit_price = last_bar.close
            c2_exit_reason = "eod"
        else:
            return None

    c1_gross = compute_leg_pnl(entry, c1_exit_price, direction)
    c2_gross = compute_leg_pnl(entry, c2_exit_price, direction)
    c1_net = c1_gross - COMMISSION
    c2_net = c2_gross - COMMISSION
    total = c1_net + c2_net

    return ReplayResult(
        c1_pnl=c1_net, c2_pnl=c2_net, total_pnl=total,
        c1_exit_reason=c1_exit_reason, c2_exit_reason=c2_exit_reason,
        direction=direction, entry_price=entry,
        month=cap.month, is_win=total > 0,
    )


def replay_trade_pure_runner(cap: TradeCapture) -> Optional[ReplayResult]:
    """
    Replay with both contracts trailing (no C1 fixed target).
    C1 moves to BE at 1x stop profit, then trails alongside C2.
    Both close on same trailing stop.
    """
    direction = cap.direction
    entry = cap.entry_price
    stop_dist = cap.stop_distance
    atr = cap.atr_at_entry

    if direction == "long":
        initial_stop = entry - stop_dist
    else:
        initial_stop = entry + stop_dist

    c1_open = True
    c1_at_be = False
    c2_stop = initial_stop
    c1_stop = initial_stop
    c2_best = entry
    c2_trailing = 0.0
    phase = "phase_1"

    c1_exit_price = 0.0
    c1_exit_reason = ""
    c2_exit_price = 0.0
    c2_exit_reason = ""
    entry_time = cap.entry_time

    for bar in cap.bars_after_entry:
        price = bar.close

        if phase == "phase_1":
            # Check stop
            if direction == "long" and price <= initial_stop:
                c1_exit_price = price
                c2_exit_price = price
                c1_exit_reason = c2_exit_reason = "stop"
                break
            elif direction == "short" and price >= initial_stop:
                c1_exit_price = price
                c2_exit_price = price
                c1_exit_reason = c2_exit_reason = "stop"
                break

            # Check if 1x stop in profit -> move to BE and start running
            if direction == "long":
                pts_profit = price - entry
            else:
                pts_profit = entry - price

            if pts_profit >= stop_dist:
                # Move both to BE
                if direction == "long":
                    be_stop = entry + C2_BE_BUFFER
                else:
                    be_stop = entry - C2_BE_BUFFER
                c1_stop = be_stop
                c2_stop = be_stop
                c2_best = price
                c1_at_be = True
                phase = "running"
                continue

            if direction == "long":
                c2_best = max(c2_best, price)
            else:
                c2_best = min(c2_best, price) if c2_best > 0 else price

        if phase == "running":
            # Both trailing
            if direction == "long":
                c2_best = max(c2_best, price)
            else:
                c2_best = min(c2_best, price) if c2_best == 0 else min(c2_best, price)

            if C2_TRAILING_TYPE == "atr":
                trail_dist = atr * C2_TRAILING_ATR_MULT
            else:
                trail_dist = C2_TRAILING_FIXED_PTS

            if direction == "long":
                new_trail = c2_best - trail_dist
                if new_trail > c2_stop:
                    c2_stop = round(new_trail, 2)
                    c1_stop = c2_stop  # Both trail together
                    c2_trailing = c2_stop
            else:
                new_trail = c2_best + trail_dist
                if new_trail < c2_stop or c2_stop == 0:
                    c2_stop = round(new_trail, 2)
                    c1_stop = c2_stop
                    c2_trailing = c2_stop

            # Check trailing stop -- closes both
            if direction == "long" and price <= c2_stop:
                c1_exit_price = c2_stop
                c2_exit_price = c2_stop
                reason = "trailing" if c2_trailing > 0 else "breakeven"
                c1_exit_reason = reason
                c2_exit_reason = reason
                break
            elif direction == "short" and price >= c2_stop:
                c1_exit_price = c2_stop
                c2_exit_price = c2_stop
                reason = "trailing" if c2_trailing > 0 else "breakeven"
                c1_exit_reason = reason
                c2_exit_reason = reason
                break

            pts_from_entry = abs(price - entry)
            if pts_from_entry >= C2_MAX_TARGET_PTS:
                c1_exit_price = price
                c2_exit_price = price
                c1_exit_reason = c2_exit_reason = "max_target"
                break

            if entry_time and bar.timestamp:
                elapsed = (bar.timestamp - entry_time).total_seconds() / 60
                if elapsed >= C2_TIME_STOP_MIN:
                    c1_exit_price = price
                    c2_exit_price = price
                    c1_exit_reason = c2_exit_reason = "time_stop"
                    break

    if not c2_exit_reason:
        last_bar = cap.bars_after_entry[-1] if cap.bars_after_entry else None
        if last_bar:
            c1_exit_price = last_bar.close
            c2_exit_price = last_bar.close
            c1_exit_reason = c2_exit_reason = "eod"
        else:
            return None

    c1_gross = compute_leg_pnl(entry, c1_exit_price, direction)
    c2_gross = compute_leg_pnl(entry, c2_exit_price, direction)
    c1_net = c1_gross - COMMISSION
    c2_net = c2_gross - COMMISSION
    total = c1_net + c2_net

    return ReplayResult(
        c1_pnl=c1_net, c2_pnl=c2_net, total_pnl=total,
        c1_exit_reason=c1_exit_reason, c2_exit_reason=c2_exit_reason,
        direction=direction, entry_price=entry,
        month=cap.month, is_win=total > 0,
    )


def replay_trade_be_step(cap: TradeCapture) -> Optional[ReplayResult]:
    """
    Replay with breakeven-step C1 exit:
    - At 1.0x stop in profit -> move C1 stop to breakeven
    - At 2.0x stop in profit -> exit C1 at market
    """
    direction = cap.direction
    entry = cap.entry_price
    stop_dist = cap.stop_distance
    atr = cap.atr_at_entry

    if direction == "long":
        initial_stop = entry - stop_dist
    else:
        initial_stop = entry + stop_dist

    c1_open = True
    c1_stop = initial_stop
    c2_stop = initial_stop
    c2_best = entry
    c2_trailing = 0.0
    phase = "phase_1"

    c1_exit_price = 0.0
    c1_exit_reason = ""
    c2_exit_price = 0.0
    c2_exit_reason = ""
    entry_time = cap.entry_time

    for bar in cap.bars_after_entry:
        price = bar.close

        if phase == "phase_1":
            # Check C1 stop (may have been moved to BE)
            if direction == "long" and price <= c1_stop:
                # If stop was moved to BE, only C1 stops out
                if c1_stop > initial_stop:
                    c1_exit_price = price  # Exit at bar close, not stop level
                    c1_exit_reason = "breakeven"
                    c1_open = False
                    # C2 stop stays at initial or wherever it is
                    c2_best = price
                    phase = "running"
                    continue
                else:
                    # Initial stop -- both out at bar close
                    c1_exit_price = price
                    c2_exit_price = price
                    c1_exit_reason = c2_exit_reason = "stop"
                    break
            elif direction == "short" and price >= c1_stop:
                if c1_stop < initial_stop:
                    c1_exit_price = price  # Exit at bar close, not stop level
                    c1_exit_reason = "breakeven"
                    c1_open = False
                    c2_best = price
                    phase = "running"
                    continue
                else:
                    c1_exit_price = price
                    c2_exit_price = price
                    c1_exit_reason = c2_exit_reason = "stop"
                    break

            # Calculate profit
            if direction == "long":
                pts_profit = price - entry
            else:
                pts_profit = entry - price

            # Step 1: At 1.0x -> move C1 to BE
            if pts_profit >= stop_dist * 1.0:
                if direction == "long":
                    be_stop = entry + C2_BE_BUFFER
                    if be_stop > c1_stop:
                        c1_stop = round(be_stop, 2)
                        c2_stop = round(be_stop, 2)
                else:
                    be_stop = entry - C2_BE_BUFFER
                    if be_stop < c1_stop:
                        c1_stop = round(be_stop, 2)
                        c2_stop = round(be_stop, 2)

            # Step 2: At 2.0x -> exit C1 at market
            if pts_profit >= stop_dist * 2.0:
                c1_exit_price = price
                c1_exit_reason = "be_step_2x"
                c1_open = False
                c2_best = price
                phase = "running"
                continue

            if direction == "long":
                c2_best = max(c2_best, price)
            else:
                c2_best = min(c2_best, price) if c2_best > 0 else price

        if phase == "running":
            # C2 trailing (same as standard)
            if direction == "long":
                c2_best = max(c2_best, price)
            else:
                c2_best = min(c2_best, price) if c2_best == 0 else min(c2_best, price)

            if C2_TRAILING_TYPE == "atr":
                trail_dist = atr * C2_TRAILING_ATR_MULT
            else:
                trail_dist = C2_TRAILING_FIXED_PTS

            if direction == "long":
                new_trail = c2_best - trail_dist
                if new_trail > c2_stop:
                    c2_stop = round(new_trail, 2)
                    c2_trailing = c2_stop
            else:
                new_trail = c2_best + trail_dist
                if new_trail < c2_stop or c2_stop == 0:
                    c2_stop = round(new_trail, 2)
                    c2_trailing = c2_stop

            if direction == "long" and price <= c2_stop:
                c2_exit_price = c2_stop
                c2_exit_reason = "trailing" if c2_trailing > 0 else "breakeven"
                break
            elif direction == "short" and price >= c2_stop:
                c2_exit_price = c2_stop
                c2_exit_reason = "trailing" if c2_trailing > 0 else "breakeven"
                break

            pts_from_entry = abs(price - entry)
            if pts_from_entry >= C2_MAX_TARGET_PTS:
                c2_exit_price = price
                c2_exit_reason = "max_target"
                break

            if entry_time and bar.timestamp:
                elapsed = (bar.timestamp - entry_time).total_seconds() / 60
                if elapsed >= C2_TIME_STOP_MIN:
                    c2_exit_price = price
                    c2_exit_reason = "time_stop"
                    break

    if not c2_exit_reason:
        last_bar = cap.bars_after_entry[-1] if cap.bars_after_entry else None
        if last_bar:
            if c1_open:
                c1_exit_price = last_bar.close
                c1_exit_reason = "eod"
            c2_exit_price = last_bar.close
            c2_exit_reason = "eod"
        else:
            return None

    c1_gross = compute_leg_pnl(entry, c1_exit_price, direction)
    c2_gross = compute_leg_pnl(entry, c2_exit_price, direction)
    c1_net = c1_gross - COMMISSION
    c2_net = c2_gross - COMMISSION
    total = c1_net + c2_net

    return ReplayResult(
        c1_pnl=c1_net, c2_pnl=c2_net, total_pnl=total,
        c1_exit_reason=c1_exit_reason, c2_exit_reason=c2_exit_reason,
        direction=direction, entry_price=entry,
        month=cap.month, is_win=total > 0,
    )


# ═══════════════════════════════════════════════════════════════════
# EXPERIMENT RUNNER
# ═══════════════════════════════════════════════════════════════════

def run_experiment(
    captures: List[TradeCapture],
    experiment_fn,
    label: str,
    months: Optional[set] = None,
) -> Dict:
    """
    Run one experiment configuration across all captured trades.
    Returns aggregated results dict.
    """
    results = []
    for cap in captures:
        if months and cap.month not in months:
            continue
        r = experiment_fn(cap)
        if r:
            results.append(r)

    if not results:
        return {
            "label": label, "trades": 0, "wins": 0, "wr": 0, "pf": 0,
            "c1_pnl": 0, "c2_pnl": 0, "total_pnl": 0, "exp": 0,
            "max_dd_pct": 0, "gross_wins": 0, "gross_losses": 0,
        }

    total_trades = len(results)
    wins = sum(1 for r in results if r.is_win)
    wr = round(100 * wins / total_trades, 1) if total_trades > 0 else 0

    c1_pnl = round(sum(r.c1_pnl for r in results), 2)
    c2_pnl = round(sum(r.c2_pnl for r in results), 2)
    total_pnl = round(sum(r.total_pnl for r in results), 2)
    exp = round(total_pnl / total_trades, 2) if total_trades > 0 else 0

    gross_wins = round(sum(r.total_pnl for r in results if r.total_pnl > 0), 2)
    gross_losses = round(abs(sum(r.total_pnl for r in results if r.total_pnl < 0)), 2)
    pf = round(gross_wins / gross_losses, 2) if gross_losses > 0 else 99.0

    # Max drawdown
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in results:
        equity += r.total_pnl
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)
    max_dd_pct = round(100 * max_dd / 25000, 1) if max_dd > 0 else 0  # $25K account

    return {
        "label": label,
        "trades": total_trades,
        "wins": wins,
        "wr": wr,
        "pf": pf,
        "c1_pnl": c1_pnl,
        "c2_pnl": c2_pnl,
        "total_pnl": total_pnl,
        "exp": exp,
        "max_dd_pct": max_dd_pct,
        "gross_wins": gross_wins,
        "gross_losses": gross_losses,
    }


def run_experiment_monthly(
    captures: List[TradeCapture],
    experiment_fn,
    label: str,
    month_keys: List[str],
) -> List[Dict]:
    """Run experiment per month."""
    monthly = []
    for mk in month_keys:
        r = run_experiment(captures, experiment_fn, f"{label} ({mk})", months={mk})
        r["month"] = mk
        monthly.append(r)
    return monthly


# ═══════════════════════════════════════════════════════════════════
# REPORT GENERATOR
# ═══════════════════════════════════════════════════════════════════

def generate_report(
    all_results: Dict[str, Dict],
    monthly_top3: Dict[str, List[Dict]],
    baseline_results: Dict,
    captures_count: int,
) -> str:
    """Generate docs/c1_exit_research.md."""
    lines = []

    lines.append("# C1 Exit Strategy Research")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"**Data:** FirstRate 1m absolute-adjusted NQ, Sep 2025 - Feb 2026")
    lines.append(f"**Config:** D (HC ON, HTF gate=0.3, 2m exec)")
    lines.append(f"**Baseline C1:** TP1 = 1.5× stop (current production)")
    lines.append(f"**Total Entries Captured:** {captures_count} trades (from full pipeline run)")
    lines.append("")
    lines.append("## Methodology")
    lines.append("")
    lines.append("Split-phase backtest:")
    lines.append("1. **Phase 1** -- Full Config D pipeline (features + signals + HC gates + risk) "
                  "run once to capture all trade entries and 2m bar data.")
    lines.append("2. **Phase 2** -- For each experiment, exit logic replayed on captured trades. "
                  "Entry signals identical across all experiments. Only C1 exit strategy varies.")
    lines.append("")
    lines.append("> **Note:** C1 exit timing affects C2 breakeven placement, so C2 PnL may vary "
                  "slightly between experiments. This is expected and correctly modeled.")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Master comparison table
    lines.append("## Master Comparison")
    lines.append("")
    lines.append("| # | Experiment | Trades | WR% | PF | C1 PnL | C2 PnL | Total PnL | Exp/Trade | Max DD |")
    lines.append("|---|------------|--------|-----|-----|--------|--------|-----------|-----------|--------|")

    sorted_results = sorted(all_results.items(), key=lambda x: x[1].get("total_pnl", 0), reverse=True)
    for i, (key, r) in enumerate(sorted_results, 1):
        pf_str = f"**{r['pf']:.2f}**"
        delta = r["total_pnl"] - FEB_BASELINE["total_pnl"]
        delta_str = f" (+${delta:,.0f})" if delta > 0 else ""
        lines.append(
            f"| {i} | {r['label']} | {r['trades']} | {r['wr']:.1f} | "
            f"{pf_str} | ${r['c1_pnl']:+,.0f} | "
            f"${r['c2_pnl']:+,.0f} | ${r['total_pnl']:+,.0f}{delta_str} | "
            f"${r['exp']:.2f} | {r['max_dd_pct']:.1f}% |"
        )

    # Baseline row
    b = FEB_BASELINE
    lines.append(
        f"| -- | *Baseline (current prod)* | {b['trades']} | {b['wr']:.1f} | "
        f"**{b['pf']:.2f}** | ${b['c1_pnl']:+,.0f} | "
        f"${b['c2_pnl']:+,.0f} | ${b['total_pnl']:+,.0f} | "
        f"${b['exp']:.2f} | -- |"
    )
    lines.append("")

    # ─────────── Experiment A Detail ───────────
    lines.append("---")
    lines.append("")
    lines.append("## Experiment A -- Vary C1 Target Ratio")
    lines.append("")
    lines.append("C1 TP1 = {ratio} × stop distance. C2 runner unchanged.")
    lines.append("")
    lines.append("| Ratio | Trades | WR% | PF | C1 PnL | C2 PnL | Total PnL | Exp | vs Baseline |")
    lines.append("|-------|--------|-----|-----|--------|--------|-----------|-----|-------------|")
    for key, r in all_results.items():
        if key.startswith("A_"):
            delta = r["total_pnl"] - FEB_BASELINE["total_pnl"]
            marker = " **(current)**" if "1.5x" in r["label"] else ""
            lines.append(
                f"| {r['label']}{marker} | {r['trades']} | {r['wr']:.1f} | "
                f"{r['pf']:.2f} | ${r['c1_pnl']:+,.0f} | ${r['c2_pnl']:+,.0f} | "
                f"${r['total_pnl']:+,.0f} | ${r['exp']:.2f} | ${delta:+,.0f} |"
            )
    lines.append("")

    # ─────────── Experiment B Detail ───────────
    lines.append("## Experiment B -- Time-Based C1 Exit")
    lines.append("")
    lines.append("Exit C1 at market after N bars if profitable. Fallback: 1.5× target or stop.")
    lines.append("")
    lines.append("| Bars | Trades | WR% | PF | C1 PnL | C2 PnL | Total PnL | Exp | vs Baseline |")
    lines.append("|------|--------|-----|-----|--------|--------|-----------|-----|-------------|")
    for key, r in all_results.items():
        if key.startswith("B_"):
            delta = r["total_pnl"] - FEB_BASELINE["total_pnl"]
            lines.append(
                f"| {r['label']} | {r['trades']} | {r['wr']:.1f} | "
                f"{r['pf']:.2f} | ${r['c1_pnl']:+,.0f} | ${r['c2_pnl']:+,.0f} | "
                f"${r['total_pnl']:+,.0f} | ${r['exp']:.2f} | ${delta:+,.0f} |"
            )
    lines.append("")

    # ─────────── Experiment C Detail ───────────
    lines.append("## Experiment C -- No C1 Target (Pure Runner)")
    lines.append("")
    lines.append("Both contracts trail with ATR-based trailing stop. Move to BE at 1× stop profit.")
    lines.append("Both legs close on the same trailing stop.")
    lines.append("")
    if "C_pure_runner" in all_results:
        r = all_results["C_pure_runner"]
        delta = r["total_pnl"] - FEB_BASELINE["total_pnl"]
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Trades | {r['trades']} |")
        lines.append(f"| Win Rate | {r['wr']:.1f}% |")
        lines.append(f"| Profit Factor | **{r['pf']:.2f}** |")
        lines.append(f"| C1 PnL | ${r['c1_pnl']:+,.0f} |")
        lines.append(f"| C2 PnL | ${r['c2_pnl']:+,.0f} |")
        lines.append(f"| Total PnL | ${r['total_pnl']:+,.0f} |")
        lines.append(f"| Expectancy | ${r['exp']:.2f} / trade |")
        lines.append(f"| vs Baseline | ${delta:+,.0f} |")
        lines.append(f"| Max DD | {r['max_dd_pct']:.1f}% |")
    lines.append("")

    # ─────────── Experiment D Detail ───────────
    lines.append("## Experiment D -- Aggressive C1 Scalp (0.5× stop)")
    lines.append("")
    lines.append("C1 target = 0.5× stop. Quick lock-in, then C2 trails.")
    lines.append("")
    if "D_scalp_0.5x" in all_results:
        r = all_results["D_scalp_0.5x"]
        delta = r["total_pnl"] - FEB_BASELINE["total_pnl"]
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Trades | {r['trades']} |")
        lines.append(f"| Win Rate | {r['wr']:.1f}% |")
        lines.append(f"| Profit Factor | **{r['pf']:.2f}** |")
        lines.append(f"| C1 PnL | ${r['c1_pnl']:+,.0f} |")
        lines.append(f"| C2 PnL | ${r['c2_pnl']:+,.0f} |")
        lines.append(f"| Total PnL | ${r['total_pnl']:+,.0f} |")
        lines.append(f"| Expectancy | ${r['exp']:.2f} / trade |")
        lines.append(f"| vs Baseline | ${delta:+,.0f} |")
        lines.append(f"| Max DD | {r['max_dd_pct']:.1f}% |")
    lines.append("")

    # ─────────── Experiment E Detail ───────────
    lines.append("## Experiment E -- Breakeven C1 (Step Exit)")
    lines.append("")
    lines.append("At 1.0× stop in profit: move C1 to BE. At 2.0× stop in profit: exit C1 at market.")
    lines.append("")
    if "E_be_step" in all_results:
        r = all_results["E_be_step"]
        delta = r["total_pnl"] - FEB_BASELINE["total_pnl"]
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Trades | {r['trades']} |")
        lines.append(f"| Win Rate | {r['wr']:.1f}% |")
        lines.append(f"| Profit Factor | **{r['pf']:.2f}** |")
        lines.append(f"| C1 PnL | ${r['c1_pnl']:+,.0f} |")
        lines.append(f"| C2 PnL | ${r['c2_pnl']:+,.0f} |")
        lines.append(f"| Total PnL | ${r['total_pnl']:+,.0f} |")
        lines.append(f"| Expectancy | ${r['exp']:.2f} / trade |")
        lines.append(f"| vs Baseline | ${delta:+,.0f} |")
        lines.append(f"| Max DD | {r['max_dd_pct']:.1f}% |")
    lines.append("")

    # ─────────── Monthly Breakdown for Top 3 ───────────
    lines.append("---")
    lines.append("")
    lines.append("## Monthly Breakdown -- Top 3 Configurations")
    lines.append("")
    lines.append("Verifying consistency across all market regimes.")
    lines.append("")

    for config_label, monthly in monthly_top3.items():
        lines.append(f"### {config_label}")
        lines.append("")
        lines.append("| Month | Trades | WR% | PF | C1 PnL | C2 PnL | Total PnL |")
        lines.append("|-------|--------|-----|-----|--------|--------|-----------|")
        agg_pnl = 0
        profitable_months = 0
        for mr in monthly:
            month = mr.get("month", "?")
            pf_str = f"{mr['pf']:.2f}" if mr['trades'] > 0 else "--"
            wr_str = f"{mr['wr']:.1f}" if mr['trades'] > 0 else "--"
            agg_pnl += mr['total_pnl']
            if mr['total_pnl'] > 0:
                profitable_months += 1
            lines.append(
                f"| {month} | {mr['trades']} | {wr_str} | "
                f"{pf_str} | ${mr['c1_pnl']:+,.0f} | ${mr['c2_pnl']:+,.0f} | "
                f"${mr['total_pnl']:+,.0f} |"
            )
        lines.append(f"| **Total** | | | | | | **${agg_pnl:+,.0f}** |")
        lines.append(f"")
        lines.append(f"Profitable months: **{profitable_months}/6**")
        lines.append("")

    # ─────────── Recommendation ───────────
    lines.append("---")
    lines.append("")
    lines.append("## Recommendation")
    lines.append("")

    best_pnl_key = sorted_results[0][0]
    best_pnl = sorted_results[0][1]
    best_pf = max(all_results.values(), key=lambda x: x.get("pf", 0))

    lines.append(f"### Best by Total PnL")
    lines.append(f"**{best_pnl['label']}** -- ${best_pnl['total_pnl']:+,.0f} "
                 f"(PF {best_pnl['pf']:.2f}, {best_pnl['wr']:.1f}% WR)")
    lines.append("")
    lines.append(f"### Best by Profit Factor")
    lines.append(f"**{best_pf['label']}** -- PF {best_pf['pf']:.2f} "
                 f"(${best_pf['total_pnl']:+,.0f})")
    lines.append("")

    # Configs beating baseline
    baseline_pnl = FEB_BASELINE["total_pnl"]
    beats = [(k, v) for k, v in all_results.items() if v["total_pnl"] > baseline_pnl]
    if beats:
        lines.append(f"### Configurations Beating 6-Month Baseline (${baseline_pnl:,.0f})")
        lines.append("")
        for k, v in sorted(beats, key=lambda x: x[1]["total_pnl"], reverse=True):
            improvement = v["total_pnl"] - baseline_pnl
            lines.append(f"- **{v['label']}**: ${v['total_pnl']:+,.0f} "
                         f"(+${improvement:,.0f}, PF {v['pf']:.2f})")
    else:
        lines.append("No configuration beat the 6-month baseline total PnL.")
    lines.append("")

    # Key insights
    lines.append("### Key Insights")
    lines.append("")

    # C1 PnL analysis
    c1_positive = [(k, v) for k, v in all_results.items() if v["c1_pnl"] > 0]
    if c1_positive:
        lines.append("**C1 strategies that turn profitable:**")
        for k, v in sorted(c1_positive, key=lambda x: x[1]["c1_pnl"], reverse=True):
            lines.append(f"- {v['label']}: C1 PnL ${v['c1_pnl']:+,.0f}")
    else:
        lines.append("**No C1 strategy produced positive C1-only PnL.** "
                     "C1 functions as a cost of doing business to protect C2.")
    lines.append("")

    # Best C1-to-C2 ratio (minimize C1 drag)
    best_c1_drag = min(all_results.values(), key=lambda x: abs(x["c1_pnl"]))
    lines.append(f"**Minimum C1 drag:** {best_c1_drag['label']} "
                 f"(C1 PnL ${best_c1_drag['c1_pnl']:+,.0f})")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*Generated by `scripts/c1_exit_experiments.py` (split-phase optimized) -- "
                 f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

async def main():
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )
    for name in ["main", "execution", "signals", "features", "risk", "data_pipeline"]:
        logging.getLogger(name).setLevel(logging.WARNING)

    print(f"\n{'=' * 70}")
    print(f"  C1 EXIT STRATEGY EXPERIMENTS -- Split-Phase Optimized")
    print(f"  Config D | Sep 2025 - Feb 2026 | FirstRate 1m")
    print(f"{'=' * 70}\n")

    # ─── Load data ───
    print("Loading data...")
    tf_bars = load_data()
    if "2m" not in tf_bars:
        print("ERROR: No 2m data found. Run aggregate_1m.py first.")
        sys.exit(1)

    # Target months (Sep 2025 - Feb 2026)
    target_months = {"2025-09", "2025-10", "2025-11", "2025-12", "2026-01", "2026-02"}
    month_keys = sorted(target_months)

    bars_2m = sum(1 for b in tf_bars.get("2m", []) if b.timestamp.strftime("%Y-%m") in target_months)
    print(f"Months: {month_keys}")
    print(f"2m bars (target months): {bars_2m:,}")
    print()

    # ═══════════════════════════════════════════════════════════════
    # PHASE 1: Full pipeline run -- capture all trade entries
    # Each month runs with fresh risk state (matching OOS validation)
    # ═══════════════════════════════════════════════════════════════
    print(f"{'─' * 70}")
    print(f"PHASE 1: Running full Config D pipeline (month by month)")
    print(f"{'─' * 70}")
    print(f"  Computing features for each month with fresh risk state...")
    print()

    captures, all_bars, baseline_results = await run_baseline_and_capture(tf_bars, month_keys)

    print(f"\n  Phase 1 complete!")
    print(f"  Trades captured: {len(captures)}")
    print(f"  2m bars indexed: {len(all_bars):,}")
    print(f"  Avg bars/trade:  {sum(len(c.bars_after_entry) for c in captures) / max(len(captures), 1):.0f}")
    print()

    # ═══════════════════════════════════════════════════════════════
    # PHASE 2: Replay all experiments (fast -- no feature computation)
    # ═══════════════════════════════════════════════════════════════
    print(f"{'─' * 70}")
    print(f"PHASE 2: Replaying exit strategies ({len(captures)} trades × 14 configs)")
    print(f"{'─' * 70}")
    print()

    all_results = {}

    # ── Experiment A: Vary C1 target ratio ──
    print("EXPERIMENT A -- Vary C1 Target Ratio")
    print("─" * 45)
    for ratio in [1.0, 1.25, 1.5, 1.75, 2.0, 2.5]:
        label = f"A: {ratio}x stop"
        fn = lambda cap, r=ratio: replay_trade_standard(cap, r)
        r = run_experiment(captures, fn, label)
        all_results[f"A_{ratio}x"] = r
        print(f"  {label:<16} -> {r['trades']}t  PF {r['pf']:.2f}  "
              f"C1 ${r['c1_pnl']:+,.0f}  C2 ${r['c2_pnl']:+,.0f}  "
              f"Total ${r['total_pnl']:+,.0f}")
    print()

    # ── Experiment B: Time-based C1 exit ──
    print("EXPERIMENT B -- Time-Based C1 Exit")
    print("─" * 45)
    for bars in [5, 10, 15, 20, 30]:
        label = f"B: {bars} bars"
        fn = lambda cap, b=bars: replay_trade_time_exit(cap, b)
        r = run_experiment(captures, fn, label)
        all_results[f"B_{bars}bars"] = r
        print(f"  {label:<16} -> {r['trades']}t  PF {r['pf']:.2f}  "
              f"C1 ${r['c1_pnl']:+,.0f}  C2 ${r['c2_pnl']:+,.0f}  "
              f"Total ${r['total_pnl']:+,.0f}")
    print()

    # ── Experiment C: Pure runner (no C1 target) ──
    print("EXPERIMENT C -- No C1 Target (Pure Runner)")
    print("─" * 45)
    fn = lambda cap: replay_trade_pure_runner(cap)
    r = run_experiment(captures, fn, "C: Pure Runner")
    all_results["C_pure_runner"] = r
    print(f"  Pure Runner     -> {r['trades']}t  PF {r['pf']:.2f}  "
          f"C1 ${r['c1_pnl']:+,.0f}  C2 ${r['c2_pnl']:+,.0f}  "
          f"Total ${r['total_pnl']:+,.0f}")
    print()

    # ── Experiment D: Aggressive scalp (0.5x) ──
    print("EXPERIMENT D -- Aggressive C1 Scalp (0.5x stop)")
    print("─" * 45)
    fn = lambda cap: replay_trade_standard(cap, 0.5)
    r = run_experiment(captures, fn, "D: 0.5x scalp")
    all_results["D_scalp_0.5x"] = r
    print(f"  0.5x scalp      -> {r['trades']}t  PF {r['pf']:.2f}  "
          f"C1 ${r['c1_pnl']:+,.0f}  C2 ${r['c2_pnl']:+,.0f}  "
          f"Total ${r['total_pnl']:+,.0f}")
    print()

    # ── Experiment E: Breakeven step ──
    print("EXPERIMENT E -- Breakeven C1 (Step Exit)")
    print("─" * 45)
    fn = lambda cap: replay_trade_be_step(cap)
    r = run_experiment(captures, fn, "E: BE Step")
    all_results["E_be_step"] = r
    print(f"  BE Step         -> {r['trades']}t  PF {r['pf']:.2f}  "
          f"C1 ${r['c1_pnl']:+,.0f}  C2 ${r['c2_pnl']:+,.0f}  "
          f"Total ${r['total_pnl']:+,.0f}")
    print()

    # ═══════════════════════════════════════════════════════════════
    # TOP 3 -- Monthly Breakdown
    # ═══════════════════════════════════════════════════════════════
    print(f"{'─' * 70}")
    print(f"TOP 3 -- Monthly Breakdown")
    print(f"{'─' * 70}")

    sorted_configs = sorted(all_results.items(), key=lambda x: x[1]["total_pnl"], reverse=True)
    top3_keys = [(k, v) for k, v in sorted_configs[:3]]

    monthly_top3 = {}
    for key, r in top3_keys:
        label = r["label"]
        print(f"\n  Running monthly for: {label}")

        # Determine replay function
        if key.startswith("A_"):
            ratio = float(key.split("_")[1].replace("x", ""))
            fn = lambda cap, r_=ratio: replay_trade_standard(cap, r_)
        elif key.startswith("B_"):
            bars = int(key.split("_")[1].replace("bars", ""))
            fn = lambda cap, b_=bars: replay_trade_time_exit(cap, b_)
        elif key == "C_pure_runner":
            fn = lambda cap: replay_trade_pure_runner(cap)
        elif key == "D_scalp_0.5x":
            fn = lambda cap: replay_trade_standard(cap, 0.5)
        elif key == "E_be_step":
            fn = lambda cap: replay_trade_be_step(cap)
        else:
            continue

        monthly_results = run_experiment_monthly(captures, fn, label, month_keys)
        monthly_top3[label] = monthly_results

        for mr in monthly_results:
            mk = mr["month"]
            pf_str = f"{mr['pf']:.2f}" if mr['trades'] > 0 else "  -- "
            print(f"    {mk}: {mr['trades']:>3}t  PF {pf_str}  ${mr['total_pnl']:+,.0f}")

    # ═══════════════════════════════════════════════════════════════
    # GENERATE REPORT
    # ═══════════════════════════════════════════════════════════════
    os.makedirs(DOCS_DIR, exist_ok=True)
    report = generate_report(all_results, monthly_top3, baseline_results, len(captures))
    report_path = os.path.join(DOCS_DIR, "c1_exit_research.md")
    with open(report_path, "w") as f:
        f.write(report)
    print(f"\nReport saved: {report_path}")

    # ─── Final Summary ───
    print(f"\n{'=' * 70}")
    print(f"  SUMMARY -- All Configurations Ranked by Total PnL")
    print(f"{'=' * 70}")
    print(f"  {'#':<3} {'Config':<25} {'Trades':>7} {'PF':>7} {'C1 PnL':>10} {'C2 PnL':>10} {'Total':>10}")
    print(f"  {'─' * 73}")
    for i, (key, r) in enumerate(sorted_configs, 1):
        print(f"  {i:<3} {r['label']:<25} {r['trades']:>7} {r['pf']:>7.2f} "
              f"${r['c1_pnl']:>9,.0f} ${r['c2_pnl']:>9,.0f} ${r['total_pnl']:>9,.0f}")
    print(f"  {'─' * 73}")
    print(f"  {'--':<3} {'Baseline (current prod)':<25} {FEB_BASELINE['trades']:>7} {FEB_BASELINE['pf']:>7.2f} "
          f"${FEB_BASELINE['c1_pnl']:>9,.0f} ${FEB_BASELINE['c2_pnl']:>9,.0f} "
          f"${FEB_BASELINE['total_pnl']:>9,.0f}")
    print(f"{'=' * 70}")
    print()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
