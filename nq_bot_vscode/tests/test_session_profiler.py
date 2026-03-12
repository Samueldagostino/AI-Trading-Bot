"""Tests for signals/session_profiler.py -- SessionProfiler."""

import pytest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from signals.session_profiler import SessionProfiler

ET = ZoneInfo("America/New_York")


def et_dt(hour, minute=0):
    """Create a timezone-aware datetime in ET."""
    return datetime(2026, 3, 4, hour, minute, tzinfo=ET)


def utc_from_et(hour, minute=0):
    """Create an ET time and convert to UTC (for testing timezone handling)."""
    return et_dt(hour, minute).astimezone(timezone.utc)


class TestGetSessionPhase:
    """Test phase detection for each time window."""

    def setup_method(self):
        self.profiler = SessionProfiler()

    def test_pre_market(self):
        assert self.profiler.get_session_phase(et_dt(4, 0)) == "PRE_MARKET"
        assert self.profiler.get_session_phase(et_dt(7, 30)) == "PRE_MARKET"
        assert self.profiler.get_session_phase(et_dt(9, 29)) == "PRE_MARKET"

    def test_opening_drive(self):
        assert self.profiler.get_session_phase(et_dt(9, 30)) == "OPENING_DRIVE"
        assert self.profiler.get_session_phase(et_dt(9, 45)) == "OPENING_DRIVE"
        assert self.profiler.get_session_phase(et_dt(9, 59)) == "OPENING_DRIVE"

    def test_ib_period(self):
        assert self.profiler.get_session_phase(et_dt(10, 0)) == "IB_PERIOD"
        assert self.profiler.get_session_phase(et_dt(10, 15)) == "IB_PERIOD"
        assert self.profiler.get_session_phase(et_dt(10, 29)) == "IB_PERIOD"

    def test_morning(self):
        assert self.profiler.get_session_phase(et_dt(10, 30)) == "MORNING"
        assert self.profiler.get_session_phase(et_dt(11, 0)) == "MORNING"
        assert self.profiler.get_session_phase(et_dt(11, 59)) == "MORNING"

    def test_lunch(self):
        assert self.profiler.get_session_phase(et_dt(12, 0)) == "LUNCH"
        assert self.profiler.get_session_phase(et_dt(12, 45)) == "LUNCH"
        assert self.profiler.get_session_phase(et_dt(13, 29)) == "LUNCH"

    def test_afternoon(self):
        assert self.profiler.get_session_phase(et_dt(13, 30)) == "AFTERNOON"
        assert self.profiler.get_session_phase(et_dt(14, 0)) == "AFTERNOON"
        assert self.profiler.get_session_phase(et_dt(14, 59)) == "AFTERNOON"

    def test_moc_window(self):
        assert self.profiler.get_session_phase(et_dt(15, 0)) == "MOC_WINDOW"
        assert self.profiler.get_session_phase(et_dt(15, 30)) == "MOC_WINDOW"
        assert self.profiler.get_session_phase(et_dt(15, 44)) == "MOC_WINDOW"

    def test_close(self):
        assert self.profiler.get_session_phase(et_dt(15, 45)) == "CLOSE"
        assert self.profiler.get_session_phase(et_dt(15, 55)) == "CLOSE"
        assert self.profiler.get_session_phase(et_dt(15, 59)) == "CLOSE"

    def test_post_market(self):
        assert self.profiler.get_session_phase(et_dt(16, 0)) == "POST_MARKET"
        assert self.profiler.get_session_phase(et_dt(17, 0)) == "POST_MARKET"
        assert self.profiler.get_session_phase(et_dt(17, 59)) == "POST_MARKET"

    def test_closed_outside_all_phases(self):
        # Before 4:00 ET
        assert self.profiler.get_session_phase(et_dt(2, 0)) == "CLOSED"
        # After 18:00 ET
        assert self.profiler.get_session_phase(et_dt(18, 0)) == "CLOSED"
        assert self.profiler.get_session_phase(et_dt(23, 0)) == "CLOSED"

    def test_utc_timestamps_converted(self):
        """Ensure UTC timestamps are properly converted to ET."""
        # 14:30 UTC = 9:30 ET (during EST, March)
        ts_utc = utc_from_et(9, 30)
        assert self.profiler.get_session_phase(ts_utc) == "OPENING_DRIVE"

    def test_boundary_exact_start(self):
        """Phase starts are inclusive."""
        assert self.profiler.get_session_phase(et_dt(9, 30)) == "OPENING_DRIVE"
        assert self.profiler.get_session_phase(et_dt(10, 0)) == "IB_PERIOD"
        assert self.profiler.get_session_phase(et_dt(12, 0)) == "LUNCH"

    def test_boundary_exact_end(self):
        """Phase ends are exclusive -- the next phase starts."""
        assert self.profiler.get_session_phase(et_dt(10, 0)) == "IB_PERIOD"
        assert self.profiler.get_session_phase(et_dt(10, 30)) == "MORNING"


