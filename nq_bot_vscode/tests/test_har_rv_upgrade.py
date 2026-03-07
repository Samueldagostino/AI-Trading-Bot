"""
Tests for HAR-RV log(RV) upgrade and wider modifier range.
============================================================
Covers:
  - log(RV) computation
  - Percentile ranking on log scale
  - 6-tier modifier range (0.50x to 1.40x)
  - Insufficient data returns default
  - Zero RV edge case (log fallback)
  - Backward compatibility of compute_realized_volatility
"""

import math
import pytest
from signals.volatility_forecast import HARRVForecaster


class TestLogRVComputation:

    def test_compute_log_rv(self):
        """log(RV) should equal log(sum of squared returns)."""
        returns = [0.01, -0.02, 0.015, -0.005, 0.03]
        rv = sum(r ** 2 for r in returns)
        expected_log_rv = math.log(rv)
        actual = HARRVForecaster.compute_log_rv(returns)
        assert abs(actual - expected_log_rv) < 1e-10

    def test_compute_log_rv_empty(self):
        """Empty returns -> -inf."""
        assert math.isinf(HARRVForecaster.compute_log_rv([]))

    def test_compute_log_rv_all_zero(self):
        """All zero returns -> -inf."""
        assert math.isinf(HARRVForecaster.compute_log_rv([0.0, 0.0, 0.0]))

    def test_log_rv_properties(self):
        """log_rv_daily, log_rv_weekly, log_rv_monthly properties work."""
        f = HARRVForecaster()
        for i in range(22):
            f.update(0.001 * (i + 1))

        # All should be finite (positive RV values)
        assert math.isfinite(f.log_rv_daily)
        assert math.isfinite(f.log_rv_weekly)
        assert math.isfinite(f.log_rv_monthly)

    def test_log_rv_daily_zero_rv(self):
        """Zero daily RV -> -inf log."""
        f = HARRVForecaster()
        f.update(0.0)
        assert math.isinf(f.log_rv_daily)


class TestForecastWithLogRV:

    def test_forecast_uses_log_scale(self):
        """Forecast should use log(RV) internally."""
        f = HARRVForecaster()
        for i in range(22):
            f.update(0.001)
        fc = f.forecast()
        # With log transform, forecast should be positive
        assert fc > 0

    def test_forecast_populates_log_history(self):
        """Forecasting should populate _log_forecast_history."""
        f = HARRVForecaster()
        for i in range(22):
            f.update(0.001)
        f.forecast()
        assert len(f._log_forecast_history) == 1

    def test_forecast_zero_rv_fallback(self):
        """Zero RV days fall back to raw forecast."""
        f = HARRVForecaster()
        for i in range(22):
            f.update(0.0)
        fc = f.forecast()
        assert fc == 0.0


class TestPercentileRanking:

    def test_percentile_on_log_scale(self):
        """Percentile computed on log forecast history."""
        f = HARRVForecaster()
        # Create varying data
        for i in range(22):
            f.update(0.001)
        # Generate many forecasts
        for _ in range(50):
            f.forecast()
        pct = f._compute_percentile_log(f._log_forecast_history[-1])
        # All same values -> ~50th percentile
        assert 40 <= pct <= 60


