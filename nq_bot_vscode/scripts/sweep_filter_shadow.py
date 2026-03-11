#!/usr/bin/env python3
"""
Sweep Filter Shadow Analysis
==============================
Tests the impact of removing VWAP and/or widening round-number intervals
on liquidity sweep quality and simulated trade PnL.

Approach:
  1. Load historical 1m data → aggregate to 2m and 15m bars
  2. Replay through the LiquiditySweepDetector with 4 configurations:
     A) BASELINE:  current settings (VWAP + 50pt rounds)
     B) NO_VWAP:   remove VWAP from key levels
     C) 100PT:     round numbers every 100pts instead of 50pts
     D) BOTH:      no VWAP + 100pt rounds
  3. For each confirmed sweep, simulate a 1-contract trade:
     - Entry: next 2m bar open + slippage
     - Stop: HTF sweep extreme + buffer
     - Exit: C1-style 5-bar time exit OR stop hit
  4. Compare PnL, win rate, and trade count across all 4 variants

Usage:
    python scripts/sweep_filter_shadow.py
    python scripts/sweep_filter_shadow.py --data data/historical/combined_1min.csv
    python scripts/sweep_filter_shadow.py --recent 30  # last 30 days only
"""

import csv
import json
import logging
import math
import sys
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

# ── Project imports ──
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from features.engine import Bar

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Constants ──
POINT_VALUE = 2.0       # MNQ $2/point
SLIPPAGE_RTH = 0.50     # pts per fill
SLIPPAGE_ETH = 1.00     # pts per fill
COMMISSION_RT = 2.58    # round-trip per contract
C1_EXIT_BARS = 5        # 5-bar time exit
STOP_BUFFER = 3.0
ET = ZoneInfo("America/New_York")


# ═══════════════════════════════════════════════════════════════
# SIMPLIFIED SWEEP DETECTOR (configurable for shadow testing)
# ═══════════════════════════════════════════════════════════════

@dataclass
class SimpleLevel:
    name: str
    price: float
    level_type: str   # "prior_day", "vwap", "round", "session", "prior_week"


@dataclass
class SweepHit:
    """A confirmed sweep with entry/stop info."""
    timestamp: datetime
    direction: str           # "LONG" or "SHORT"
    swept_levels: List[str]
    sweep_depth: float
    htf_volume_ratio: float
    score: float
    entry_price: float       # 2m confirmation bar close
    stop_price: float        # HTF sweep extreme + buffer
    htf_timeframe: str


