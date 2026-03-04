"""Tests for signals/vwap_tracker.py — VWAPTracker."""

import pytest
from datetime import datetime, timezone
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from signals.vwap_tracker import VWAPTracker

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


def make_bar_et(hour, minute, o, h, l, c, vol=100, date_day=4):
    """Create a bar with an ET timestamp."""
    ts = datetime(2026, 3, date_day, hour, minute, tzinfo=ET)
    return MockBar(timestamp=ts, open=o, high=h, low=l, close=c, volume=vol)


# ================================================================
# VWAP CALCULATION TESTS
# ================================================================
class TestVWAPCalculation:
    """Test basic VWAP computation."""

    def test_single_bar_vwap(self):
        tracker = VWAPTracker()
        bar = make_bar_et(9, 30, 18000, 18010, 17990, 18005, vol=1000)
        tracker.update(bar)
        # typical_price = (18010 + 17990 + 18005) / 3 = 18001.666...
        expected = round((18010 + 17990 + 18005) / 3, 2)
        assert tracker.get_vwap() == expected

    def test_two_bar_vwap(self):
        tracker = VWAPTracker()
        bar1 = make_bar_et(9, 30, 18000, 18010, 17990, 18005, vol=1000)
        bar2 = make_bar_et(9, 35, 18005, 18020, 18000, 18015, vol=2000)
        tracker.update(bar1)
        tracker.update(bar2)

        tp1 = (18010 + 17990 + 18005) / 3
        tp2 = (18020 + 18000 + 18015) / 3
        expected_vwap = (tp1 * 1000 + tp2 * 2000) / 3000
        assert tracker.get_vwap() == round(expected_vwap, 2)

    def test_vwap_zero_without_data(self):
        tracker = VWAPTracker()
        assert tracker.get_vwap() == 0.0

    def test_zero_volume_treated_as_one(self):
        tracker = VWAPTracker()
        bar = make_bar_et(9, 30, 18000, 18010, 17990, 18005, vol=0)
        tracker.update(bar)
        assert tracker.get_vwap() > 0


# ================================================================
# SESSION RESET TESTS
# ================================================================
class TestVWAPReset:
    """Test VWAP resets at new session."""

    def test_resets_on_new_day(self):
        tracker = VWAPTracker()
        # Day 1
        bar1 = make_bar_et(9, 30, 18000, 18010, 17990, 18005, vol=1000, date_day=4)
        tracker.update(bar1)
        vwap_day1 = tracker.get_vwap()
        assert vwap_day1 > 0

        # Day 2
        bar2 = make_bar_et(9, 30, 19000, 19010, 18990, 19005, vol=500, date_day=5)
        tracker.update(bar2)
        vwap_day2 = tracker.get_vwap()

        # VWAP should reflect day 2 data only
        expected = round((19010 + 18990 + 19005) / 3, 2)
        assert vwap_day2 == expected

    def test_pre_market_bars_not_accumulated(self):
        tracker = VWAPTracker()
        bar = make_bar_et(8, 0, 18000, 18010, 17990, 18005, vol=1000)
        tracker.update(bar)
        assert tracker.get_vwap() == 0.0

    def test_post_market_bars_not_accumulated(self):
        tracker = VWAPTracker()
        # First add an RTH bar
        tracker.update(make_bar_et(9, 30, 18000, 18010, 17990, 18005, vol=1000))
        vwap_before = tracker.get_vwap()
        # Post-market bar should not change VWAP
        tracker.update(make_bar_et(16, 30, 18050, 18060, 18040, 18055, vol=500))
        assert tracker.get_vwap() == vwap_before


# ================================================================
# DISTANCE TESTS
# ================================================================
class TestDistanceFromVWAP:
    """Test distance calculation."""

    def test_above_vwap(self):
        tracker = VWAPTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18010, 17990, 18005, vol=1000))
        vwap = tracker.get_vwap()
        dist = tracker.get_distance_from_vwap(vwap + 10)
        assert dist == 10.0

    def test_below_vwap(self):
        tracker = VWAPTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18010, 17990, 18005, vol=1000))
        vwap = tracker.get_vwap()
        dist = tracker.get_distance_from_vwap(vwap - 15)
        assert dist == -15.0

    def test_at_vwap(self):
        tracker = VWAPTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18010, 17990, 18005, vol=1000))
        vwap = tracker.get_vwap()
        dist = tracker.get_distance_from_vwap(vwap)
        assert dist == 0.0

    def test_distance_zero_without_vwap(self):
        tracker = VWAPTracker()
        assert tracker.get_distance_from_vwap(18000) == 0.0


