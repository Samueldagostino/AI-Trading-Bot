"""Tests for signals/overnight_levels.py — OvernightLevelTracker."""

import pytest
from datetime import datetime, timezone
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from signals.overnight_levels import OvernightLevelTracker

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
# LEVEL TRACKING TESTS
# ================================================================
class TestLevelTracking:
    """Test that reference levels are correctly tracked."""

    def test_initial_levels_none(self):
        tracker = OvernightLevelTracker()
        levels = tracker.get_levels()
        assert levels["prev_close"] is None
        assert levels["prev_high"] is None
        assert levels["prev_low"] is None
        assert levels["overnight_high"] is None
        assert levels["overnight_low"] is None

    def test_rth_levels_tracked(self):
        tracker = OvernightLevelTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18020, 17990, 18010))
        tracker.update(make_bar_et(10, 0, 18010, 18050, 17980, 18040))
        tracker.update(make_bar_et(15, 55, 18040, 18045, 18030, 18035))
        levels = tracker.get_levels()
        assert levels["rth_open"] == 18000

    def test_overnight_levels_tracked(self):
        tracker = OvernightLevelTracker()
        # Pre-market bars
        tracker.update(make_bar_et(5, 0, 17950, 17980, 17930, 17970))
        tracker.update(make_bar_et(6, 0, 17970, 18000, 17960, 17990))
        levels = tracker.get_levels()
        assert levels["overnight_high"] == 18000
        assert levels["overnight_low"] == 17930


# ================================================================
# DAY ROLLOVER TESTS
# ================================================================
class TestDayRollover:
    """Test that previous-day levels are set on day rollover."""

    def test_prev_day_levels_after_rollover(self):
        tracker = OvernightLevelTracker()
        # Day 1 RTH
        tracker.update(make_bar_et(9, 30, 18000, 18050, 17960, 18010, date_day=4))
        tracker.update(make_bar_et(15, 55, 18010, 18020, 18000, 18015, date_day=4))

        # Day 2 starts — triggers rollover
        tracker.update(make_bar_et(9, 30, 18020, 18030, 18010, 18025, date_day=5))

        levels = tracker.get_levels()
        assert levels["prev_close"] == 18015
        assert levels["prev_high"] == 18050
        assert levels["prev_low"] == 17960

    def test_overnight_resets_on_new_day(self):
        tracker = OvernightLevelTracker()
        # Day 1 — some overnight data
        tracker.update(make_bar_et(5, 0, 17950, 17980, 17930, 17970, date_day=4))
        # Day 1 RTH
        tracker.update(make_bar_et(9, 30, 18000, 18050, 17960, 18010, date_day=4))

        # Day 2 — new overnight data
        tracker.update(make_bar_et(5, 0, 18010, 18030, 18000, 18020, date_day=5))

        levels = tracker.get_levels()
        # Overnight should reflect day 2 only
        assert levels["overnight_high"] == 18030
        assert levels["overnight_low"] == 18000


# ================================================================
# GAP ANALYSIS TESTS
# ================================================================
class TestGapAnalysis:
    """Test gap detection and fill tracking."""

    def _setup_with_prev_close(self, prev_close):
        """Create tracker with a known previous close."""
        tracker = OvernightLevelTracker()
        # Day 1 — establish prev close
        tracker.update(make_bar_et(9, 30, 18000, 18050, 17960, prev_close, date_day=3))
        # Day 2 — trigger rollover
        return tracker

    def test_gap_up(self):
        tracker = self._setup_with_prev_close(18000)
        # Day 2 opens higher
        tracker.update(make_bar_et(9, 30, 18020, 18025, 18015, 18022, date_day=4))
        gap = tracker.get_gap_info(18022)
        assert gap["gap_direction"] == "UP"
        assert gap["gap_size_pts"] == 20.0

    def test_gap_down(self):
        tracker = self._setup_with_prev_close(18000)
        # Day 2 opens lower
        tracker.update(make_bar_et(9, 30, 17970, 17975, 17960, 17972, date_day=4))
        gap = tracker.get_gap_info(17972)
        assert gap["gap_direction"] == "DOWN"
        assert gap["gap_size_pts"] == 30.0

    def test_no_gap(self):
        tracker = self._setup_with_prev_close(18000)
        # Day 2 opens at same price
        tracker.update(make_bar_et(9, 30, 18000, 18005, 17995, 18002, date_day=4))
        gap = tracker.get_gap_info(18002)
        assert gap["gap_direction"] == "NONE"
        assert gap["gap_size_pts"] == 0.0

    def test_no_prev_close(self):
        tracker = OvernightLevelTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18010, 17990, 18005, date_day=4))
        gap = tracker.get_gap_info(18005)
        assert gap["gap_direction"] == "NONE"


