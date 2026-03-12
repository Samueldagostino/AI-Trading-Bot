"""
Overnight Level Tracker
========================
Tracks key reference levels from the overnight (Globex) session and
previous RTH day, including gap analysis.

Levels tracked:
  - Previous day close (settlement)
  - Previous day high / low
  - Overnight (Globex) high / low
  - Gap direction, size, and fill status

Gap analysis:
  - Gap fill > 50% -> likely full fill (mean-reversion)
  - Gap unfilled after 30 min -> institutional commitment (trend)

Reference: Lou, Polk & Skouras (2019), Berkman et al. (2012)
"""

import logging
from datetime import datetime, time, timedelta
from typing import Dict, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
GLOBEX_START = time(18, 0)

# Minutes after RTH open to evaluate gap fill commitment
GAP_COMMITMENT_MINUTES = 30


class OvernightLevelTracker:
    """
    Tracks overnight and previous-day reference levels with gap analysis.

    Usage:
        tracker = OvernightLevelTracker()
        for bar in bars:
            tracker.update(bar)
        levels = tracker.get_levels()
        gap = tracker.get_gap_info(current_price)
    """

    def __init__(self):
        # Previous RTH day levels
        self._prev_close: Optional[float] = None
        self._prev_high: Optional[float] = None
        self._prev_low: Optional[float] = None

        # Current RTH day accumulation
        self._rth_high: Optional[float] = None
        self._rth_low: Optional[float] = None
        self._rth_close: Optional[float] = None

        # Overnight (Globex) levels
        self._overnight_high: Optional[float] = None
        self._overnight_low: Optional[float] = None

        # Gap tracking
        self._rth_open_price: Optional[float] = None
        self._gap_direction: Optional[str] = None
        self._gap_size: float = 0.0
        self._gap_filled: bool = False
        self._gap_fill_pct: float = 0.0
        self._gap_fill_checked_at_30min: bool = False
        self._gap_unfilled_at_30min: bool = False

        # Day tracking
        self._current_date: Optional[object] = None
        self._rth_open_seen: bool = False
        self._bars_since_open: int = 0

    def update(self, bar) -> None:
        """
        Process a new bar and update levels.

        Args:
            bar: Object with timestamp (tz-aware), open, high, low, close.
        """
        et_dt = bar.timestamp.astimezone(ET)
        et_time = et_dt.time()
        et_date = et_dt.date()

        # Detect new trading day -- simple calendar date change
        is_new_day = False
        if self._current_date is None:
            is_new_day = True
        elif et_date != self._current_date:
            is_new_day = True

        if is_new_day and self._current_date is not None:
            self._roll_day()

        if is_new_day:
            self._current_date = et_date

        # Classify bar as RTH or overnight
        is_rth = RTH_OPEN <= et_time < RTH_CLOSE

        if is_rth:
            # First RTH bar -- capture open and initialize gap
            if not self._rth_open_seen:
                self._rth_open_seen = True
                self._rth_open_price = bar.open
                self._bars_since_open = 0
                self._gap_filled = False
                self._gap_fill_pct = 0.0
                self._gap_fill_checked_at_30min = False
                self._gap_unfilled_at_30min = False
                self._compute_gap()

            self._bars_since_open += 1

            # Update RTH high/low/close
            if self._rth_high is None or bar.high > self._rth_high:
                self._rth_high = bar.high
            if self._rth_low is None or bar.low < self._rth_low:
                self._rth_low = bar.low
            self._rth_close = bar.close

            # Update gap fill tracking
            self._update_gap_fill(bar)
        else:
            # Overnight session -- update overnight high/low
            if self._overnight_high is None or bar.high > self._overnight_high:
                self._overnight_high = bar.high
            if self._overnight_low is None or bar.low < self._overnight_low:
                self._overnight_low = bar.low

    def get_levels(self) -> Dict[str, Optional[float]]:
        """
        Return all tracked reference levels.

        Returns:
            Dict with prev_close, prev_high, prev_low,
            overnight_high, overnight_low, rth_open.
        """
        return {
            "prev_close": self._prev_close,
            "prev_high": self._prev_high,
            "prev_low": self._prev_low,
            "overnight_high": self._overnight_high,
            "overnight_low": self._overnight_low,
            "rth_open": self._rth_open_price,
        }

    def get_gap_info(self, current_price: float) -> Dict:
        """
        Return gap analysis for the current session.

        Args:
            current_price: Current market price.

        Returns:
            Dict with gap_direction, gap_size_pts, gap_filled,
            gap_fill_pct, and gap_unfilled_at_30min.
        """
        # Update fill percentage with current price
        if self._gap_direction and self._prev_close is not None and self._gap_size > 0:
            if self._gap_direction == "UP":
                filled = max(0, self._rth_open_price - current_price) if self._rth_open_price else 0
            else:
                filled = max(0, current_price - self._rth_open_price) if self._rth_open_price else 0
            fill_pct = min((filled / self._gap_size) * 100, 100.0)
            self._gap_fill_pct = max(self._gap_fill_pct, fill_pct)
            if self._gap_fill_pct >= 100.0:
                self._gap_filled = True

        return {
            "gap_direction": self._gap_direction or "NONE",
            "gap_size_pts": round(self._gap_size, 2),
            "gap_filled": self._gap_filled,
            "gap_fill_pct": round(self._gap_fill_pct, 2),
            "gap_unfilled_at_30min": self._gap_unfilled_at_30min,
        }

    def _compute_gap(self) -> None:
        """Compute gap direction and size at RTH open."""
        if self._prev_close is None or self._rth_open_price is None:
            self._gap_direction = None
            self._gap_size = 0.0
            return

        diff = self._rth_open_price - self._prev_close
        if abs(diff) < 0.25:  # 1 tick on NQ -- effectively no gap
            self._gap_direction = "NONE"
            self._gap_size = 0.0
        elif diff > 0:
            self._gap_direction = "UP"
            self._gap_size = diff
        else:
            self._gap_direction = "DOWN"
            self._gap_size = abs(diff)

    def _update_gap_fill(self, bar) -> None:
        """Update gap fill tracking with new RTH bar."""
        if not self._gap_direction or self._gap_direction == "NONE":
            return
        if self._gap_size <= 0 or self._prev_close is None:
            return

        # Check if gap has been filled
        if self._gap_direction == "UP":
            if bar.low <= self._prev_close:
                self._gap_filled = True
                self._gap_fill_pct = 100.0
            else:
                filled = max(0, self._rth_open_price - bar.low)
                fill_pct = min((filled / self._gap_size) * 100, 100.0)
                self._gap_fill_pct = max(self._gap_fill_pct, fill_pct)
        elif self._gap_direction == "DOWN":
            if bar.high >= self._prev_close:
                self._gap_filled = True
                self._gap_fill_pct = 100.0
            else:
                filled = max(0, bar.high - self._rth_open_price)
                fill_pct = min((filled / self._gap_size) * 100, 100.0)
                self._gap_fill_pct = max(self._gap_fill_pct, fill_pct)

        # 30-minute commitment check (approximate via bar count)
        # For 5-min bars: 6 bars = 30 min; for 1-min bars: 30 bars
        if self._bars_since_open >= 6 and not self._gap_fill_checked_at_30min:
            self._gap_fill_checked_at_30min = True
            if not self._gap_filled and self._gap_fill_pct < 50:
                self._gap_unfilled_at_30min = True
                logger.debug(
                    "Gap unfilled at 30min: %s %.2f pts (%.1f%% filled)",
                    self._gap_direction, self._gap_size, self._gap_fill_pct,
                )

    def _roll_day(self) -> None:
        """Roll previous-day levels and reset overnight tracking."""
        # Store previous RTH as "prev day" levels
        if self._rth_close is not None:
            self._prev_close = self._rth_close
        if self._rth_high is not None:
            self._prev_high = self._rth_high
        if self._rth_low is not None:
            self._prev_low = self._rth_low

        # Reset for new day
        self._rth_high = None
        self._rth_low = None
        self._rth_close = None
        self._overnight_high = None
        self._overnight_low = None
        self._rth_open_price = None
        self._rth_open_seen = False
        self._bars_since_open = 0
        self._gap_direction = None
        self._gap_size = 0.0
        self._gap_filled = False
        self._gap_fill_pct = 0.0
        self._gap_fill_checked_at_30min = False
        self._gap_unfilled_at_30min = False
