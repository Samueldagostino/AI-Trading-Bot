"""
Session VWAP Tracker
=====================
Calculates session VWAP (Volume-Weighted Average Price) and provides
distance-based mean-reversion and crossover signals.

VWAP = cumulative(price * volume) / cumulative(volume)

Institutional traders benchmark execution against VWAP, creating a
self-reinforcing gravitational pull toward this level.

Reference: Berkowitz, Logue & Noser (1988), Biais, Hillion & Spatt (1995)
"""

import logging
import math
from datetime import datetime, time
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)

# Number of recent bars to check for VWAP crossover
CROSS_LOOKBACK = 5


class VWAPTracker:
    """
    Session VWAP calculator with distance and crossover signals.

    Resets at RTH open (9:30 ET) each day.

    Usage:
        tracker = VWAPTracker()
        for bar in bars:
            tracker.update(bar)
        signal = tracker.get_vwap_signal(current_price)
    """

    def __init__(self, atr: float = 0.0):
        self._cumulative_pv: float = 0.0  # sum(typical_price * volume)
        self._cumulative_vol: float = 0.0  # sum(volume)
        self._vwap: float = 0.0
        self._current_date: Optional[object] = None
        self._atr: float = atr
        self._recent_positions: List[str] = []  # "ABOVE" / "BELOW" for crossover

    def update(self, bar) -> None:
        """
        Add a bar to the VWAP calculation.

        Args:
            bar: Object with timestamp (tz-aware), high, low, close, volume.
        """
        et_dt = bar.timestamp.astimezone(ET)
        et_time = et_dt.time()
        et_date = et_dt.date()

        # New day -- reset VWAP at RTH open
        if self._current_date != et_date and et_time >= RTH_OPEN:
            self._reset()
            self._current_date = et_date

        # Only accumulate during RTH
        if not (RTH_OPEN <= et_time < RTH_CLOSE):
            return

        vol = bar.volume if bar.volume > 0 else 1
        typical_price = (bar.high + bar.low + bar.close) / 3.0

        # Guard: skip NaN/Inf prices to prevent permanent accumulator corruption
        if not math.isfinite(typical_price):
            return


        self._cumulative_pv += typical_price * vol
        self._cumulative_vol += vol

        if self._cumulative_vol > 0:
            self._vwap = self._cumulative_pv / self._cumulative_vol

        # Track position relative to VWAP for crossover detection
        position = "ABOVE" if bar.close > self._vwap else "BELOW"
        self._recent_positions.append(position)
        if len(self._recent_positions) > CROSS_LOOKBACK + 1:
            self._recent_positions = self._recent_positions[-(CROSS_LOOKBACK + 1):]

    def set_atr(self, atr: float) -> None:
        """Update the ATR value used for extension detection."""
        self._atr = atr

    def get_vwap(self) -> float:
        """Return current session VWAP. Returns 0.0 if no data."""
        return round(self._vwap, 2)

    def get_distance_from_vwap(self, current_price: float) -> float:
        """
        Distance from current price to VWAP in points.

        Positive = above VWAP, negative = below VWAP.
        Returns 0.0 if VWAP not established.
        """
        if self._vwap == 0.0:
            return 0.0
        return round(current_price - self._vwap, 2)

    def get_vwap_signal(self, current_price: float) -> Dict:
        """
        Return VWAP signal context for the current price.

        Args:
            current_price: Current market price.

        Returns:
            Dict with vwap, distance_pts, distance_pct, position,
            crossed_recently, and extended.
        """
        vwap = self.get_vwap()
        distance_pts = self.get_distance_from_vwap(current_price)
        distance_pct = 0.0
        if vwap > 0:
            distance_pct = round((distance_pts / vwap) * 100, 4)

        position = "ABOVE" if current_price >= vwap else "BELOW"
        crossed = self._check_crossed_recently()
        extended = self._check_extended(abs(distance_pts))

        return {
            "vwap": vwap,
            "distance_pts": distance_pts,
            "distance_pct": distance_pct,
            "position": position,
            "crossed_recently": crossed,
            "extended": extended,
        }

    def _check_crossed_recently(self) -> bool:
        """Check if price crossed VWAP within the last CROSS_LOOKBACK bars."""
        if len(self._recent_positions) < 2:
            return False
        # A cross occurred if position changed in recent history
        recent = self._recent_positions[-CROSS_LOOKBACK:]
        return len(set(recent)) > 1

    def _check_extended(self, abs_distance: float) -> bool:
        """Check if price is extended (> 1 ATR from VWAP)."""
        if self._atr <= 0:
            return False
        return abs_distance > self._atr

    def _reset(self) -> None:
        """Reset VWAP calculation for a new session."""
        self._cumulative_pv = 0.0
        self._cumulative_vol = 0.0
        self._vwap = 0.0
        self._recent_positions = []
