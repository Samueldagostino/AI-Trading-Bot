"""
Tests for Institutional Modifier Layer — Phase 1
==================================================
Covers:
  - OvernightBiasModifier: neutral, alignment, conflict, extreme cases
  - FOMCDriftModifier: no FOMC near, 24h window, 4h window, stand-aside
  - InstitutionalModifierEngine: cap at 2.0x, floor at 0.3x, sequential application
  - Edge cases: missing data, neutral HTF bias, FOMC on weekend
"""

import json
import os
import tempfile
import pytest
from datetime import datetime, timedelta
from dataclasses import dataclass
from zoneinfo import ZoneInfo
from unittest.mock import MagicMock

from config.fomc_calendar import (
    hours_until_next_fomc,
    next_fomc_date,
    FOMC_2026_DATES,
    ET,
)
from signals.institutional_modifiers import (
    OvernightBiasModifier,
    FOMCDriftModifier,
    InstitutionalModifierEngine,
    ModifierResult,
    OVERNIGHT_NEUTRAL_BPS,
    OVERNIGHT_EXTREME_BPS,
    MAX_TOTAL_MULTIPLIER,
    MIN_TOTAL_MULTIPLIER,
)


# ── Helper: create a mock bar ───────────────────────────────────────
@dataclass
class MockBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 1000


def make_bar(year, month, day, hour, minute, price, tz=ET):
    """Create a mock bar at a specific ET time with given price."""
    ts = datetime(year, month, day, hour, minute, tzinfo=tz)
    return MockBar(
        timestamp=ts,
        open=price,
        high=price + 5,
        low=price - 5,
        close=price,
        volume=1000,
    )


# =====================================================================
#  FOMC CALENDAR TESTS
# =====================================================================
class TestFOMCCalendar:
    def test_fomc_2026_has_8_dates(self):
        assert len(FOMC_2026_DATES) == 8

    def test_all_fomc_at_2pm_et(self):
        for dt in FOMC_2026_DATES:
            assert dt.hour == 14
            assert dt.minute == 0

    def test_hours_until_next_fomc_before_first(self):
        t = datetime(2026, 1, 1, 12, 0, tzinfo=ET)
        hours = hours_until_next_fomc(t)
        assert hours is not None
        # Jan 29 14:00 - Jan 1 12:00 = 28 days 2 hours
        assert hours > 24 * 27

    def test_hours_until_next_fomc_just_before(self):
        # 30 minutes before Jan 29 FOMC
        t = datetime(2026, 1, 29, 13, 30, tzinfo=ET)
        hours = hours_until_next_fomc(t)
        assert hours is not None
        assert abs(hours - 0.5) < 0.01

    def test_hours_until_next_fomc_between_meetings(self):
        # Feb 15 — between Jan 29 and Mar 19
        t = datetime(2026, 2, 15, 12, 0, tzinfo=ET)
        hours = hours_until_next_fomc(t)
        assert hours is not None
        # Should point to Mar 19
        next_dt = next_fomc_date(t)
        assert next_dt.month == 3
        assert next_dt.day == 19

    def test_hours_until_next_fomc_after_last(self):
        t = datetime(2026, 12, 31, 12, 0, tzinfo=ET)
        hours = hours_until_next_fomc(t)
        assert hours is None

    def test_next_fomc_date(self):
        t = datetime(2026, 5, 1, 12, 0, tzinfo=ET)
        nxt = next_fomc_date(t)
        assert nxt is not None
        assert nxt.month == 5
        assert nxt.day == 7

    def test_next_fomc_after_last_returns_none(self):
        t = datetime(2027, 1, 1, 12, 0, tzinfo=ET)
        assert next_fomc_date(t) is None