class TestGetPhaseModifier:
    """Test phase modifiers return correct multipliers."""

    def setup_method(self):
        self.profiler = SessionProfiler()

    def test_opening_drive_modifiers(self):
        mods = self.profiler.get_phase_modifier("OPENING_DRIVE")
        assert mods["position_size_mult"] == 1.2
        assert mods["stop_width_mult"] == 1.1

    def test_lunch_modifiers(self):
        mods = self.profiler.get_phase_modifier("LUNCH")
        assert mods["position_size_mult"] == 0.7
        assert mods["stop_width_mult"] == 0.8

    def test_moc_window_modifiers(self):
        mods = self.profiler.get_phase_modifier("MOC_WINDOW")
        assert mods["position_size_mult"] == 1.15
        assert mods["stop_width_mult"] == 1.1

    def test_close_modifiers(self):
        mods = self.profiler.get_phase_modifier("CLOSE")
        assert mods["position_size_mult"] == 0.5
        assert mods["stop_width_mult"] == 0.7

    def test_pre_market_no_trading(self):
        mods = self.profiler.get_phase_modifier("PRE_MARKET")
        assert mods["position_size_mult"] == 0.0

    def test_post_market_no_trading(self):
        mods = self.profiler.get_phase_modifier("POST_MARKET")
        assert mods["position_size_mult"] == 0.0

    def test_unknown_phase_defaults_to_no_trade(self):
        mods = self.profiler.get_phase_modifier("UNKNOWN")
        assert mods["position_size_mult"] == 0.0

    def test_ib_period_neutral(self):
        mods = self.profiler.get_phase_modifier("IB_PERIOD")
        assert mods["position_size_mult"] == 1.0
        assert mods["stop_width_mult"] == 1.0

    def test_morning_modifiers(self):
        mods = self.profiler.get_phase_modifier("MORNING")
        assert mods["position_size_mult"] == 1.1
        assert mods["stop_width_mult"] == 1.0

    def test_afternoon_neutral(self):
        mods = self.profiler.get_phase_modifier("AFTERNOON")
        assert mods["position_size_mult"] == 1.0
        assert mods["stop_width_mult"] == 1.0


class TestIsRTH:
    """Test RTH detection."""

    def setup_method(self):
        self.profiler = SessionProfiler()

    def test_rth_open(self):
        assert self.profiler.is_rth(et_dt(9, 30)) is True

    def test_rth_midday(self):
        assert self.profiler.is_rth(et_dt(12, 0)) is True

    def test_before_rth(self):
        assert self.profiler.is_rth(et_dt(9, 29)) is False

    def test_at_close(self):
        assert self.profiler.is_rth(et_dt(16, 0)) is False

    def test_post_market(self):
        assert self.profiler.is_rth(et_dt(17, 0)) is False


class TestAllowsNewEntries:
    """Test entry permission logic."""

    def setup_method(self):
        self.profiler = SessionProfiler()

    def test_allows_during_rth_active_phases(self):
        assert self.profiler.allows_new_entries(et_dt(9, 30)) is True
        assert self.profiler.allows_new_entries(et_dt(11, 0)) is True
        assert self.profiler.allows_new_entries(et_dt(15, 0)) is True

    def test_blocks_pre_market(self):
        assert self.profiler.allows_new_entries(et_dt(8, 0)) is False

    def test_blocks_post_market(self):
        assert self.profiler.allows_new_entries(et_dt(17, 0)) is False

    def test_allows_during_lunch_reduced(self):
        """Lunch allows entries but at reduced size."""
        assert self.profiler.allows_new_entries(et_dt(12, 30)) is True
        mods = self.profiler.get_phase_modifier("LUNCH")
        assert mods["position_size_mult"] < 1.0


class TestGetSessionInfo:
    """Test full session info response."""

    def setup_method(self):
        self.profiler = SessionProfiler()

    def test_session_info_structure(self):
        info = self.profiler.get_session_info(et_dt(10, 0))
        assert "phase" in info
        assert "position_size_mult" in info
        assert "stop_width_mult" in info
        assert "is_rth" in info
        assert "allows_new_entries" in info

    def test_session_info_values(self):
        info = self.profiler.get_session_info(et_dt(12, 30))
        assert info["phase"] == "LUNCH"
        assert info["position_size_mult"] == 0.7
        assert info["is_rth"] is True
        assert info["allows_new_entries"] is True
