"""
Liquidity Sweep Detector
=========================
Detects institutional liquidity sweeps of key price levels with
multi-bar reclaim confirmation.

A liquidity sweep occurs when:
  1. Price breaks beyond a key level (wick through, stop hunt)
  2. Volume spikes on the sweep candle (institutional absorption)
  3. Price reclaims the level within 1-3 bars (closes back inside)

Key Levels Tracked (updated each session):
  - Prior Day High (PDH) / Prior Day Low (PDL)
  - Current Session High / Low (rolling)
  - Prior Week High / Low
  - Session VWAP (from feature engine)
  - Round numbers (every 50pts)

Signal Output:
  direction: LONG (sell-side sweep) or SHORT (buy-side sweep)
  score: 0.0 to 1.0 (multi-factor)
  swept_levels: list of level names swept
  entry_price: close of reclaim bar
  stop_price: extreme of sweep candle ± 2pt buffer

Integration:
  Called from TradingOrchestrator.process_bar() as an additive
  signal source alongside existing technical signals. Does NOT
  replace any existing signals or filters.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Tuple

logger = logging.getLogger(__name__)


@dataclass
class KeyLevel:
    """A tracked key price level."""
    name: str           # e.g., "PDH", "PDL", "session_high", "round_24750"
    price: float
    level_type: str     # "prior_day", "session", "prior_week", "vwap", "round"
    updated_at: Optional[datetime] = None


@dataclass
class SweepCandidate:
    """A detected sweep awaiting reclaim confirmation."""
    timestamp: datetime
    direction: str           # "LONG" (sell-side swept) or "SHORT" (buy-side swept)
    swept_levels: List[str]  # names of levels swept
    sweep_price: float       # extreme of sweep candle (low for sell-side, high for buy-side)
    reclaim_level: float     # price that must be reclaimed (the swept level)
    volume_ratio: float      # sweep bar volume / 20-bar avg
    sweep_depth_pts: float   # how far past the level the sweep went
    bars_since: int = 0      # bars elapsed since sweep detected
    reclaimed: bool = False
    invalidated: bool = False


@dataclass
class SweepSignal:
    """A confirmed liquidity sweep signal."""
    timestamp: datetime
    direction: str           # "LONG" or "SHORT"
    swept_levels: List[str]
    sweep_depth_pts: float
    reclaim_bars: int        # how many bars to reclaim
    volume_ratio: float
    score: float             # 0.0 to 1.0
    entry_price: float       # close of reclaim bar
    stop_price: float        # extreme of sweep candle ± buffer
    sweep_candle_time: datetime


class LiquiditySweepDetector:
    """
    Detects liquidity sweeps of key structural levels.

    Usage:
        detector = LiquiditySweepDetector()
        detector.update_bar(bar, vwap, htf_bias)
        signal = detector.get_signal()
    """

    RECLAIM_MAX_BARS = 3       # Must reclaim within 3 bars
    MIN_SWEEP_DEPTH = 2.0      # Minimum 2pts past level to qualify
    STOP_BUFFER = 2.0          # Buffer beyond sweep extreme for stop
    ROUND_NUMBER_INTERVAL = 50 # Round numbers every 50pts
    VOL_SPIKE_THRESHOLD = 1.5  # 1.5x 20-bar avg volume for sweep confirmation

    def __init__(self):
        # Key levels
        self._key_levels: List[KeyLevel] = []

        # Session tracking
        self._current_date: str = ""
        self._prior_day_high: float = 0.0
        self._prior_day_low: float = 0.0
        self._session_high: float = 0.0
        self._session_low: float = 0.0
        self._prior_week_high: float = 0.0
        self._prior_week_low: float = 0.0
        self._current_week_start: str = ""

        # Bar history for daily/weekly tracking
        self._daily_highs: Dict[str, float] = {}
        self._daily_lows: Dict[str, float] = {}
        self._week_highs: Dict[str, float] = {}
        self._week_lows: Dict[str, float] = {}

        # Volume tracking
        self._volume_history: List[int] = []
        self._volume_window = 20

        # Pending sweep candidates (waiting for reclaim)
        self._candidates: List[SweepCandidate] = []

        # Confirmed signals (current bar only)
        self._current_signal: Optional[SweepSignal] = None

        # Stats
        self.total_sweeps_detected = 0
        self.total_sweeps_confirmed = 0
        self.sweep_log: List[Dict] = []

    def update_bar(
        self,
        bar,
        vwap: float = 0.0,
        htf_bias: Optional[object] = None,
        is_rth: bool = False,
    ) -> Optional[SweepSignal]:
        """
        Process a new bar through the sweep detection pipeline.

        Args:
            bar: Bar dataclass (timestamp, open, high, low, close, volume)
            vwap: Current session VWAP
            htf_bias: HTFBiasResult for alignment scoring
            is_rth: True if within RTH (9:30-16:00 ET)

        Returns:
            SweepSignal if a confirmed sweep is detected, None otherwise
        """
        self._current_signal = None

        # Update session/daily/weekly tracking
        self._update_session_tracking(bar)

        # Update volume history
        self._volume_history.append(bar.volume)
        if len(self._volume_history) > self._volume_window:
            self._volume_history = self._volume_history[-self._volume_window:]

        # Rebuild key levels
        self._rebuild_key_levels(vwap, bar.close)

        # Check existing candidates for reclaim
        self._check_reclaims(bar, htf_bias, is_rth)

        # Detect new sweeps on this bar
        self._detect_new_sweeps(bar)

        return self._current_signal

    def get_signal(self) -> Optional[SweepSignal]:
        """Get the current bar's confirmed sweep signal (if any)."""
        return self._current_signal

    # ================================================================
    # SESSION / KEY LEVEL TRACKING
    # ================================================================

    def _update_session_tracking(self, bar) -> None:
        """Update daily and weekly high/low tracking."""
        date_str = bar.timestamp.strftime("%Y-%m-%d")

        # Compute week start (Monday)
        dt = bar.timestamp
        if hasattr(dt, 'weekday'):
            week_start = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
        else:
            week_start = date_str

        # New day
        if date_str != self._current_date:
            # Save prior day's H/L
            if self._current_date and self._current_date in self._daily_highs:
                self._prior_day_high = self._daily_highs[self._current_date]
                self._prior_day_low = self._daily_lows[self._current_date]

            self._current_date = date_str
            self._daily_highs[date_str] = bar.high
            self._daily_lows[date_str] = bar.low
            self._session_high = bar.high
            self._session_low = bar.low

        # New week
        if week_start != self._current_week_start:
            if self._current_week_start and self._current_week_start in self._week_highs:
                self._prior_week_high = self._week_highs[self._current_week_start]
                self._prior_week_low = self._week_lows[self._current_week_start]
            self._current_week_start = week_start
            self._week_highs[week_start] = bar.high
            self._week_lows[week_start] = bar.low

        # Update current day
        self._daily_highs[date_str] = max(
            self._daily_highs.get(date_str, bar.high), bar.high
        )
        self._daily_lows[date_str] = min(
            self._daily_lows.get(date_str, bar.low), bar.low
        )
        self._session_high = max(self._session_high, bar.high)
        self._session_low = min(self._session_low, bar.low)

        # Update current week
        self._week_highs[week_start] = max(
            self._week_highs.get(week_start, bar.high), bar.high
        )
        self._week_lows[week_start] = min(
            self._week_lows.get(week_start, bar.low), bar.low
        )

        # Trim old data (keep last 10 days, 4 weeks)
        if len(self._daily_highs) > 10:
            oldest = sorted(self._daily_highs.keys())[0]
            del self._daily_highs[oldest]
            if oldest in self._daily_lows:
                del self._daily_lows[oldest]
        if len(self._week_highs) > 4:
            oldest = sorted(self._week_highs.keys())[0]
            del self._week_highs[oldest]
            if oldest in self._week_lows:
                del self._week_lows[oldest]

    def _rebuild_key_levels(self, vwap: float, current_price: float) -> None:
        """Rebuild the list of key levels for sweep detection."""
        levels = []

        # Prior Day High / Low
        if self._prior_day_high > 0:
            levels.append(KeyLevel("PDH", self._prior_day_high, "prior_day"))
        if self._prior_day_low > 0:
            levels.append(KeyLevel("PDL", self._prior_day_low, "prior_day"))

        # Current Session High / Low (only if distinct from current bar)
        if self._session_high > 0:
            levels.append(KeyLevel("session_high", self._session_high, "session"))
        if self._session_low > 0:
            levels.append(KeyLevel("session_low", self._session_low, "session"))

        # Prior Week High / Low
        if self._prior_week_high > 0:
            levels.append(KeyLevel("PWH", self._prior_week_high, "prior_week"))
        if self._prior_week_low > 0:
            levels.append(KeyLevel("PWL", self._prior_week_low, "prior_week"))

        # VWAP
        if vwap > 0:
            levels.append(KeyLevel("VWAP", vwap, "vwap"))

        # Round numbers (every 50pts around current price)
        if current_price > 0:
            base = int(current_price / self.ROUND_NUMBER_INTERVAL) * self.ROUND_NUMBER_INTERVAL
            for offset in range(-3, 4):
                rn = base + offset * self.ROUND_NUMBER_INTERVAL
                if rn > 0:
                    levels.append(KeyLevel(f"round_{rn}", float(rn), "round"))

        self._key_levels = levels

    # ================================================================
    # SWEEP DETECTION
    # ================================================================

    def _avg_volume(self) -> float:
        """20-bar average volume."""
        if not self._volume_history:
            return 0.0
        return sum(self._volume_history) / len(self._volume_history)

    def _detect_new_sweeps(self, bar) -> None:
        """Detect new sweep candidates on the current bar."""
        avg_vol = self._avg_volume()
        if avg_vol <= 0:
            return

        vol_ratio = bar.volume / avg_vol

        for level in self._key_levels:
            # ── Sell-side sweep (bullish): price wicks BELOW level, closes ABOVE ──
            if bar.low < level.price - self.MIN_SWEEP_DEPTH and bar.close > level.price:
                sweep_depth = level.price - bar.low
                # Check if we already have this level in pending candidates
                if self._has_pending_for_level(level.name, "LONG"):
                    continue

                candidate = SweepCandidate(
                    timestamp=bar.timestamp,
                    direction="LONG",
                    swept_levels=[level.name],
                    sweep_price=bar.low,
                    reclaim_level=level.price,
                    volume_ratio=vol_ratio,
                    sweep_depth_pts=sweep_depth,
                    bars_since=0,
                    reclaimed=(bar.close > level.price),  # Immediate reclaim
                )

                # If volume confirms AND immediate reclaim, it's confirmed on this bar
                if vol_ratio >= self.VOL_SPIKE_THRESHOLD and candidate.reclaimed:
                    candidate.reclaimed = True
                    # Will be scored in _check_reclaims
                else:
                    candidate.reclaimed = False

                self._candidates.append(candidate)
                self.total_sweeps_detected += 1

            # ── Buy-side sweep (bearish): price wicks ABOVE level, closes BELOW ──
            if bar.high > level.price + self.MIN_SWEEP_DEPTH and bar.close < level.price:
                sweep_depth = bar.high - level.price
                if self._has_pending_for_level(level.name, "SHORT"):
                    continue

                candidate = SweepCandidate(
                    timestamp=bar.timestamp,
                    direction="SHORT",
                    swept_levels=[level.name],
                    sweep_price=bar.high,
                    reclaim_level=level.price,
                    volume_ratio=vol_ratio,
                    sweep_depth_pts=sweep_depth,
                    bars_since=0,
                    reclaimed=(bar.close < level.price),
                )

                if vol_ratio >= self.VOL_SPIKE_THRESHOLD and candidate.reclaimed:
                    candidate.reclaimed = True
                else:
                    candidate.reclaimed = False

                self._candidates.append(candidate)
                self.total_sweeps_detected += 1

        # Merge candidates that swept multiple levels on the same bar
        self._merge_same_bar_candidates(bar.timestamp)

    def _has_pending_for_level(self, level_name: str, direction: str) -> bool:
        """Check if there's already a pending candidate for this level+direction."""
        for c in self._candidates:
            if not c.invalidated and not c.reclaimed and c.direction == direction:
                if level_name in c.swept_levels:
                    return True
        return False

    def _merge_same_bar_candidates(self, timestamp: datetime) -> None:
        """Merge multiple sweep candidates from the same bar+direction."""
        by_dir: Dict[str, List[SweepCandidate]] = {"LONG": [], "SHORT": []}
        other = []

        for c in self._candidates:
            if c.timestamp == timestamp and not c.invalidated:
                by_dir[c.direction].append(c)
            else:
                other.append(c)

        merged = list(other)
        for direction, candidates in by_dir.items():
            if len(candidates) <= 1:
                merged.extend(candidates)
                continue

            # Merge: combine swept_levels, take best volume_ratio, deepest sweep
            primary = candidates[0]
            for c in candidates[1:]:
                for lvl in c.swept_levels:
                    if lvl not in primary.swept_levels:
                        primary.swept_levels.append(lvl)
                primary.volume_ratio = max(primary.volume_ratio, c.volume_ratio)
                primary.sweep_depth_pts = max(primary.sweep_depth_pts, c.sweep_depth_pts)
                if direction == "LONG":
                    primary.sweep_price = min(primary.sweep_price, c.sweep_price)
                else:
                    primary.sweep_price = max(primary.sweep_price, c.sweep_price)
            merged.append(primary)

        self._candidates = merged

    # ================================================================
    # RECLAIM CONFIRMATION
    # ================================================================

    def _check_reclaims(self, bar, htf_bias, is_rth: bool) -> None:
        """Check pending candidates for reclaim confirmation."""
        still_pending = []

        for candidate in self._candidates:
            if candidate.invalidated:
                continue

            candidate.bars_since += 1

            # Already reclaimed on sweep bar itself
            if candidate.reclaimed and candidate.bars_since == 1:
                signal = self._score_and_emit(candidate, bar, htf_bias, is_rth)
                if signal:
                    self._current_signal = signal
                continue

            # Check reclaim on subsequent bars
            if not candidate.reclaimed:
                if candidate.direction == "LONG":
                    # For bullish sweep: bar must close ABOVE the swept level
                    if bar.close > candidate.reclaim_level:
                        candidate.reclaimed = True
                        signal = self._score_and_emit(candidate, bar, htf_bias, is_rth)
                        if signal:
                            self._current_signal = signal
                        continue
                else:
                    # For bearish sweep: bar must close BELOW the swept level
                    if bar.close < candidate.reclaim_level:
                        candidate.reclaimed = True
                        signal = self._score_and_emit(candidate, bar, htf_bias, is_rth)
                        if signal:
                            self._current_signal = signal
                        continue

            # Timeout: no reclaim within max bars
            if candidate.bars_since > self.RECLAIM_MAX_BARS:
                candidate.invalidated = True
                continue

            still_pending.append(candidate)

        self._candidates = still_pending

    # ================================================================
    # SCORING
    # ================================================================

    def _score_and_emit(
        self,
        candidate: SweepCandidate,
        bar,
        htf_bias,
        is_rth: bool,
    ) -> Optional[SweepSignal]:
        """Score a confirmed sweep and emit a signal."""
        score = 0.5  # Base: sweep + reclaim detected

        # +0.1 if volume >= 2.0x average (strong absorption)
        if candidate.volume_ratio >= 2.0:
            score += 0.1

        # +0.1 if sweep depth 3-8pts (clean wick, not a crash)
        if 3.0 <= candidate.sweep_depth_pts <= 8.0:
            score += 0.1

        # +0.1 if multiple key levels swept simultaneously
        if len(candidate.swept_levels) >= 2:
            score += 0.1

        # +0.1 if HTF bias aligns with sweep direction
        if htf_bias is not None:
            htf_dir = getattr(htf_bias, 'consensus_direction', 'neutral')
            if candidate.direction == "LONG" and htf_dir == "bullish":
                score += 0.1
            elif candidate.direction == "SHORT" and htf_dir == "bearish":
                score += 0.1

        # +0.1 if sweep occurs during first 30min of RTH (highest probability)
        if is_rth:
            et_time = bar.timestamp.astimezone(ZoneInfo("America/New_York"))
            h, m = et_time.hour, et_time.minute
            t = h + m / 60.0
            if 9.5 <= t <= 10.0:  # First 30 min of RTH
                score += 0.1

        score = min(score, 1.0)

        # Compute entry and stop
        entry_price = bar.close
        if candidate.direction == "LONG":
            stop_price = candidate.sweep_price - self.STOP_BUFFER
        else:
            stop_price = candidate.sweep_price + self.STOP_BUFFER

        signal = SweepSignal(
            timestamp=bar.timestamp,
            direction=candidate.direction,
            swept_levels=candidate.swept_levels,
            sweep_depth_pts=round(candidate.sweep_depth_pts, 2),
            reclaim_bars=candidate.bars_since,
            volume_ratio=round(candidate.volume_ratio, 2),
            score=round(score, 2),
            entry_price=round(entry_price, 2),
            stop_price=round(stop_price, 2),
            sweep_candle_time=candidate.timestamp,
        )

        self.total_sweeps_confirmed += 1
        self.sweep_log.append({
            "timestamp": bar.timestamp.isoformat(),
            "direction": signal.direction,
            "swept_levels": signal.swept_levels,
            "sweep_depth_pts": signal.sweep_depth_pts,
            "reclaim_bars": signal.reclaim_bars,
            "volume_ratio": signal.volume_ratio,
            "score": signal.score,
            "entry_price": signal.entry_price,
            "stop_price": signal.stop_price,
        })

        logger.info(
            f"SWEEP CONFIRMED: {signal.direction} | "
            f"Levels: {', '.join(signal.swept_levels)} | "
            f"Depth: {signal.sweep_depth_pts:.1f}pts | "
            f"Vol: {signal.volume_ratio:.1f}x | "
            f"Score: {signal.score:.2f} | "
            f"Reclaim bars: {signal.reclaim_bars}"
        )

        return signal

    # ================================================================
    # STATS
    # ================================================================

    def get_stats(self) -> Dict:
        """Return sweep detection statistics."""
        return {
            "total_sweeps_detected": self.total_sweeps_detected,
            "total_sweeps_confirmed": self.total_sweeps_confirmed,
            "confirmation_rate": round(
                self.total_sweeps_confirmed / self.total_sweeps_detected * 100, 1
            ) if self.total_sweeps_detected > 0 else 0.0,
            "sweep_log_count": len(self.sweep_log),
        }

    def reset(self) -> None:
        """Reset detector state for a new run."""
        self._key_levels.clear()
        self._candidates.clear()
        self._current_signal = None
        self._volume_history.clear()
        self._daily_highs.clear()
        self._daily_lows.clear()
        self._week_highs.clear()
        self._week_lows.clear()
        self._current_date = ""
        self._prior_day_high = 0.0
        self._prior_day_low = 0.0
        self._session_high = 0.0
        self._session_low = 0.0
        self._prior_week_high = 0.0
        self._prior_week_low = 0.0
        self._current_week_start = ""
        self.total_sweeps_detected = 0
        self.total_sweeps_confirmed = 0
        self.sweep_log.clear()
