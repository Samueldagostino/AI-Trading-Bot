"""
Replay Simulator — Real-Time Paper Trading Validation
=======================================================
Replays historical FirstRate 1m data through the EXACT same pipeline
as the backtester and paper trading runner. Validates that the
pipeline produces consistent results before going live.

Modes:
  Real-time replay:  Feeds bars at configurable speed with live dashboard
  Validate mode:     Runs at max speed, compares output to OOS baseline

Pipeline (identical to run_paper.py):
  FirstRate 1m bars -> aggregate to 2m
    -> TradingOrchestrator.process_bar()  (HC filter + HTF gate)
      -> ScaleOutExecutor  (trade lifecycle)
        -> Fill simulation  (slippage + commission)
          -> Log to paper_trades.json / paper_decisions.json

Session rules enforced:
  - No entries before 6:01 PM ET
  - Flat by 4:30 PM ET
  - No trading during maintenance (5:00–6:00 PM ET)
  - Daily loss limit: $500 -> halt for the day
  - Max position: 2 contracts

Fill simulation (calibrated slippage — permanent model):
  - RTH (9:30-16:00 ET): 0.50pt base, +0.50 vol spike, cap 1.50pt
  - ETH (18:01-9:29 ET): 1.00pt base, +0.75 vol spike, cap 2.50pt
  - News window (FOMC/CPI/NFP): +1.00pt, cap 3.00pt
  - All fills (entry + exit + stop) get adverse slippage

C1 Exit: Variant C (Trail from Profit) is now the DEFAULT.
  - Trail activates once unrealized profit >= 3.0pts
  - Trail distance: 2.5pts from HWM
  - Fallback: market exit at bar 12 if trailing never activates
  Old variants (baseline/A/B/D) still available via --c1-variant flag.

Usage:
    # Real-time replay at 100x speed, starting from 2025-10-01
    python scripts/replay_simulator.py --speed 100 --start-date 2025-10-01

    # Max speed with dashboard
    python scripts/replay_simulator.py --speed max --start-date 2025-09-01

    # Validate mode — compare to OOS baseline (uses Variant C by default)
    python scripts/replay_simulator.py --validate

    # Test old baseline C1 (Time 10) for comparison
    python scripts/replay_simulator.py --validate --c1-variant baseline

    # Compare all 5 variants (baseline + A/B/C/D)
    python scripts/replay_simulator.py --validate --compare-all

    # Validate specific months
    python scripts/replay_simulator.py --validate --start-date 2025-09-01 --end-date 2026-03-01
"""

import argparse
import asyncio
import json
import logging
import math
import os
import random
import sys
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

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
from signals.liquidity_sweep import LiquiditySweepDetector

logger = logging.getLogger(__name__)

# ── Paths ──
LOGS_DIR = project_dir / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
TRADES_LOG = LOGS_DIR / "paper_trades.json"
DECISIONS_LOG = LOGS_DIR / "paper_decisions.json"

# ── OOS Baseline (Config D + C1 Variant C + Calibrated Slippage, Sep 2025 – Feb 2026) ──
OOS_BASELINE = {
    "total_trades": 1161,
    "trades_per_month": 194,
    "win_rate": 62.0,
    "profit_factor": 1.61,
    "total_pnl": 15894.0,
    "pnl_per_month": 2649.0,
    "expectancy": 13.69,
    "max_drawdown_pct": 1.4,
    "c1_pnl": 6382.0,
    "c2_pnl": 9512.0,
    "months": 6,
}

# ── Session rules (ET times) ──
SESSION_OPEN_HOUR = 18    # 6:00 PM ET
SESSION_OPEN_MINUTE = 1   # 6:01 PM ET
SESSION_CLOSE_HOUR = 16   # 4:30 PM ET
SESSION_CLOSE_MINUTE = 30
MAINTENANCE_START = 17    # 5:00 PM ET
MAINTENANCE_END = 18      # 6:00 PM ET

# ── Safety limits ──
DAILY_LOSS_LIMIT = 500.0
MAX_CONTRACTS = 2

EXEC_TF = "2m"


# ================================================================
# DATA LOADING (reuses run_oos_validation.py pattern)
# ================================================================
def load_firstrate_mtf(data_dir: str) -> Dict[str, List[BarData]]:
    """Load aggregated FirstRate CSVs by timeframe."""
    dir_path = Path(data_dir)
    if not dir_path.exists():
        print(f"ERROR: Data directory not found: {data_dir}")
        sys.exit(1)

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
            continue
        bars = importer.import_file(str(csv_file))
        if bars:
            for bar in bars:
                bar.source = "firstrate"
            tf_bars[tf_label] = bars
            logger.info(f"  Loaded {tf_label}: {len(bars):,} bars")

    return tf_bars


def filter_by_date(
    tf_bars: Dict[str, List[BarData]],
    start_date: Optional[str],
    end_date: Optional[str],
) -> Dict[str, List[BarData]]:
    """Filter bars to a date range."""
    if not start_date and not end_date:
        return tf_bars

    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(
        tzinfo=timezone.utc) if start_date else None
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
        tzinfo=timezone.utc) if end_date else None

    filtered = {}
    for tf, bars in tf_bars.items():
        f = bars
        if start_dt:
            f = [b for b in f if b.timestamp >= start_dt]
        if end_dt:
            f = [b for b in f if b.timestamp < end_dt]
        if f:
            filtered[tf] = f

    return filtered


# ================================================================
# SESSION RULES (same logic as TradovatePaperConnector)
# ================================================================
def bar_to_et(bar_time: datetime) -> datetime:
    """Convert a UTC bar timestamp to ET — DST-aware via ZoneInfo."""
    return bar_time.astimezone(ZoneInfo("America/New_York"))


def is_within_session(et_time: datetime) -> bool:
    """Check if ET time is within trading session (6:01 PM – 4:30 PM next day)."""
    h, m = et_time.hour, et_time.minute

    # Maintenance window 5:00–6:00 PM ET
    if h == MAINTENANCE_START:
        return False
    # Before session open (6:01 PM)
    if h == SESSION_OPEN_HOUR and m < SESSION_OPEN_MINUTE:
        return False
    # After session close (4:30 PM)
    if h == SESSION_CLOSE_HOUR and m >= SESSION_CLOSE_MINUTE:
        return False
    if SESSION_CLOSE_HOUR < h < MAINTENANCE_START:
        return False
    return True


def should_be_flat(et_time: datetime) -> bool:
    """Check if we should be flat (approaching session close or maintenance)."""
    h, m = et_time.hour, et_time.minute

    if h == SESSION_CLOSE_HOUR and m >= SESSION_CLOSE_MINUTE:
        return True
    if h == MAINTENANCE_START:
        return True
    if SESSION_CLOSE_HOUR < h < MAINTENANCE_START:
        return True
    return False


# ================================================================
# DYNAMIC SLIPPAGE ENGINE
# ================================================================
# News windows: FOMC meetings, CPI, PPI, NFP, GDP, PCE, ISM
# These are approximate dates for the OOS window (Sep 2025–Feb 2026)
NEWS_WINDOWS_UTC = [
    # FOMC decisions (2:00 PM ET = 19:00 UTC, ±2 hours)
    ("2025-09-17 17:00", "2025-09-17 21:00"),
    ("2025-11-05 17:00", "2025-11-05 21:00"),
    ("2025-12-17 17:00", "2025-12-17 21:00"),
    ("2026-01-28 17:00", "2026-01-28 21:00"),
    # CPI releases (8:30 AM ET = 13:30 UTC, ±1 hour)
    ("2025-09-10 12:30", "2025-09-10 14:30"),
    ("2025-10-10 12:30", "2025-10-10 14:30"),
    ("2025-11-13 12:30", "2025-11-13 14:30"),
    ("2025-12-10 12:30", "2025-12-10 14:30"),
    ("2026-01-15 12:30", "2026-01-15 14:30"),
    ("2026-02-12 12:30", "2026-02-12 14:30"),
    # NFP (first Friday, 8:30 AM ET = 13:30 UTC, ±1 hour)
    ("2025-09-05 12:30", "2025-09-05 14:30"),
    ("2025-10-03 12:30", "2025-10-03 14:30"),
    ("2025-11-07 12:30", "2025-11-07 14:30"),
    ("2025-12-05 12:30", "2025-12-05 14:30"),
    ("2026-01-10 12:30", "2026-01-10 14:30"),
    ("2026-02-06 12:30", "2026-02-06 14:30"),
]

# Pre-parse news windows
_NEWS_RANGES = []
for _start, _end in NEWS_WINDOWS_UTC:
    _NEWS_RANGES.append((
        datetime.strptime(_start, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc),
        datetime.strptime(_end, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc),
    ))


