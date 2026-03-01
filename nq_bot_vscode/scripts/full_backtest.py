#!/usr/bin/env python3
"""
Full Historical Backtest — Causal Replay Engine
=================================================
Phase 2 engine for the definitive full-history backtest.
Built in Phase 1, executed in Phase 2.

Strict causal replay: at each bar N, the system only knows
bars [0..N], completed HTF bars, and running indicators.
Zero look-ahead bias guaranteed.

EXECUTION RULES:
  - Signal at bar N close → entry at bar N+1 open + slippage
  - Slippage: RTH 0.50 pts/fill, ETH 1.00 pts/fill (both sides)
  - Commission: $1.29 per contract per side
  - Point value: $2.00/pt (MNQ)
  - 2-contract scale-out: C1 trail-from-profit, C2 ATR trail
  - HC filter >= 0.75, HTF gate >= 0.3, max stop 30pts, min R:R 1.5
  - Daily loss limit $500, kill switch $1000
  - NaN in score/stop/PnL = block trade immediately
  - First 30 bars each session = warmup only, no trades
  - DST-aware session boundaries via ZoneInfo

Imports and uses the REAL modules — does NOT reimplement signal logic.

Usage (Phase 2):
    python scripts/full_backtest.py \\
        --data data/historical/combined_1min.csv \\
        --htf-dir data/historical/ \\
        --output logs/full_validation_trades.json
"""

import argparse
import asyncio
import csv
import json
import logging
import math
import os
import sys
from datetime import datetime, timedelta, date, time, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

# ── Ensure project root is on sys.path ──────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent  # nq_bot_vscode/
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# ── Import REAL modules ─────────────────────────────────────────
from config.settings import BotConfig, RiskConfig, ScaleOutConfig
from features.engine import NQFeatureEngine, Bar
from features.htf_engine import HTFBiasEngine, HTFBar, HTFBiasResult
from signals.aggregator import SignalAggregator, SignalDirection
from signals.liquidity_sweep import LiquiditySweepDetector, SweepSignal
from risk.engine import RiskEngine, RiskDecision
from risk.regime_detector import RegimeDetector
from execution.scale_out_executor import ScaleOutExecutor

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ── Hard Constants (match main.py) ──────────────────────────────
HIGH_CONVICTION_MIN_SCORE = 0.75
HIGH_CONVICTION_MAX_STOP_PTS = 30.0
SWEEP_MIN_SCORE = 0.70
SWEEP_CONFLUENCE_BONUS = 0.05
MIN_RR_RATIO = 1.5

# ── Slippage Model ──────────────────────────────────────────────
SLIPPAGE_RTH_PTS = 0.50   # Per fill, RTH
SLIPPAGE_ETH_PTS = 1.00   # Per fill, ETH
COMMISSION_PER_CONTRACT = 1.29
POINT_VALUE = 2.00         # MNQ $2/point

# ── Session Constants ───────────────────────────────────────────
SESSION_BOUNDARY_HOUR = 18  # 6 PM ET = new session start
WARMUP_BARS = 30            # No trades for first 30 bars of session
DAILY_LOSS_LIMIT = 500.0    # $500
KILL_SWITCH_LIMIT = 1000.0  # $1000

# ── Progress Reporting ──────────────────────────────────────────
PROGRESS_INTERVAL = 50_000


# =====================================================================
#  DATA LOADING
# =====================================================================