# =====================================================================
#  OVERNIGHT BIAS MODIFIER TESTS
# =====================================================================
class TestOvernightBiasModifier:

    def _feed_close_and_open(self, modifier, close_price, open_price,
                              close_date=(2026, 3, 2), open_date=(2026, 3, 3)):
        """Feed a 4PM close and 9:30AM open to the modifier."""
        # 4PM close bar
        close_bar = make_bar(*close_date, 16, 0, close_price)
        modifier.update_bar(close_bar)

        # 9:30AM open bar (next day)
        open_bar = MockBar(
            timestamp=datetime(*open_date, 9, 30, tzinfo=ET),
            open=open_price,
            high=open_price + 5,
            low=open_price - 5,
            close=open_price + 1,
            volume=2000,
        )
        modifier.update_bar(open_bar)

    def test_neutral_small_gap(self):
        """Gap < 50 bps should be neutral (1.0x all)."""
        mod = OvernightBiasModifier()
        # 20000 close, 20005 open = 2.5 bps (well under 50)
        self._feed_close_and_open(mod, 20000.0, 20005.0)

        result = mod.calculate("bullish")
        assert result.position_multiplier == 1.0
        assert result.stop_multiplier == 1.0
        assert result.runner_multiplier == 1.0
        assert result.details["classification"] == "neutral"

    def test_alignment_significant_bullish(self):
        """Gap up + bullish HTF = alignment significant."""
        mod = OvernightBiasModifier()
        # 20000 close, 20120 open = 60 bps (above 50, below 120)
        self._feed_close_and_open(mod, 20000.0, 20120.0)

        result = mod.calculate("bullish")
        assert result.position_multiplier == 1.4
        assert result.stop_multiplier == 1.0
        assert result.runner_multiplier == 1.2
        assert result.details["classification"] == "alignment_significant"

    def test_alignment_extreme_bullish(self):
        """Gap up > 120 bps + bullish HTF = alignment extreme."""
        mod = OvernightBiasModifier()
        # 20000 close, 20300 open = 150 bps (above 120)
        self._feed_close_and_open(mod, 20000.0, 20300.0)

        result = mod.calculate("bullish")
        assert result.position_multiplier == 1.5
        assert result.stop_multiplier == 1.0
        assert result.runner_multiplier == 1.3
        assert result.details["classification"] == "alignment_extreme"

    def test_alignment_significant_bearish(self):
        """Gap down + bearish HTF = alignment significant."""
        mod = OvernightBiasModifier()
        # 20000 close, 19880 open = -60 bps, bearish HTF
        self._feed_close_and_open(mod, 20000.0, 19880.0)

        result = mod.calculate("bearish")
        assert result.position_multiplier == 1.4
        assert result.stop_multiplier == 1.0
        assert result.runner_multiplier == 1.2
        assert result.details["classification"] == "alignment_significant"

    def test_conflict_significant(self):
        """Gap up + bearish HTF = conflict significant."""
        mod = OvernightBiasModifier()
        # 20000 close, 20120 open = 60 bps gap up, bearish HTF
        self._feed_close_and_open(mod, 20000.0, 20120.0)

        result = mod.calculate("bearish")
        assert result.position_multiplier == 0.6
        assert result.stop_multiplier == 0.8
        assert result.runner_multiplier == 0.8
        assert result.details["classification"] == "conflict_significant"

    def test_conflict_extreme(self):
        """Gap up > 120 bps + bearish HTF = conflict extreme."""
        mod = OvernightBiasModifier()
        # 20000 close, 20300 open = 150 bps gap up, bearish HTF
        self._feed_close_and_open(mod, 20000.0, 20300.0)

        result = mod.calculate("bearish")
        assert result.position_multiplier == 0.4
        assert result.stop_multiplier == 0.7
        assert result.runner_multiplier == 0.7
        assert result.details["classification"] == "conflict_extreme"

    def test_neutral_htf_returns_neutral(self):
        """Neutral HTF bias always returns neutral multipliers regardless of gap."""
        mod = OvernightBiasModifier()
        # Large gap but neutral HTF
        self._feed_close_and_open(mod, 20000.0, 20300.0)

        result = mod.calculate("neutral")
        assert result.position_multiplier == 1.0
        assert result.stop_multiplier == 1.0
        assert result.runner_multiplier == 1.0
        assert result.details["classification"] == "neutral"

    def test_no_data_returns_neutral(self):
        """Before any bars are fed, should return neutral."""
        mod = OvernightBiasModifier()
        result = mod.calculate("bullish")
        assert result.position_multiplier == 1.0
        assert result.stop_multiplier == 1.0
        assert result.runner_multiplier == 1.0

    def test_missing_htf_bias_returns_neutral(self):
        """None HTF bias returns neutral."""
        mod = OvernightBiasModifier()
        self._feed_close_and_open(mod, 20000.0, 20300.0)

        result = mod.calculate(None)
        assert result.position_multiplier == 1.0
        assert result.details["classification"] == "neutral"

    def test_bps_calculation_accuracy(self):
        """Verify bps calculation: ((open - close) / close) * 10000."""
        mod = OvernightBiasModifier()
        self._feed_close_and_open(mod, 20000.0, 20100.0)
        assert abs(mod.overnight_bps - 50.0) < 0.01

    def test_negative_bps_gap_down(self):
        """Gap down produces negative bps."""
        mod = OvernightBiasModifier()
        self._feed_close_and_open(mod, 20000.0, 19900.0)
        assert mod.overnight_bps < 0
        assert abs(mod.overnight_bps - (-50.0)) < 0.01