# ================================================================
# GAP FILL TESTS
# ================================================================
class TestGapFill:
    """Test gap fill tracking."""

    def test_gap_up_fully_filled(self):
        tracker = OvernightLevelTracker()
        # Day 1
        tracker.update(make_bar_et(9, 30, 18000, 18050, 17960, 18000, date_day=3))
        # Day 2 — gap up
        tracker.update(make_bar_et(9, 30, 18020, 18025, 18015, 18022, date_day=4))
        # Price retraces to fill gap
        tracker.update(make_bar_et(9, 35, 18022, 18022, 17995, 17998, date_day=4))

        gap = tracker.get_gap_info(17998)
        assert gap["gap_filled"] is True
        assert gap["gap_fill_pct"] == 100.0

    def test_gap_up_partially_filled(self):
        tracker = OvernightLevelTracker()
        # Day 1
        tracker.update(make_bar_et(9, 30, 18000, 18050, 17960, 18000, date_day=3))
        # Day 2 — gap up 20 points
        tracker.update(make_bar_et(9, 30, 18020, 18025, 18015, 18022, date_day=4))
        # Price retraces 10 points (50%)
        tracker.update(make_bar_et(9, 35, 18022, 18022, 18010, 18012, date_day=4))

        gap = tracker.get_gap_info(18012)
        assert gap["gap_filled"] is False
        assert gap["gap_fill_pct"] == 50.0

    def test_gap_down_fully_filled(self):
        tracker = OvernightLevelTracker()
        # Day 1
        tracker.update(make_bar_et(9, 30, 18000, 18050, 17960, 18000, date_day=3))
        # Day 2 — gap down
        tracker.update(make_bar_et(9, 30, 17970, 17975, 17960, 17972, date_day=4))
        # Price rallies to fill gap
        tracker.update(make_bar_et(9, 35, 17972, 18005, 17970, 18002, date_day=4))

        gap = tracker.get_gap_info(18002)
        assert gap["gap_filled"] is True

    def test_gap_unfilled_at_30min(self):
        tracker = OvernightLevelTracker()
        # Day 1
        tracker.update(make_bar_et(9, 30, 18000, 18050, 17960, 18000, date_day=3))
        # Day 2 — gap up 50 points
        tracker.update(make_bar_et(9, 30, 18050, 18055, 18045, 18052, date_day=4))
        # 6 more bars (simulating 30 min on 5-min bars) — gap not filled
        minutes = [35, 40, 45, 50, 55, 59]
        for m in minutes:
            tracker.update(make_bar_et(9, m, 18050, 18060, 18048, 18055, date_day=4))

        gap = tracker.get_gap_info(18055)
        assert gap["gap_unfilled_at_30min"] is True


# ================================================================
# GET_LEVELS STRUCTURE TEST
# ================================================================
class TestGetLevelsStructure:
    """Test get_levels() returns correct keys."""

    def test_keys_present(self):
        tracker = OvernightLevelTracker()
        levels = tracker.get_levels()
        expected_keys = {"prev_close", "prev_high", "prev_low",
                        "overnight_high", "overnight_low", "rth_open"}
        assert set(levels.keys()) == expected_keys


# ================================================================
# GET_GAP_INFO STRUCTURE TEST
# ================================================================
class TestGetGapInfoStructure:
    """Test get_gap_info() returns correct keys."""

    def test_keys_present(self):
        tracker = OvernightLevelTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18010, 17990, 18005))
        gap = tracker.get_gap_info(18005)
        expected_keys = {"gap_direction", "gap_size_pts", "gap_filled",
                        "gap_fill_pct", "gap_unfilled_at_30min"}
        assert set(gap.keys()) == expected_keys


# ================================================================
# EDGE CASES
# ================================================================
class TestEdgeCases:
    """Test edge cases."""

    def test_tiny_gap_treated_as_none(self):
        tracker = OvernightLevelTracker()
        # Day 1
        tracker.update(make_bar_et(9, 30, 18000, 18050, 17960, 18000, date_day=3))
        # Day 2 — opens 0.10 above (< 1 tick)
        tracker.update(make_bar_et(9, 30, 18000.10, 18005, 17995, 18002, date_day=4))
        gap = tracker.get_gap_info(18002)
        assert gap["gap_direction"] == "NONE"

    def test_multiple_days_accumulate(self):
        tracker = OvernightLevelTracker()
        # Day 1 — RTH open and close bars
        tracker.update(make_bar_et(9, 30, 18000, 18050, 17960, 18010, date_day=3))
        tracker.update(make_bar_et(15, 55, 18010, 18020, 18000, 18010, date_day=3))
        # Day 2
        tracker.update(make_bar_et(9, 30, 18010, 18070, 17950, 18060, date_day=4))
        tracker.update(make_bar_et(15, 55, 18060, 18065, 18050, 18060, date_day=4))
        # Day 3
        tracker.update(make_bar_et(9, 30, 18060, 18080, 18050, 18070, date_day=5))

        levels = tracker.get_levels()
        assert levels["prev_close"] == 18060
        assert levels["prev_high"] == 18070
        assert levels["prev_low"] == 17950