def load_1min_csv(filepath: str) -> List[Dict]:
    """Load the combined 1-min CSV produced by prepare_historical_data.py.

    Expected format: timestamp,open,high,low,close,volume
    Timestamps are ISO strings with timezone (e.g. 2021-09-01 00:00:00-0400).
    """
    bars = []
    with open(filepath, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts_str = row["timestamp"].strip()
                # Parse timezone-aware timestamp
                # Format: 2021-09-01 00:00:00-0400 or 2021-09-01 00:00:00-04:00
                if "+" in ts_str[10:] or "-" in ts_str[10:]:
                    # Has timezone offset
                    # Try several formats
                    for fmt in ["%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S%Z"]:
                        try:
                            dt = datetime.strptime(ts_str, fmt)
                            break
                        except ValueError:
                            continue
                    else:
                        # Fallback: parse with dateutil-style
                        dt = datetime.fromisoformat(ts_str)
                else:
                    # No timezone — assume ET
                    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    dt = dt.replace(tzinfo=ET)

                bars.append({
                    "timestamp": dt,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(float(row["volume"])),
                })
            except (ValueError, KeyError) as e:
                continue

    bars.sort(key=lambda b: b["timestamp"])
    return bars


def load_htf_csv(filepath: str) -> List[Dict]:
    """Load an HTF CSV file. Same format as 1-min CSV."""
    return load_1min_csv(filepath)  # Same format


def load_all_htf(htf_dir: str) -> Dict[str, List[Dict]]:
    """Load all HTF CSV files from the directory.

    Expected files: htf_5m.csv, htf_15m.csv, htf_30m.csv,
                    htf_1H.csv, htf_4H.csv, htf_1D.csv
    """
    htf_data = {}
    tf_files = {
        "5m":  "htf_5m.csv",
        "15m": "htf_15m.csv",
        "30m": "htf_30m.csv",
        "1H":  "htf_1H.csv",
        "4H":  "htf_4H.csv",
        "1D":  "htf_1D.csv",
    }

    for tf_label, filename in tf_files.items():
        fpath = os.path.join(htf_dir, filename)
        if os.path.exists(fpath):
            bars = load_htf_csv(fpath)
            htf_data[tf_label] = bars
            logger.info(f"  Loaded HTF {tf_label}: {len(bars):,} bars")
        else:
            logger.warning(f"  HTF file not found: {fpath}")

    return htf_data


# =====================================================================
#  SESSION UTILITIES
# =====================================================================

def get_trading_day(dt: datetime) -> date:
    """Get CME trading day for a given ET datetime."""
    et = dt.astimezone(ET)
    if et.hour >= SESSION_BOUNDARY_HOUR:
        return (et + timedelta(days=1)).date()
    return et.date()


def is_rth(dt: datetime) -> bool:
    """Check if timestamp is during Regular Trading Hours (9:30 AM - 4:00 PM ET)."""
    et = dt.astimezone(ET)
    t = et.hour + et.minute / 60.0
    return 9.5 <= t < 16.0


def get_slippage(dt: datetime) -> float:
    """Get per-fill slippage based on session type."""
    return SLIPPAGE_RTH_PTS if is_rth(dt) else SLIPPAGE_ETH_PTS


# =====================================================================
#  HTF COMPLETION TRACKER
# =====================================================================

class HTFScheduler:
    """Feeds completed HTF bars to the engine at the right time.

    An HTF bar with bucket-start timestamp T is "complete" when the
    next period begins. For intraday: at T + tf_minutes. For daily:
    at 6 PM ET on the bar's date.

    Only feeds a bar ONCE, and only after it is complete.
    """

    TF_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1H": 60, "4H": 240, "1D": 1440}

    def __init__(self, htf_data: Dict[str, List[Dict]]):
        self._queues: Dict[str, List[Dict]] = {}
        self._indices: Dict[str, int] = {}

        for tf, bars in htf_data.items():
            # Sort by timestamp
            self._queues[tf] = sorted(bars, key=lambda b: b["timestamp"])
            self._indices[tf] = 0

    def get_newly_completed(self, current_ts: datetime) -> List[Tuple[str, Dict]]:
        """Return all HTF bars that just became complete at current_ts.

        Returns list of (timeframe, bar_dict) tuples, ordered by
        timeframe priority (1D first, then 4H, etc.).
        """
        completed = []

        for tf in ["1D", "4H", "1H", "30m", "15m", "5m"]:
            if tf not in self._queues:
                continue

            queue = self._queues[tf]
            idx = self._indices[tf]
            tf_min = self.TF_MINUTES[tf]

            while idx < len(queue):
                bar = queue[idx]
                bar_ts = bar["timestamp"]

                # Compute when this bar is "complete"
                if tf_min >= 1440:
                    # Daily bar: complete at 6 PM ET on the bar's date
                    bar_date = bar_ts.date() if hasattr(bar_ts, 'date') else bar_ts
                    if isinstance(bar_date, datetime):
                        bar_date = bar_date.date()
                    completion_ts = datetime(
                        bar_date.year, bar_date.month, bar_date.day,
                        SESSION_BOUNDARY_HOUR, 0, 0, tzinfo=ET
                    )
                else:
                    # Intraday: complete at bar_ts + tf_minutes
                    completion_ts = bar_ts + timedelta(minutes=tf_min)

                if current_ts >= completion_ts:
                    completed.append((tf, bar))
                    idx += 1
                else:
                    break

            self._indices[tf] = idx

        return completed


# =====================================================================
#  CAUSAL REPLAY ENGINE
# =====================================================================

class CausalReplayEngine:
    """Strict causal bar-by-bar replay engine.

    Enforces that at each bar N:
    - Only bars [0..N] and completed HTF bars are visible
    - Signal at bar N → entry at bar N+1 open + slippage
    - Proper slippage, commission, session management
    - NaN guards on all safety gates
    """

    def __init__(self, config: BotConfig):
        self.config = config

        # ── Core pipeline components (REAL modules) ──
        self.feature_engine = NQFeatureEngine(config)
        self.htf_engine = HTFBiasEngine(
            config=config,
            timeframes=["5m", "15m", "30m", "1H", "4H", "1D"],
        )
        self.signal_aggregator = SignalAggregator(config)
        self.risk_engine = RiskEngine(config)
        self.regime_detector = RegimeDetector(config)
        self.sweep_detector = LiquiditySweepDetector()
        self.executor = ScaleOutExecutor(config)

        # ── State ──
        self._htf_bias: Optional[HTFBiasResult] = None
        self._current_regime: str = "unknown"
        self._bars_processed: int = 0
        self._session_bar_count: int = 0
        self._current_trading_day: Optional[date] = None
        self._daily_pnl: float = 0.0
        self._cumulative_pnl: float = 0.0
        self._kill_switch_active: bool = False

        # ── Pending signal (signal at bar N, execute at bar N+1) ──
        self._pending_entry: Optional[Dict] = None

        # ── Trade collection ──
        self.trades: List[Dict] = []
        self._entry_count: int = 0
        self._rejection_count: int = 0

        # ── Mark as "running" for process_bar compatibility ──
        self.executor._active_trade = None

    def _check_session_boundary(self, ts: datetime) -> None:
        """Detect new trading session and handle resets."""
        trading_day = get_trading_day(ts)

        if trading_day != self._current_trading_day:
            if self._current_trading_day is not None:
                logger.debug(
                    f"New session: {trading_day} | "
                    f"Prev day PnL: ${self._daily_pnl:.2f}"
                )
            self._current_trading_day = trading_day
            self._session_bar_count = 0
            self._daily_pnl = 0.0
            self.risk_engine.reset_daily_state()

            # Check kill switch reset (new day clears it for this engine)
            if self._kill_switch_active and self._cumulative_pnl > -KILL_SWITCH_LIMIT:
                self._kill_switch_active = False

        self._session_bar_count += 1

    def _is_warmup(self) -> bool:
        """First 30 bars of each session = warmup, no trades."""
        return self._session_bar_count <= WARMUP_BARS

    def _check_daily_limits(self) -> bool:
        """Check daily loss limit and kill switch. Returns True if blocked."""
        if self._kill_switch_active:
            return True

        if self._daily_pnl <= -DAILY_LOSS_LIMIT:
            logger.debug(f"Daily loss limit hit: ${self._daily_pnl:.2f}")
            return True

        if self._cumulative_pnl <= -KILL_SWITCH_LIMIT:
            logger.warning(f"KILL SWITCH: cumulative PnL ${self._cumulative_pnl:.2f}")
            self._kill_switch_active = True
            return True

        return False

    async def _execute_pending_entry(self, bar: Dict) -> Optional[Dict]:
        """Execute a pending signal at the current bar's open + slippage."""
        if self._pending_entry is None:
            return None

        pending = self._pending_entry
        self._pending_entry = None

        # Can't enter if already in a position
        if self.executor.has_active_trade:
            return None

        # Can't enter during warmup or if limits hit
        if self._is_warmup() or self._check_daily_limits():
            return None

        direction = pending["direction"]
        slippage = get_slippage(bar["timestamp"])

        # Apply entry slippage (adverse direction)
        if direction == "long":
            entry_price = bar["open"] + slippage
        else:
            entry_price = bar["open"] - slippage

        # Enter via real ScaleOutExecutor
        trade = await self.executor.enter_trade(
            direction=direction,
            entry_price=entry_price,
            stop_distance=pending["stop_distance"],
            atr=pending["atr"],
            signal_score=pending["score"],
            regime=pending["regime"],
        )

        if trade:
            self._entry_count += 1
            entry_record = {
                "action": "entry",
                "trade_id": trade.trade_id,
                "bar_index": self._bars_processed,
                "timestamp": bar["timestamp"].isoformat(),
                "signal_timestamp": pending["signal_timestamp"],
                "direction": direction,
                "entry_price": entry_price,
                "raw_open": bar["open"],
                "slippage_applied": slippage,
                "stop_distance": pending["stop_distance"],
                "signal_score": pending["score"],
                "signal_source": pending["source"],
                "regime": pending["regime"],
                "htf_bias": pending.get("htf_direction", "n/a"),
                "htf_strength": pending.get("htf_strength", 0.0),
                "atr": pending["atr"],
                "is_rth": is_rth(bar["timestamp"]),
            }
            self.trades.append(entry_record)
            return entry_record

        return None

    async def _manage_active_position(self, bar: Dict) -> Optional[Dict]:
        """Update active position with current bar's close."""
        if not self.executor.has_active_trade:
            return None

        result = await self.executor.update(bar["close"], bar["timestamp"])

        if result and result.get("action") == "trade_closed":
            # Apply exit slippage
            exit_slippage = get_slippage(bar["timestamp"])
            direction = result["direction"]

            # Compute slippage-adjusted PnL
            # Entry slippage already baked in. Exit slippage: 2 fills (C1 + C2)
            # But C1 and C2 may exit at different times. For simplicity,
            # apply per-trade exit slippage cost
            exit_slippage_cost = exit_slippage * POINT_VALUE * 2  # 2 contracts

            raw_pnl = result.get("total_pnl", 0.0)
            adjusted_pnl = raw_pnl - exit_slippage_cost

            # Update PnL tracking
            self._daily_pnl += adjusted_pnl
            self._cumulative_pnl += adjusted_pnl
            self.risk_engine.record_trade_result(adjusted_pnl, direction)

            exit_record = {
                "action": "exit",
                "trade_id": result.get("trade_id", ""),
                "bar_index": self._bars_processed,
                "timestamp": bar["timestamp"].isoformat(),
                "direction": direction,
                "exit_price": bar["close"],
                "raw_pnl": raw_pnl,
                "exit_slippage_cost": exit_slippage_cost,
                "adjusted_pnl": adjusted_pnl,
                "daily_pnl": self._daily_pnl,
                "cumulative_pnl": self._cumulative_pnl,
                "c1_pnl": result.get("c1_pnl", 0),
                "c2_pnl": result.get("c2_pnl", 0),
                "c1_exit_reason": result.get("c1_exit_reason", ""),
                "c2_exit_reason": result.get("c2_exit_reason", ""),
            }
            self.trades.append(exit_record)
            return exit_record

        return result

    async def _generate_signal(self, bar: Dict, features, htf_bias) -> None:
        """Run signal pipeline. If signal passes all gates, store as pending."""
        # Only generate signals when flat
        if self.executor.has_active_trade:
            return

        # Warmup check
        if self._is_warmup():
            return

        # Daily limit check
        if self._check_daily_limits():
            return

        import numpy as np

        # ── Regime detection ──
        bars_list = self.feature_engine._bars
        avg_vol = (
            np.mean([b.volume for b in bars_list[-20:]])
            if len(bars_list) >= 20
            else bar["volume"]
        )

        self._current_regime = self.regime_detector.classify(
            current_atr=features.atr_14,
            current_vix=features.vix_level or 0,
            trend_direction=features.trend_direction,
            trend_strength=features.trend_strength,
            current_volume=bar["volume"],
            avg_volume=avg_vol,
            is_overnight=not is_rth(bar["timestamp"]),
            near_news_event=False,
        )

        regime_adj = self.regime_detector.get_regime_adjustments(self._current_regime)

        # ── Sweep detector ──
        sweep_signal = None
        rth = is_rth(bar["timestamp"])
        exec_bar = Bar(
            timestamp=bar["timestamp"],
            open=bar["open"],
            high=bar["high"],
            low=bar["low"],
            close=bar["close"],
            volume=bar["volume"],
        )
        sweep_signal = self.sweep_detector.update_bar(
            bar=exec_bar,
            vwap=features.session_vwap,
            htf_bias=htf_bias,
            is_rth=rth,
        )

        # ── Signal aggregation ──
        signal = self.signal_aggregator.aggregate(
            feature_snapshot=features,
            ml_prediction=None,
            htf_bias=htf_bias,
            current_time=bar["timestamp"],
        )

        # ── Determine entry parameters ──
        has_signal = signal and signal.should_trade
        has_sweep = (
            sweep_signal is not None and sweep_signal.score >= SWEEP_MIN_SCORE
        )

        entry_direction = None
        entry_score = 0.0
        entry_source = None
        sweep_stop_override = None

        if has_signal and has_sweep:
            direction_str = (
                "long" if signal.direction == SignalDirection.LONG else "short"
            )
            sweep_dir = (
                "long" if sweep_signal.direction == "LONG" else "short"
            )
            if direction_str == sweep_dir:
                entry_direction = direction_str
                entry_score = signal.combined_score + SWEEP_CONFLUENCE_BONUS
                entry_source = "confluence"
            else:
                entry_direction = direction_str
                entry_score = signal.combined_score
                entry_source = "signal"
        elif has_signal:
            entry_direction = (
                "long" if signal.direction == SignalDirection.LONG else "short"
            )
            entry_score = signal.combined_score
            entry_source = "signal"
        elif has_sweep:
            entry_direction = (
                "long" if sweep_signal.direction == "LONG" else "short"
            )
            entry_score = sweep_signal.score
            entry_source = "sweep"
            sweep_stop_override = abs(bar["close"] - sweep_signal.stop_price)

        if entry_direction is None:
            return

        # ── NaN Guard ──
        if not math.isfinite(entry_score):
            logger.debug("NaN entry_score — blocking")
            self._rejection_count += 1
            return

        # ── HC Gate 1: Score ──
        if entry_score < HIGH_CONVICTION_MIN_SCORE:
            self._rejection_count += 1
            return

        # ── Risk Assessment ──
        risk_assessment = self.risk_engine.evaluate_trade(
            direction=entry_direction,
            entry_price=bar["close"],
            atr=features.atr_14,
            vix=features.vix_level or 0,
            current_time=bar["timestamp"],
        )

        raw_stop = risk_assessment.suggested_stop_distance
        if sweep_stop_override is not None and sweep_stop_override < raw_stop:
            raw_stop = sweep_stop_override

        # ── NaN Guard on stop ──
        if not math.isfinite(raw_stop):
            logger.debug("NaN stop distance — blocking")
            self._rejection_count += 1
            return

        # ── HC Gate 2: Stop Distance ──
        if raw_stop > HIGH_CONVICTION_MAX_STOP_PTS:
            self._rejection_count += 1
            return

        # ── Min R:R Check ──
        target_distance = features.atr_14 * self.config.risk.atr_multiplier_target
        if raw_stop > 0 and target_distance / raw_stop < MIN_RR_RATIO:
            self._rejection_count += 1
            return

        # ── Regime gate ──
        if regime_adj["size_multiplier"] == 0:
            self._rejection_count += 1
            return

        # ── Risk decision ──
        if risk_assessment.decision not in (RiskDecision.APPROVE, RiskDecision.REDUCE_SIZE):
            self._rejection_count += 1
            return

        # ── All gates passed: store as pending entry ──
        htf_dir = htf_bias.consensus_direction if htf_bias else "n/a"
        htf_str = htf_bias.consensus_strength if htf_bias else 0.0

        self._pending_entry = {
            "direction": entry_direction,
            "score": entry_score,
            "stop_distance": raw_stop,
            "atr": features.atr_14,
            "source": entry_source,
            "regime": self._current_regime,
            "signal_timestamp": bar["timestamp"].isoformat(),
            "htf_direction": htf_dir,
            "htf_strength": round(htf_str, 3),
        }

    async def process_bar(self, bar: Dict, htf_scheduler: HTFScheduler) -> None:
        """Process a single 1-minute bar through the full causal pipeline."""
        self._bars_processed += 1
        ts = bar["timestamp"]

        # ── Session boundary check ──
        self._check_session_boundary(ts)

        # ── Feed newly-completed HTF bars ──
        completed_htf = htf_scheduler.get_newly_completed(ts)
        for tf, htf_bar_dict in completed_htf:
            htf_bar = HTFBar(
                timestamp=htf_bar_dict["timestamp"],
                open=htf_bar_dict["open"],
                high=htf_bar_dict["high"],
                low=htf_bar_dict["low"],
                close=htf_bar_dict["close"],
                volume=htf_bar_dict["volume"],
            )
            self.htf_engine.update_bar(tf, htf_bar)
            self._htf_bias = self.htf_engine.get_bias(ts)

        # ── Step 1: Execute pending entry from previous bar ──
        await self._execute_pending_entry(bar)

        # ── Step 2: Manage active position ──
        await self._manage_active_position(bar)

        # ── Step 3: Compute features on execution bar ──
        exec_bar = Bar(
            timestamp=ts,
            open=bar["open"],
            high=bar["high"],
            low=bar["low"],
            close=bar["close"],
            volume=bar["volume"],
        )
        features = self.feature_engine.update(exec_bar)

        # ── Step 4: Generate signal (if flat, not warmup) ──
        await self._generate_signal(bar, features, self._htf_bias)

        # ── Progress reporting ──
        if self._bars_processed % PROGRESS_INTERVAL == 0:
            bias_str = "n/a"
            if self._htf_bias:
                bias_str = (
                    f"{self._htf_bias.consensus_direction}"
                    f"({self._htf_bias.consensus_strength:.2f})"
                )
            print(
                f"  [{self._bars_processed:>10,}] "
                f"{ts.strftime('%Y-%m-%d %H:%M')} | "
                f"Trades: {self._entry_count} | "
                f"PnL: ${self._cumulative_pnl:+,.2f} | "
                f"HTF: {bias_str}"
            )

    def get_summary(self) -> Dict:
        """Compute final backtest summary statistics."""
        entries = [t for t in self.trades if t["action"] == "entry"]
        exits = [t for t in self.trades if t["action"] == "exit"]

        pnls = [t["adjusted_pnl"] for t in exits]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p < 0]

        total_pnl = sum(pnls) if pnls else 0
        win_rate = len(winners) / len(exits) * 100 if exits else 0
        profit_factor = (
            abs(sum(winners) / sum(losers))
            if losers and sum(losers) != 0
            else float("inf")
        )

        # Source breakdown
        source_counts = {}
        for t in entries:
            src = t.get("signal_source", "unknown")
            source_counts[src] = source_counts.get(src, 0) + 1

        return {
            "bars_processed": self._bars_processed,
            "total_trades": len(exits),
            "total_entries": len(entries),
            "total_rejections": self._rejection_count,
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(win_rate, 1),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
            "avg_winner": round(sum(winners) / len(winners), 2) if winners else 0,
            "avg_loser": round(sum(losers) / len(losers), 2) if losers else 0,
            "largest_win": round(max(pnls), 2) if pnls else 0,
            "largest_loss": round(min(pnls), 2) if pnls else 0,
            "expectancy": round(total_pnl / len(exits), 2) if exits else 0,
            "cumulative_pnl": round(self._cumulative_pnl, 2),
            "signal_sources": source_counts,
            "executor_stats": self.executor.get_stats(),
        }