class DynamicSlippageEngine:
    """
    Calibrated slippage model (v2) based on:
    - Time of day (RTH vs ETH)
    - Volume spikes (>2.0x of 20-bar average)
    - News windows (FOMC, CPI, NFP)

    Slippage tiers (per fill, always adverse):
      RTH (9:30-16:00 ET):  0.50pt base, +0.50 vol spike, cap 1.50pt
      ETH (18:01-9:29 ET):  1.00pt base, +0.75 vol spike, cap 2.50pt
      News window:          +1.00pt addon, cap 3.00pt
    """

    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)
        self.volume_window: deque = deque(maxlen=20)
        self.total_slippage_points = 0.0
        self.fill_count = 0
        self.entry_slippage_total = 0.0
        self.exit_slippage_total = 0.0
        self.news_fills = 0
        self.vol_spike_fills = 0

    def feed_volume(self, volume: int) -> None:
        """Feed bar volume to maintain rolling 20-bar average."""
        self.volume_window.append(volume)

    def _avg_volume(self) -> float:
        if len(self.volume_window) == 0:
            return 0.0
        return sum(self.volume_window) / len(self.volume_window)

    def _is_volume_spike(self, current_volume: int) -> bool:
        avg = self._avg_volume()
        if avg <= 0:
            return False
        return current_volume > avg * 2.0  # 2.0x threshold (was 1.5x)

    @staticmethod
    def _is_news_window(bar_time: datetime) -> bool:
        for start, end in _NEWS_RANGES:
            if start <= bar_time <= end:
                return True
        return False

    @staticmethod
    def _session_tier(et_time: datetime) -> str:
        """Determine slippage tier based on ET time of day."""
        h, m = et_time.hour, et_time.minute
        t = h + m / 60.0
        # RTH: 9:30 – 16:00 ET
        if 9.5 <= t < 16.0:
            return "rth"
        # ETH: everything else (18:01-9:29 next day)
        return "eth"

    def compute_slippage(
        self,
        bar_time: datetime,
        et_time: datetime,
        current_volume: int,
        fill_type: str = "entry",  # "entry", "exit", "stop"
    ) -> float:
        """
        Compute calibrated slippage in points for a single fill.
        Always positive (caller applies adverse direction).
        """
        tier = self._session_tier(et_time)

        if tier == "rth":
            base = 0.50
            vol_addon_range = (0.25, 0.50)
            cap = 1.50
        else:  # eth
            base = 1.00
            vol_addon_range = (0.50, 0.75)
            cap = 2.50

        # Volume spike addon (>2.0x 20-bar avg)
        vol_addon = 0.0
        if self._is_volume_spike(current_volume):
            vol_addon = self.rng.uniform(*vol_addon_range)
            self.vol_spike_fills += 1

        # News window addon
        news_addon = 0.0
        is_news = self._is_news_window(bar_time)
        if is_news:
            news_addon = 1.00
            cap = 3.00  # raise cap during news
            self.news_fills += 1

        total = base + vol_addon + news_addon

        # Apply cap
        total = min(total, cap)

        # Round to nearest 0.25 (tick size)
        total = round(total * 4) / 4

        # Track stats
        self.total_slippage_points += total
        self.fill_count += 1
        if fill_type == "entry":
            self.entry_slippage_total += total
        else:
            self.exit_slippage_total += total

        return total

    def apply_adverse_slippage(
        self,
        price: float,
        direction: str,
        fill_type: str,
        bar_time: datetime,
        et_time: datetime,
        current_volume: int,
    ) -> Tuple[float, float]:
        """
        Apply adverse slippage to a fill price.
        Returns (adjusted_price, slippage_points).

        Entry long: price + slippage (pay more)
        Entry short: price - slippage (sell lower)
        Exit long: price - slippage (get less)
        Exit short: price + slippage (buy back higher)
        Stop long: price - slippage (gap through stop)
        Stop short: price + slippage (gap through stop)
        """
        slippage = self.compute_slippage(bar_time, et_time, current_volume, fill_type)

        if fill_type == "entry":
            # Entry slippage: against your entry direction
            if direction == "long":
                adjusted = price + slippage
            else:
                adjusted = price - slippage
        else:
            # Exit/stop slippage: against your exit (you get worse fill)
            if direction == "long":
                adjusted = price - slippage
            else:
                adjusted = price + slippage

        return round(adjusted, 2), slippage

    @property
    def avg_slippage(self) -> float:
        if self.fill_count == 0:
            return 0.0
        return self.total_slippage_points / self.fill_count

    def summary(self) -> Dict:
        return {
            "total_fills": self.fill_count,
            "total_slippage_points": round(self.total_slippage_points, 2),
            "avg_slippage_per_fill": round(self.avg_slippage, 2),
            "entry_slippage_total": round(self.entry_slippage_total, 2),
            "exit_slippage_total": round(self.exit_slippage_total, 2),
            "news_window_fills": self.news_fills,
            "volume_spike_fills": self.vol_spike_fills,
        }