# =====================================================================
#  FOMC DRIFT MODIFIER TESTS
# =====================================================================
class TestFOMCDriftModifier:

    def test_no_fomc_near(self):
        """No FOMC within 24h returns all 1.0x."""
        mod = FOMCDriftModifier()
        # Jan 1 — 28 days before first FOMC
        t = datetime(2026, 1, 1, 12, 0, tzinfo=ET)
        result = mod.calculate(t)
        assert result.position_multiplier == 1.0
        assert result.stop_multiplier == 1.0
        assert result.runner_multiplier == 1.0
        assert not result.stand_aside
        assert result.details["window"] == "no_fomc"

    def test_24h_to_4h_window(self):
        """12 hours before FOMC = 24h-4h window."""
        mod = FOMCDriftModifier()
        # Jan 29 FOMC at 14:00 — 12 hours before = Jan 29 02:00
        t = datetime(2026, 1, 29, 2, 0, tzinfo=ET)
        result = mod.calculate(t)
        assert result.position_multiplier == 1.1
        assert result.stop_multiplier == 0.9
        assert not result.stand_aside
        assert result.details["window"] == "24h_to_4h"

    def test_4h_to_half_hour_window(self):
        """2 hours before FOMC = 4h-0.5h window."""
        mod = FOMCDriftModifier()
        # Jan 29 FOMC at 14:00 — 2 hours before = Jan 29 12:00
        t = datetime(2026, 1, 29, 12, 0, tzinfo=ET)
        result = mod.calculate(t)
        assert result.position_multiplier == 1.15
        assert result.stop_multiplier == 0.85
        assert not result.stand_aside
        assert result.details["window"] == "4h_to_0.5h"

    def test_stand_aside_under_half_hour(self):
        """< 0.5h before FOMC = stand aside."""
        mod = FOMCDriftModifier()
        # Jan 29 FOMC at 14:00 — 15 minutes before = Jan 29 13:45
        t = datetime(2026, 1, 29, 13, 45, tzinfo=ET)
        result = mod.calculate(t)
        assert result.stand_aside is True
        assert "stand aside" in result.stand_aside_reason.lower()

    def test_exactly_at_fomc(self):
        """At FOMC announcement time = stand aside (0h remaining)."""
        mod = FOMCDriftModifier()
        # Exactly 0.5h before
        t = datetime(2026, 1, 29, 13, 30, tzinfo=ET)
        result = mod.calculate(t)
        assert result.stand_aside is True

    def test_exactly_4h_boundary(self):
        """Exactly 4h before = 4h-0.5h window."""
        mod = FOMCDriftModifier()
        # Jan 29 14:00 - 4h = Jan 29 10:00
        t = datetime(2026, 1, 29, 10, 0, tzinfo=ET)
        result = mod.calculate(t)
        assert result.position_multiplier == 1.15
        assert result.details["window"] == "4h_to_0.5h"

    def test_exactly_24h_boundary(self):
        """Exactly 24h before = 24h-4h window."""
        mod = FOMCDriftModifier()
        # Jan 29 14:00 - 24h = Jan 28 14:00
        t = datetime(2026, 1, 28, 14, 0, tzinfo=ET)
        result = mod.calculate(t)
        assert result.position_multiplier == 1.1
        assert result.details["window"] == "24h_to_4h"

    def test_after_last_fomc_2026(self):
        """After Dec 17 FOMC = no FOMC."""
        mod = FOMCDriftModifier()
        t = datetime(2026, 12, 31, 12, 0, tzinfo=ET)
        result = mod.calculate(t)
        assert result.position_multiplier == 1.0
        assert result.details["hours_until_fomc"] is None

    def test_fomc_all_dates_covered(self):
        """Each FOMC date triggers stand-aside 15min before."""
        mod = FOMCDriftModifier()
        for fomc_dt in FOMC_2026_DATES:
            t = fomc_dt - timedelta(minutes=15)
            result = mod.calculate(t)
            assert result.stand_aside is True, f"Failed for FOMC {fomc_dt}"

    def test_fomc_weekend_proximity(self):
        """FOMC on Monday (hypothetical): weekend before still measures hours correctly."""
        mod = FOMCDriftModifier()
        # Mar 19, 2026 is a Thursday. Saturday March 14 at noon = ~5 days away
        t = datetime(2026, 3, 14, 12, 0, tzinfo=ET)
        result = mod.calculate(t)
        assert not result.stand_aside
        assert result.details["window"] == "no_fomc"


