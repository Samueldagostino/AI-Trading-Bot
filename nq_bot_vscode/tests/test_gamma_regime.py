"""
Tests for GammaRegimeModifier and unified 4-modifier engine.
==============================================================
Covers:
  - GammaRegimeModifier: strong/weak positive/negative gamma regimes
  - VIX amplification at VIX > 20 and dampening at VIX < 12
  - Time-of-day weighting for opening, midday, and close sessions
  - Fallback mode with None VIX inputs
  - Full engine with all 4 modifiers active (overnight × fomc × gamma × vol)
  - Cap at 2.0x and floor at 0.3x with extreme combined multipliers
  - Modifier decision logging to modifier_decisions.json
"""

import json
import os
import tempfile
import pytest
from datetime import datetime, timedelta
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from signals.institutional_modifiers import (
    GammaRegimeModifier,
    InstitutionalModifierEngine,
    ModifierResult,
    MAX_TOTAL_MULTIPLIER,
    MIN_TOTAL_MULTIPLIER,
    GAMMA_THRESHOLDS,
)
from signals.volatility_forecast import HARRVForecaster

ET = ZoneInfo("America/New_York")


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
#  GAMMA REGIME MODIFIER TESTS
# =====================================================================
class TestGammaRegimeStrongPositive:
    """slope > 5%: Strong positive gamma -> position 0.7x, stop 1.0x"""

    def test_steep_contango(self):
        """VIX term structure in steep contango (slope > 5%)."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)  # opening drive
        # slope = (21 - 20) / 20 = 0.05 -> exactly 5% is weak_positive (>5% needed)
        # slope = (21.5 - 20) / 20 = 0.075 -> 7.5% > 5% = strong_positive
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=21.5)
        assert result.position_multiplier == pytest.approx(0.7, abs=0.01)
        assert result.stop_multiplier == 1.0
        assert result.details["regime"] == "strong_positive"
        assert result.details["slope"] == pytest.approx(0.075, abs=0.001)

    def test_very_steep_contango(self):
        """Very steep contango with 10% slope."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        # slope = (22 - 20) / 20 = 0.10
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=22.0)
        assert result.position_multiplier == pytest.approx(0.7, abs=0.01)
        assert result.details["regime"] == "strong_positive"


