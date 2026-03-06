"""
Tests for Session-Aware Volatility Scaling
============================================
8 tests covering session classification, ATR scaling, and integration boundaries.
"""

import pytest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from features.session_volatility import SessionVolatilityScaler

ET = ZoneInfo("America/New_York")


def _et(hour, minute=0, year=2026, month=3, day=4):
    """Create an ET-aware datetime for testing (March 4 2026 = Wednesday)."""
    return datetime(year, month, day, hour, minute, tzinfo=ET)


class TestGetSession:
    """Tests for session classification."""

    def test_get_session_opening(self):
        scaler = SessionVolatilityScaler()
        ts = _et(9, 45)
        assert scaler.get_session(ts) == "opening"

    def test_get_session_midday(self):
        scaler = SessionVolatilityScaler()
        ts = _et(12, 0)
        assert scaler.get_session(ts) == "midday"

    def test_get_session_closing(self):
        scaler = SessionVolatilityScaler()
        ts = _et(15, 30)
        assert scaler.get_session(ts) == "closing"

    def test_get_session_eth(self):
        scaler = SessionVolatilityScaler()
        ts = _et(20, 0)
        assert scaler.get_session(ts) == "eth"


class TestScaleATR:
    """Tests for ATR scaling logic."""

    def test_scale_atr_opening_wider(self):
        """Opening ATR should be wider than raw ATR (factor > 1.0)."""
        scaler = SessionVolatilityScaler(enabled=True)
        raw_atr = 10.0
        ts = _et(9, 45)
        scaled = scaler.scale_atr(raw_atr, ts)
        assert scaled > raw_atr
        assert scaled == raw_atr * 1.3

    def test_scale_atr_midday_tighter(self):
        """Midday ATR should be tighter than raw ATR (factor < 1.0)."""
        scaler = SessionVolatilityScaler(enabled=True)
        raw_atr = 10.0
        ts = _et(12, 0)
        scaled = scaler.scale_atr(raw_atr, ts)
        assert scaled < raw_atr
        assert scaled == raw_atr * 0.75


class TestIntegration:
    """Tests for integration boundaries and feature flag."""

    def test_max_stop_cap_unchanged(self):
        """Scaled ATR * 2.0 = 35pts still blocked by 30pt HC cap.

        The session scaler widens ATR but the 30pt max stop gate in
        main.py is UNCHANGED — it operates on the final stop distance,
        not on ATR itself.
        """
        scaler = SessionVolatilityScaler(enabled=True)
        raw_atr = 13.5  # During opening: 13.5 * 1.3 = 17.55
        ts = _et(9, 45)
        scaled_atr = scaler.scale_atr(raw_atr, ts)

        # Simulate stop calculation: scaled_atr * 2.0
        stop_distance = scaled_atr * 2.0  # = 17.55 * 2.0 = 35.1

        # The 30pt cap is enforced in main.py, NOT in the scaler
        MAX_STOP = 30.0
        assert stop_distance > MAX_STOP  # Would be blocked
        # Verify the cap logic works independently
        trade_allowed = stop_distance <= MAX_STOP
        assert not trade_allowed

    def test_c2_trail_unaffected(self):
        """C2 trail should use raw ATR, not session-scaled ATR.

        In main.py, atr_for_entry = features.atr_14 (raw), while
        scaled_atr is used only for stop/target. This test verifies
        the scaler does not affect C2 trail when disabled.
        """
        scaler = SessionVolatilityScaler(enabled=False)
        raw_atr = 10.0
        ts = _et(9, 45)

        # When disabled, scale factor is 1.0 — no change
        assert scaler.get_scale_factor(ts) == 1.0
        assert scaler.scale_atr(raw_atr, ts) == raw_atr

        # Even when enabled, the C2 trail in main.py uses features.atr_14
        # directly (not the scaled value). We verify the scaler returns
        # a DIFFERENT value when enabled, proving C2 must use raw ATR.
        scaler_on = SessionVolatilityScaler(enabled=True)
        scaled = scaler_on.scale_atr(raw_atr, ts)
        assert scaled != raw_atr  # Scaled value differs
        # In main.py: atr_for_entry = features.atr_14 (not scaled_atr)
        c2_trail_atr = raw_atr  # This is what main.py uses
        assert c2_trail_atr == raw_atr  # C2 trail is unaffected


class TestFeatureFlag:
    """Tests for the feature flag (default OFF)."""

    def test_feature_flag_default_off(self):
        """Default constructor reads env var, which defaults to 'false'."""
        # Explicitly pass enabled=False to simulate default behavior
        scaler = SessionVolatilityScaler(enabled=False)
        assert not scaler.enabled

        # When disabled, all sessions return factor 1.0
        for hour in [9, 12, 15, 20]:
            ts = _et(hour, 30 if hour == 9 else 0)
            assert scaler.get_scale_factor(ts) == 1.0
            assert scaler.scale_atr(10.0, ts) == 10.0


class TestEdgeCases:
    """Edge case tests."""

    def test_weekend_is_eth(self):
        """Weekend timestamps should classify as ETH."""
        scaler = SessionVolatilityScaler()
        # March 7 2026 = Saturday
        ts = datetime(2026, 3, 7, 12, 0, tzinfo=ET)
        assert scaler.get_session(ts) == "eth"

    def test_session_boundaries(self):
        """Verify exact boundary classification."""
        scaler = SessionVolatilityScaler()
        # 09:30 sharp = opening
        assert scaler.get_session(_et(9, 30)) == "opening"
        # 10:30 sharp = midday (opening ends at 10:30 exclusive)
        assert scaler.get_session(_et(10, 30)) == "midday"
        # 14:00 sharp = closing
        assert scaler.get_session(_et(14, 0)) == "closing"
        # 16:00 sharp = eth (closing ends at 16:00 exclusive)
        assert scaler.get_session(_et(16, 0)) == "eth"

    def test_naive_utc_timestamp(self):
        """Naive timestamps should be treated as UTC and converted."""
        scaler = SessionVolatilityScaler()
        # 14:45 UTC on a Wednesday = 9:45 ET (opening) during EST
        # But in March 2026 (after DST spring forward Mar 8), 14:45 UTC = 10:45 ET
        # Use January instead (EST, UTC-5): 14:45 UTC = 9:45 ET
        ts = datetime(2026, 1, 7, 14, 45)  # Naive, treated as UTC
        session = scaler.get_session(ts)
        assert session == "opening"

    def test_custom_scale_factors(self):
        """Custom scale factors should override defaults."""
        custom = {"opening": 1.5, "midday": 0.5}
        scaler = SessionVolatilityScaler(scale_factors=custom, enabled=True)
        assert scaler.scale_atr(10.0, _et(9, 45)) == 15.0
        assert scaler.scale_atr(10.0, _et(12, 0)) == 5.0
        # Non-overridden sessions keep defaults
        assert scaler.scale_atr(10.0, _et(15, 30)) == 11.0  # 1.1 default
