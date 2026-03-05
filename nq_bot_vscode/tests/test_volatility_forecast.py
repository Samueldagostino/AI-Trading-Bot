"""Tests for HARRVForecaster."""

import pytest
from signals.volatility_forecast import HARRVForecaster


class TestComputeRealizedVolatility:
    def test_basic_computation(self):
        returns = [0.01, -0.02, 0.015, -0.005, 0.03]
        rv = HARRVForecaster.compute_realized_volatility(returns)
        expected = 0.01**2 + 0.02**2 + 0.015**2 + 0.005**2 + 0.03**2
        assert abs(rv - expected) < 1e-10

    def test_empty_returns(self):
        assert HARRVForecaster.compute_realized_volatility([]) == 0.0

    def test_single_return(self):
        rv = HARRVForecaster.compute_realized_volatility([0.05])
        assert abs(rv - 0.0025) < 1e-10

    def test_all_zero_returns(self):
        assert HARRVForecaster.compute_realized_volatility([0.0, 0.0, 0.0]) == 0.0

    def test_negative_returns(self):
        returns = [-0.01, -0.02, -0.03]
        rv = HARRVForecaster.compute_realized_volatility(returns)
        expected = 0.01**2 + 0.02**2 + 0.03**2
        assert abs(rv - expected) < 1e-10


class TestRollingWindows:
    def test_rv_daily_no_data(self):
        f = HARRVForecaster()
        assert f.rv_daily == 0.0

    def test_rv_daily_single_update(self):
        f = HARRVForecaster()
        f.update(0.001)
        assert f.rv_daily == 0.001

    def test_rv_weekly_insufficient_data(self):
        f = HARRVForecaster()
        for i in range(4):
            f.update(0.001 * (i + 1))
        assert f.rv_weekly == 0.0  # Need at least 5

    def test_rv_weekly_exact_5_days(self):
        f = HARRVForecaster()
        values = [0.001, 0.002, 0.003, 0.004, 0.005]
        for v in values:
            f.update(v)
        expected = sum(values) / 5.0
        assert abs(f.rv_weekly - expected) < 1e-10

    def test_rv_monthly_insufficient_data(self):
        f = HARRVForecaster()
        for i in range(21):
            f.update(0.001)
        assert f.rv_monthly == 0.0  # Need 22

    def test_rv_monthly_exact_22_days(self):
        f = HARRVForecaster()
        for i in range(22):
            f.update(0.001 * (i + 1))
        values = [0.001 * (i + 1) for i in range(22)]
        expected = sum(values) / 22.0
        assert abs(f.rv_monthly - expected) < 1e-10

    def test_rolling_window_max_size(self):
        """Ensure deque doesn't grow beyond 22 entries."""
        f = HARRVForecaster()
        for i in range(50):
            f.update(float(i))
        # rv_daily should be the last value
        assert f.rv_daily == 49.0
        # rv_weekly should be avg of last 5
        expected_weekly = sum(range(45, 50)) / 5.0
        assert abs(f.rv_weekly - expected_weekly) < 1e-10


class TestHasEnoughData:
    def test_not_enough(self):
        f = HARRVForecaster()
        for i in range(21):
            f.update(0.001)
        assert f.has_enough_data is False

    def test_exactly_enough(self):
        f = HARRVForecaster()
        for i in range(22):
            f.update(0.001)
        assert f.has_enough_data is True

    def test_more_than_enough(self):
        f = HARRVForecaster()
        for i in range(30):
            f.update(0.001)
        assert f.has_enough_data is True