class TestGammaRegimeWeakPositive:
    """slope 0-5%: Weak positive gamma -> position 0.85x, stop 1.0x"""

    def test_mild_contango(self):
        """Slope between 0% and 5%."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        # slope = (20.5 - 20) / 20 = 0.025 (2.5%)
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=20.5)
        assert result.position_multiplier == pytest.approx(0.85, abs=0.01)
        assert result.stop_multiplier == 1.0
        assert result.details["regime"] == "weak_positive"

    def test_flat_term_structure(self):
        """Exactly 0% slope = weak positive (>= 0)."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        # slope = (20 - 20) / 20 = 0.0
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=20.0)
        assert result.position_multiplier == pytest.approx(0.85, abs=0.01)
        assert result.details["regime"] == "weak_positive"

    def test_exactly_5_percent_slope(self):
        """Exactly 5% slope should be weak positive (not strong)."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        # slope = (21 - 20) / 20 = 0.05 exactly
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=21.0)
        assert result.details["regime"] == "weak_positive"


class TestGammaRegimeWeakNegative:
    """slope -5% to 0%: Weak negative gamma -> position 1.15x, stop 1.0x"""

    def test_mild_backwardation(self):
        """Slope between -5% and 0%."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        # slope = (19.5 - 20) / 20 = -0.025 (-2.5%)
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=19.5)
        assert result.position_multiplier == pytest.approx(1.15, abs=0.01)
        assert result.stop_multiplier == 1.0
        assert result.details["regime"] == "weak_negative"

    def test_exactly_negative_5_percent(self):
        """Exactly -5% slope should be weak negative (not strong)."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        # slope = (19 - 20) / 20 = -0.05 exactly
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=19.0)
        assert result.details["regime"] == "weak_negative"


class TestGammaRegimeStrongNegative:
    """slope < -5%: Strong negative gamma -> position 1.3x, stop 1.0x"""

    def test_steep_backwardation(self):
        """VIX term structure in steep backwardation (slope < -5%)."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        # slope = (18 - 20) / 20 = -0.10 (-10%)
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=18.0)
        assert result.position_multiplier == pytest.approx(1.3, abs=0.01)
        assert result.stop_multiplier == 1.0
        assert result.details["regime"] == "strong_negative"

    def test_extreme_backwardation(self):
        """Very steep backwardation."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        # slope = (15 - 20) / 20 = -0.25 (-25%)
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=15.0)
        assert result.details["regime"] == "strong_negative"
        assert result.position_multiplier == pytest.approx(1.3, abs=0.01)


# =====================================================================
#  VIX AMPLIFICATION TESTS
# =====================================================================
class TestVIXAmplification:

    def test_high_vix_negative_gamma_amplification(self):
        """VIX > 20 AND negative gamma: position *= 1.2."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        # slope = (18 - 20) / 20 = -0.10 -> strong_negative (1.3x base)
        # VIX > 20 AND negative slope -> 1.3 * 1.2 = 1.56
        result = mod.calculate(t, vix_spot=25.0, vix_front_month=20.0, vix_second_month=18.0)
        assert result.details["vix_amplification"] == 1.2
        assert result.position_multiplier == pytest.approx(1.3 * 1.2, abs=0.01)

    def test_high_vix_weak_negative_gamma(self):
        """VIX > 20 AND weak negative gamma: amplified."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        # slope = (19.5 - 20) / 20 = -0.025 -> weak_negative (1.15x base)
        # VIX > 20 AND negative slope -> 1.15 * 1.2 = 1.38
        result = mod.calculate(t, vix_spot=22.0, vix_front_month=20.0, vix_second_month=19.5)
        assert result.details["vix_amplification"] == 1.2
        assert result.position_multiplier == pytest.approx(1.15 * 1.2, abs=0.01)

    def test_high_vix_positive_gamma_no_amplification(self):
        """VIX > 20 but positive gamma: no amplification."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        # slope = (21.5 - 20) / 20 = 0.075 -> strong_positive (0.7x)
        # VIX > 20 but positive slope -> no amplification
        result = mod.calculate(t, vix_spot=25.0, vix_front_month=20.0, vix_second_month=21.5)
        assert result.details["vix_amplification"] == 1.0
        assert result.position_multiplier == pytest.approx(0.7, abs=0.01)

    def test_low_vix_dampening(self):
        """VIX < 12: position *= 0.9."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        # slope = (21.5 - 20) / 20 = 0.075 -> strong_positive (0.7x)
        # VIX < 12 -> 0.7 * 0.9 = 0.63
        result = mod.calculate(t, vix_spot=10.0, vix_front_month=20.0, vix_second_month=21.5)
        assert result.details["vix_amplification"] == 0.9
        assert result.position_multiplier == pytest.approx(0.7 * 0.9, abs=0.01)

    def test_low_vix_with_negative_gamma(self):
        """VIX < 12 with negative gamma: dampening takes precedence over amplification."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        # slope = (18 - 20) / 20 = -0.10 -> strong_negative (1.3x)
        # VIX < 12 -> dampening (0.9x), NOT amplification
        result = mod.calculate(t, vix_spot=10.0, vix_front_month=20.0, vix_second_month=18.0)
        assert result.details["vix_amplification"] == 0.9
        assert result.position_multiplier == pytest.approx(1.3 * 0.9, abs=0.01)

    def test_normal_vix_no_adjustment(self):
        """VIX 12-20: no amplification or dampening."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        result = mod.calculate(t, vix_spot=16.0, vix_front_month=20.0, vix_second_month=18.0)
        assert result.details["vix_amplification"] == 1.0

    def test_vix_exactly_20_no_amplification(self):
        """VIX exactly 20: no amplification (need > 20)."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        # negative slope
        result = mod.calculate(t, vix_spot=20.0, vix_front_month=20.0, vix_second_month=18.0)
        assert result.details["vix_amplification"] == 1.0

    def test_vix_exactly_12_no_dampening(self):
        """VIX exactly 12: no dampening (need < 12)."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        result = mod.calculate(t, vix_spot=12.0, vix_front_month=20.0, vix_second_month=18.0)
        assert result.details["vix_amplification"] == 1.0


# =====================================================================
#  TIME-OF-DAY WEIGHTING TESTS
# =====================================================================
class TestTimeOfDayWeighting:

    def test_opening_drive_full_weight(self):
        """9:30-10:30 ET: full gamma multiplier (time_weight=1.0)."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)  # 10:00 AM ET
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=18.0)
        assert result.details["time_weight"] == 1.0

    def test_opening_drive_930(self):
        """Exactly 9:30 AM ET should be opening drive."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 9, 30, tzinfo=ET)
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=18.0)
        assert result.details["time_weight"] == 1.0

    def test_midday_reduced_weight(self):
        """10:30-14:00 ET: 0.75x of gamma effect."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 12, 0, tzinfo=ET)  # noon
        # slope = -0.10 -> strong_negative (1.3x base)
        # After time weighting: 1.0 + (1.3 - 1.0) * 0.75 = 1.225
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=18.0)
        assert result.details["time_weight"] == 0.75
        assert result.position_multiplier == pytest.approx(1.0 + (1.3 - 1.0) * 0.75, abs=0.01)

    def test_midday_1030(self):
        """Exactly 10:30 should be midday."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 30, tzinfo=ET)
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=18.0)
        assert result.details["time_weight"] == 0.75

    def test_close_session_full_weight(self):
        """14:00-16:00 ET: full gamma multiplier."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 15, 0, tzinfo=ET)  # 3:00 PM ET
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=18.0)
        assert result.details["time_weight"] == 1.0

    def test_close_session_1400(self):
        """Exactly 14:00 should be close session."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 14, 0, tzinfo=ET)
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=18.0)
        assert result.details["time_weight"] == 1.0

    def test_midday_reduces_strong_positive_gamma_deviation(self):
        """Midday should reduce the deviation of strong positive gamma from 1.0."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 12, 0, tzinfo=ET)
        # slope > 5% -> strong_positive (0.7x base)
        # After time weighting: 1.0 + (0.7 - 1.0) * 0.75 = 1.0 + (-0.3 * 0.75) = 0.775
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=21.5)
        assert result.position_multiplier == pytest.approx(1.0 + (0.7 - 1.0) * 0.75, abs=0.01)

    def test_outside_rth_full_weight(self):
        """Outside RTH hours: full weight (default)."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 8, 0, tzinfo=ET)  # 8:00 AM pre-market
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=18.0)
        assert result.details["time_weight"] == 1.0


# =====================================================================
#  FALLBACK MODE TESTS
# =====================================================================
class TestGammaFallbackMode:

    def test_all_none_inputs(self):
        """All VIX inputs None returns neutral (1.0x)."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        result = mod.calculate(t, vix_spot=None, vix_front_month=None, vix_second_month=None)
        assert result.position_multiplier == 1.0
        assert result.stop_multiplier == 1.0
        assert result.details["regime"] == "neutral_fallback"
        assert "unavailable" in result.details["reason"].lower()

    def test_vix_spot_none(self):
        """VIX spot None returns neutral."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        result = mod.calculate(t, vix_spot=None, vix_front_month=20.0, vix_second_month=21.0)
        assert result.position_multiplier == 1.0
        assert result.details["regime"] == "neutral_fallback"

    def test_vix_front_none(self):
        """VIX front month None returns neutral."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=None, vix_second_month=21.0)
        assert result.position_multiplier == 1.0

    def test_vix_second_none(self):
        """VIX second month None returns neutral."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=None)
        assert result.position_multiplier == 1.0

    def test_default_args_neutral(self):
        """Default args (all None) returns neutral."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        result = mod.calculate(t)
        assert result.position_multiplier == 1.0
        assert result.stop_multiplier == 1.0

    def test_zero_front_month_neutral(self):
        """Front month VIX of 0.0 returns neutral (division by zero guard)."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=0.0, vix_second_month=21.0)
        assert result.position_multiplier == 1.0
        assert result.details["regime"] == "neutral_fallback"


# =====================================================================
#  VOLATILITY FORECASTER FALLBACK TESTS
# =====================================================================
class TestVolatilityFallback:

    def test_insufficient_data_returns_neutral(self):
        """HARRVForecaster with < 22 days returns neutral modifier."""
        f = HARRVForecaster()
        for i in range(10):
            f.update(0.001)
        mod = f.get_volatility_modifier()
        assert mod == {"position": 1.0, "stop": 1.0}

    def test_exactly_22_days_no_forecast_returns_neutral(self):
        """22 days but no forecast() called yet returns neutral."""
        f = HARRVForecaster()
        for i in range(22):
            f.update(0.001)
        mod = f.get_volatility_modifier()
        assert mod == {"position": 1.0, "stop": 1.0}


# =====================================================================
#  FULL ENGINE WITH ALL 4 MODIFIERS
# =====================================================================
class TestFullEngine:

    def _make_engine_with_vol(self, vol_level="normal"):
        """Create engine with a pre-loaded vol forecaster."""
        tmpdir = tempfile.mkdtemp()
        forecaster = HARRVForecaster()

        if vol_level == "high":
            # Build history then spike
            for i in range(22):
                forecaster.update(0.001)
            for _ in range(30):
                forecaster.forecast()
            for _ in range(5):
                forecaster.update(0.010)
            forecaster.forecast()
        elif vol_level == "low":
            for i in range(22):
                forecaster.update(0.010)
            for _ in range(30):
                forecaster.forecast()
            for _ in range(5):
                forecaster.update(0.0001)
            forecaster.forecast()
        elif vol_level == "normal":
            for i in range(22):
                forecaster.update(0.001)
            for _ in range(5):
                forecaster.forecast()
        else:  # "none" - no data
            pass

        engine = InstitutionalModifierEngine(log_dir=tmpdir, vol_forecaster=forecaster)
        return engine, tmpdir

    def test_all_4_modifiers_neutral(self):
        """All modifiers at neutral = 1.0x total."""
        engine, tmpdir = self._make_engine_with_vol("none")  # vol neutral
        t = datetime(2026, 6, 1, 10, 0, tzinfo=ET)  # no FOMC nearby
        result = engine.calculate(t, "bullish")  # no bars = overnight neutral
        # gamma = neutral (no VIX data)
        assert result.position_multiplier == 1.0
        assert result.stop_multiplier == 1.0

    def test_4_modifier_sequential_multiplication(self):
        """Verify all 4 modifiers multiply sequentially."""
        engine, tmpdir = self._make_engine_with_vol("normal")

        # Setup overnight alignment significant: pos=1.4, stop=1.0
        engine.update_bar(make_bar(2026, 6, 1, 16, 0, 20000.0))
        engine.update_bar(MockBar(
            timestamp=datetime(2026, 6, 2, 9, 30, tzinfo=ET),
            open=20120.0, high=20125.0, low=20115.0, close=20121.0,
        ))

        # No FOMC near Jun 2 -> fomc pos=1.0
        # Gamma: slope = (18-20)/20 = -0.10 -> strong_negative (1.3x)
        # VIX = 15 (normal, no amplification)
        # Time = 10:00 (opening drive, full weight)
        # Vol: normal -> pos=1.0
        t = datetime(2026, 6, 2, 10, 0, tzinfo=ET)
        result = engine.calculate(
            t, "bullish",
            vix_spot=15.0, vix_front_month=20.0, vix_second_month=18.0,
        )

        # Expected: 1.4 * 1.0 * 1.3 * 1.0 = 1.82
        assert result.position_multiplier == pytest.approx(1.82, abs=0.02)

    def test_4_modifier_with_fomc_window(self):
        """All 4 modifiers active with FOMC in 24h-4h window."""
        engine, tmpdir = self._make_engine_with_vol("normal")

        # Setup overnight alignment significant: pos=1.4
        engine.update_bar(make_bar(2026, 1, 28, 16, 0, 20000.0))
        engine.update_bar(MockBar(
            timestamp=datetime(2026, 1, 29, 9, 30, tzinfo=ET),
            open=20120.0, high=20125.0, low=20115.0, close=20121.0,
        ))

        # FOMC Jan 29 at 14:00, time=9:30 => ~4.5h away => 24h-4h window (1.1x)
        # Gamma: mild contango (slope ~2.5%) -> weak_positive (0.85x)
        # Vol: normal (1.0x)
        t = datetime(2026, 1, 29, 9, 30, tzinfo=ET)
        result = engine.calculate(
            t, "bullish",
            vix_spot=15.0, vix_front_month=20.0, vix_second_month=20.5,
        )

        # Expected position: 1.4 * 1.1 * 0.85 * 1.0 = 1.309
        assert result.position_multiplier == pytest.approx(1.4 * 1.1 * 0.85, abs=0.02)

    def test_fomc_stand_aside_blocks_all(self):
        """FOMC stand-aside blocks trade regardless of other modifiers."""
        engine, tmpdir = self._make_engine_with_vol("normal")

        # 15 min before FOMC
        t = datetime(2026, 1, 29, 13, 45, tzinfo=ET)
        result = engine.calculate(
            t, "bullish",
            vix_spot=15.0, vix_front_month=20.0, vix_second_month=18.0,
        )
        assert result.stand_aside is True
        assert "FOMC" in result.stand_aside_reason

    def test_engine_with_no_vix_data(self):
        """Engine works cleanly when VIX data is None (backtest mode)."""
        engine, tmpdir = self._make_engine_with_vol("none")
        t = datetime(2026, 6, 1, 10, 0, tzinfo=ET)
        # No VIX data passed -> gamma returns neutral
        result = engine.calculate(t, "bullish")
        assert result.position_multiplier == 1.0
        assert not result.stand_aside

    def test_engine_with_high_vol(self):
        """High volatility reduces position size."""
        engine, tmpdir = self._make_engine_with_vol("high")
        t = datetime(2026, 6, 1, 10, 0, tzinfo=ET)
        result = engine.calculate(t, "bullish")
        # vol modifier = 0.85x for high vol, everything else neutral
        assert result.position_multiplier == pytest.approx(0.85, abs=0.02)

    def test_engine_disabled_returns_neutral(self):
        """Disabled engine returns all 1.0x."""
        engine, tmpdir = self._make_engine_with_vol("normal")
        engine.enabled = False
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        result = engine.calculate(
            t, "bullish",
            vix_spot=25.0, vix_front_month=20.0, vix_second_month=18.0,
        )
        assert result.position_multiplier == 1.0
        assert result.stop_multiplier == 1.0
        assert not result.stand_aside


# =====================================================================
#  CAP AND FLOOR ENFORCEMENT TESTS
# =====================================================================
class TestCapAndFloor:

    def test_cap_at_2x_with_extreme_multipliers(self):
        """Total position multiplier capped at 2.0x."""
        # Simulate extreme combination:
        # overnight alignment_extreme = 1.5x
        # fomc 4h window = 1.15x
        # gamma strong_negative + VIX amplification = 1.3*1.2 = 1.56x
        # vol normal = 1.0x
        # Total raw = 1.5 * 1.15 * 1.56 * 1.0 = 2.691 -> capped at 2.0
        tmpdir = tempfile.mkdtemp()
        engine = InstitutionalModifierEngine(log_dir=tmpdir)

        # Feed extreme overnight alignment
        engine.update_bar(make_bar(2026, 1, 28, 16, 0, 20000.0))
        engine.update_bar(MockBar(
            timestamp=datetime(2026, 1, 29, 9, 30, tzinfo=ET),
            open=20300.0, high=20305.0, low=20295.0, close=20301.0,
        ))

        # 10h before FOMC = 24h-4h window (1.1x pos)
        t = datetime(2026, 1, 29, 4, 0, tzinfo=ET)
        result = engine.calculate(
            t, "bullish",
            vix_spot=25.0, vix_front_month=20.0, vix_second_month=18.0,
        )

        assert result.position_multiplier <= MAX_TOTAL_MULTIPLIER
        assert result.position_multiplier == MAX_TOTAL_MULTIPLIER  # should be capped

    def test_floor_at_03x_with_extreme_conflict(self):
        """Total position multiplier floored at 0.3x."""
        # Simulate extreme conflict combination:
        # overnight conflict_extreme = 0.4x pos
        # fomc no_fomc = 1.0x
        # gamma strong_positive + VIX dampening = 0.7 * 0.9 = 0.63x
        # vol = 1.0x
        # Total raw = 0.4 * 1.0 * 0.63 * 1.0 = 0.252 -> floored at 0.3
        tmpdir = tempfile.mkdtemp()
        engine = InstitutionalModifierEngine(log_dir=tmpdir)

        # Feed extreme overnight conflict
        engine.update_bar(make_bar(2026, 6, 1, 16, 0, 20000.0))
        engine.update_bar(MockBar(
            timestamp=datetime(2026, 6, 2, 9, 30, tzinfo=ET),
            open=20300.0, high=20305.0, low=20295.0, close=20301.0,
        ))

        t = datetime(2026, 6, 2, 10, 0, tzinfo=ET)
        result = engine.calculate(
            t, "bearish",  # conflict with gap up
            vix_spot=10.0, vix_front_month=20.0, vix_second_month=21.5,
        )

        assert result.position_multiplier >= MIN_TOTAL_MULTIPLIER
        assert result.position_multiplier == MIN_TOTAL_MULTIPLIER  # should be floored

    def test_cap_clamp_logic(self):
        """Direct test of clamp logic."""
        assert max(MIN_TOTAL_MULTIPLIER, min(MAX_TOTAL_MULTIPLIER, 5.0)) == 2.0
        assert max(MIN_TOTAL_MULTIPLIER, min(MAX_TOTAL_MULTIPLIER, 0.1)) == 0.3
        assert max(MIN_TOTAL_MULTIPLIER, min(MAX_TOTAL_MULTIPLIER, 1.5)) == 1.5


# =====================================================================
#  MODIFIER DECISION LOGGING TESTS
# =====================================================================
class TestModifierDecisionLogging:

    def test_decision_log_created(self):
        """modifier_decisions.json is created on calculate."""
        tmpdir = tempfile.mkdtemp()
        engine = InstitutionalModifierEngine(log_dir=tmpdir)

        t = datetime(2026, 6, 1, 10, 0, tzinfo=ET)
        engine.calculate(t, "bullish")

        decision_path = os.path.join(tmpdir, "modifier_decisions.json")
        assert os.path.exists(decision_path)

    def test_decision_log_contains_all_modifiers(self):
        """Decision log has individual modifier values."""
        tmpdir = tempfile.mkdtemp()
        engine = InstitutionalModifierEngine(log_dir=tmpdir)

        t = datetime(2026, 6, 1, 10, 0, tzinfo=ET)
        engine.calculate(
            t, "bullish",
            vix_spot=15.0, vix_front_month=20.0, vix_second_month=18.0,
        )

        decision_path = os.path.join(tmpdir, "modifier_decisions.json")
        with open(decision_path) as f:
            entry = json.loads(f.readline())

        assert "individual" in entry
        assert "overnight_position" in entry["individual"]
        assert "fomc_position" in entry["individual"]
        assert "gamma_position" in entry["individual"]
        assert "volatility_position" in entry["individual"]
        assert "combined_position" in entry
        assert "combined_stop" in entry

    def test_institutional_log_contains_gamma_and_vol(self):
        """Main log file includes gamma and volatility sections."""
        tmpdir = tempfile.mkdtemp()
        engine = InstitutionalModifierEngine(log_dir=tmpdir)

        t = datetime(2026, 6, 1, 10, 0, tzinfo=ET)
        engine.calculate(
            t, "bullish",
            vix_spot=15.0, vix_front_month=20.0, vix_second_month=18.0,
        )

        log_path = os.path.join(tmpdir, "institutional_modifiers_log.json")
        with open(log_path) as f:
            entry = json.loads(f.readline())

        assert "gamma" in entry
        assert "volatility" in entry
        assert entry["gamma"]["regime"] == "strong_negative"


# =====================================================================
#  INTEGRATION TESTS
# =====================================================================
class TestIntegration:

    def test_engine_backwards_compatible_no_vix_args(self):
        """Engine.calculate() works without VIX args (all optional)."""
        tmpdir = tempfile.mkdtemp()
        engine = InstitutionalModifierEngine(log_dir=tmpdir)
        t = datetime(2026, 6, 1, 10, 0, tzinfo=ET)
        result = engine.calculate(t, "bullish")
        assert result.position_multiplier == 1.0
        assert not result.stand_aside

    def test_gamma_modifier_standalone(self):
        """GammaRegimeModifier can be used independently."""
        mod = GammaRegimeModifier()
        t = datetime(2026, 3, 3, 10, 0, tzinfo=ET)
        result = mod.calculate(t, vix_spot=15.0, vix_front_month=20.0, vix_second_month=18.0)
        assert isinstance(result, ModifierResult)
        assert result.details["slope"] == pytest.approx(-0.1, abs=0.001)

    def test_vol_forecaster_wired_into_engine(self):
        """HARRVForecaster is properly used by the engine."""
        tmpdir = tempfile.mkdtemp()
        forecaster = HARRVForecaster()
        # Build enough data for high vol
        for i in range(22):
            forecaster.update(0.001)
        for _ in range(30):
            forecaster.forecast()
        for _ in range(5):
            forecaster.update(0.010)
        forecaster.forecast()

        engine = InstitutionalModifierEngine(log_dir=tmpdir, vol_forecaster=forecaster)
        t = datetime(2026, 6, 1, 10, 0, tzinfo=ET)
        result = engine.calculate(t, "bullish")
        # High vol -> pos 0.85
        assert result.position_multiplier == pytest.approx(0.85, abs=0.02)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