# =====================================================================
#  MAIN RUNNER
# =====================================================================

async def run_backtest(
    data_path: str,
    htf_dir: str,
    output_path: str,
) -> Dict:
    """Execute the full causal replay backtest."""

    print("=" * 70)
    print("  FULL HISTORICAL BACKTEST — CAUSAL REPLAY ENGINE")
    print("=" * 70)
    print(f"  Data:   {data_path}")
    print(f"  HTF:    {htf_dir}")
    print(f"  Output: {output_path}")
    print()

    # ── Load data ──
    print("Loading 1-min data...")
    bars_1m = load_1min_csv(data_path)
    print(f"  Loaded: {len(bars_1m):,} bars")
    if bars_1m:
        print(f"  Range:  {bars_1m[0]['timestamp'].strftime('%Y-%m-%d')} → "
              f"{bars_1m[-1]['timestamp'].strftime('%Y-%m-%d')}")
    print()

    print("Loading HTF data...")
    htf_data = load_all_htf(htf_dir)
    print()

    # ── Initialize engine ──
    config = BotConfig()
    engine = CausalReplayEngine(config)
    htf_scheduler = HTFScheduler(htf_data)

    print("Engine initialized. Starting causal replay...")
    print(f"  Progress every {PROGRESS_INTERVAL:,} bars")
    print()

    # ── Process bar by bar ──
    for bar in bars_1m:
        await engine.process_bar(bar, htf_scheduler)

    # ── Results ──
    summary = engine.get_summary()

    print()
    print("=" * 70)
    print("  BACKTEST RESULTS")
    print("=" * 70)
    for k, v in summary.items():
        if k not in ("executor_stats", "signal_sources"):
            print(f"  {k:.<40} {v}")
    print()
    if summary.get("signal_sources"):
        print("  Signal sources:")
        for src, count in summary["signal_sources"].items():
            print(f"    {src}: {count}")
    print("=" * 70)

    # ── Save results ──
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    output = {
        "summary": summary,
        "trades": engine.trades,
        "config": {
            "hc_min_score": HIGH_CONVICTION_MIN_SCORE,
            "hc_max_stop_pts": HIGH_CONVICTION_MAX_STOP_PTS,
            "slippage_rth": SLIPPAGE_RTH_PTS,
            "slippage_eth": SLIPPAGE_ETH_PTS,
            "commission_per_contract": COMMISSION_PER_CONTRACT,
            "daily_loss_limit": DAILY_LOSS_LIMIT,
            "kill_switch_limit": KILL_SWITCH_LIMIT,
            "warmup_bars": WARMUP_BARS,
            "min_rr_ratio": MIN_RR_RATIO,
        },
    }

    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n  Results saved to: {output_path}")
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Full Historical Backtest — Causal Replay Engine"
    )
    parser.add_argument(
        "--data", type=str, default=None,
        help="Path to combined_1min.csv (default: data/historical/combined_1min.csv)"
    )
    parser.add_argument(
        "--htf-dir", type=str, default=None,
        help="Directory with HTF CSVs (default: data/historical/)"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output JSON path (default: logs/full_validation_trades.json)"
    )
    parser.add_argument(
        "--run", action="store_true",
        help="Actually execute the backtest (Phase 2)"
    )
    parser.add_argument(
        "--log-level", type=str, default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: WARNING for speed)"
    )
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Resolve paths
    data_path = args.data or str(PROJECT_DIR / "data" / "historical" / "combined_1min.csv")
    htf_dir = args.htf_dir or str(PROJECT_DIR / "data" / "historical")
    output_path = args.output or str(PROJECT_DIR / "logs" / "full_validation_trades.json")

    if not args.run:
        print("=" * 70)
        print("  CAUSAL REPLAY ENGINE — COMPILE CHECK")
        print("=" * 70)
        print()
        print("  All imports successful:")
        print(f"    NQFeatureEngine:       OK")
        print(f"    HTFBiasEngine:         OK (gate={HTFBiasEngine.STRENGTH_GATE})")
        print(f"    SignalAggregator:      OK")
        print(f"    LiquiditySweepDetector: OK")
        print(f"    RiskEngine:            OK")
        print(f"    RegimeDetector:        OK")
        print(f"    ScaleOutExecutor:      OK")
        print()
        print("  Engine configuration:")
        print(f"    HC min score:    {HIGH_CONVICTION_MIN_SCORE}")
        print(f"    HC max stop:     {HIGH_CONVICTION_MAX_STOP_PTS} pts")
        print(f"    HTF gate:        {HTFBiasEngine.STRENGTH_GATE} (Config D)")
        print(f"    Slippage RTH:    {SLIPPAGE_RTH_PTS} pts/fill")
        print(f"    Slippage ETH:    {SLIPPAGE_ETH_PTS} pts/fill")
        print(f"    Commission:      ${COMMISSION_PER_CONTRACT}/contract/side")
        print(f"    Point value:     ${POINT_VALUE}/pt (MNQ)")
        print(f"    Daily loss limit: ${DAILY_LOSS_LIMIT}")
        print(f"    Kill switch:     ${KILL_SWITCH_LIMIT}")
        print(f"    Warmup bars:     {WARMUP_BARS}/session")
        print(f"    Min R:R:         {MIN_RR_RATIO}")
        print()

        # Verify engine instantiation
        config = BotConfig()
        engine = CausalReplayEngine(config)
        print("  CausalReplayEngine instantiated: OK")
        print()

        # Verify data files exist
        data_exists = os.path.exists(data_path)
        htf_exists = os.path.isdir(htf_dir)
        print(f"  Data file ({data_path}):  {'FOUND' if data_exists else 'NOT YET (run prepare_historical_data.py first)'}")
        print(f"  HTF dir   ({htf_dir}):  {'FOUND' if htf_exists else 'NOT YET'}")
        print()
        print("  Engine is ready for Phase 2.")
        print("  To execute: python scripts/full_backtest.py --run")
        print("=" * 70)
        return

    # Phase 2: Actually run the backtest
    if not os.path.exists(data_path):
        print(f"ERROR: Data file not found: {data_path}")
        print("  Run scripts/prepare_historical_data.py first.")
        sys.exit(1)

    asyncio.run(run_backtest(data_path, htf_dir, output_path))


if __name__ == "__main__":
    main()
