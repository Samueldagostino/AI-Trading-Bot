"""
HAR-RV (Heterogeneous AutoRegressive - Realized Volatility) Forecaster.

Implements the HAR-RV model for volatility forecasting using three horizons:
daily, weekly (5-day), and monthly (22-day) realized volatility components.
"""

import math
from collections import deque


class HARRVForecaster:
    """
    HAR-RV volatility forecaster.

    Maintains rolling windows of realized volatility at daily, weekly, and
    monthly horizons and produces one-step-ahead forecasts using:
        RV(t+1) = alpha + beta_d * RV_daily + beta_w * RV_weekly + beta_m * RV_monthly
    """

    MIN_HISTORY = 22  # Minimum days of data before forecasting

    def __init__(self, alpha=0.0, beta_d=0.4, beta_w=0.35, beta_m=0.25):
        self.alpha = alpha
        self.beta_d = beta_d
        self.beta_w = beta_w
        self.beta_m = beta_m

        # Rolling windows for RV values
        self._daily_history = deque(maxlen=22)  # stores daily RV values
        self._forecast_history = []  # all forecasts for percentile calc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def compute_realized_volatility(five_min_returns: list) -> float:
        """
        Compute realized volatility from intraday 5-minute returns.

        RV = sum of squared returns.

        Args:
            five_min_returns: List of 5-minute log returns for one day.

        Returns:
            Realized volatility (sum of squared returns).
        """
        if not five_min_returns:
            return 0.0
        return sum(r ** 2 for r in five_min_returns)

    def update(self, daily_rv: float) -> None:
        """
        Add a new daily realized volatility observation.

        Args:
            daily_rv: The realized volatility for one trading day.
        """
        self._daily_history.append(daily_rv)

    @property
    def rv_daily(self) -> float:
        """Current day RV (most recent observation)."""
        if not self._daily_history:
            return 0.0
        return self._daily_history[-1]

    @property
    def rv_weekly(self) -> float:
        """5-day average RV."""
        if len(self._daily_history) < 5:
            return 0.0
        recent_5 = list(self._daily_history)[-5:]
        return sum(recent_5) / 5.0

    @property
    def rv_monthly(self) -> float:
        """22-day average RV."""
        if len(self._daily_history) < 22:
            return 0.0
        recent_22 = list(self._daily_history)[-22:]
        return sum(recent_22) / 22.0

    @property
    def has_enough_data(self) -> bool:
        """Whether we have at least MIN_HISTORY days of data."""
        return len(self._daily_history) >= self.MIN_HISTORY

    def forecast(self) -> float:
        """
        Produce one-step-ahead RV forecast.

        RV(t+1) = alpha + beta_d * RV_daily + beta_w * RV_weekly + beta_m * RV_monthly

        Returns:
            Forecasted realized volatility, or 0.0 if insufficient data.
        """
        if not self.has_enough_data:
            return 0.0

        fc = (
            self.alpha
            + self.beta_d * self.rv_daily
            + self.beta_w * self.rv_weekly
            + self.beta_m * self.rv_monthly
        )
        self._forecast_history.append(fc)
        return fc

    def get_volatility_modifier(self) -> dict:
        """
        Return position-size and stop-distance modifiers based on current
        volatility regime relative to historical distribution.

        Returns:
            dict with keys 'position' and 'stop':
            - High vol (top 25% historical): {"position": 0.85, "stop": 1.2}
            - Low vol (bottom 25%):          {"position": 1.15, "stop": 0.85}
            - Normal:                        {"position": 1.0, "stop": 1.0}
        """
        if not self.has_enough_data or not self._forecast_history:
            return {"position": 1.0, "stop": 1.0}

        current_forecast = self._forecast_history[-1]
        percentile = self._compute_percentile(current_forecast)

        if percentile >= 75:
            # High volatility regime
            return {"position": 0.85, "stop": 1.2}
        elif percentile <= 25:
            # Low volatility regime
            return {"position": 1.15, "stop": 0.85}
        else:
            # Normal volatility regime
            return {"position": 1.0, "stop": 1.0}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_percentile(self, value: float) -> float:
        """Compute the percentile rank of a value within forecast history."""
        if not self._forecast_history:
            return 50.0
        sorted_history = sorted(self._forecast_history)
        n = len(sorted_history)
        count_below = sum(1 for v in sorted_history if v < value)
        count_equal = sum(1 for v in sorted_history if v == value)
        # Percentile rank using midpoint method
        percentile = ((count_below + 0.5 * count_equal) / n) * 100
        return percentile