class TestForecast:
    def test_forecast_insufficient_data(self):
        f = HARRVForecaster()
        for i in range(10):
            f.update(0.001)
        assert f.forecast() == 0.0

    def test_forecast_with_enough_data(self):
        import math
        f = HARRVForecaster()
        for i in range(22):
            f.update(0.001)
        fc = f.forecast()
        # With log(RV): log(0.001) = -6.9078
        # log_fc = 0 + 0.4*(-6.9078) + 0.35*(-6.9078) + 0.25*(-6.9078) = -6.9078
        # fc = exp(-6.9078) = 0.001
        expected = math.exp(math.log(0.001))  # Should be ~0.001
        assert abs(fc - expected) < 1e-10

    def test_forecast_stores_history(self):
        f = HARRVForecaster()
        for i in range(22):
            f.update(0.001)
        f.forecast()
        assert len(f._forecast_history) == 1
        f.forecast()
        assert len(f._forecast_history) == 2

    def test_forecast_varying_rv(self):
        f = HARRVForecaster(alpha=0.0, beta_d=0.4, beta_w=0.35, beta_m=0.25)
        # Create varying data: lower early, higher later
        for i in range(22):
            f.update(0.001 + i * 0.0001)
        fc = f.forecast()
        # rv_daily = last value (0.001 + 21*0.0001 = 0.0031)
        # rv_weekly = avg of last 5
        # rv_monthly = avg of all 22
        assert fc > 0

    def test_custom_coefficients(self):
        import math
        f = HARRVForecaster(alpha=0.5, beta_d=0.3, beta_w=0.3, beta_m=0.4)
        for i in range(22):
            f.update(1.0)
        fc = f.forecast()
        # With log(RV): log(1.0)=0, so log_fc = 0.5 + 0 + 0 + 0 = 0.5
        # fc = exp(0.5) = 1.6487...
        expected = math.exp(0.5)
        assert abs(fc - expected) < 1e-4


class TestVolatilityModifier:
    def test_no_data_returns_neutral(self):
        f = HARRVForecaster()
        mod = f.get_volatility_modifier()
        assert mod["position"] == 1.0
        assert mod["stop"] == 1.0

    def test_no_forecast_history_returns_neutral(self):
        f = HARRVForecaster()
        for i in range(22):
            f.update(0.001)
        # has_enough_data is True but no forecast() called yet
        mod = f.get_volatility_modifier()
        assert mod["position"] == 1.0
        assert mod["stop"] == 1.0

    def test_high_volatility_regime(self):
        """When current forecast is in top percentiles, reduce size, widen stops."""
        f = HARRVForecaster()
        # Build history with gradually increasing volatility
        for i in range(22):
            f.update(0.001)
        # Generate many low forecasts first
        for _ in range(30):
            f.forecast()
        # Now add high volatility days and forecast again
        for _ in range(5):
            f.update(0.010)  # 10x higher
        fc = f.forecast()
        mod = f.get_volatility_modifier()
        # With wider range: high vol -> position <= 0.90, stop >= 1.10
        assert mod["position"] <= 0.90
        assert mod["stop"] >= 1.10

    def test_low_volatility_regime(self):
        """When current forecast is in bottom percentiles, increase size, tighten stops."""
        f = HARRVForecaster()
        # Start with high volatility
        for i in range(22):
            f.update(0.010)
        # Generate many high forecasts
        for _ in range(30):
            f.forecast()
        # Now switch to very low volatility
        for _ in range(5):
            f.update(0.0001)
        fc = f.forecast()
        mod = f.get_volatility_modifier()
        # With wider range: low vol -> position >= 1.10, stop <= 0.95
        assert mod["position"] >= 1.10
        assert mod["stop"] <= 0.95

    def test_normal_volatility_regime(self):
        """When forecast is in the middle percentiles, return near-neutral."""
        f = HARRVForecaster()
        # All the same values -> percentile ~50%
        for i in range(22):
            f.update(0.001)
        f.forecast()
        # Add small number of forecasts with same level
        for _ in range(10):
            f.forecast()
        mod = f.get_volatility_modifier()
        # 25-50 percentile range: position 1.10, stop 0.95
        # or 50-75 range: position 0.90, stop 1.10
        # With identical values, midpoint ~50% -> could be either tier
        assert 0.85 <= mod["position"] <= 1.15
        assert 0.90 <= mod["stop"] <= 1.15


class TestEdgeCases:
    def test_zero_rv_values(self):
        f = HARRVForecaster()
        for i in range(22):
            f.update(0.0)
        fc = f.forecast()
        assert fc == 0.0

    def test_large_rv_values(self):
        f = HARRVForecaster()
        for i in range(22):
            f.update(100.0)
        fc = f.forecast()
        expected = 0.0 + 0.4 * 100 + 0.35 * 100 + 0.25 * 100
        assert abs(fc - expected) < 1e-6

    def test_min_history_constant(self):
        assert HARRVForecaster.MIN_HISTORY == 22
