"""Tests for signals/initial_balance.py — InitialBalanceTracker."""

import pytest
from datetime import datetime, timezone
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from signals.initial_balance import InitialBalanceTracker

ET = ZoneInfo("America/New_York")


@dataclass
class MockBar:
    """Minimal bar for testing."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 100


def make_bar_et(hour, minute, o, h, l, c, date_day=4):
    """Create a bar with an ET timestamp."""
    ts = datetime(2026, 3, date_day, hour, minute, tzinfo=ET)
    return MockBar(timestamp=ts, open=o, high=h, low=l, close=c)


# ================================================================
# IB ACCUMULATION TESTS
# ================================================================
class TestIBAccumulation:
    """Test that IB high/low are correctly tracked during IB window."""

    def test_first_bar_sets_ib(self):
        tracker = InitialBalanceTracker()
        bar = make_bar_et(9, 30, 18000, 18020, 17990, 18010)
        tracker.update(bar)
        assert tracker.get_ib_high() == 18020
        assert tracker.get_ib_low() == 17990

    def test_multiple_bars_expand_ib(self):
        tracker = InitialBalanceTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18020, 17990, 18010))
        tracker.update(make_bar_et(9, 35, 18010, 18050, 17985, 18040))
        assert tracker.get_ib_high() == 18050
        assert tracker.get_ib_low() == 17985

    def test_ib_range_calculation(self):
        tracker = InitialBalanceTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18050, 17980, 18010))
        assert tracker.get_ib_range() == 18050 - 17980

    def test_bars_outside_ib_window_ignored(self):
        tracker = InitialBalanceTracker()
        # Before RTH
        tracker.update(make_bar_et(9, 25, 18000, 18100, 17900, 18050))
        assert tracker.get_ib_high() is None
        assert tracker.get_ib_low() is None

    def test_bars_after_ib_window_ignored(self):
        tracker = InitialBalanceTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18020, 17990, 18010))
        # After IB window — trigger completion first
        tracker.update(make_bar_et(10, 35, 18010, 18100, 17900, 18050))
        # IB should not be updated by this bar
        assert tracker.get_ib_high() == 18020
        assert tracker.get_ib_low() == 17990


# ================================================================
# IB COMPLETION TESTS
# ================================================================
class TestIBCompletion:
    """Test IB completion detection."""

    def test_not_complete_during_ib(self):
        tracker = InitialBalanceTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18020, 17990, 18010))
        assert tracker.is_ib_complete() is False

    def test_complete_after_ib_window(self):
        tracker = InitialBalanceTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18020, 17990, 18010))
        tracker.update(make_bar_et(10, 30, 18010, 18015, 18005, 18010))
        assert tracker.is_ib_complete() is True

    def test_no_data_no_completion(self):
        tracker = InitialBalanceTracker()
        assert tracker.is_ib_complete() is False

    def test_ib_range_zero_without_data(self):
        tracker = InitialBalanceTracker()
        assert tracker.get_ib_range() == 0.0


# ================================================================
# IB CLASSIFICATION TESTS
# ================================================================
class TestIBClassification:
    """Test IB range classification against rolling history."""

    def _build_tracker_with_history(self, history_ranges, current_ib_range):
        """Helper to build a tracker with predefined IB history."""
        tracker = InitialBalanceTracker(history_length=20)

        # Simulate N days of history
        for day_idx, ib_range in enumerate(history_ranges):
            midpoint = 18000
            ib_high = midpoint + ib_range / 2
            ib_low = midpoint - ib_range / 2
            day = day_idx + 1
            if day > 28:
                day = 28  # cap at month end
            tracker.update(make_bar_et(9, 30, midpoint, ib_high, ib_low, midpoint, date_day=day))
            tracker.update(make_bar_et(10, 35, midpoint, midpoint + 5, midpoint - 5, midpoint, date_day=day))

        # Current day with target IB range
        midpoint = 18000
        cur_high = midpoint + current_ib_range / 2
        cur_low = midpoint - current_ib_range / 2
        # Use a different month to avoid date collision
        cur_ts = datetime(2026, 4, 1, 9, 30, tzinfo=ET)
        tracker.update(MockBar(timestamp=cur_ts, open=midpoint, high=cur_high, low=cur_low, close=midpoint))
        complete_ts = datetime(2026, 4, 1, 10, 35, tzinfo=ET)
        tracker.update(MockBar(timestamp=complete_ts, open=midpoint, high=midpoint + 1, low=midpoint - 1, close=midpoint))

        return tracker

    def test_narrow_ib(self):
        # 10 days of ranges 40-80, current = 10 (very narrow)
        history = [40, 50, 55, 60, 65, 70, 50, 55, 60, 80]
        tracker = self._build_tracker_with_history(history, 10)
        assert tracker.get_ib_classification() == "NARROW"

    def test_wide_ib(self):
        # 10 days of ranges 20-60, current = 100 (very wide)
        history = [20, 25, 30, 35, 40, 45, 30, 35, 40, 60]
        tracker = self._build_tracker_with_history(history, 100)
        assert tracker.get_ib_classification() == "WIDE"

    def test_normal_ib(self):
        # 10 days of ranges 20-80, current = 50 (middle)
        history = [20, 30, 40, 50, 60, 70, 80, 35, 45, 55]
        tracker = self._build_tracker_with_history(history, 50)
        assert tracker.get_ib_classification() == "NORMAL"

    def test_insufficient_history(self):
        tracker = InitialBalanceTracker()
        # Only 1 day
        tracker.update(make_bar_et(9, 30, 18000, 18020, 17990, 18010))
        tracker.update(make_bar_et(10, 35, 18010, 18015, 18005, 18010))
        assert tracker.get_ib_classification() == "UNKNOWN"

    def test_not_complete_returns_unknown(self):
        tracker = InitialBalanceTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18020, 17990, 18010))
        assert tracker.get_ib_classification() == "UNKNOWN"


# ================================================================
# IB BREAK DIRECTION TESTS
# ================================================================
class TestIBBreakDirection:
    """Test IB break detection."""

    def _make_complete_tracker(self):
        tracker = InitialBalanceTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18050, 17950, 18020))
        tracker.update(make_bar_et(10, 35, 18020, 18025, 18015, 18020))
        return tracker  # IB: 17950-18050

    def test_break_long(self):
        tracker = self._make_complete_tracker()
        assert tracker.get_ib_break_direction(18060) == "LONG"

    def test_break_short(self):
        tracker = self._make_complete_tracker()
        assert tracker.get_ib_break_direction(17940) == "SHORT"

    def test_inside_ib(self):
        tracker = self._make_complete_tracker()
        assert tracker.get_ib_break_direction(18000) is None

    def test_at_ib_high(self):
        tracker = self._make_complete_tracker()
        assert tracker.get_ib_break_direction(18050) is None

    def test_at_ib_low(self):
        tracker = self._make_complete_tracker()
        assert tracker.get_ib_break_direction(17950) is None

    def test_ib_not_complete(self):
        tracker = InitialBalanceTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18050, 17950, 18020))
        assert tracker.get_ib_break_direction(18100) is None


# ================================================================
# DAY TYPE FORECAST TESTS
# ================================================================
class TestDayTypeForecast:
    """Test day-type forecasting based on IB classification."""

    def test_narrow_means_trend_day(self):
        tracker = InitialBalanceTracker()
        # Mock classification by directly checking mapping
        # We need enough history for classification to work
        # Use the helper from classification tests
        history = [40, 50, 55, 60, 65, 70, 50, 55, 60, 80]
        midpoint = 18000
        for day_idx, ib_range in enumerate(history):
            ib_high = midpoint + ib_range / 2
            ib_low = midpoint - ib_range / 2
            day = day_idx + 1
            tracker.update(make_bar_et(9, 30, midpoint, ib_high, ib_low, midpoint, date_day=day))
            tracker.update(make_bar_et(10, 35, midpoint, midpoint + 1, midpoint - 1, midpoint, date_day=day))

        # Narrow IB day
        cur_ts = datetime(2026, 4, 1, 9, 30, tzinfo=ET)
        tracker.update(MockBar(timestamp=cur_ts, open=midpoint, high=midpoint + 5, low=midpoint - 5, close=midpoint))
        complete_ts = datetime(2026, 4, 1, 10, 35, tzinfo=ET)
        tracker.update(MockBar(timestamp=complete_ts, open=midpoint, high=midpoint + 1, low=midpoint - 1, close=midpoint))
        assert tracker.get_day_type_forecast() == "TREND_DAY"

    def test_unknown_without_data(self):
        tracker = InitialBalanceTracker()
        assert tracker.get_day_type_forecast() == "UNKNOWN"


# ================================================================
# DAY ROLLOVER TESTS
# ================================================================
class TestDayRollover:
    """Test that tracker resets on new trading days."""

    def test_new_day_resets_ib(self):
        tracker = InitialBalanceTracker()
        # Day 1
        tracker.update(make_bar_et(9, 30, 18000, 18050, 17950, 18020, date_day=4))
        tracker.update(make_bar_et(10, 35, 18020, 18025, 18015, 18020, date_day=4))
        assert tracker.is_ib_complete() is True
        assert tracker.get_ib_high() == 18050

        # Day 2
        tracker.update(make_bar_et(9, 30, 19000, 19030, 18980, 19010, date_day=5))
        assert tracker.is_ib_complete() is False
        assert tracker.get_ib_high() == 19030

    def test_history_accumulates(self):
        tracker = InitialBalanceTracker()
        # Day 1
        tracker.update(make_bar_et(9, 30, 18000, 18050, 17950, 18020, date_day=4))
        tracker.update(make_bar_et(10, 35, 18020, 18025, 18015, 18020, date_day=4))
        # Day 2
        tracker.update(make_bar_et(9, 30, 19000, 19030, 18980, 19010, date_day=5))
        tracker.update(make_bar_et(10, 35, 19010, 19015, 19005, 19010, date_day=5))
        info = tracker.get_ib_info()
        assert info["history_length"] == 2


# ================================================================
# IB INFO TESTS
# ================================================================
class TestIBInfo:
    """Test get_ib_info() returns correct structure."""

    def test_info_structure(self):
        tracker = InitialBalanceTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18050, 17950, 18020))
        tracker.update(make_bar_et(10, 35, 18020, 18025, 18015, 18020))
        info = tracker.get_ib_info()
        assert "ib_high" in info
        assert "ib_low" in info
        assert "ib_range" in info
        assert "ib_complete" in info
        assert "classification" in info
        assert "day_type_forecast" in info
        assert "history_length" in info