class ConfigurableSweepDetector:
    """
    Stripped-down sweep detector that mirrors the real one but allows
    toggling VWAP and round-number interval for shadow testing.
    """

    MIN_SWEEP_DEPTH_15M = 5.0
    ENTRY_WINDOW_BARS = 15

    def __init__(self, include_vwap: bool = True, round_interval: int = 50):
        self.include_vwap = include_vwap
        self.round_interval = round_interval

        # Session tracking
        self._current_date = ""
        self._prior_day_high = 0.0
        self._prior_day_low = 0.0
        self._session_high = 0.0
        self._session_low = 0.0
        self._prior_week_high = 0.0
        self._prior_week_low = 0.0
        self._current_week_start = ""
        self._daily_highs: Dict[str, float] = {}
        self._daily_lows: Dict[str, float] = {}
        self._week_highs: Dict[str, float] = {}
        self._week_lows: Dict[str, float] = {}

        # HTF volume tracking
        self._htf_volumes: List[int] = []
        self._htf_vol_window = 10

        # Pending candidates
        self._candidates: List[dict] = []

        # Results
        self.confirmed_sweeps: List[SweepHit] = []

    def _update_session(self, bar: Bar):
        """Track daily/weekly highs and lows."""
        et = bar.timestamp.astimezone(ET)
        date_str = et.strftime("%Y-%m-%d")
        week_start = (et - timedelta(days=et.weekday())).strftime("%Y-%m-%d")

        if date_str != self._current_date:
            if self._current_date and self._current_date in self._daily_highs:
                self._prior_day_high = self._daily_highs[self._current_date]
                self._prior_day_low = self._daily_lows[self._current_date]
            self._current_date = date_str
            self._daily_highs[date_str] = bar.high
            self._daily_lows[date_str] = bar.low
            self._session_high = bar.high
            self._session_low = bar.low

        if week_start != self._current_week_start:
            if self._current_week_start and self._current_week_start in self._week_highs:
                self._prior_week_high = self._week_highs[self._current_week_start]
                self._prior_week_low = self._week_lows[self._current_week_start]
            self._current_week_start = week_start
            self._week_highs[week_start] = bar.high
            self._week_lows[week_start] = bar.low

        self._daily_highs[date_str] = max(self._daily_highs.get(date_str, bar.high), bar.high)
        self._daily_lows[date_str] = min(self._daily_lows.get(date_str, bar.low), bar.low)
        self._session_high = max(self._session_high, bar.high)
        self._session_low = min(self._session_low, bar.low)
        self._week_highs[week_start] = max(self._week_highs.get(week_start, bar.high), bar.high)
        self._week_lows[week_start] = min(self._week_lows.get(week_start, bar.low), bar.low)

        # Cleanup old dates
        if len(self._daily_highs) > 10:
            oldest = sorted(self._daily_highs.keys())[0]
            self._daily_highs.pop(oldest, None)
            self._daily_lows.pop(oldest, None)
        if len(self._week_highs) > 4:
            oldest = sorted(self._week_highs.keys())[0]
            self._week_highs.pop(oldest, None)
            self._week_lows.pop(oldest, None)

    def _build_levels(self, vwap: float, current_price: float) -> List[SimpleLevel]:
        """Build key levels based on configuration."""
        levels = []

        if self._prior_day_high > 0:
            levels.append(SimpleLevel("PDH", self._prior_day_high, "prior_day"))
        if self._prior_day_low > 0:
            levels.append(SimpleLevel("PDL", self._prior_day_low, "prior_day"))
        if self._session_high > 0:
            levels.append(SimpleLevel("session_high", self._session_high, "session"))
        if self._session_low > 0:
            levels.append(SimpleLevel("session_low", self._session_low, "session"))
        if self._prior_week_high > 0:
            levels.append(SimpleLevel("PWH", self._prior_week_high, "prior_week"))
        if self._prior_week_low > 0:
            levels.append(SimpleLevel("PWL", self._prior_week_low, "prior_week"))

        # VWAP — configurable
        if self.include_vwap and vwap > 0:
            levels.append(SimpleLevel("VWAP", vwap, "vwap"))

        # Round numbers — configurable interval
        if current_price > 0:
            base = int(current_price / self.round_interval) * self.round_interval
            for offset in range(-3, 4):
                rn = base + offset * self.round_interval
                if rn > 0:
                    levels.append(SimpleLevel(f"round_{rn}", float(rn), "round"))

        return levels

    def process_htf_bar(self, htf_bar: Bar, vwap: float, current_price: float):
        """Check 15m bar for sweep candidates."""
        # Track volume
        self._htf_volumes.append(htf_bar.volume)
        if len(self._htf_volumes) > self._htf_vol_window:
            self._htf_volumes = self._htf_volumes[-self._htf_vol_window:]

        levels = self._build_levels(vwap, current_price)
        min_depth = self.MIN_SWEEP_DEPTH_15M

        for level in levels:
            # Sell-side sweep (LONG): wick below level, close above
            if htf_bar.low < level.price - min_depth and htf_bar.close > level.price:
                depth = level.price - htf_bar.low
                if not self._has_pending(level.name, "LONG"):
                    self._candidates.append({
                        "timestamp": htf_bar.timestamp,
                        "direction": "LONG",
                        "level_name": level.name,
                        "level_type": level.level_type,
                        "level_price": level.price,
                        "sweep_price": htf_bar.low,
                        "depth": depth,
                        "htf_volume": htf_bar.volume,
                        "htf_close": htf_bar.close,
                        "htf_open": htf_bar.open,
                        "bars_since": 0,
                        "confirmed": False,
                    })

            # Buy-side sweep (SHORT): wick above level, close below
            if htf_bar.high > level.price + min_depth and htf_bar.close < level.price:
                depth = htf_bar.high - level.price
                if not self._has_pending(level.name, "SHORT"):
                    self._candidates.append({
                        "timestamp": htf_bar.timestamp,
                        "direction": "SHORT",
                        "level_name": level.name,
                        "level_type": level.level_type,
                        "level_price": level.price,
                        "sweep_price": htf_bar.high,
                        "depth": depth,
                        "htf_volume": htf_bar.volume,
                        "htf_close": htf_bar.close,
                        "htf_open": htf_bar.open,
                        "bars_since": 0,
                        "confirmed": False,
                    })

    def _has_pending(self, level_name: str, direction: str) -> bool:
        for c in self._candidates:
            if not c["confirmed"] and c["level_name"] == level_name and c["direction"] == direction:
                return True
        return False

    def process_2m_bar(self, bar: Bar) -> Optional[SweepHit]:
        """Check pending candidates for 2m reversal confirmation."""
        self._update_session(bar)

        best = None
        still_pending = []

        for c in self._candidates:
            if c["confirmed"]:
                continue
            c["bars_since"] += 1

            if c["bars_since"] > self.ENTRY_WINDOW_BARS:
                continue  # expired

            # Reversal confirmation
            confirmed = False
            if c["direction"] == "LONG":
                if bar.close > c["level_price"] and bar.close > bar.open:
                    if bar.low > c["sweep_price"]:
                        confirmed = True
            else:
                if bar.close < c["level_price"] and bar.close < bar.open:
                    if bar.high < c["sweep_price"]:
                        confirmed = True

            if confirmed:
                c["confirmed"] = True
                hit = self._score(c, bar)
                if hit and (best is None or hit.score > best.score):
                    best = hit
                continue

            still_pending.append(c)

        self._candidates = still_pending
        if best:
            self.confirmed_sweeps.append(best)
        return best

    def _score(self, c: dict, bar: Bar) -> SweepHit:
        """Score a confirmed sweep (mirrors real scorer)."""
        score = 0.50  # Base

        # HTF volume spike
        avg_vol = sum(self._htf_volumes) / len(self._htf_volumes) if self._htf_volumes else 1
        vol_ratio = c["htf_volume"] / avg_vol if avg_vol > 0 else 0
        if vol_ratio >= 2.0:
            score += 0.10

        # Strong institutional level
        strong = {"PDH", "PDL", "PWH", "PWL"}
        if c["level_name"] in strong:
            score += 0.10

        # Stop price
        if c["direction"] == "LONG":
            stop_price = c["sweep_price"] - STOP_BUFFER
        else:
            stop_price = c["sweep_price"] + STOP_BUFFER

        return SweepHit(
            timestamp=bar.timestamp,
            direction=c["direction"],
            swept_levels=[c["level_name"]],
            sweep_depth=c["depth"],
            htf_volume_ratio=round(vol_ratio, 2),
            score=round(score, 2),
            entry_price=bar.close,
            stop_price=round(stop_price, 2),
            htf_timeframe="15m",
        )