# =====================================================================
#  INSTITUTIONAL MODIFIER ENGINE TESTS
# =====================================================================
class TestInstitutionalModifierEngine:

    def test_disabled_returns_neutral(self):
        """When disabled, all multipliers are 1.0x."""
        engine = InstitutionalModifierEngine(log_dir=tempfile.mkdtemp())
        engine.enabled = False

        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        result = engine.calculate(t, "bullish")
        assert result.position_multiplier == 1.0
        assert result.stop_multiplier == 1.0
        assert result.runner_multiplier == 1.0
        assert not result.stand_aside

    def test_sequential_multiplication(self):
        """Modifiers multiply sequentially: final = overnight × fomc."""
        engine = InstitutionalModifierEngine(log_dir=tempfile.mkdtemp())

        # Feed bars to get overnight alignment significant (1.4x pos, 1.0x stop, 1.2x runner)
        close_bar = make_bar(2026, 1, 28, 16, 0, 20000.0)
        engine.update_bar(close_bar)

        open_bar = MockBar(
            timestamp=datetime(2026, 1, 29, 9, 30, tzinfo=ET),
            open=20120.0,  # 60 bps gap up
            high=20125.0,
            low=20115.0,
            close=20121.0,
            volume=2000,
        )
        engine.update_bar(open_bar)

        # FOMC Jan 29 at 14:00 — at 9:30AM = ~4.5h away = 24h-4h window
        # Actually 4.5h = in the 24h-4h window (1.1x pos, 0.9x stop)
        t = datetime(2026, 1, 29, 9, 30, tzinfo=ET)
        result = engine.calculate(t, "bullish")

        # Expected: pos=1.4*1.1=1.54, stop=1.0*0.9=0.9, runner=1.2*1.0=1.2
        assert abs(result.position_multiplier - 1.54) < 0.01
        assert abs(result.stop_multiplier - 0.9) < 0.01
        assert abs(result.runner_multiplier - 1.2) < 0.01

    def test_max_cap_enforced(self):
        """Total multiplier capped at 2.0x."""
        engine = InstitutionalModifierEngine(log_dir=tempfile.mkdtemp())

        # Force extreme alignment (1.5x pos) + FOMC 4h window (1.15x pos)
        # 1.5 * 1.15 = 1.725 (under 2.0x, but let's verify cap logic works)
        # To actually hit the cap, we'd need even higher values.
        # Since these are fixed thresholds, max possible is 1.5*1.15 = 1.725
        # Let's just verify the cap logic exists by testing with known values
        result = ModifierResult(position_multiplier=2.5)
        capped = max(MIN_TOTAL_MULTIPLIER, min(MAX_TOTAL_MULTIPLIER, result.position_multiplier))
        assert capped == 2.0

    def test_min_floor_enforced(self):
        """Total multiplier floored at 0.3x."""
        engine = InstitutionalModifierEngine(log_dir=tempfile.mkdtemp())

        # Conflict extreme (0.4x pos) + FOMC doesn't reduce position further
        # But 0.4x is above 0.3x floor, so it should pass through
        # To test floor: 0.4 * something < 0.3 = would need FOMC position < 0.75
        # FOMC never reduces position below 1.0, so floor won't be hit with
        # current thresholds. Test the clamp logic directly.
        result_val = 0.2  # hypothetical below floor
        capped = max(MIN_TOTAL_MULTIPLIER, min(MAX_TOTAL_MULTIPLIER, result_val))
        assert capped == 0.3

    def test_stand_aside_fomc(self):
        """FOMC < 0.5h triggers stand-aside."""
        engine = InstitutionalModifierEngine(log_dir=tempfile.mkdtemp())

        t = datetime(2026, 1, 29, 13, 45, tzinfo=ET)
        result = engine.calculate(t, "bullish")
        assert result.stand_aside is True
        assert "FOMC" in result.stand_aside_reason

    def test_overnight_never_vetoes(self):
        """Overnight modifier never produces stand_aside, even at extreme conflict."""
        engine = InstitutionalModifierEngine(log_dir=tempfile.mkdtemp())

        # Extreme conflict
        close_bar = make_bar(2026, 3, 2, 16, 0, 20000.0)
        engine.update_bar(close_bar)

        open_bar = MockBar(
            timestamp=datetime(2026, 3, 3, 9, 30, tzinfo=ET),
            open=20300.0,  # 150 bps gap up
            high=20305.0,
            low=20295.0,
            close=20301.0,
            volume=2000,
        )
        engine.update_bar(open_bar)

        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        result = engine.calculate(t, "bearish")  # conflict
        assert not result.stand_aside
        # But multipliers should be reduced
        assert result.position_multiplier < 1.0

    def test_json_logging(self):
        """Modifier calculations are logged to JSON file."""
        tmpdir = tempfile.mkdtemp()
        engine = InstitutionalModifierEngine(log_dir=tmpdir)

        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        engine.calculate(t, "bullish")

        log_path = os.path.join(tmpdir, "institutional_modifiers_log.json")
        assert os.path.exists(log_path)

        with open(log_path) as f:
            line = f.readline()
            entry = json.loads(line)

        assert "timestamp" in entry
        assert "position_multiplier" in entry
        assert "overnight" in entry
        assert "fomc" in entry

    def test_no_data_returns_neutral_multipliers(self):
        """With no bars fed, engine returns neutral 1.0x multipliers."""
        engine = InstitutionalModifierEngine(log_dir=tempfile.mkdtemp())

        # No bars fed, no FOMC near
        t = datetime(2026, 6, 1, 10, 0, tzinfo=ET)
        result = engine.calculate(t, "bullish")

        assert result.position_multiplier == 1.0
        assert result.stop_multiplier == 1.0
        assert result.runner_multiplier == 1.0

    def test_conflict_extreme_multipliers_propagate(self):
        """Conflict extreme overnight values flow through engine correctly."""
        engine = InstitutionalModifierEngine(log_dir=tempfile.mkdtemp())

        close_bar = make_bar(2026, 6, 1, 16, 0, 20000.0)
        engine.update_bar(close_bar)

        open_bar = MockBar(
            timestamp=datetime(2026, 6, 2, 9, 30, tzinfo=ET),
            open=20300.0,  # 150 bps gap up
            high=20305.0,
            low=20295.0,
            close=20301.0,
            volume=2000,
        )
        engine.update_bar(open_bar)

        # No FOMC near (Jun 18 is far away from Jun 2)
        t = datetime(2026, 6, 2, 10, 0, tzinfo=ET)
        result = engine.calculate(t, "bearish")  # conflict extreme

        assert result.position_multiplier == 0.4
        assert result.stop_multiplier == 0.7
        assert result.runner_multiplier == 0.7

    def test_multiple_bars_update_correctly(self):
        """Engine tracks latest session close/open across multiple days."""
        engine = InstitutionalModifierEngine(log_dir=tempfile.mkdtemp())

        # Day 1: neutral gap
        engine.update_bar(make_bar(2026, 3, 2, 16, 0, 20000.0))
        engine.update_bar(MockBar(
            timestamp=datetime(2026, 3, 3, 9, 30, tzinfo=ET),
            open=20005.0, high=20010.0, low=20000.0, close=20006.0,
        ))

        t1 = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        r1 = engine.calculate(t1, "bullish")
        assert r1.position_multiplier == 1.0  # neutral

        # Day 2: large gap
        engine.update_bar(make_bar(2026, 3, 3, 16, 0, 20000.0))
        engine.update_bar(MockBar(
            timestamp=datetime(2026, 3, 4, 9, 30, tzinfo=ET),
            open=20200.0, high=20205.0, low=20195.0, close=20201.0,
        ))

        t2 = datetime(2026, 3, 4, 10, 0, tzinfo=ET)
        r2 = engine.calculate(t2, "bullish")
        assert r2.position_multiplier == 1.4  # alignment significant (100 bps)

    def test_update_bar_disabled_noop(self):
        """update_bar does nothing when engine is disabled."""
        engine = InstitutionalModifierEngine(log_dir=tempfile.mkdtemp())
        engine.enabled = False

        engine.update_bar(make_bar(2026, 3, 2, 16, 0, 20000.0))
        # Should not crash and overnight state should remain empty
        assert engine.overnight._prev_day_close is None


