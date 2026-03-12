"""
Liquidity Sweep Detector -- HTF-First Architecture
====================================================
Detects institutional liquidity sweeps using a TOP-DOWN approach:

  1. HTF bars (15m, 1H) detect the sweep -- a candle whose wick violates
     a key level but closes back inside.  Institutional volume lives on
     these timeframes.
  2. The 2-minute execution bar provides precise entry timing once
     an HTF sweep is confirmed.
  3. Stops reference the HTF sweep candle extreme (naturally wider,
     letting reversals develop).

Why HTF-first?
  - A 2-minute wick past a level by 2 points is often noise.
  - Real institutional sweeps involve large order fills visible on
    the 15m or 1H chart.
  - Backtest evidence (2,704 trades): 83.5% of 2m-only sweeps
    stopped out immediately.  Wider HTF-based stops had 10+ pct
    higher win rate.

Key Levels Tracked:
  - Prior Day High (PDH) / Prior Day Low (PDL)
  - Current Session High / Low (rolling)
  - Prior Week High / Low
  - Round numbers (every 100pts)
  Note: VWAP removed after shadow analysis showed negative edge

Signal Output (unchanged interface):
  direction: LONG (sell-side sweep) or SHORT (buy-side sweep)
  score: 0.0 to 1.0 (multi-factor)
  swept_levels: list of level names swept
  entry_price: close of 2m reclaim bar
  stop_price: extreme of HTF sweep candle ± buffer

Integration:
  Called from TradingOrchestrator.process_bar() as the primary
  signal source (PATH C architecture).

Changes from v1:
  - HTF bars (15m, 1H) fed via update_htf_bar()
  - Sweeps detected on HTF bars, not 2m bars
  - 2m bars used only for entry timing (reclaim confirmation)
  - Stops placed at HTF candle extreme (wider, more realistic)
  - RTH-only filter: no sweeps outside 9:30-16:00 ET
  - Reversal candle confirmation required on 2m
  - Scoring recalibrated: removed broken bonuses, added HTF-validated ones
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
class HTFSweepCandidate:
    """A sweep detected on an HTF bar, awaiting 2m entry confirmation."""
    timestamp: datetime          # HTF bar timestamp
    htf_timeframe: str           # "15m" or "1H"
    direction: str               # "LONG" (sell-side swept) or "SHORT" (buy-side swept)
    swept_levels: List[str]      # names of levels swept
    sweep_price: float           # extreme of HTF sweep candle (low for sell-side, high for buy-side)
    reclaim_level: float         # price that must be reclaimed (the swept level)
    htf_volume: int              # volume of the HTF sweep candle
    sweep_depth_pts: float       # how far past the level the sweep went
    htf_close: float             # close of the HTF candle
    htf_open: float              # open of the HTF candle
    # 2m entry tracking
    exec_bars_since: int = 0     # 2m bars since HTF sweep detected
    confirmed: bool = False      # True when 2m reversal candle confirms
    invalidated: bool = False
    # Reversal candle state
    reversal_candle_seen: bool = False


@dataclass
class SweepSignal:
    """A confirmed liquidity sweep signal (interface unchanged from v1)."""
    timestamp: datetime
    direction: str           # "LONG" or "SHORT"
    swept_levels: List[str]
    sweep_depth_pts: float
    reclaim_bars: int        # how many 2m bars to confirm
    volume_ratio: float
    score: float             # 0.0 to 1.0
    entry_price: float       # close of 2m confirmation bar
    stop_price: float        # extreme of HTF sweep candle ± buffer
    sweep_candle_time: datetime
    htf_timeframe: str = ""  # NEW: which HTF timeframe detected the sweep


class LiquiditySweepDetector:
    """
    HTF-first liquidity sweep detector.

    Flow:
      1. update_htf_bar(tf, bar) -- feed completed HTF bars (15m, 1H)
         Detects sweep candidates on these timeframes.
      2. update_bar(bar, ...) -- feed 2m execution bars
         Confirms entries via reversal candle pattern.
      3. get_signal() -- returns confirmed SweepSignal or None

    Usage:
        detector = LiquiditySweepDetector()
        # On each HTF bar completion:
        detector.update_htf_bar("15m", htf_bar)
        # On each 2m bar:
        signal = detector.update_bar(bar, vwap, htf_bias, is_rth)
    """

    # ── Configuration ──
    # Min sweep depth scaled by timeframe.  A 1H candle has far larger
    # range than 15m, so a 3pt wick past a level on 1H is just noise.
    # These thresholds ensure the sweep is actually meaningful for that TF.
    MIN_SWEEP_DEPTH_BY_TF: Dict[str, float] = {
        "5m":  3.0,     # 5m avg range ~8-15pts; 3pt wick = significant
        "15m": 5.0,     # 15m avg range ~15-25pts; 5pt wick = meaningful
        "30m": 10.0,    # 30m avg range ~25-40pts; 10pt wick = meaningful
        "1H":  13.0,    # 1H avg range ~40-60pts; 13pt wick = institutional
        "4H":  40.0,    # 4H avg range ~80-120pts; 40pt wick = major sweep
        "1D":  60.0,    # Daily avg range ~120-200pts; 60pt wick = monster sweep
    }
    MIN_SWEEP_DEPTH_DEFAULT = 10.0  # Fallback for unlisted timeframes
    STOP_BUFFER = 3.0               # Buffer beyond HTF sweep extreme for stop
    # Round number intervals for sweep detection.
    # 100pt removed -- too dense, generates noise sweeps on every intraday move.
    # 1000pt = major psychological (19000, 20000, 21000) -- massive liquidity pools.
    # 500pt  = intermediate psychological (19500, 20500) -- significant but less dense.
    ROUND_NUMBER_MAJOR = 1000       # Major round levels (strongest)
    ROUND_NUMBER_MINOR = 500        # Intermediate round levels
    ENTRY_WINDOW_BARS = 15          # Max 2m bars to find entry after HTF sweep (30 min)
    REVERSAL_CONFIRM_BARS = 5       # 2m bars to confirm reversal pattern (10 min)

    # ── HTF timeframes we detect sweeps on (ordered by priority) ──
    # Higher TFs = higher conviction sweeps.  All feed via update_htf_bar().
    SWEEP_TIMEFRAMES = ["15m", "30m", "1H", "4H", "1D"]

    # ── Session windows (ET) where institutional volume is present ──
    # We allow sweeps to be confirmed during active sessions, not just RTH.
    # Dead zones (low volume chop) are blocked.
    # Backtest data: 1-3 AM ET was 13-27% win rate (dead chop).
    # London open (3-4:30 AM ET) and regular Asia (7-9 PM ET) have
    # clean institutional flow.
    #
    # Format: (start_hour, end_hour) in ET -- fractional hours.
    ACTIVE_SESSION_WINDOWS = [
        (18.5, 23.5),   # Asia session: 6:30 PM - 11:30 PM ET
        (2.0,  4.5),    # London open: 2:00 AM - 4:30 AM ET
        (6.0,  16.0),   # US Pre-market + RTH: 6:00 AM - 4:00 PM ET
    ]
    # Dead zones (blocked):
    #   DZ1: 11:30 PM - 2:00 AM ET (post-Asia chop)
    #   DZ2: 4:30 AM - 6:00 AM ET (post-London, pre-US lull)

    def __init__(self):
        # Key levels
        self._key_levels: List[KeyLevel] = []

        # Session tracking (same as v1)
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

        # Volume tracking (2m bars for volume context)
        self._volume_history: List[int] = []
        self._volume_window = 20

        # HTF bar history (for volume comparison within each TF)
        self._htf_volume_history: Dict[str, List[int]] = {
            tf: [] for tf in self.SWEEP_TIMEFRAMES
        }
        self._htf_volume_window = 10

        # HTF sweep candidates awaiting 2m entry
        self._htf_candidates: List[HTFSweepCandidate] = []

        # Confirmed signals (current 2m bar only)
        self._current_signal: Optional[SweepSignal] = None

        # Stats
        self.total_sweeps_detected = 0     # HTF sweeps detected
        self.total_sweeps_confirmed = 0    # 2m entries confirmed
        self.total_htf_sweeps_expired = 0  # HTF sweeps that found no 2m entry
        self.sweep_log: List[Dict] = []

    # ================================================================
    # HTF BAR PROCESSING -- where sweeps are actually detected
    # ================================================================

    def update_htf_bar(self, timeframe: str, htf_bar) -> None:
        """
        Process a completed HTF bar for sweep detection.

        Args:
            timeframe: "15m", "30m", "1H", "4H", or "1D"
            htf_bar: HTFBar with timestamp, open, high, low, close, volume
        """
        if timeframe not in self.SWEEP_TIMEFRAMES:
            return

        # Track HTF volume history
        vol_list = self._htf_volume_history.get(timeframe, [])
        vol_list.append(htf_bar.volume)
        if len(vol_list) > self._htf_volume_window:
            vol_list = vol_list[-self._htf_volume_window:]
        self._htf_volume_history[timeframe] = vol_list

        # Check this HTF bar against key levels for sweep patterns
        self._detect_htf_sweep(timeframe, htf_bar)

    def _htf_avg_volume(self, timeframe: str) -> float:
        """Average volume for HTF timeframe."""
        vols = self._htf_volume_history.get(timeframe, [])
        if not vols:
            return 0.0
        return sum(vols) / len(vols)

    def _detect_htf_sweep(self, timeframe: str, htf_bar) -> None:
        """Detect sweep candidates on an HTF bar."""
        min_depth = self.MIN_SWEEP_DEPTH_BY_TF.get(
            timeframe, self.MIN_SWEEP_DEPTH_DEFAULT
        )
        for level in self._key_levels:
            # ── Sell-side sweep (bullish): HTF wick BELOW level, closes ABOVE ──
            if (htf_bar.low < level.price - min_depth
                    and htf_bar.close > level.price):
                sweep_depth = level.price - htf_bar.low

                # Skip if we already have a pending candidate for this level+direction
                if self._has_pending_htf(level.name, "LONG"):
                    continue

                candidate = HTFSweepCandidate(
                    timestamp=htf_bar.timestamp,
                    htf_timeframe=timeframe,
                    direction="LONG",
                    swept_levels=[level.name],
                    sweep_price=htf_bar.low,
                    reclaim_level=level.price,
                    htf_volume=htf_bar.volume,
                    sweep_depth_pts=sweep_depth,
                    htf_close=htf_bar.close,
                    htf_open=htf_bar.open,
                )
                self._htf_candidates.append(candidate)
                self.total_sweeps_detected += 1
                logger.info(
                    f"HTF SWEEP DETECTED [{timeframe}]: LONG | "
                    f"Level: {level.name} ({level.price:.2f}) | "
                    f"Depth: {sweep_depth:.1f}pts | "
                    f"Close: {htf_bar.close:.2f}"
                )

            # ── Buy-side sweep (bearish): HTF wick ABOVE level, closes BELOW ──
            if (htf_bar.high > level.price + min_depth
                    and htf_bar.close < level.price):
                sweep_depth = htf_bar.high - level.price

                if self._has_pending_htf(level.name, "SHORT"):
                    continue

                candidate = HTFSweepCandidate(
                    timestamp=htf_bar.timestamp,
                    htf_timeframe=timeframe,
                    direction="SHORT",
                    swept_levels=[level.name],
                    sweep_price=htf_bar.high,
                    reclaim_level=level.price,
                    htf_volume=htf_bar.volume,
                    sweep_depth_pts=sweep_depth,
                    htf_close=htf_bar.close,
                    htf_open=htf_bar.open,
                )
                self._htf_candidates.append(candidate)
                self.total_sweeps_detected += 1
                logger.info(
                    f"HTF SWEEP DETECTED [{timeframe}]: SHORT | "
                    f"Level: {level.name} ({level.price:.2f}) | "
                    f"Depth: {sweep_depth:.1f}pts | "
                    f"Close: {htf_bar.close:.2f}"
                )

        # Merge HTF candidates from same bar that swept multiple levels
        self._merge_htf_candidates(htf_bar.timestamp, timeframe)

    def _has_pending_htf(self, level_name: str, direction: str) -> bool:
        """Check if there's already a pending HTF candidate for this level+direction."""
        for c in self._htf_candidates:
            if not c.invalidated and not c.confirmed and c.direction == direction:
                if level_name in c.swept_levels:
                    return True
        return False

    def _merge_htf_candidates(self, timestamp: datetime, timeframe: str) -> None:
        """Merge multiple HTF sweep candidates from the same bar+direction."""
        by_dir: Dict[str, List[HTFSweepCandidate]] = {"LONG": [], "SHORT": []}
        other = []

        for c in self._htf_candidates:
            if (c.timestamp == timestamp and c.htf_timeframe == timeframe
                    and not c.invalidated):
                by_dir[c.direction].append(c)
            else:
                other.append(c)

        merged = list(other)
        for direction, candidates in by_dir.items():
            if len(candidates) <= 1:
                merged.extend(candidates)
                continue

            primary = candidates[0]
            for c in candidates[1:]:
                for lvl in c.swept_levels:
                    if lvl not in primary.swept_levels:
                        primary.swept_levels.append(lvl)
                primary.sweep_depth_pts = max(primary.sweep_depth_pts, c.sweep_depth_pts)
                if direction == "LONG":
                    primary.sweep_price = min(primary.sweep_price, c.sweep_price)
                else:
                    primary.sweep_price = max(primary.sweep_price, c.sweep_price)
            merged.append(primary)

        self._htf_candidates = merged

    # ================================================================
    # 2M BAR PROCESSING -- entry timing & confirmation
    # ================================================================

    def update_bar(
        self,
        bar,
        vwap: float = 0.0,
        htf_bias: Optional[object] = None,
        is_rth: bool = False,
    ) -> Optional[SweepSignal]:
        """
        Process a 2m execution bar.  Checks pending HTF sweep candidates
        for reversal candle confirmation to generate entry signals.

        Interface is identical to v1 for backward compatibility.

        Args:
            bar: Bar dataclass (timestamp, open, high, low, close, volume)
            vwap: Current session VWAP
            htf_bias: HTFBiasResult for alignment scoring
            is_rth: True if within RTH (9:30-16:00 ET)

        Returns:
            SweepSignal if a confirmed entry is found, None otherwise
        """
        self._current_signal = None

        # Update session/daily/weekly tracking (same as v1, needed for key levels)
        self._update_session_tracking(bar)

        # Update 2m volume history
        self._volume_history.append(bar.volume)
        if len(self._volume_history) > self._volume_window:
            self._volume_history = self._volume_history[-self._volume_window:]

        # Rebuild key levels (needed for HTF sweep detection)
        self._rebuild_key_levels(vwap, bar.close)

        # ── SESSION FILTER ──
        # Only confirm entries during active session windows where institutional
        # volume is present.  Dead zones (11:30 PM - 3 AM ET, 4:30 - 8 AM ET)
        # showed 13-27% win rate in backtest -- pure chop.
        # Asia/London opens and US session are allowed.
        if not self._is_active_session(bar.timestamp):
            # Still age out candidates, but don't confirm entries
            self._age_htf_candidates()
            return None

        # ── Check pending HTF candidates for 2m entry confirmation ──
        self._check_entry_confirmations(bar, htf_bias, is_rth)

        return self._current_signal

    def _is_active_session(self, timestamp: datetime) -> bool:
        """
        Check if the given timestamp falls within an active trading session.

        Active sessions are defined by ACTIVE_SESSION_WINDOWS (ET hours):
          - Asia:   7:00 PM - 11:30 PM ET
          - London: 3:00 AM - 4:30 AM ET
          - US:     8:00 AM - 4:00 PM ET

        Dead zones (blocked):
          - 11:30 PM - 3:00 AM ET  (low volume chop)
          - 4:30 AM - 8:00 AM ET   (gap between London close and US pre-market)
        """
        et_time = timestamp.astimezone(ZoneInfo("America/New_York"))
        t = et_time.hour + et_time.minute / 60.0

        for start, end in self.ACTIVE_SESSION_WINDOWS:
            if start <= t < end:
                return True
        return False

    def _age_htf_candidates(self) -> None:
        """Age and expire HTF candidates (called each 2m bar)."""
        still_pending = []
        for c in self._htf_candidates:
            if c.invalidated or c.confirmed:
                continue
            c.exec_bars_since += 1
            if c.exec_bars_since > self.ENTRY_WINDOW_BARS:
                c.invalidated = True
                self.total_htf_sweeps_expired += 1
                continue
            still_pending.append(c)
        self._htf_candidates = still_pending

    def _check_entry_confirmations(self, bar, htf_bias, is_rth: bool) -> None:
        """
        Check each pending HTF sweep candidate for 2m reversal confirmation.

        Reversal confirmation (must happen within ENTRY_WINDOW_BARS):
          LONG: 2m bar makes a higher low than previous bar AND closes above reclaim level
          SHORT: 2m bar makes a lower high than previous bar AND closes below reclaim level
        """
        still_pending = []
        best_signal: Optional[SweepSignal] = None
        best_score: float = 0.0

        for candidate in self._htf_candidates:
            if candidate.invalidated or candidate.confirmed:
                continue

            candidate.exec_bars_since += 1

            # Timeout check
            if candidate.exec_bars_since > self.ENTRY_WINDOW_BARS:
                candidate.invalidated = True
                self.total_htf_sweeps_expired += 1
                continue

            # ── Reversal candle confirmation ──
            confirmed = False
            if candidate.direction == "LONG":
                # Price must be above the key level (reclaimed on 2m)
                # AND the bar itself shows buying pressure (close > open = green candle)
                if bar.close > candidate.reclaim_level and bar.close > bar.open:
                    # Additional: bar low must be above sweep candle extreme
                    # (price is actually bouncing, not still dumping)
                    if bar.low > candidate.sweep_price:
                        confirmed = True
            else:  # SHORT
                if bar.close < candidate.reclaim_level and bar.close < bar.open:
                    if bar.high < candidate.sweep_price:
                        confirmed = True

            if confirmed:
                candidate.confirmed = True
                signal = self._score_and_emit(candidate, bar, htf_bias, is_rth)
                if signal and signal.score > best_score:
                    best_signal = signal
                    best_score = signal.score
                continue

            still_pending.append(candidate)

        self._htf_candidates = still_pending
        if best_signal:
            self._current_signal = best_signal

    def get_signal(self) -> Optional[SweepSignal]:
        """Get the current bar's confirmed sweep signal (if any)."""
        return self._current_signal

    # ================================================================
    # SESSION / KEY LEVEL TRACKING (unchanged from v1)
    # ================================================================

    def _update_session_tracking(self, bar) -> None:
        """Update daily and weekly high/low tracking."""
        date_str = bar.timestamp.strftime("%Y-%m-%d")

        dt = bar.timestamp
        if hasattr(dt, 'weekday'):
            week_start = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
        else:
            week_start = date_str

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

        self._daily_highs[date_str] = max(
            self._daily_highs.get(date_str, bar.high), bar.high
        )
        self._daily_lows[date_str] = min(
            self._daily_lows.get(date_str, bar.low), bar.low
        )
        self._session_high = max(self._session_high, bar.high)
        self._session_low = min(self._session_low, bar.low)

        self._week_highs[week_start] = max(
            self._week_highs.get(week_start, bar.high), bar.high
        )
        self._week_lows[week_start] = min(
            self._week_lows.get(week_start, bar.low), bar.low
        )

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

        if self._prior_day_high > 0:
            levels.append(KeyLevel("PDH", self._prior_day_high, "prior_day"))
        if self._prior_day_low > 0:
            levels.append(KeyLevel("PDL", self._prior_day_low, "prior_day"))

        if self._session_high > 0:
            levels.append(KeyLevel("session_high", self._session_high, "session"))
        if self._session_low > 0:
            levels.append(KeyLevel("session_low", self._session_low, "session"))

        if self._prior_week_high > 0:
            levels.append(KeyLevel("PWH", self._prior_week_high, "prior_week"))
        if self._prior_week_low > 0:
            levels.append(KeyLevel("PWL", self._prior_week_low, "prior_week"))

        # VWAP removed -- shadow analysis showed it's not a true liquidity pool
        # (36.6% WR, -$3,427 over 590 trades vs 39.5% WR without it)

        # Major round numbers (1000pt): 19000, 20000, 21000 -- strongest psychological levels
        # These get "round_major" type so scoring treats them like PDH/PDL (strong).
        if current_price > 0:
            base_major = int(current_price / self.ROUND_NUMBER_MAJOR) * self.ROUND_NUMBER_MAJOR
            for offset in range(-2, 3):
                rn = base_major + offset * self.ROUND_NUMBER_MAJOR
                if rn > 0:
                    levels.append(KeyLevel(f"round_{rn}", float(rn), "round_major"))

        # Minor round numbers (500pt): 19500, 20500 -- significant but not as strong
        # Skip any that overlap with major levels (e.g. 20000 is both 500 and 1000).
        if current_price > 0:
            base_minor = int(current_price / self.ROUND_NUMBER_MINOR) * self.ROUND_NUMBER_MINOR
            major_prices = {base_major + o * self.ROUND_NUMBER_MAJOR for o in range(-2, 3)}
            for offset in range(-2, 3):
                rn = base_minor + offset * self.ROUND_NUMBER_MINOR
                if rn > 0 and rn not in major_prices:
                    levels.append(KeyLevel(f"round_{rn}", float(rn), "round_minor"))

        self._key_levels = levels

    # ================================================================
    # SCORING -- recalibrated for HTF-first
    # ================================================================

    def _score_and_emit(
        self,
        candidate: HTFSweepCandidate,
        bar,
        htf_bias,
        is_rth: bool,
    ) -> Optional[SweepSignal]:
        """
        Score a confirmed HTF sweep + 2m entry and emit a signal.

        Scoring recalibrated based on backtest data:
          - Base 0.50: HTF sweep detected + 2m reversal confirmed
          - +0.10: HTF volume spike (2x+ avg for that TF)
          - +0.10: Multiple key levels swept simultaneously
          - +0.10: HTF bias aligns with sweep direction
          - +0.05/0.10/0.15: HTF timeframe bonus (30m/1H/4H+1D)
          - +0.10: Sweep of prior day or prior week level (strongest levels)

        Removed (data showed no predictive value on 2m):
          - 2m volume spike (noise on execution TF)
          - Depth 3-8pts bonus (meaningless once HTF filters noise)
          - First 30min RTH bonus (no edge in data)
        """
        score = 0.50  # Base: HTF sweep + 2m reversal confirmed

        # +0.10 if HTF volume >= 2.0x average for that timeframe
        htf_avg_vol = self._htf_avg_volume(candidate.htf_timeframe)
        htf_vol_ratio = (
            candidate.htf_volume / htf_avg_vol if htf_avg_vol > 0 else 0.0
        )
        if htf_vol_ratio >= 2.0:
            score += 0.10

        # +0.10 if multiple key levels swept simultaneously
        if len(candidate.swept_levels) >= 2:
            score += 0.10

        # +0.10 if HTF bias aligns with sweep direction
        if htf_bias is not None:
            htf_dir = getattr(htf_bias, 'consensus_direction', 'neutral')
            if candidate.direction == "LONG" and htf_dir == "bullish":
                score += 0.10
            elif candidate.direction == "SHORT" and htf_dir == "bearish":
                score += 0.10

        # +0.05 to +0.15 based on HTF timeframe (higher = more significant)
        tf_bonus = {
            "15m": 0.0, "30m": 0.05, "1H": 0.10, "4H": 0.15, "1D": 0.15,
        }
        score += tf_bonus.get(candidate.htf_timeframe, 0.0)

        # +0.10 if sweeping prior day, prior week, or major round number level
        # Major rounds (20000, 21000) have institutional significance comparable to PDH/PDL.
        strong_levels = {"PDH", "PDL", "PWH", "PWL"}
        has_strong = any(lvl in strong_levels for lvl in candidate.swept_levels)
        has_major_round = any(lvl.startswith("round_") and
                              self._is_major_round(lvl) for lvl in candidate.swept_levels)
        if has_strong or has_major_round:
            score += 0.10

        score = min(score, 1.0)

        # ── Entry and stop prices ──
        entry_price = bar.close

        # Stop at HTF sweep candle extreme + buffer (wider, lets reversals develop)
        if candidate.direction == "LONG":
            stop_price = candidate.sweep_price - self.STOP_BUFFER
        else:
            stop_price = candidate.sweep_price + self.STOP_BUFFER

        # Compute volume ratio for interface compatibility
        avg_vol_2m = self._avg_volume()
        vol_ratio = bar.volume / avg_vol_2m if avg_vol_2m > 0 else 0.0

        signal = SweepSignal(
            timestamp=bar.timestamp,
            direction=candidate.direction,
            swept_levels=candidate.swept_levels,
            sweep_depth_pts=round(candidate.sweep_depth_pts, 2),
            reclaim_bars=candidate.exec_bars_since,
            volume_ratio=round(htf_vol_ratio, 2),  # Use HTF volume ratio (more meaningful)
            score=round(score, 2),
            entry_price=round(entry_price, 2),
            stop_price=round(stop_price, 2),
            sweep_candle_time=candidate.timestamp,
            htf_timeframe=candidate.htf_timeframe,
        )

        self.total_sweeps_confirmed += 1
        self.sweep_log.append({
            "timestamp": bar.timestamp.isoformat(),
            "htf_timeframe": candidate.htf_timeframe,
            "direction": signal.direction,
            "swept_levels": signal.swept_levels,
            "sweep_depth_pts": signal.sweep_depth_pts,
            "reclaim_bars": signal.reclaim_bars,
            "htf_volume_ratio": htf_vol_ratio,
            "score": signal.score,
            "entry_price": signal.entry_price,
            "stop_price": signal.stop_price,
        })

        logger.info(
            f"SWEEP CONFIRMED [HTF-{candidate.htf_timeframe}]: {signal.direction} | "
            f"Levels: {', '.join(signal.swept_levels)} | "
            f"Depth: {signal.sweep_depth_pts:.1f}pts | "
            f"HTF Vol: {htf_vol_ratio:.1f}x | "
            f"Score: {signal.score:.2f} | "
            f"2m confirm after {signal.reclaim_bars} bars"
        )

        return signal

    def _is_major_round(self, level_name: str) -> bool:
        """Check if a round level name corresponds to a major (1000pt) round."""
        try:
            price = float(level_name.replace("round_", ""))
            return price % self.ROUND_NUMBER_MAJOR == 0
        except (ValueError, ZeroDivisionError):
            return False

    def _avg_volume(self) -> float:
        """20-bar average volume (2m bars)."""
        if not self._volume_history:
            return 0.0
        return sum(self._volume_history) / len(self._volume_history)

    # ================================================================
    # STATS
    # ================================================================

    def get_stats(self) -> Dict:
        """Return sweep detection statistics."""
        return {
            "total_sweeps_detected": self.total_sweeps_detected,
            "total_sweeps_confirmed": self.total_sweeps_confirmed,
            "total_htf_sweeps_expired": self.total_htf_sweeps_expired,
            "confirmation_rate": round(
                self.total_sweeps_confirmed / self.total_sweeps_detected * 100, 1
            ) if self.total_sweeps_detected > 0 else 0.0,
            "sweep_log_count": len(self.sweep_log),
        }

    def reset(self) -> None:
        """Reset detector state for a new run."""
        self._key_levels.clear()
        self._htf_candidates.clear()
        self._current_signal = None
        self._volume_history.clear()
        self._htf_volume_history = {tf: [] for tf in self.SWEEP_TIMEFRAMES}
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
        self.total_htf_sweeps_expired = 0
        self.sweep_log.clear()