# ================================================================
# VWAP SIGNAL TESTS
# ================================================================
class TestVWAPSignal:
    """Test get_vwap_signal() output."""

    def test_signal_structure(self):
        tracker = VWAPTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18010, 17990, 18005, vol=1000))
        signal = tracker.get_vwap_signal(18010)
        assert "vwap" in signal
        assert "distance_pts" in signal
        assert "distance_pct" in signal
        assert "position" in signal
        assert "crossed_recently" in signal
        assert "extended" in signal

    def test_position_above(self):
        tracker = VWAPTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18010, 17990, 18005, vol=1000))
        vwap = tracker.get_vwap()
        signal = tracker.get_vwap_signal(vwap + 20)
        assert signal["position"] == "ABOVE"

    def test_position_below(self):
        tracker = VWAPTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18010, 17990, 18005, vol=1000))
        vwap = tracker.get_vwap()
        signal = tracker.get_vwap_signal(vwap - 20)
        assert signal["position"] == "BELOW"

    def test_not_extended_without_atr(self):
        tracker = VWAPTracker(atr=0.0)
        tracker.update(make_bar_et(9, 30, 18000, 18010, 17990, 18005, vol=1000))
        signal = tracker.get_vwap_signal(19000)
        assert signal["extended"] is False

    def test_extended_with_atr(self):
        tracker = VWAPTracker(atr=20.0)
        tracker.update(make_bar_et(9, 30, 18000, 18010, 17990, 18005, vol=1000))
        vwap = tracker.get_vwap()
        signal = tracker.get_vwap_signal(vwap + 25)
        assert signal["extended"] is True

    def test_not_extended_within_atr(self):
        tracker = VWAPTracker(atr=20.0)
        tracker.update(make_bar_et(9, 30, 18000, 18010, 17990, 18005, vol=1000))
        vwap = tracker.get_vwap()
        signal = tracker.get_vwap_signal(vwap + 10)
        assert signal["extended"] is False


# ================================================================
# CROSSOVER TESTS
# ================================================================
class TestVWAPCrossover:
    """Test VWAP crossover detection."""

    def test_no_cross_single_bar(self):
        tracker = VWAPTracker()
        tracker.update(make_bar_et(9, 30, 18000, 18010, 17990, 18005, vol=1000))
        signal = tracker.get_vwap_signal(18010)
        assert signal["crossed_recently"] is False

    def test_cross_detected(self):
        tracker = VWAPTracker()
        # Bars that start above VWAP then go below
        tracker.update(make_bar_et(9, 30, 18000, 18020, 17990, 18015, vol=1000))
        tracker.update(make_bar_et(9, 35, 18015, 18025, 18010, 18020, vol=1000))
        tracker.update(make_bar_et(9, 40, 18020, 18022, 17980, 17985, vol=1000))
        signal = tracker.get_vwap_signal(17985)
        # Close went from above VWAP to below — cross detected
        assert signal["crossed_recently"] is True

    def test_consistent_side_no_cross(self):
        tracker = VWAPTracker()
        # All bars close well above VWAP
        tracker.update(make_bar_et(9, 30, 18000, 18050, 18000, 18040, vol=100))
        tracker.update(make_bar_et(9, 35, 18040, 18060, 18035, 18055, vol=100))
        tracker.update(make_bar_et(9, 40, 18055, 18070, 18050, 18065, vol=100))
        signal = tracker.get_vwap_signal(18070)
        assert signal["crossed_recently"] is False


# ================================================================
# SET ATR TESTS
# ================================================================
class TestSetATR:
    """Test dynamic ATR updates."""

    def test_set_atr(self):
        tracker = VWAPTracker()
        tracker.set_atr(25.0)
        tracker.update(make_bar_et(9, 30, 18000, 18010, 17990, 18005, vol=1000))
        vwap = tracker.get_vwap()
        signal = tracker.get_vwap_signal(vwap + 30)
        assert signal["extended"] is True