# ================================================================
# REPLAY STATE
# ================================================================
class ReplayState:
    """Tracks replay session state."""

    def __init__(self):
        # Position tracking
        self.has_position = False
        self.position_direction = ""
        self.position_entry_price = 0.0
        self.position_stop = 0.0
        self.position_score = 0.0
        self.position_entry_time = ""

        # Daily state
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.daily_wins = 0
        self.daily_losses = 0
        self.daily_loss_limit_hit = False
        self.current_date = ""

        # Session totals
        self.total_trades = 0
        self.total_pnl = 0.0
        self.total_wins = 0
        self.total_losses = 0
        self.c1_pnl = 0.0
        self.c2_pnl = 0.0

        # Filter stats
        self.hc_score_blocks = 0
        self.hc_stop_blocks = 0
        self.htf_blocks = 0
        self.session_blocks = 0

        # Bars
        self.bars_processed = 0
        self.exec_bars_processed = 0
        self.htf_bars_processed = 0
        self.current_price = 0.0
        self.current_time = ""

        # Equity
        self.equity = CONFIG.risk.account_size
        self.peak_equity = CONFIG.risk.account_size
        self.max_drawdown_pct = 0.0

        # Decisions log
        self.decisions: List[Dict] = []
        self.trades_log: List[Dict] = []

        # Session flattens
        self.session_flattens = 0

        # Slippage tracking
        self.total_slippage_cost = 0.0  # Dollars lost to slippage
        self.friction_losses = 0  # Trades where C1 profit < slippage cost

        # Sweep tracking
        self.sweep_trades = 0         # Trades triggered by sweep signal
        self.sweep_wins = 0
        self.sweep_losses = 0
        self.sweep_pnl = 0.0
        self.confluence_trades = 0    # Trades with sweep+signal confluence
        self.confluence_wins = 0
        self.confluence_pnl = 0.0
        self.signal_only_trades = 0   # Trades from existing signals only
        self.signal_only_wins = 0
        self.signal_only_pnl = 0.0

    def reset_daily(self, date_str: str) -> None:
        """Reset daily counters for a new trading day."""
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.daily_wins = 0
        self.daily_losses = 0
        self.daily_loss_limit_hit = False
        self.current_date = date_str

    def record_trade(self, pnl: float, c1_pnl: float, c2_pnl: float) -> None:
        """Record a completed trade."""
        self.total_trades += 1
        self.total_pnl += pnl
        self.c1_pnl += c1_pnl
        self.c2_pnl += c2_pnl
        self.daily_trades += 1
        self.daily_pnl += pnl

        if pnl > 0:
            self.total_wins += 1
            self.daily_wins += 1
        elif pnl < 0:
            self.total_losses += 1
            self.daily_losses += 1

        # Equity tracking
        self.equity += pnl
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
        dd = (self.peak_equity - self.equity) / self.peak_equity * 100
        if dd > self.max_drawdown_pct:
            self.max_drawdown_pct = dd

        # Daily loss limit
        if self.daily_pnl <= -DAILY_LOSS_LIMIT:
            self.daily_loss_limit_hit = True

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.total_wins / self.total_trades * 100

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.get("total_pnl", 0) for t in self.trades_log
                          if t.get("total_pnl", 0) > 0)
        gross_loss = abs(sum(t.get("total_pnl", 0) for t in self.trades_log
                            if t.get("total_pnl", 0) < 0))
        if gross_loss == 0:
            return float('inf') if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def expectancy(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.total_pnl / self.total_trades


# ================================================================
# LIVE DASHBOARD
# ================================================================
def render_dashboard(state: ReplayState, speed: str, elapsed_secs: float) -> str:
    """Render terminal dashboard."""
    lines = []

    lines.append("")
    lines.append(f"  REPLAY SIMULATOR — Config D + C1 Trail from Profit")
    lines.append(f"  Speed: {speed} | Elapsed: {elapsed_secs:.0f}s | "
                 f"Bar: {state.current_time[:19] if state.current_time else '—'}")
    lines.append(f"  {'=' * 60}")

    # Current position
    lines.append(f"  POSITION")
    lines.append(f"  {'─' * 60}")
    if state.has_position:
        lines.append(f"  {state.position_direction.upper()} @ "
                     f"{state.position_entry_price:.2f} | "
                     f"Stop: {state.position_stop:.2f} | "
                     f"Score: {state.position_score:.3f}")
        lines.append(f"  Entry: {state.position_entry_time[:19]}")
        if state.current_price > 0:
            if state.position_direction == "long":
                unrealized = (state.current_price - state.position_entry_price) * 2 * 2
            else:
                unrealized = (state.position_entry_price - state.current_price) * 2 * 2
            lines.append(f"  Unrealized: ${unrealized:+.2f}  "
                         f"(price: {state.current_price:.2f})")
    else:
        lines.append(f"  FLAT")
    lines.append("")

    # Today's stats
    today_wr = (state.daily_wins / state.daily_trades * 100
                if state.daily_trades > 0 else 0)
    lines.append(f"  TODAY ({state.current_date})")
    lines.append(f"  {'─' * 60}")
    lines.append(f"  Trades: {state.daily_trades} | "
                 f"PnL: ${state.daily_pnl:+.2f} | "
                 f"WR: {today_wr:.0f}% | "
                 f"Limit: {'HIT' if state.daily_loss_limit_hit else 'OK'}")
    lines.append("")

    # Session totals
    lines.append(f"  SESSION TOTALS")
    lines.append(f"  {'─' * 60}")
    pf = state.profit_factor
    pf_str = f"{pf:.2f}" if pf < 100 else "inf"
    lines.append(f"  Trades: {state.total_trades:>5} | "
                 f"WR: {state.win_rate:.1f}% (OOS: {OOS_BASELINE['win_rate']}%) | "
                 f"PF: {pf_str} (OOS: {OOS_BASELINE['profit_factor']})")
    lines.append(f"  PnL:  ${state.total_pnl:>+10,.2f} | "
                 f"Exp: ${state.expectancy:+.2f}/trade (OOS: ${OOS_BASELINE['expectancy']:.2f})")
    lines.append(f"  C1:   ${state.c1_pnl:>+10,.2f} | "
                 f"C2: ${state.c2_pnl:+,.2f}")
    lines.append(f"  DD:   {state.max_drawdown_pct:.2f}% (OOS: {OOS_BASELINE['max_drawdown_pct']}%)")
    lines.append("")

    # Pipeline stats
    lines.append(f"  PIPELINE")
    lines.append(f"  {'─' * 60}")
    lines.append(f"  Exec bars: {state.exec_bars_processed:,} | "
                 f"HTF bars: {state.htf_bars_processed:,}")
    lines.append(f"  Session blocks: {state.session_blocks} | "
                 f"HTF blocks: {state.htf_blocks}")
    lines.append(f"  Flattens: {state.session_flattens}")
    lines.append(f"  Slippage cost: ${state.total_slippage_cost:,.2f}")
    lines.append("")
    lines.append(f"  {'=' * 60}")

    return "\n".join(lines)


# ================================================================
# REPLAY ENGINE
# ================================================================
class ReplaySimulator:
    """Replays historical data through the production pipeline."""

    # C1 variant descriptions
    C1_VARIANTS = {
        "baseline": "Original Time 10 bars (exit if profitable)",
        "A": "Minimum Profit Gate (>=4pts or convert to trail)",
        "B": "Fixed TP at entry+6pts (limit=0 slip), fallback 15 bars",
        "C": "Trail from profit (>=3pts -> trail 2.5pt), fallback 12 bars",
        "D": "RTH-only + Original Time 10 (restrict to 9:30-16:00 ET)",
    }

    def __init__(
        self,
        speed: str = "max",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        validate: bool = False,
        data_dir: Optional[str] = None,
        c1_variant: str = "C",
        quiet: bool = False,
        sweep_enabled: bool = True,
    ):
        # Default validate mode to the OOS window
        if validate and not start_date:
            start_date = "2025-09-01"
        if validate and not end_date:
            end_date = "2026-03-01"

        self.speed = speed
        self.start_date = start_date
        self.end_date = end_date
        self.validate = validate
        self.data_dir = data_dir or str(project_dir / "data" / "firstrate")
        self.c1_variant = c1_variant
        self.quiet = quiet  # suppress verbose output in compare-all mode
        self.sweep_enabled = sweep_enabled

        self.state = ReplayState()
        self.bot: Optional[TradingOrchestrator] = None
        self.slippage_engine = DynamicSlippageEngine(seed=42)

        # Per-trade slippage accumulator (reset on each new trade)
        self._current_trade_slippage = 0.0
        self._current_trade_entry_slippage = 0.0
        self._current_trade_exit_slippage = 0.0
        self._current_bar_volume = 0
        self._current_bar_time: Optional[datetime] = None
        self._current_et_time: Optional[datetime] = None

        # Track signal source for current open trade
        self._current_trade_source = "signal"

        # Speed -> delay between exec bars (seconds)
        self._delay = self._parse_speed(speed)

    @staticmethod
    def _parse_speed(speed: str) -> float:
        """Convert speed flag to inter-bar delay in seconds.
        Real 2m bar = 120s. 1x = 120s, 10x = 12s, 100x = 1.2s, max = 0.
        """
        if speed == "max":
            return 0.0
        try:
            multiplier = float(speed)
            if multiplier <= 0:
                return 0.0
            return 120.0 / multiplier
        except ValueError:
            return 0.0

    async def run(self) -> Dict:
        """Main entry point — load data, replay, return results."""
        t0 = time.time()

        # ── Load data ──
        variant_desc = self.C1_VARIANTS.get(self.c1_variant, self.c1_variant)
        sweep_str = "ENABLED" if self.sweep_enabled else "DISABLED"
        if not self.quiet:
            print(f"\n{'=' * 62}")
            print(f"  REPLAY SIMULATOR — Config D")
            print(f"  C1 variant: {self.c1_variant} — {variant_desc}")
            print(f"  Sweep detector: {sweep_str}")
            print(f"  Speed: {self.speed} | Validate: {self.validate}")
            if self.start_date:
                print(f"  Start: {self.start_date}")
            if self.end_date:
                print(f"  End:   {self.end_date}")
            print(f"{'=' * 62}\n")

        if not self.quiet:
            print("Loading FirstRate data...")
        tf_bars = load_firstrate_mtf(self.data_dir)

        if not tf_bars or EXEC_TF not in tf_bars:
            print(f"ERROR: No {EXEC_TF} data found in {self.data_dir}")
            print("Run: python scripts/aggregate_1m.py --output-dir data/firstrate/")
            sys.exit(1)

        # Print data summary
        if not self.quiet:
            for tf in sorted(tf_bars.keys()):
                bars = tf_bars[tf]
                print(f"  {tf:>4s}: {len(bars):>7,} bars  "
                      f"({bars[0].timestamp.strftime('%Y-%m-%d')} -> "
                      f"{bars[-1].timestamp.strftime('%Y-%m-%d')})")

        # Filter by date
        tf_bars = filter_by_date(tf_bars, self.start_date, self.end_date)

        if EXEC_TF not in tf_bars:
            print(f"\nERROR: No {EXEC_TF} data in date range")
            sys.exit(1)

        exec_count = len(tf_bars.get(EXEC_TF, []))
        if not self.quiet:
            print(f"\nReplay window: {exec_count:,} exec bars")

        # ── Build MTF iterator ──
        pipeline = DataPipeline(CONFIG)
        mtf_iterator = pipeline.create_mtf_iterator(tf_bars)
        if not self.quiet:
            print(f"Total bars (all TFs): {len(mtf_iterator):,}")

        # ── Initialize orchestrator ──
        CONFIG.execution.paper_trading = True
        self.bot = TradingOrchestrator(CONFIG)
        self.bot._sweep_enabled = self.sweep_enabled
        await self.bot.initialize(skip_db=True)

        # ── Patch executor with dynamic slippage ──
        self._patch_executor_slippage()

        if not self.quiet:
            print(f"\nStarting replay...\n")

        # ── Replay loop ──
        last_date = ""
        dashboard_update_counter = 0
        dashboard_interval = max(1, exec_count // 200) if self.validate else 1

        for i, (timeframe, bar_data) in enumerate(mtf_iterator):
            self.state.bars_processed += 1

            if timeframe in HTF_TIMEFRAMES:
                # Route to HTF engine
                self.bot.process_htf_bar(timeframe, bar_data)
                self.state.htf_bars_processed += 1
                continue

            if timeframe != EXEC_TF:
                continue

            # ── Execution bar ──
            self.state.exec_bars_processed += 1
            self.state.current_price = bar_data.close
            self.state.current_time = bar_data.timestamp.isoformat()

            # Feed volume to slippage engine
            self.slippage_engine.feed_volume(bar_data.volume)
            self._current_bar_volume = bar_data.volume
            self._current_bar_time = bar_data.timestamp
            self._current_et_time = bar_to_et(bar_data.timestamp)

            # Daily reset check
            date_str = bar_data.timestamp.strftime("%Y-%m-%d")
            if date_str != last_date:
                # Flatten any open position at day boundary
                if self.bot.executor.has_active_trade and last_date:
                    result = await self.bot.executor.emergency_flatten(
                        bar_data.close
                    )
                    if result:
                        self._record_trade_result(result, bar_data.timestamp)
                        self.state.session_flattens += 1

                self.state.reset_daily(date_str)
                last_date = date_str

                # Reset risk engine daily state (matches real paper trading)
                risk_state = self.bot.risk_engine.state
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

            # Session rules
            et_time = bar_to_et(bar_data.timestamp)

            if not is_within_session(et_time):
                self.state.session_blocks += 1
                continue

            # Variant D: restrict to RTH only (9:30-16:00 ET)
            if self.c1_variant == "D":
                h, m = et_time.hour, et_time.minute
                t = h + m / 60.0
                if t < 9.5 or t >= 16.0:
                    self.state.session_blocks += 1
                    continue

            # Daily loss limit
            if self.state.daily_loss_limit_hit:
                continue

            # Should be flat?
            if should_be_flat(et_time):
                if self.bot.executor.has_active_trade:
                    result = await self.bot.executor.emergency_flatten(
                        bar_data.close
                    )
                    if result:
                        self._record_trade_result(result, bar_data.timestamp)
                        self.state.session_flattens += 1
                        self._log_decision("session_flatten", {
                            "time_et": et_time.strftime("%H:%M"),
                            "price": bar_data.close,
                        }, bar_data.timestamp)
                continue

            # ── Process through pipeline ──
            exec_bar = bardata_to_bar(bar_data)
            result = await self.bot.process_bar(exec_bar)

            if result:
                self._handle_result(result, bar_data.timestamp)

            # ── Dashboard update ──
            dashboard_update_counter += 1
            if not self.validate and dashboard_update_counter >= dashboard_interval:
                dashboard_update_counter = 0
                elapsed = time.time() - t0
                os.system("clear" if os.name != "nt" else "cls")
                print(render_dashboard(self.state, self.speed, elapsed))

            # ── Speed throttle ──
            if self._delay > 0:
                await asyncio.sleep(self._delay)

        # ── Final flatten ──
        if self.bot.executor.has_active_trade:
            last_bar = tf_bars[EXEC_TF][-1]
            result = await self.bot.executor.emergency_flatten(last_bar.close)
            if result:
                self._record_trade_result(result, last_bar.timestamp)

        elapsed = time.time() - t0

        # ── Save logs ──
        self._save_logs()

        # ── Print final dashboard ──
        if not self.validate:
            os.system("clear" if os.name != "nt" else "cls")
        print(render_dashboard(self.state, self.speed, elapsed))

        # ── Build results ──
        results = self._build_results(elapsed)

        if self.validate:
            self._run_validation(results)

        return results

    # ================================================================
    # DYNAMIC SLIPPAGE PATCHING
    # ================================================================
    def _patch_executor_slippage(self) -> None:
        """
        Monkey-patch the executor to use calibrated slippage + C1 variant.
        """
        executor = self.bot.executor
        sim = self
        variant = self.c1_variant

        # ── Shared patched fills ──────────────────────────────────

        async def patched_paper_enter(trade, price):
            """Entry fill with calibrated slippage."""
            bar_time = sim._current_bar_time or datetime.now(timezone.utc)
            et_time = sim._current_et_time or bar_to_et(bar_time)
            volume = sim._current_bar_volume

            adjusted_price, slippage = sim.slippage_engine.apply_adverse_slippage(
                price, trade.direction, "entry", bar_time, et_time, volume
            )

            # Reset per-trade accumulators
            sim._current_trade_slippage = slippage
            sim._current_trade_entry_slippage = slippage
            sim._current_trade_exit_slippage = 0.0

            fill_price = round(adjusted_price, 2)
            now = datetime.now(timezone.utc)

            for leg in [trade.c1, trade.c2]:
                leg.entry_price = fill_price
                leg.entry_time = now
                leg.is_filled = True
                leg.is_open = True
                leg.commission = executor.risk_config.commission_per_contract

            trade.entry_price = fill_price
            trade.entry_time = now
            trade.c2_best_price = fill_price

            from execution.scale_out_executor import ScaleOutPhase
            trade._set_phase(ScaleOutPhase.PHASE_1)

        async def patched_close_c2(trade, price, time, reason):
            """C2 exit with calibrated slippage."""
            bar_time = sim._current_bar_time or time
            et_time = sim._current_et_time or bar_to_et(bar_time)
            volume = sim._current_bar_volume

            fill_type = "stop" if reason in ("trailing", "breakeven", "stop") else "exit"
            adjusted_price, slippage = sim.slippage_engine.apply_adverse_slippage(
                price, trade.direction, fill_type, bar_time, et_time, volume
            )
            sim._current_trade_slippage += slippage
            sim._current_trade_exit_slippage += slippage

            trade.c2.exit_price = round(adjusted_price, 2)
            trade.c2.exit_time = time
            trade.c2.exit_reason = reason
            trade.c2.is_open = False
            trade.c2.gross_pnl = executor._compute_leg_pnl(trade.c2, trade.direction)
            trade.c2.net_pnl = trade.c2.gross_pnl - trade.c2.commission

            return executor._finalize_trade(trade, time)

        async def patched_close_all(trade, price, time, reason):
            """Both contracts stopped out with calibrated slippage."""
            bar_time = sim._current_bar_time or time
            et_time = sim._current_et_time or bar_to_et(bar_time)
            volume = sim._current_bar_volume

            for leg in [trade.c1, trade.c2]:
                if leg.is_open:
                    adjusted_price, slippage = sim.slippage_engine.apply_adverse_slippage(
                        price, trade.direction, "stop", bar_time, et_time, volume
                    )
                    sim._current_trade_slippage += slippage
                    sim._current_trade_exit_slippage += slippage

                    leg.exit_price = round(adjusted_price, 2)
                    leg.exit_time = time
                    leg.exit_reason = reason
                    leg.is_open = False
                    leg.gross_pnl = executor._compute_leg_pnl(leg, trade.direction)
                    leg.net_pnl = leg.gross_pnl - leg.commission

            return executor._finalize_trade(trade, time)

        # ── Helper: close C1 with slippage and transition to runner ─

        def _close_c1_and_run(trade, exit_price, time, reason, slippage_pts):
            """Close C1, move C2 stop to BE, transition to runner."""
            from execution.scale_out_executor import ScaleOutPhase
            direction = trade.direction

            trade.c1.exit_price = round(exit_price, 2)
            trade.c1.exit_time = time
            trade.c1.exit_reason = reason
            trade.c1.is_open = False
            trade.c1.gross_pnl = executor._compute_leg_pnl(trade.c1, trade.direction)
            trade.c1.net_pnl = trade.c1.gross_pnl - trade.c1.commission

            sim._current_trade_slippage += slippage_pts
            sim._current_trade_exit_slippage += slippage_pts

            # Move C2 stop to breakeven + buffer
            if executor.scale_config.c2_move_stop_to_breakeven:
                buffer = executor.scale_config.c2_breakeven_buffer_points
                if direction == "long":
                    new_stop = trade.entry_price + buffer
                else:
                    new_stop = trade.entry_price - buffer
                trade.c2.stop_price = round(new_stop, 2)

            trade._set_phase(ScaleOutPhase.C1_HIT)
            trade.c2_best_price = trade.c2.stop_price if trade.c2.stop_price else exit_price
            trade._set_phase(ScaleOutPhase.RUNNING)

            return {
                "action": "c1_time_exit",
                "c1_pnl": trade.c1.net_pnl,
                "c1_bars": trade.c1_bars_elapsed,
                "c2_new_stop": trade.c2.stop_price,
                "price": exit_price,
            }

        # ── Helper: get current slippage context ────────────────

        def _slip_ctx(time):
            bar_time = sim._current_bar_time or time
            et_time = sim._current_et_time or bar_to_et(bar_time)
            volume = sim._current_bar_volume
            return bar_time, et_time, volume

        # ── Phase 1 manager factory (per variant) ──────────────

        async def phase1_baseline(trade, price, time):
            """BASELINE: Original Time 10 bars, exit if profitable."""
            from execution.scale_out_executor import ScaleOutPhase
            direction = trade.direction

            # Check stop
            if (direction == "long" and price <= trade.initial_stop) or \
               (direction == "short" and price >= trade.initial_stop):
                return await patched_close_all(trade, price, time, "stop")

            trade.c1_bars_elapsed += 1
            c1_exit_bars = executor.scale_config.c1_time_exit_bars

            if trade.c1_bars_elapsed >= c1_exit_bars:
                c1_in_profit = (price > trade.entry_price) if direction == "long" else (price < trade.entry_price)
                if c1_in_profit:
                    bar_time, et_time, volume = _slip_ctx(time)
                    adj, slip = sim.slippage_engine.apply_adverse_slippage(
                        price, direction, "exit", bar_time, et_time, volume)
                    return _close_c1_and_run(trade, adj, time,
                                             f"time_{c1_exit_bars}bars", slip)

            # Update best price for C2 tracking
            if direction == "long":
                trade.c2_best_price = max(trade.c2_best_price, price)
            else:
                trade.c2_best_price = min(trade.c2_best_price, price) if trade.c2_best_price > 0 else price
            return None

        async def phase1_variant_a(trade, price, time):
            """VARIANT A: Minimum Profit Gate — exit only if profit >= 4.0pts."""
            from execution.scale_out_executor import ScaleOutPhase
            direction = trade.direction

            if (direction == "long" and price <= trade.initial_stop) or \
               (direction == "short" and price >= trade.initial_stop):
                return await patched_close_all(trade, price, time, "stop")

            trade.c1_bars_elapsed += 1
            c1_exit_bars = executor.scale_config.c1_time_exit_bars
            unrealized = abs(price - trade.entry_price) if \
                ((direction == "long" and price > trade.entry_price) or
                 (direction == "short" and price < trade.entry_price)) else 0.0

            if trade.c1_bars_elapsed >= c1_exit_bars:
                if unrealized >= 4.0:
                    # Enough profit to absorb slippage — exit C1
                    bar_time, et_time, volume = _slip_ctx(time)
                    adj, slip = sim.slippage_engine.apply_adverse_slippage(
                        price, direction, "exit", bar_time, et_time, volume)
                    return _close_c1_and_run(trade, adj, time,
                                             "min_profit_gate_4pt", slip)
                elif unrealized > 0:
                    # Profitable but < 4pts: convert C1 to trailing stop
                    # Use same C2 trail logic: trail 2.5pts from HWM
                    if not hasattr(trade, '_c1_trailing'):
                        trade._c1_trailing = True
                        trade._c1_hwm = price if direction == "long" else price
                        trade._c1_trail_distance = 2.5
                    # Fall through to trailing logic below

            # C1 trailing mode (activated by variant A when profit < 4pts at bar 10)
            if hasattr(trade, '_c1_trailing') and trade._c1_trailing:
                if direction == "long":
                    trade._c1_hwm = max(trade._c1_hwm, price)
                    c1_trail_stop = trade._c1_hwm - trade._c1_trail_distance
                    if price <= c1_trail_stop:
                        bar_time, et_time, volume = _slip_ctx(time)
                        adj, slip = sim.slippage_engine.apply_adverse_slippage(
                            c1_trail_stop, direction, "exit", bar_time, et_time, volume)
                        return _close_c1_and_run(trade, adj, time,
                                                 "c1_trail_stop", slip)
                else:
                    trade._c1_hwm = min(trade._c1_hwm, price)
                    c1_trail_stop = trade._c1_hwm + trade._c1_trail_distance
                    if price >= c1_trail_stop:
                        bar_time, et_time, volume = _slip_ctx(time)
                        adj, slip = sim.slippage_engine.apply_adverse_slippage(
                            c1_trail_stop, direction, "exit", bar_time, et_time, volume)
                        return _close_c1_and_run(trade, adj, time,
                                                 "c1_trail_stop", slip)

            if direction == "long":
                trade.c2_best_price = max(trade.c2_best_price, price)
            else:
                trade.c2_best_price = min(trade.c2_best_price, price) if trade.c2_best_price > 0 else price
            return None

        async def phase1_variant_b(trade, price, time):
            """VARIANT B: Fixed TP at entry+6pts (limit = 0 slippage)."""
            from execution.scale_out_executor import ScaleOutPhase
            direction = trade.direction

            if (direction == "long" and price <= trade.initial_stop) or \
               (direction == "short" and price >= trade.initial_stop):
                return await patched_close_all(trade, price, time, "stop")

            trade.c1_bars_elapsed += 1
            tp_distance = 6.0
            max_bars = 15

            if direction == "long":
                tp_price = trade.entry_price + tp_distance
                tp_hit = price >= tp_price
            else:
                tp_price = trade.entry_price - tp_distance
                tp_hit = price <= tp_price

            if tp_hit:
                # Limit order fill at TP price — ZERO slippage
                return _close_c1_and_run(trade, tp_price, time,
                                         "limit_tp_6pt", 0.0)

            if trade.c1_bars_elapsed >= max_bars:
                # Fallback: market exit at bar 15 with slippage
                c1_in_profit = (price > trade.entry_price) if direction == "long" else (price < trade.entry_price)
                if c1_in_profit:
                    bar_time, et_time, volume = _slip_ctx(time)
                    adj, slip = sim.slippage_engine.apply_adverse_slippage(
                        price, direction, "exit", bar_time, et_time, volume)
                    return _close_c1_and_run(trade, adj, time,
                                             "time_15bars_fallback", slip)

            if direction == "long":
                trade.c2_best_price = max(trade.c2_best_price, price)
            else:
                trade.c2_best_price = min(trade.c2_best_price, price) if trade.c2_best_price > 0 else price
            return None

        async def phase1_variant_c(trade, price, time):
            """VARIANT C: Trailing from profit >= 3pts, trail 2.5pts."""
            from execution.scale_out_executor import ScaleOutPhase
            direction = trade.direction

            if (direction == "long" and price <= trade.initial_stop) or \
               (direction == "short" and price >= trade.initial_stop):
                return await patched_close_all(trade, price, time, "stop")

            trade.c1_bars_elapsed += 1
            profit_threshold = 3.0
            trail_distance = 2.5
            max_bars = 12

            unrealized = (price - trade.entry_price) if direction == "long" else (trade.entry_price - price)

            # Initialize C1 HWM tracking
            if not hasattr(trade, '_c1_hwm_c'):
                trade._c1_hwm_c = trade.entry_price
                trade._c1_trailing_active = False

            # Update HWM
            if direction == "long":
                trade._c1_hwm_c = max(trade._c1_hwm_c, price)
            else:
                trade._c1_hwm_c = min(trade._c1_hwm_c, price)

            # Activate trailing once profit >= threshold
            if unrealized >= profit_threshold and not trade._c1_trailing_active:
                trade._c1_trailing_active = True

            # Check trailing stop
            if trade._c1_trailing_active:
                if direction == "long":
                    trail_stop = trade._c1_hwm_c - trail_distance
                    if price <= trail_stop:
                        bar_time, et_time, volume = _slip_ctx(time)
                        adj, slip = sim.slippage_engine.apply_adverse_slippage(
                            trail_stop, direction, "exit", bar_time, et_time, volume)
                        return _close_c1_and_run(trade, adj, time,
                                                 "c1_trail_from_profit", slip)
                else:
                    trail_stop = trade._c1_hwm_c + trail_distance
                    if price >= trail_stop:
                        bar_time, et_time, volume = _slip_ctx(time)
                        adj, slip = sim.slippage_engine.apply_adverse_slippage(
                            trail_stop, direction, "exit", bar_time, et_time, volume)
                        return _close_c1_and_run(trade, adj, time,
                                                 "c1_trail_from_profit", slip)

            # Fallback: market exit at bar 12 if trailing never activated
            if trade.c1_bars_elapsed >= max_bars and not trade._c1_trailing_active:
                if unrealized > 0:
                    bar_time, et_time, volume = _slip_ctx(time)
                    adj, slip = sim.slippage_engine.apply_adverse_slippage(
                        price, direction, "exit", bar_time, et_time, volume)
                    return _close_c1_and_run(trade, adj, time,
                                             "time_12bars_fallback", slip)

            if direction == "long":
                trade.c2_best_price = max(trade.c2_best_price, price)
            else:
                trade.c2_best_price = min(trade.c2_best_price, price) if trade.c2_best_price > 0 else price
            return None

        # Select C1 phase 1 strategy
        # "C" uses the native executor Variant C (now the production default)
        # "baseline" and "D" use the old Time 10 logic
        phase1_map = {
            "baseline": phase1_baseline,
            "A": phase1_variant_a,
            "B": phase1_variant_b,
            "C": phase1_variant_c,
            "D": phase1_baseline,  # D uses original Time 10, RTH restriction is in the loop
        }
        selected_phase1 = phase1_map.get(variant, phase1_variant_c)

        # ── Patched runner (same for all variants) ────────────

        async def patched_manage_runner(trade, price, time):
            """C2 runner with calibrated slippage."""
            direction = trade.direction
            cfg = executor.scale_config

            if direction == "long":
                trade.c2_best_price = max(trade.c2_best_price, price)
            else:
                if trade.c2_best_price == 0:
                    trade.c2_best_price = price
                trade.c2_best_price = min(trade.c2_best_price, price)

            if cfg.c2_trailing_stop_enabled:
                new_trail = executor._compute_trailing_stop(trade, price)
                if direction == "long" and new_trail > trade.c2.stop_price:
                    trade.c2.stop_price = round(new_trail, 2)
                    trade.c2_trailing_stop = trade.c2.stop_price
                elif direction == "short" and (new_trail < trade.c2.stop_price or trade.c2.stop_price == 0):
                    trade.c2.stop_price = round(new_trail, 2)
                    trade.c2_trailing_stop = trade.c2.stop_price

            stop_hit = False
            if direction == "long" and price <= trade.c2.stop_price:
                stop_hit = True
            elif direction == "short" and price >= trade.c2.stop_price:
                stop_hit = True

            if stop_hit:
                exit_reason = "trailing" if trade.c2_trailing_stop > 0 else "breakeven"
                return await patched_close_c2(trade, trade.c2.stop_price, time, exit_reason)

            points_from_entry = abs(price - trade.entry_price)
            if points_from_entry >= cfg.c2_max_target_points:
                return await patched_close_c2(trade, price, time, "max_target")

            if trade.entry_time:
                elapsed_minutes = (time - trade.entry_time).total_seconds() / 60
                if elapsed_minutes >= cfg.c2_time_stop_minutes:
                    return await patched_close_c2(trade, price, time, "time_stop")

            return None

        # Apply patches
        executor._paper_enter = patched_paper_enter
        executor._close_c2 = patched_close_c2
        executor._close_all = patched_close_all
        executor._manage_phase_1 = selected_phase1
        executor._manage_runner = patched_manage_runner

        if not self.quiet:
            print(f"  Slippage engine v2 (calibrated):")
            print(f"    RTH (9:30-16:00 ET): 0.50pt base, cap 1.50pt")
            print(f"    ETH (18:01-9:29 ET): 1.00pt base, cap 2.50pt")
            print(f"    Volume spike (>2x 20-bar avg): +0.50-0.75pt")
            print(f"    News window: +1.00pt, cap 3.00pt")
            print(f"  C1 variant: {variant} — {self.C1_VARIANTS.get(variant, variant)}")

    def _handle_result(self, result: Dict, timestamp: datetime) -> None:
        """Handle a trade action from process_bar()."""
        action = result.get("action", "")

        if action == "entry":
            self.state.has_position = True
            self.state.position_direction = result.get("direction", "")
            self.state.position_entry_price = result.get("entry_price", 0)
            self.state.position_stop = result.get("stop", 0)
            self.state.position_score = result.get("signal_score", 0)
            self.state.position_entry_time = result.get("timestamp", "")

            # Track signal source for sweep analysis
            self._current_trade_source = result.get("signal_source", "signal")

            self._log_decision("entry", {
                "direction": result.get("direction"),
                "entry_price": result.get("entry_price"),
                "stop": result.get("stop"),
                "c1_exit_rule": result.get("c1_exit_rule"),
                "signal_score": result.get("signal_score"),
                "signal_source": result.get("signal_source"),
                "regime": result.get("regime"),
                "htf_bias": result.get("htf_bias"),
                "htf_strength": result.get("htf_strength"),
                "sweep_levels": result.get("sweep_levels"),
                "sweep_score": result.get("sweep_score"),
            }, timestamp)

        elif action == "c1_time_exit":
            self._log_decision("c1_time_exit", {
                "c1_pnl": result.get("c1_pnl"),
                "c1_bars": result.get("c1_bars"),
                "c2_new_stop": result.get("c2_new_stop"),
                "price": result.get("price"),
            }, timestamp)

        elif action == "trade_closed":
            self._record_trade_result(result, timestamp)

    def _record_trade_result(self, result: Dict, timestamp: datetime) -> None:
        """Record a closed trade."""
        pnl = result.get("total_pnl", 0)
        c1_pnl = result.get("c1_pnl", 0)
        c2_pnl = result.get("c2_pnl", 0)

        # Capture slippage for this trade
        total_slip = round(self._current_trade_slippage, 2)
        entry_slip = round(self._current_trade_entry_slippage, 2)
        exit_slip = round(self._current_trade_exit_slippage, 2)
        slippage_cost = total_slip * 2.0  # $2/pt MNQ
        self.state.total_slippage_cost += slippage_cost
        self._current_trade_slippage = 0.0
        self._current_trade_entry_slippage = 0.0
        self._current_trade_exit_slippage = 0.0

        # Friction loss: C1 profit < total slippage cost for this trade
        if c1_pnl < slippage_cost and c1_pnl >= 0:
            self.state.friction_losses += 1

        self.state.record_trade(pnl, c1_pnl, c2_pnl)
        self.state.has_position = False

        # Track sweep-specific metrics
        source = getattr(self, '_current_trade_source', 'signal')
        if source == "sweep":
            self.state.sweep_trades += 1
            self.state.sweep_pnl += pnl
            if pnl > 0:
                self.state.sweep_wins += 1
            elif pnl < 0:
                self.state.sweep_losses += 1
        elif source == "confluence":
            self.state.confluence_trades += 1
            self.state.confluence_pnl += pnl
            if pnl > 0:
                self.state.confluence_wins += 1
        else:
            self.state.signal_only_trades += 1
            self.state.signal_only_pnl += pnl
            if pnl > 0:
                self.state.signal_only_wins += 1
        self._current_trade_source = "signal"  # Reset

        self.state.trades_log.append({
            "timestamp": timestamp.isoformat(),
            "event": "trade_closed",
            "direction": result.get("direction"),
            "entry_price": result.get("entry_price"),
            "total_pnl": pnl,
            "c1_pnl": c1_pnl,
            "c1_reason": result.get("c1_exit_reason"),
            "c2_pnl": c2_pnl,
            "c2_reason": result.get("c2_exit_reason"),
            "entry_slippage_pts": entry_slip,
            "exit_slippage_pts": exit_slip,
            "total_slippage_pts": total_slip,
            "daily_pnl": self.state.daily_pnl,
            "signal_source": source,
        })

        self._log_decision("trade_closed", {
            "direction": result.get("direction"),
            "entry_price": result.get("entry_price"),
            "total_pnl": pnl,
            "c1_pnl": c1_pnl,
            "c2_pnl": c2_pnl,
            "c1_reason": result.get("c1_exit_reason"),
            "c2_reason": result.get("c2_exit_reason"),
            "daily_pnl": self.state.daily_pnl,
            "total_equity": self.state.equity,
        }, timestamp)

    def _log_decision(self, decision_type: str, data: Dict, timestamp: datetime) -> None:
        """Log a decision for paper_decisions.json."""
        self.state.decisions.append({
            "timestamp": timestamp.isoformat(),
            "decision": decision_type,
            **data,
        })

    def _save_logs(self) -> None:
        """Save trades and decisions to log files."""
        try:
            with open(str(TRADES_LOG), "w") as f:
                json.dump(self.state.trades_log, f, indent=2, default=str)
            with open(str(DECISIONS_LOG), "w") as f:
                json.dump(self.state.decisions, f, indent=2, default=str)
            print(f"\n  Logs saved:")
            print(f"    {TRADES_LOG}")
            print(f"    {DECISIONS_LOG}")
        except Exception as e:
            print(f"  WARNING: Failed to save logs: {e}")

    def _build_results(self, elapsed: float) -> Dict:
        """Build results dict for validation comparison."""
        stats = self.bot.executor.get_stats() if self.bot else {}
        slippage_summary = self.slippage_engine.summary()

        # Sweep detector stats
        sweep_stats = {}
        if self.sweep_enabled and self.bot:
            sweep_det = self.bot.sweep_detector
            sweep_stats = sweep_det.get_stats()
            sweep_stats["sweep_trades"] = self.state.sweep_trades
            sweep_stats["sweep_wins"] = self.state.sweep_wins
            sweep_stats["sweep_losses"] = self.state.sweep_losses
            sweep_stats["sweep_pnl"] = round(self.state.sweep_pnl, 2)
            sweep_stats["sweep_wr"] = round(
                self.state.sweep_wins / self.state.sweep_trades * 100, 1
            ) if self.state.sweep_trades > 0 else 0.0
            sweep_stats["confluence_trades"] = self.state.confluence_trades
            sweep_stats["confluence_wins"] = self.state.confluence_wins
            sweep_stats["confluence_pnl"] = round(self.state.confluence_pnl, 2)
            sweep_stats["confluence_wr"] = round(
                self.state.confluence_wins / self.state.confluence_trades * 100, 1
            ) if self.state.confluence_trades > 0 else 0.0
            sweep_stats["signal_only_trades"] = self.state.signal_only_trades
            sweep_stats["signal_only_wins"] = self.state.signal_only_wins
            sweep_stats["signal_only_pnl"] = round(self.state.signal_only_pnl, 2)
            sweep_stats["sweep_log"] = sweep_det.sweep_log

        return {
            "total_trades": self.state.total_trades,
            "total_pnl": round(self.state.total_pnl, 2),
            "win_rate": round(self.state.win_rate, 1),
            "profit_factor": round(self.state.profit_factor, 2),
            "expectancy": round(self.state.expectancy, 2),
            "max_drawdown_pct": round(self.state.max_drawdown_pct, 1),
            "c1_pnl": round(self.state.c1_pnl, 2),
            "c2_pnl": round(self.state.c2_pnl, 2),
            "exec_bars": self.state.exec_bars_processed,
            "htf_bars": self.state.htf_bars_processed,
            "session_blocks": self.state.session_blocks,
            "session_flattens": self.state.session_flattens,
            "elapsed_seconds": round(elapsed, 1),
            "bars_per_second": round(
                self.state.exec_bars_processed / elapsed, 0
            ) if elapsed > 0 else 0,
            "slippage": slippage_summary,
            "total_slippage_cost": round(self.state.total_slippage_cost, 2),
            "friction_losses": self.state.friction_losses,
            "c1_variant": self.c1_variant,
            "sweep_enabled": self.sweep_enabled,
            "sweep_stats": sweep_stats,
        }

    # ================================================================
    # VALIDATION
    # ================================================================
    def _run_validation(self, results: Dict) -> None:
        """Compare replay results to OOS baseline."""
        print(f"\n{'=' * 62}")
        print(f"  VALIDATION — Replay vs OOS Baseline")
        print(f"{'=' * 62}\n")

        # Compute months in replay window
        if self.start_date and self.end_date:
            sd = datetime.strptime(self.start_date, "%Y-%m-%d")
            ed = datetime.strptime(self.end_date, "%Y-%m-%d")
            months = max(1, (ed.year - sd.year) * 12 + ed.month - sd.month)
        elif self.start_date:
            # Assume through end of data
            months = OOS_BASELINE["months"]
        else:
            months = OOS_BASELINE["months"]

        # Scale baseline to match replay window
        baseline_trades = round(OOS_BASELINE["trades_per_month"] * months)
        baseline_pnl = round(OOS_BASELINE["pnl_per_month"] * months, 2)

        checks = []

        def check(name: str, actual, expected, tolerance_pct: float,
                  unit: str = "") -> bool:
            if expected == 0:
                passed = True
                pct_diff = 0
            else:
                pct_diff = abs(actual - expected) / abs(expected) * 100
                passed = pct_diff <= tolerance_pct

            status = "PASS" if passed else "FAIL"
            print(f"  [{status:>4}] {name:<25} "
                  f"Replay: {actual:>10}{unit}  "
                  f"OOS: {expected:>10}{unit}  "
                  f"Delta: {pct_diff:>5.1f}%  "
                  f"(tol: {tolerance_pct}%)")
            checks.append(passed)
            return passed

        # Trade count — 15% tolerance (session rules may cause minor differences)
        check("Trades", results["total_trades"], baseline_trades, 15)

        # Win rate — 5% absolute tolerance
        wr_diff = abs(results["win_rate"] - OOS_BASELINE["win_rate"])
        wr_pass = wr_diff <= 5.0
        status = "PASS" if wr_pass else "FAIL"
        print(f"  [{status:>4}] {'Win Rate':<25} "
              f"Replay: {results['win_rate']:>9.1f}%  "
              f"OOS: {OOS_BASELINE['win_rate']:>9.1f}%  "
              f"Delta: {wr_diff:>5.1f}pp  "
              f"(tol: 5.0pp)")
        checks.append(wr_pass)

        # Profit factor — 20% tolerance
        check("Profit Factor", results["profit_factor"],
              OOS_BASELINE["profit_factor"], 20)

        # Total PnL — 25% tolerance (slippage model differences)
        check("Total PnL", results["total_pnl"], baseline_pnl, 25, "$")

        # Max drawdown — should not exceed 2x OOS baseline
        dd_pass = results["max_drawdown_pct"] <= OOS_BASELINE["max_drawdown_pct"] * 2
        status = "PASS" if dd_pass else "FAIL"
        print(f"  [{status:>4}] {'Max Drawdown':<25} "
              f"Replay: {results['max_drawdown_pct']:>9.1f}%  "
              f"OOS: {OOS_BASELINE['max_drawdown_pct']:>9.1f}%  "
              f"Limit: {OOS_BASELINE['max_drawdown_pct'] * 2:.1f}%")
        checks.append(dd_pass)

        # C1 PnL — must be positive (key invariant)
        c1_pass = results["c1_pnl"] > 0
        status = "PASS" if c1_pass else "FAIL"
        print(f"  [{status:>4}] {'C1 PnL Positive':<25} "
              f"Replay: ${results['c1_pnl']:>+10,.2f}")
        checks.append(c1_pass)

        # Expectancy — 25% tolerance
        check("Expectancy/Trade", results["expectancy"],
              OOS_BASELINE["expectancy"], 25, "$")

        # Verdict
        passed = sum(checks)
        total = len(checks)
        all_pass = all(checks)

        print(f"\n  {'─' * 60}")
        if all_pass:
            print(f"  VERDICT: ALL CHECKS PASSED ({passed}/{total})")
            print(f"  Pipeline is validated end-to-end.")
            print(f"  Ready to swap data source from replay to live feed.")
        else:
            failed = total - passed
            print(f"  VERDICT: {failed} CHECK(S) FAILED ({passed}/{total} passed)")
            print(f"  Investigate discrepancies before proceeding to live.")

        print(f"\n  Replay speed: {results.get('bars_per_second', 0):.0f} bars/sec")
        print(f"  Total time:   {results.get('elapsed_seconds', 0):.1f}s")

        # ── Slippage Report ──
        slip = results.get("slippage", {})
        print(f"\n{'=' * 62}")
        print(f"  SLIPPAGE REPORT  (C1 variant: {results.get('c1_variant', '?')})")
        print(f"{'=' * 62}")
        print(f"  Total fills:           {slip.get('total_fills', 0)}")
        print(f"  Avg slippage/fill:     {slip.get('avg_slippage_per_fill', 0):.2f} pts")
        print(f"  Entry slippage total:  {slip.get('entry_slippage_total', 0):.1f} pts")
        print(f"  Exit slippage total:   {slip.get('exit_slippage_total', 0):.1f} pts")
        print(f"  Volume spike fills:    {slip.get('volume_spike_fills', 0)}")
        print(f"  News window fills:     {slip.get('news_window_fills', 0)}")
        print(f"  Total slippage cost:   ${results.get('total_slippage_cost', 0):,.2f}")
        print(f"  Friction losses:       {results.get('friction_losses', 0)} "
              f"(C1 profit < slippage cost)")
        baseline_pnl_val = OOS_BASELINE["pnl_per_month"] * OOS_BASELINE["months"]
        if baseline_pnl_val > 0:
            pnl_reduction = (1 - results["total_pnl"] / baseline_pnl_val) * 100
            print(f"  PnL reduction vs OOS:  {pnl_reduction:.1f}%")

        # ── Live-Readiness Assessment ──
        print(f"\n{'=' * 62}")
        print(f"  LIVE-READINESS ASSESSMENT")
        print(f"{'=' * 62}")
        pf = results["profit_factor"]
        if pf >= 1.3:
            print(f"  PF {pf:.2f} >= 1.3: LIVE-READY")
            print(f"  System maintains edge with realistic slippage.")
        elif pf >= 1.2:
            print(f"  PF {pf:.2f} >= 1.2 but < 1.3: MARGINAL")
            print(f"  Consider tightening entry criteria or widening stops.")
        else:
            print(f"  PF {pf:.2f} < 1.2: NOT READY")
            print(f"  Need to tighten entry criteria or widen stops.")

        # ── Monthly Breakdown ──
        self._print_monthly_breakdown()

        print(f"{'=' * 62}\n")

    def _print_monthly_breakdown(self) -> None:
        """Print monthly performance breakdown from trades log."""
        monthly = {}
        for t in self.state.trades_log:
            ts = t.get("timestamp", "")
            month = ts[:7]
            if not month:
                continue
            if month not in monthly:
                monthly[month] = {
                    "trades": 0, "wins": 0, "pnl": 0.0,
                    "c1_pnl": 0.0, "c2_pnl": 0.0,
                    "gross_profit": 0.0, "gross_loss": 0.0,
                    "slippage_pts": 0.0,
                }
            m = monthly[month]
            pnl = t.get("total_pnl", 0)
            m["trades"] += 1
            m["pnl"] += pnl
            m["c1_pnl"] += t.get("c1_pnl", 0)
            m["c2_pnl"] += t.get("c2_pnl", 0)
            m["slippage_pts"] += t.get("slippage_points", 0)
            if pnl > 0:
                m["wins"] += 1
                m["gross_profit"] += pnl
            else:
                m["gross_loss"] += abs(pnl)

        if not monthly:
            return

        print(f"\n{'=' * 62}")
        print(f"  MONTHLY BREAKDOWN (Dynamic Slippage)")
        print(f"{'=' * 62}")
        print(f"  {'Month':<10} {'Trades':>6} {'WR':>6} {'PF':>6} "
              f"{'PnL':>10} {'C1':>9} {'C2':>9} {'Slip':>6}")
        print(f"  {'─' * 60}")

        total_pnl = 0.0
        profitable_months = 0
        for month in sorted(monthly.keys()):
            m = monthly[month]
            wr = (m["wins"] / m["trades"] * 100) if m["trades"] > 0 else 0
            pf = (m["gross_profit"] / m["gross_loss"]) if m["gross_loss"] > 0 else (
                float('inf') if m["gross_profit"] > 0 else 0)
            pf_str = f"{pf:.2f}" if pf < 100 else "inf"
            total_pnl += m["pnl"]
            if m["pnl"] > 0:
                profitable_months += 1

            print(f"  {month:<10} {m['trades']:>6} {wr:>5.1f}% {pf_str:>6} "
                  f"${m['pnl']:>+9,.0f} ${m['c1_pnl']:>+8,.0f} "
                  f"${m['c2_pnl']:>+8,.0f} {m['slippage_pts']:>5.1f}")

        print(f"  {'─' * 60}")
        total_months = len(monthly)
        print(f"  {'TOTAL':<10} {self.state.total_trades:>6} "
              f"{self.state.win_rate:>5.1f}% "
              f"{self.state.profit_factor:.2f}  "
              f"${total_pnl:>+9,.0f} ${self.state.c1_pnl:>+8,.0f} "
              f"${self.state.c2_pnl:>+8,.0f}")
        print(f"  Profitable months: {profitable_months}/{total_months}")


# ================================================================
# COMPARE-ALL: Run all 5 variants and print comparison
# ================================================================
async def run_compare_all(args):
    """Run baseline + all 4 C1 variants, print comparison table."""
    variants = ["baseline", "A", "B", "C", "D"]
    all_results = {}

    for v in variants:
        desc = ReplaySimulator.C1_VARIANTS.get(v, v)
        print(f"\n{'#' * 62}")
        print(f"  RUNNING VARIANT: {v} — {desc}")
        print(f"{'#' * 62}")

        sim = ReplaySimulator(
            speed="max",
            start_date=args.start_date,
            end_date=args.end_date,
            validate=True,
            data_dir=args.data_dir,
            c1_variant=v,
            quiet=True,
        )
        results = await sim.run()
        # Attach monthly breakdown
        monthly = {}
        for t in sim.state.trades_log:
            ts = t.get("timestamp", "")
            month = ts[:7]
            if not month:
                continue
            if month not in monthly:
                monthly[month] = {"trades": 0, "wins": 0, "pnl": 0.0,
                                  "c1_pnl": 0.0, "c2_pnl": 0.0,
                                  "gross_profit": 0.0, "gross_loss": 0.0}
            m = monthly[month]
            pnl = t.get("total_pnl", 0)
            m["trades"] += 1
            m["pnl"] += pnl
            m["c1_pnl"] += t.get("c1_pnl", 0)
            m["c2_pnl"] += t.get("c2_pnl", 0)
            if pnl > 0:
                m["wins"] += 1
                m["gross_profit"] += pnl
            else:
                m["gross_loss"] += abs(pnl)
        results["monthly"] = monthly
        all_results[v] = results

        print(f"  -> {v}: PF {results['profit_factor']:.2f} | "
              f"PnL ${results['total_pnl']:+,.0f} | "
              f"WR {results['win_rate']:.1f}% | "
              f"DD {results['max_drawdown_pct']:.1f}% | "
              f"Trades {results['total_trades']}")

    # ── Comparison Table ──
    print(f"\n\n{'=' * 80}")
    print(f"  C1 VARIANT COMPARISON — Calibrated Slippage v2")
    print(f"{'=' * 80}")
    print(f"  {'Variant':<12} {'Trades':>6} {'WR':>6} {'PF':>6} "
          f"{'PnL':>10} {'C1 PnL':>9} {'C2 PnL':>9} "
          f"{'MaxDD':>6} {'Slip/Fill':>9} {'Frict':>5}")
    print(f"  {'─' * 76}")

    for v in variants:
        r = all_results[v]
        slip = r.get("slippage", {})
        desc = ReplaySimulator.C1_VARIANTS.get(v, v)[:40]
        pf_str = f"{r['profit_factor']:.2f}" if r['profit_factor'] < 100 else "inf"
        print(f"  {v:<12} {r['total_trades']:>6} {r['win_rate']:>5.1f}% {pf_str:>6} "
              f"${r['total_pnl']:>+9,.0f} ${r['c1_pnl']:>+8,.0f} ${r['c2_pnl']:>+8,.0f} "
              f"{r['max_drawdown_pct']:>5.1f}% "
              f"{slip.get('avg_slippage_per_fill', 0):>8.2f}pt "
              f"{r.get('friction_losses', 0):>5}")

    # ── Rank by PF, then PnL, then DD ──
    ranked = sorted(variants,
                    key=lambda v: (all_results[v]['profit_factor'],
                                   all_results[v]['total_pnl'],
                                   -all_results[v]['max_drawdown_pct']),
                    reverse=True)

    print(f"\n  RANKING (by PF -> PnL -> DD):")
    for i, v in enumerate(ranked, 1):
        r = all_results[v]
        desc = ReplaySimulator.C1_VARIANTS.get(v, v)
        status = "LIVE-READY" if r['profit_factor'] >= 1.3 else \
                 "MARGINAL" if r['profit_factor'] >= 1.2 else "NOT READY"
        print(f"  #{i}: {v:<10} PF {r['profit_factor']:.2f} | "
              f"${r['total_pnl']:>+9,.0f} | DD {r['max_drawdown_pct']:.1f}% | "
              f"{status}")
        print(f"        {desc}")

    # ── Monthly breakdown for top variant ──
    best = ranked[0]
    r_best = all_results[best]
    print(f"\n{'=' * 80}")
    print(f"  MONTHLY BREAKDOWN — Best: {best} "
          f"({ReplaySimulator.C1_VARIANTS.get(best, best)})")
    print(f"{'=' * 80}")
    print(f"  {'Month':<10} {'Trades':>6} {'WR':>6} {'PF':>6} "
          f"{'PnL':>10} {'C1':>9} {'C2':>9}")
    print(f"  {'─' * 56}")

    monthly = r_best.get("monthly", {})
    prof_months = 0
    for month in sorted(monthly.keys()):
        m = monthly[month]
        wr = (m["wins"] / m["trades"] * 100) if m["trades"] > 0 else 0
        pf = (m["gross_profit"] / m["gross_loss"]) if m["gross_loss"] > 0 else (
            float('inf') if m["gross_profit"] > 0 else 0)
        pf_str = f"{pf:.2f}" if pf < 100 else "inf"
        if m["pnl"] > 0:
            prof_months += 1
        print(f"  {month:<10} {m['trades']:>6} {wr:>5.1f}% {pf_str:>6} "
              f"${m['pnl']:>+9,.0f} ${m['c1_pnl']:>+8,.0f} ${m['c2_pnl']:>+8,.0f}")

    print(f"  {'─' * 56}")
    print(f"  Profitable months: {prof_months}/{len(monthly)}")
    print(f"{'=' * 80}\n")


# ================================================================
# SWEEP COMPARE: Baseline (no sweep) vs Test (with sweep)
# ================================================================
SWEEP_ANALYSIS_LOG = LOGS_DIR / "sweep_analysis.json"


async def run_sweep_compare(args):
    """Run BASELINE (no sweeps) vs TEST (with sweeps), print comparison."""
    print(f"\n{'=' * 70}")
    print(f"  SWEEP DETECTOR A/B TEST — BASELINE vs BASELINE+SWEEPS")
    print(f"  C1 variant: {args.c1_variant} | OOS window: Sep 2025 – Feb 2026")
    print(f"{'=' * 70}\n")

    runs = {}
    for label, sweep_on in [("BASELINE", False), ("TEST", True)]:
        sweep_str = "ENABLED" if sweep_on else "DISABLED"
        print(f"\n{'#' * 62}")
        print(f"  RUNNING: {label} (sweep detector {sweep_str})")
        print(f"{'#' * 62}")

        sim = ReplaySimulator(
            speed="max",
            start_date=args.start_date or "2025-09-01",
            end_date=args.end_date or "2026-03-01",
            validate=False,
            data_dir=args.data_dir,
            c1_variant=args.c1_variant,
            quiet=True,
            sweep_enabled=sweep_on,
        )
        results = await sim.run()

        # Attach monthly breakdown
        monthly = {}
        for t in sim.state.trades_log:
            ts = t.get("timestamp", "")
            month = ts[:7]
            if not month:
                continue
            if month not in monthly:
                monthly[month] = {
                    "trades": 0, "wins": 0, "pnl": 0.0,
                    "c1_pnl": 0.0, "c2_pnl": 0.0,
                    "gross_profit": 0.0, "gross_loss": 0.0,
                    "sweep_trades": 0, "sweep_pnl": 0.0, "sweep_wins": 0,
                    "confluence_trades": 0, "confluence_pnl": 0.0,
                }
            m = monthly[month]
            pnl = t.get("total_pnl", 0)
            m["trades"] += 1
            m["pnl"] += pnl
            m["c1_pnl"] += t.get("c1_pnl", 0)
            m["c2_pnl"] += t.get("c2_pnl", 0)
            if pnl > 0:
                m["wins"] += 1
                m["gross_profit"] += pnl
            else:
                m["gross_loss"] += abs(pnl)
            src = t.get("signal_source", "signal")
            if src == "sweep":
                m["sweep_trades"] += 1
                m["sweep_pnl"] += pnl
                if pnl > 0:
                    m["sweep_wins"] += 1
            elif src == "confluence":
                m["confluence_trades"] += 1
                m["confluence_pnl"] += pnl

        results["monthly"] = monthly
        runs[label] = results

        print(f"  -> {label}: PF {results['profit_factor']:.2f} | "
              f"PnL ${results['total_pnl']:+,.0f} | "
              f"WR {results['win_rate']:.1f}% | "
              f"DD {results['max_drawdown_pct']:.1f}% | "
              f"Trades {results['total_trades']}")

    # ── Side-by-side comparison ──
    base = runs["BASELINE"]
    test = runs["TEST"]

    print(f"\n\n{'=' * 70}")
    print(f"  SWEEP DETECTOR COMPARISON — BASELINE vs TEST")
    print(f"{'=' * 70}")
    print(f"  {'Metric':<25} {'BASELINE':>12} {'TEST':>12} {'Delta':>12}")
    print(f"  {'─' * 65}")

    def compare(name, bv, tv, unit="", fmt=".1f"):
        delta = tv - bv
        d_str = f"{delta:+{fmt}}{unit}"
        print(f"  {name:<25} {bv:>11{fmt}}{unit} {tv:>11{fmt}}{unit} {d_str:>12}")

    compare("Trades", base["total_trades"], test["total_trades"], "", ".0f")
    compare("Win Rate", base["win_rate"], test["win_rate"], "%")
    compare("Profit Factor", base["profit_factor"], test["profit_factor"], "", ".2f")
    compare("Total PnL ($)", base["total_pnl"], test["total_pnl"], "", ".0f")
    compare("Expectancy/Trade ($)", base["expectancy"], test["expectancy"], "", ".2f")
    compare("Max Drawdown (%)", base["max_drawdown_pct"], test["max_drawdown_pct"], "%")
    compare("C1 PnL ($)", base["c1_pnl"], test["c1_pnl"], "", ".0f")
    compare("C2 PnL ($)", base["c2_pnl"], test["c2_pnl"], "", ".0f")

    # ── Sweep-specific breakdown (test only) ──
    sweep = test.get("sweep_stats", {})
    print(f"\n{'=' * 70}")
    print(f"  SWEEP DETECTOR METRICS (TEST run)")
    print(f"{'=' * 70}")
    print(f"  Sweeps detected:        {sweep.get('total_sweeps_detected', 0)}")
    print(f"  Sweeps confirmed:       {sweep.get('total_sweeps_confirmed', 0)}")
    conf_rate = sweep.get('confirmation_rate', 0)
    print(f"  Confirmation rate:      {conf_rate:.1f}%")
    print(f"  Sweep-only trades:      {sweep.get('sweep_trades', 0)}")
    print(f"  Sweep-only WR:          {sweep.get('sweep_wr', 0):.1f}%")
    print(f"  Sweep-only PnL:         ${sweep.get('sweep_pnl', 0):+,.2f}")
    print(f"  Confluence trades:      {sweep.get('confluence_trades', 0)}")
    print(f"  Confluence WR:          {sweep.get('confluence_wr', 0):.1f}%")
    print(f"  Confluence PnL:         ${sweep.get('confluence_pnl', 0):+,.2f}")
    print(f"  Signal-only trades:     {sweep.get('signal_only_trades', 0)}")
    print(f"  Signal-only PnL:        ${sweep.get('signal_only_pnl', 0):+,.2f}")

    # ── Sample size warning ──
    total_sweep_trades = sweep.get('sweep_trades', 0) + sweep.get('confluence_trades', 0)
    if total_sweep_trades < 20:
        print(f"\n  *** WARNING: Only {total_sweep_trades} sweep-related trades. "
              f"Insufficient sample size (<20). "
              f"Results may not be statistically significant. ***")

    # ── Monthly breakdown for TEST ──
    monthly = test.get("monthly", {})
    if monthly:
        print(f"\n{'=' * 70}")
        print(f"  MONTHLY BREAKDOWN — TEST (with sweeps)")
        print(f"{'=' * 70}")
        print(f"  {'Month':<10} {'Trades':>6} {'WR':>6} {'PF':>6} "
              f"{'PnL':>10} {'SwpTrd':>6} {'SwpPnL':>9} {'ConfTrd':>7}")
        print(f"  {'─' * 65}")

        prof_months = 0
        for month in sorted(monthly.keys()):
            m = monthly[month]
            wr = (m["wins"] / m["trades"] * 100) if m["trades"] > 0 else 0
            pf = (m["gross_profit"] / m["gross_loss"]) if m["gross_loss"] > 0 else (
                float('inf') if m["gross_profit"] > 0 else 0)
            pf_str = f"{pf:.2f}" if pf < 100 else "inf"
            if m["pnl"] > 0:
                prof_months += 1
            print(f"  {month:<10} {m['trades']:>6} {wr:>5.1f}% {pf_str:>6} "
                  f"${m['pnl']:>+9,.0f} {m['sweep_trades']:>6} "
                  f"${m['sweep_pnl']:>+8,.0f} {m['confluence_trades']:>7}")

        print(f"  {'─' * 65}")
        print(f"  Profitable months: {prof_months}/{len(monthly)}")

    # ── Feb 27, 2026 analysis (if data exists) ──
    sweep_log = sweep.get("sweep_log", [])
    feb27_sweeps = [s for s in sweep_log if s.get("timestamp", "").startswith("2026-02-27")]
    if feb27_sweeps:
        print(f"\n{'=' * 70}")
        print(f"  FEB 27, 2026 — SWEEP ACTIVITY (330pt MNQ reversal)")
        print(f"{'=' * 70}")
        for s in feb27_sweeps:
            print(f"  {s['timestamp'][:19]} | {s['direction']} | "
                  f"Levels: {', '.join(s['swept_levels'])} | "
                  f"Depth: {s['sweep_depth_pts']:.1f}pts | "
                  f"Vol: {s['volume_ratio']:.1f}x | "
                  f"Score: {s['score']:.2f}")
    else:
        print(f"\n  Note: No sweep signals detected on Feb 27, 2026.")
        print(f"  (Data may not extend to that date, or no key levels were swept.)")

    # ── Save sweep_analysis.json ──
    analysis = {
        "run_date": datetime.now(timezone.utc).isoformat(),
        "c1_variant": args.c1_variant,
        "oos_window": f"{args.start_date or '2025-09-01'} to {args.end_date or '2026-03-01'}",
        "baseline": {
            "total_trades": base["total_trades"],
            "win_rate": base["win_rate"],
            "profit_factor": base["profit_factor"],
            "total_pnl": base["total_pnl"],
            "expectancy": base["expectancy"],
            "max_drawdown_pct": base["max_drawdown_pct"],
        },
        "test": {
            "total_trades": test["total_trades"],
            "win_rate": test["win_rate"],
            "profit_factor": test["profit_factor"],
            "total_pnl": test["total_pnl"],
            "expectancy": test["expectancy"],
            "max_drawdown_pct": test["max_drawdown_pct"],
        },
        "delta": {
            "trades": test["total_trades"] - base["total_trades"],
            "win_rate": round(test["win_rate"] - base["win_rate"], 1),
            "profit_factor": round(test["profit_factor"] - base["profit_factor"], 2),
            "total_pnl": round(test["total_pnl"] - base["total_pnl"], 2),
            "expectancy": round(test["expectancy"] - base["expectancy"], 2),
        },
        "sweep_metrics": {
            "total_sweeps_detected": sweep.get("total_sweeps_detected", 0),
            "total_sweeps_confirmed": sweep.get("total_sweeps_confirmed", 0),
            "confirmation_rate": conf_rate,
            "sweep_only_trades": sweep.get("sweep_trades", 0),
            "sweep_only_wr": sweep.get("sweep_wr", 0),
            "sweep_only_pnl": sweep.get("sweep_pnl", 0),
            "confluence_trades": sweep.get("confluence_trades", 0),
            "confluence_wr": sweep.get("confluence_wr", 0),
            "confluence_pnl": sweep.get("confluence_pnl", 0),
            "total_sweep_related_trades": total_sweep_trades,
            "insufficient_sample": total_sweep_trades < 20,
        },
        "feb_27_2026_sweeps": feb27_sweeps,
        "monthly_test": {m: {k: v for k, v in d.items()}
                         for m, d in monthly.items()},
        "full_sweep_log": sweep_log,
    }

    try:
        with open(str(SWEEP_ANALYSIS_LOG), "w") as f:
            json.dump(analysis, f, indent=2, default=str)
        print(f"\n  Sweep analysis saved: {SWEEP_ANALYSIS_LOG}")
    except Exception as e:
        print(f"  WARNING: Failed to save sweep analysis: {e}")

    print(f"\n{'=' * 70}\n")


# ================================================================
# ENTRYPOINT
# ================================================================
async def async_main():
    parser = argparse.ArgumentParser(
        description="Replay Simulator — Paper Trading Validation"
    )
    parser.add_argument(
        "--speed", type=str, default="max",
        help="Replay speed: 1 (real-time), 10, 100, max (default: max)"
    )
    parser.add_argument(
        "--start-date", type=str, default=None,
        help="Start date YYYY-MM-DD (default: start of data)"
    )
    parser.add_argument(
        "--end-date", type=str, default=None,
        help="End date YYYY-MM-DD (default: end of data)"
    )
    parser.add_argument(
        "--validate", action="store_true",
        help="Validation mode — max speed, compare to OOS baseline"
    )
    parser.add_argument(
        "--data-dir", type=str, default=None,
        help="Data directory (default: data/firstrate/)"
    )
    parser.add_argument(
        "--c1-variant", type=str, default="C",
        choices=["baseline", "A", "B", "C", "D"],
        help="C1 exit strategy variant (default: C — trail from profit)"
    )
    parser.add_argument(
        "--compare-all", action="store_true",
        help="Run all 5 C1 variants and print comparison table"
    )
    parser.add_argument(
        "--sweep-compare", action="store_true",
        help="A/B test: baseline (no sweeps) vs test (with sweeps)"
    )
    parser.add_argument(
        "--no-sweep", action="store_true",
        help="Disable the liquidity sweep detector"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.compare_all:
        await run_compare_all(args)
        return

    if args.sweep_compare:
        await run_sweep_compare(args)
        return

    speed = "max" if args.validate else args.speed

    sim = ReplaySimulator(
        speed=speed,
        start_date=args.start_date,
        end_date=args.end_date,
        validate=args.validate,
        data_dir=args.data_dir,
        c1_variant=args.c1_variant,
        sweep_enabled=not args.no_sweep,
    )

    await sim.run()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
