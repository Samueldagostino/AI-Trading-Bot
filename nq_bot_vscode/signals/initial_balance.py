"""
Initial Balance Tracker
========================
Tracks the first 30 and 60 minutes of RTH to establish the
Initial Balance (IB) range, classify the day type, and detect
IB breaks.

IB Theory (Steidlmayer & Dalton):
  - Narrow IB (< 20th percentile of last 20 days) -> TREND_DAY
  - Wide IB (> 80th percentile) -> RANGE_DAY
  - IB break direction has 65-70% follow-through rate

Reference: Steidlmayer & Hawkins (1986), Dalton "Mind Over Markets" (1993)
"""

import logging
from collections import deque
from datetime import datetime, time
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

RTH_OPEN = time(9, 30)
IB_30_END = time(10, 0)
IB_60_END = time(10, 30)


class InitialBalanceTracker:
    """
    Tracks Initial Balance range and classifies day type.

    Usage:
        tracker = InitialBalanceTracker()
        for bar in bars:
            tracker.update(bar)
        if tracker.is_ib_complete():
            classification = tracker.get_ib_classification()
            day_type = tracker.get_day_type_forecast()
            break_dir = tracker.get_ib_break_direction(current_price)
    """

    def __init__(self, history_length: int = 20):
        self._history_length = history_length
        self._ib_range_history: deque = deque(maxlen=history_length)

        # Current day state
        self._ib_high: Optional[float] = None
        self._ib_low: Optional[float] = None
        self._ib_complete: bool = False
        self._current_date: Optional[object] = None
        self._bar_count: int = 0

    def update(self, bar) -> None:
        """
        Process a new bar and accumulate IB high/low.

        Args:
            bar: Object with timestamp (tz-aware), high, low, close attributes.
        """
        et_dt = bar.timestamp.astimezone(ET)
        et_time = et_dt.time()
        et_date = et_dt.date()

        # New trading day -- reset
        if self._current_date != et_date:
            self._finalize_day()
            self._current_date = et_date
            self._ib_high = None
            self._ib_low = None
            self._ib_complete = False
            self._bar_count = 0

        # Only accumulate during IB window (9:30 - 10:30 ET)
        if RTH_OPEN <= et_time < IB_60_END:
            self._bar_count += 1
            if self._ib_high is None or bar.high > self._ib_high:
                self._ib_high = bar.high
            if self._ib_low is None or bar.low < self._ib_low:
                self._ib_low = bar.low

        # Mark complete once past IB window
        if et_time >= IB_60_END and not self._ib_complete and self._ib_high is not None:
            self._ib_complete = True
            ib_range = self._ib_high - self._ib_low
            self._ib_range_history.append(ib_range)
            logger.debug(
                "IB complete: high=%.2f low=%.2f range=%.2f",
                self._ib_high, self._ib_low, ib_range,
            )

    def is_ib_complete(self) -> bool:
        """True after the 60-minute IB window has closed."""
        return self._ib_complete

    def get_ib_high(self) -> Optional[float]:
        """Return IB high, or None if no data yet."""
        return self._ib_high

    def get_ib_low(self) -> Optional[float]:
        """Return IB low, or None if no data yet."""
        return self._ib_low

    def get_ib_range(self) -> float:
        """
        Return IB range in points (IB high - IB low).
        Returns 0.0 if IB is not yet established.
        """
        if self._ib_high is None or self._ib_low is None:
            return 0.0
        return self._ib_high - self._ib_low

    def get_ib_classification(self) -> str:
        """
        Classify current IB range relative to rolling history.

        Returns:
            "NARROW" if IB range < 20th percentile of last N days.
            "WIDE"   if IB range > 80th percentile.
            "NORMAL" otherwise.
            "UNKNOWN" if insufficient history.
        """
        if not self._ib_complete:
            return "UNKNOWN"

        current_range = self.get_ib_range()
        history = list(self._ib_range_history)

        # Need at least 5 days of history for percentile to be meaningful
        if len(history) < 5:
            return "UNKNOWN"

        sorted_history = sorted(history)
        n = len(sorted_history)
        count_below = sum(1 for v in sorted_history if v < current_range)
        percentile = (count_below / n) * 100

        if percentile < 20:
            return "NARROW"
        elif percentile > 80:
            return "WIDE"
        else:
            return "NORMAL"

    def get_ib_break_direction(self, current_price: float) -> Optional[str]:
        """
        Determine if price has broken out of the IB range.

        Args:
            current_price: Current market price.

        Returns:
            "LONG" if price > IB high.
            "SHORT" if price < IB low.
            None if inside IB range or IB not complete.
        """
        if not self._ib_complete:
            return None
        if self._ib_high is None or self._ib_low is None:
            return None

        if current_price > self._ib_high:
            return "LONG"
        elif current_price < self._ib_low:
            return "SHORT"
        return None

    def get_day_type_forecast(self) -> str:
        """
        Forecast the day type based on IB classification.

        Returns:
            "TREND_DAY" for narrow IB (expect range expansion).
            "RANGE_DAY" for wide IB (expect mean-reversion).
            "NORMAL_DAY" for normal IB.
            "UNKNOWN" if IB not complete or insufficient history.
        """
        classification = self.get_ib_classification()
        if classification == "NARROW":
            return "TREND_DAY"
        elif classification == "WIDE":
            return "RANGE_DAY"
        elif classification == "NORMAL":
            return "NORMAL_DAY"
        return "UNKNOWN"

    def get_ib_info(self) -> Dict:
        """Return full IB context."""
        return {
            "ib_high": self._ib_high,
            "ib_low": self._ib_low,
            "ib_range": self.get_ib_range(),
            "ib_complete": self._ib_complete,
            "classification": self.get_ib_classification(),
            "day_type_forecast": self.get_day_type_forecast(),
            "history_length": len(self._ib_range_history),
        }

    def _finalize_day(self) -> None:
        """Store IB range at end of day if not already stored."""
        if (
            self._ib_high is not None
            and self._ib_low is not None
            and not self._ib_complete
        ):
            # Day ended without IB completing (e.g. partial day)
            ib_range = self._ib_high - self._ib_low
            self._ib_range_history.append(ib_range)