# ═══════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════

def _parse_timestamp(ts_str: str) -> datetime:
    """Parse timestamp handling non-standard timezone offsets like -0400."""
    ts_str = ts_str.strip()
    # Handle offset without colon: '2022-07-01 00:00:00-0400' → '2022-07-01 00:00:00-04:00'
    import re
    m = re.match(r'^(.+)([+-])(\d{2})(\d{2})$', ts_str)
    if m:
        ts_str = f"{m.group(1)}{m.group(2)}{m.group(3)}:{m.group(4)}"
    return datetime.fromisoformat(ts_str)


def load_1m_bars(csv_path: Path) -> List[Bar]:
    """Load 1-minute bars from CSV."""
    bars = []
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = _parse_timestamp(row["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=ET)
                ts = ts.astimezone(timezone.utc)

                bars.append(Bar(
                    timestamp=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(float(row["volume"])),
                ))
            except (ValueError, KeyError):
                continue
    return bars


def aggregate_bars(bars_1m: List[Bar], minutes: int) -> List[Bar]:
    """Aggregate 1m bars to Nm bars (causal, clock-aligned)."""
    if not bars_1m:
        return []

    buckets: Dict[datetime, List[Bar]] = {}
    for b in bars_1m:
        aligned = b.timestamp.replace(
            minute=(b.timestamp.minute // minutes) * minutes,
            second=0, microsecond=0,
        )
        if aligned not in buckets:
            buckets[aligned] = []
        buckets[aligned].append(b)

    result = []
    for ts in sorted(buckets.keys()):
        chunk = buckets[ts]
        result.append(Bar(
            timestamp=ts,
            open=chunk[0].open,
            high=max(b.high for b in chunk),
            low=min(b.low for b in chunk),
            close=chunk[-1].close,
            volume=sum(b.volume for b in chunk),
        ))
    return result


def compute_vwap_series(bars_2m: List[Bar]) -> Dict[datetime, float]:
    """Compute rolling session VWAP for each 2m bar.
    Session resets at 6 PM ET (CME session boundary).
    """
    vwap_map = {}
    cum_pv = 0.0
    cum_vol = 0
    last_session_date = None

    for b in bars_2m:
        et = b.timestamp.astimezone(ET)
        # CME session starts at 6 PM ET
        session_date = et.date()
        if et.hour < 18:
            session_date = (et - timedelta(days=1)).date() if et.hour < 18 else session_date

        # Rough session boundary: reset at 6 PM ET
        if et.hour == 18 and et.minute == 0:
            cum_pv = 0.0
            cum_vol = 0

        if last_session_date is not None and session_date != last_session_date:
            cum_pv = 0.0
            cum_vol = 0
        last_session_date = session_date

        typical = (b.high + b.low + b.close) / 3.0
        cum_pv += typical * b.volume
        cum_vol += b.volume
        vwap_map[b.timestamp] = cum_pv / cum_vol if cum_vol > 0 else b.close

    return vwap_map


# ═══════════════════════════════════════════════════════════════
# TRADE SIMULATION
# ═══════════════════════════════════════════════════════════════

def simulate_trades(sweeps: List[SweepHit], bars_2m: List[Bar]) -> Dict:
    """
    Simulate C1-style 5-bar time exit for each sweep.
    Entry at next bar open + slippage. Exit at bar 5 close or stop.
    """
    # Build bar index for fast lookup
    bar_map = {b.timestamp: i for i, b in enumerate(bars_2m)}
    bar_list = bars_2m

    trades = []
    total_pnl = 0.0
    wins = 0
    losses = 0

    for sweep in sweeps:
        # Find the bar index of this sweep
        idx = bar_map.get(sweep.timestamp)
        if idx is None or idx + 1 >= len(bar_list):
            continue

        # Entry at next bar open + slippage
        entry_bar = bar_list[idx + 1]
        et_time = entry_bar.timestamp.astimezone(ET)
        t = et_time.hour + et_time.minute / 60.0
        is_rth = 9.5 <= t < 16.0
        slip = SLIPPAGE_RTH if is_rth else SLIPPAGE_ETH

        if sweep.direction == "LONG":
            entry_price = entry_bar.open + slip
            stop_dist = entry_price - sweep.stop_price
        else:
            entry_price = entry_bar.open - slip
            stop_dist = sweep.stop_price - entry_price

        if stop_dist <= 0 or stop_dist > 50:
            continue  # Invalid stop

        # Walk forward up to C1_EXIT_BARS bars
        pnl = 0.0
        outcome = "TIME_EXIT"
        exit_price = entry_price

        for j in range(idx + 2, min(idx + 2 + C1_EXIT_BARS, len(bar_list))):
            check_bar = bar_list[j]

            if sweep.direction == "LONG":
                # Check stop
                if check_bar.low <= entry_price - stop_dist:
                    outcome = "STOP"
                    exit_price = entry_price - stop_dist - slip
                    break
                exit_price = check_bar.close
            else:
                if check_bar.high >= entry_price + stop_dist:
                    outcome = "STOP"
                    exit_price = entry_price + stop_dist + slip
                    break
                exit_price = check_bar.close

        # Calculate PnL
        if sweep.direction == "LONG":
            raw_pnl = (exit_price - entry_price) * POINT_VALUE
        else:
            raw_pnl = (entry_price - exit_price) * POINT_VALUE

        # Subtract commission and exit slippage
        if outcome == "TIME_EXIT":
            raw_pnl -= slip * POINT_VALUE  # exit slippage
        raw_pnl -= COMMISSION_RT

        total_pnl += raw_pnl
        if raw_pnl > 0:
            wins += 1
        else:
            losses += 1

        trades.append({
            "timestamp": sweep.timestamp.isoformat(),
            "direction": sweep.direction,
            "levels": sweep.swept_levels,
            "score": sweep.score,
            "entry": round(entry_price, 2),
            "stop": round(sweep.stop_price, 2),
            "stop_dist": round(stop_dist, 1),
            "exit": round(exit_price, 2),
            "outcome": outcome,
            "pnl": round(raw_pnl, 2),
        })

    total = wins + losses
    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total * 100, 1) if total > 0 else 0,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / total, 2) if total > 0 else 0,
        "profit_factor": round(
            sum(t["pnl"] for t in trades if t["pnl"] > 0) /
            abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
            if sum(t["pnl"] for t in trades if t["pnl"] < 0) != 0 else 0,
            2
        ),
        "trades": trades,
    }