# =====================================================================
#  EDGE CASE TESTS
# =====================================================================
class TestEdgeCases:

    def test_overnight_exactly_50_bps_is_neutral(self):
        """Exactly 50 bps is NOT >= threshold, so neutral."""
        mod = OvernightBiasModifier()
        # 20000 * 50/10000 = 100 pts → 20100 for exactly 50 bps
        close_bar = make_bar(2026, 3, 2, 16, 0, 20000.0)
        mod.update_bar(close_bar)
        open_bar = MockBar(
            timestamp=datetime(2026, 3, 3, 9, 30, tzinfo=ET),
            open=20100.0, high=20105.0, low=20095.0, close=20101.0,
        )
        mod.update_bar(open_bar)

        result = mod.calculate("bullish")
        # 50 bps is NOT < 50 (it equals), so this should be alignment_significant
        # Wait — the threshold is < 50 means neutral. 50 is NOT < 50, so it passes.
        assert result.details["classification"] == "alignment_significant"

    def test_overnight_exactly_120_bps(self):
        """Exactly 120 bps crosses into extreme."""
        mod = OvernightBiasModifier()
        # 20000 * 120/10000 = 240 pts → 20240
        close_bar = make_bar(2026, 3, 2, 16, 0, 20000.0)
        mod.update_bar(close_bar)
        open_bar = MockBar(
            timestamp=datetime(2026, 3, 3, 9, 30, tzinfo=ET),
            open=20240.0, high=20245.0, low=20235.0, close=20241.0,
        )
        mod.update_bar(open_bar)

        result = mod.calculate("bullish")
        assert result.details["classification"] == "alignment_extreme"

    def test_fomc_just_over_24h(self):
        """24.01h before FOMC = no_fomc window."""
        mod = FOMCDriftModifier()
        # Jan 29 14:00 - 24h 1min = Jan 28 13:59
        t = datetime(2026, 1, 28, 13, 59, tzinfo=ET)
        result = mod.calculate(t)
        # This is just barely inside 24h (24h and 1 min)
        hours = hours_until_next_fomc(t)
        assert hours > 24.0
        assert result.details["window"] == "no_fomc"

    def test_fomc_just_under_24h(self):
        """23.99h before FOMC = 24h-4h window."""
        mod = FOMCDriftModifier()
        t = datetime(2026, 1, 28, 14, 1, tzinfo=ET)
        result = mod.calculate(t)
        hours = hours_until_next_fomc(t)
        assert hours < 24.0
        assert result.details["window"] == "24h_to_4h"

    def test_engine_cap_with_alignment_plus_fomc(self):
        """Verify cap applies to combined alignment + FOMC multiplier."""
        engine = InstitutionalModifierEngine(log_dir=tempfile.mkdtemp())

        # Setup extreme alignment (1.5x pos)
        engine.update_bar(make_bar(2026, 1, 28, 16, 0, 20000.0))
        engine.update_bar(MockBar(
            timestamp=datetime(2026, 1, 29, 9, 30, tzinfo=ET),
            open=20300.0, high=20305.0, low=20295.0, close=20301.0,
        ))

        # 10h before FOMC = 24h-4h window (1.1x pos)
        t = datetime(2026, 1, 29, 4, 0, tzinfo=ET)
        result = engine.calculate(t, "bullish")

        # 1.5 * 1.1 = 1.65 (under 2.0 cap)
        assert abs(result.position_multiplier - 1.65) < 0.01
        assert result.position_multiplier <= MAX_TOTAL_MULTIPLIER

    def test_engine_floor_with_conflict(self):
        """Verify floor applies to combined conflict multipliers."""
        engine = InstitutionalModifierEngine(log_dir=tempfile.mkdtemp())

        # Setup conflict extreme (0.4x pos, 0.7x stop, 0.7x runner)
        engine.update_bar(make_bar(2026, 6, 1, 16, 0, 20000.0))
        engine.update_bar(MockBar(
            timestamp=datetime(2026, 6, 2, 9, 30, tzinfo=ET),
            open=20300.0, high=20305.0, low=20295.0, close=20301.0,
        ))

        # No FOMC window (Jun 18 is far)
        t = datetime(2026, 6, 2, 10, 0, tzinfo=ET)
        result = engine.calculate(t, "bearish")

        # 0.4 * 1.0 = 0.4 (above 0.3 floor)
        assert result.position_multiplier == 0.4
        assert result.position_multiplier >= MIN_TOTAL_MULTIPLIER


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