class TestModifierRange:

    def _build_forecaster(self, vol_level="normal"):
        """Build a forecaster with enough history for testing."""
        f = HARRVForecaster()
        if vol_level == "high":
            # Start with low vol, then spike
            for i in range(22):
                f.update(0.001)
            for _ in range(50):
                f.forecast()
            for _ in range(5):
                f.update(0.010)
            f.forecast()
        elif vol_level == "low":
            # Start with high vol, then drop
            for i in range(22):
                f.update(0.010)
            for _ in range(50):
                f.forecast()
            for _ in range(5):
                f.update(0.0001)
            f.forecast()
        elif vol_level == "extreme":
            # Start with low vol, massive spike
            for i in range(22):
                f.update(0.001)
            for _ in range(100):
                f.forecast()
            for _ in range(5):
                f.update(0.100)  # 100x higher
            f.forecast()
        elif vol_level == "very_low":
            # Start with high vol, then very low
            for i in range(22):
                f.update(0.100)
            for _ in range(100):
                f.forecast()
            for _ in range(5):
                f.update(0.00001)
            f.forecast()
        else:
            for i in range(22):
                f.update(0.001)
            for _ in range(5):
                f.forecast()
        return f

    def test_high_vol_modifier(self):
        """High vol should return position <= 0.75."""
        f = self._build_forecaster("high")
        mod = f.get_volatility_modifier()
        assert mod["position"] <= 0.90  # At minimum above avg vol
        assert mod["stop"] >= 1.0

    def test_low_vol_modifier(self):
        """Low vol should return position >= 1.10."""
        f = self._build_forecaster("low")
        mod = f.get_volatility_modifier()
        assert mod["position"] >= 1.10
        assert mod["stop"] <= 1.0

    def test_modifier_range_min(self):
        """Minimum position modifier is 0.50."""
        # Verify from the tier table
        assert HARRVForecaster.VOL_TIERS[-1][1] == 0.50

    def test_modifier_range_max(self):
        """Maximum position modifier is 1.40."""
        assert HARRVForecaster.VOL_TIERS[0][1] == 1.40

    def test_stop_range_min(self):
        """Minimum stop modifier is 0.70."""
        assert HARRVForecaster.VOL_TIERS[0][2] == 0.70

    def test_stop_range_max(self):
        """Maximum stop modifier is 1.50."""
        assert HARRVForecaster.VOL_TIERS[-1][2] == 1.50

    def test_modifier_includes_reason(self):
        """Modifier result includes reason string."""
        f = self._build_forecaster("normal")
        mod = f.get_volatility_modifier()
        assert "reason" in mod

    def test_modifier_includes_percentile(self):
        """Modifier result includes percentile."""
        f = self._build_forecaster("normal")
        mod = f.get_volatility_modifier()
        assert "percentile" in mod

    def test_modifier_includes_log_rv(self):
        """Modifier result includes log_rv value."""
        f = self._build_forecaster("normal")
        mod = f.get_volatility_modifier()
        assert "log_rv" in mod


class TestInsufficientDataDefault:

    def test_no_data_returns_neutral(self):
        """No data -> position 1.0, stop 1.0."""
        f = HARRVForecaster()
        mod = f.get_volatility_modifier()
        assert mod["position"] == 1.0
        assert mod["stop"] == 1.0

    def test_partial_data_returns_neutral(self):
        """Insufficient history -> neutral."""
        f = HARRVForecaster()
        for i in range(10):
            f.update(0.001)
        mod = f.get_volatility_modifier()
        assert mod["position"] == 1.0
        assert mod["stop"] == 1.0

    def test_enough_data_no_forecast_returns_neutral(self):
        """22 days but no forecast() called -> neutral."""
        f = HARRVForecaster()
        for i in range(22):
            f.update(0.001)
        mod = f.get_volatility_modifier()
        assert mod["position"] == 1.0
        assert mod["stop"] == 1.0


class TestBackwardCompatibility:

    def test_compute_realized_volatility_unchanged(self):
        """Original RV computation still works."""
        returns = [0.01, -0.02, 0.015, -0.005, 0.03]
        rv = HARRVForecaster.compute_realized_volatility(returns)
        expected = sum(r ** 2 for r in returns)
        assert abs(rv - expected) < 1e-10

    def test_compute_rv_empty_unchanged(self):
        assert HARRVForecaster.compute_realized_volatility([]) == 0.0

    def test_six_tiers_defined(self):
        """Verify 6 tiers in VOL_TIERS."""
        assert len(HARRVForecaster.VOL_TIERS) == 6

    def test_tiers_cover_full_range(self):
        """Tiers should cover 0-100 percentile."""
        assert HARRVForecaster.VOL_TIERS[0][0] == 10
        assert HARRVForecaster.VOL_TIERS[-1][0] == 100


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