# ═══════════════════════════════════════════════════════════════
# MAIN ANALYSIS
# ═══════════════════════════════════════════════════════════════

def run_variant(
    name: str,
    bars_2m: List[Bar],
    bars_15m: List[Bar],
    vwap_map: Dict[datetime, float],
    include_vwap: bool,
    round_interval: int,
) -> Dict:
    """Run one sweep detector variant and simulate trades."""
    detector = ConfigurableSweepDetector(
        include_vwap=include_vwap,
        round_interval=round_interval,
    )

    # Build 15m bar schedule (when each 15m bar becomes available)
    htf_schedule = {}
    for b in bars_15m:
        # 15m bar is available at its timestamp + 15 minutes
        avail_at = b.timestamp + timedelta(minutes=15)
        htf_schedule[avail_at] = b

    # Track which 15m bars we've already fed
    htf_fed = set()
    current_vwap = 0.0

    for bar in bars_2m:
        # Update session tracking
        detector._update_session(bar)

        # Feed any newly available 15m bars
        for avail_ts, htf_bar in htf_schedule.items():
            if avail_ts <= bar.timestamp and id(htf_bar) not in htf_fed:
                htf_fed.add(id(htf_bar))
                current_vwap = vwap_map.get(bar.timestamp, current_vwap)
                detector.process_htf_bar(htf_bar, current_vwap, bar.close)

        # Process 2m bar for entry confirmation
        current_vwap = vwap_map.get(bar.timestamp, current_vwap)
        detector.process_2m_bar(bar)

    # Simulate trades on confirmed sweeps
    results = simulate_trades(detector.confirmed_sweeps, bars_2m)
    results["name"] = name
    results["include_vwap"] = include_vwap
    results["round_interval"] = round_interval
    results["total_sweeps_detected"] = len(detector.confirmed_sweeps)

    # Break down by level type
    by_level = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0})
    for t in results["trades"]:
        for lvl in t["levels"]:
            if lvl.startswith("round_"):
                key = "round"
            elif lvl in ("PDH", "PDL"):
                key = "prior_day"
            elif lvl in ("PWH", "PWL"):
                key = "prior_week"
            elif lvl == "VWAP":
                key = "vwap"
            elif lvl.startswith("session"):
                key = "session"
            else:
                key = lvl
            by_level[key]["count"] += 1
            by_level[key]["pnl"] += t["pnl"]
            if t["pnl"] > 0:
                by_level[key]["wins"] += 1

    for k, v in by_level.items():
        v["win_rate"] = round(v["wins"] / v["count"] * 100, 1) if v["count"] > 0 else 0
        v["pnl"] = round(v["pnl"], 2)

    results["by_level_type"] = dict(by_level)

    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Sweep Filter Shadow Analysis")
    parser.add_argument("--data", type=str, default=None, help="Path to 1m CSV")
    parser.add_argument("--recent", type=int, default=0, help="Only use last N days")
    args = parser.parse_args()

    # Find data file
    data_path = Path(args.data) if args.data else PROJECT_DIR / "data" / "historical" / "combined_1min.csv"
    if not data_path.exists():
        # Try other locations
        alt = PROJECT_DIR.parent / "data" / "replay_week" / "output" / "combined_1min.csv"
        if alt.exists():
            data_path = alt
        else:
            logger.error("No data file found. Specify with --data")
            sys.exit(1)

    logger.info("Loading 1m bars from %s ...", data_path)
    bars_1m = load_1m_bars(data_path)
    logger.info("Loaded %d 1m bars", len(bars_1m))

    if args.recent > 0:
        cutoff = bars_1m[-1].timestamp - timedelta(days=args.recent)
        bars_1m = [b for b in bars_1m if b.timestamp >= cutoff]
        logger.info("Filtered to last %d days: %d bars", args.recent, len(bars_1m))

    # Aggregate
    logger.info("Aggregating to 2m and 15m bars...")
    bars_2m = aggregate_bars(bars_1m, 2)
    bars_15m = aggregate_bars(bars_1m, 15)
    logger.info("  2m bars: %d", len(bars_2m))
    logger.info("  15m bars: %d", len(bars_15m))

    # Compute VWAP
    logger.info("Computing session VWAP...")
    vwap_map = compute_vwap_series(bars_2m)

    # ── Run 4 variants ──
    variants = [
        ("A) BASELINE (VWAP + 50pt rounds)", True, 50),
        ("B) NO_VWAP (50pt rounds only)", False, 50),
        ("C) 100PT_ROUNDS (VWAP + 100pt rounds)", True, 100),
        ("D) NO_VWAP + 100PT_ROUNDS", False, 100),
    ]

    all_results = []
    for name, inc_vwap, rnd_int in variants:
        logger.info("=" * 60)
        logger.info("Running: %s", name)
        logger.info("=" * 60)
        result = run_variant(name, bars_2m, bars_15m, vwap_map, inc_vwap, rnd_int)
        all_results.append(result)

        logger.info("  Trades: %d | WR: %.1f%% | PnL: $%.2f | PF: %.2f | Avg: $%.2f",
                     result["total_trades"], result["win_rate"],
                     result["total_pnl"], result["profit_factor"], result["avg_pnl"])

        if result["by_level_type"]:
            logger.info("  Breakdown by level type:")
            for lvl, stats in sorted(result["by_level_type"].items(),
                                     key=lambda x: x[1]["pnl"], reverse=True):
                logger.info("    %-12s: %3d trades | WR %.1f%% | PnL $%.2f",
                           lvl, stats["count"], stats["win_rate"], stats["pnl"])

    # ── Summary comparison ──
    print("\n" + "=" * 80)
    print("SWEEP FILTER SHADOW ANALYSIS — COMPARISON")
    print("=" * 80)
    print(f"\nData: {data_path.name}")
    print(f"Period: {bars_1m[0].timestamp.strftime('%Y-%m-%d')} to {bars_1m[-1].timestamp.strftime('%Y-%m-%d')}")
    print(f"Bars: {len(bars_2m):,} (2m), {len(bars_15m):,} (15m)")
    print()

    header = f"{'Variant':<40} {'Trades':>7} {'WR':>7} {'PnL':>10} {'PF':>6} {'Avg':>8}"
    print(header)
    print("-" * len(header))

    baseline_pnl = all_results[0]["total_pnl"] if all_results else 0
    for r in all_results:
        delta = r["total_pnl"] - baseline_pnl
        delta_str = f" ({'+' if delta >= 0 else ''}{delta:.0f})" if r["name"] != all_results[0]["name"] else ""
        print(f"{r['name']:<40} {r['total_trades']:>7} {r['win_rate']:>6.1f}% "
              f"${r['total_pnl']:>9.2f}{delta_str} {r['profit_factor']:>5.2f} "
              f"${r['avg_pnl']:>7.2f}")

    print()

    # Level breakdown for baseline
    if all_results[0]["by_level_type"]:
        print("\nBASELINE — PnL by Level Type:")
        print(f"  {'Level':<15} {'Trades':>7} {'WR':>7} {'PnL':>10}")
        print("  " + "-" * 42)
        for lvl, stats in sorted(all_results[0]["by_level_type"].items(),
                                 key=lambda x: x[1]["pnl"], reverse=True):
            print(f"  {lvl:<15} {stats['count']:>7} {stats['win_rate']:>6.1f}% "
                  f"${stats['pnl']:>9.2f}")

    # ── Save results ──
    output_path = PROJECT_DIR / "logs" / "sweep_filter_shadow.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Strip individual trades for cleaner output
    save_results = []
    for r in all_results:
        save_r = {k: v for k, v in r.items() if k != "trades"}
        save_r["sample_trades"] = r["trades"][:10]  # Keep 10 samples
        save_results.append(save_r)

    with open(output_path, "w") as f:
        json.dump({
            "analysis": "sweep_filter_shadow",
            "data_file": str(data_path),
            "period_start": bars_1m[0].timestamp.isoformat(),
            "period_end": bars_1m[-1].timestamp.isoformat(),
            "bars_2m": len(bars_2m),
            "bars_15m": len(bars_15m),
            "variants": save_results,
        }, f, indent=2)

    logger.info("\nResults saved to %s", output_path)


if __name__ == "__main__":
    main()
