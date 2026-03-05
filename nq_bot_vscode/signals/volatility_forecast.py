"""
HAR-RV (Heterogeneous AutoRegressive - Realized Volatility) Forecaster.

Implements the HAR-RV model for volatility forecasting using three horizons:
daily, weekly (5-day), and monthly (22-day) realized volatility components.

Upgrade (Clements & Preve 2021):
  - Uses log(RV) instead of raw RV to prevent negative forecasts and handle outliers
  - Wider modifier range (0.50x-1.40x) reflecting PRIMARY modifier status
  - 6-tier percentile-based classification
"""

import math
from collections import deque


class HARRVForecaster:
    """
    HAR-RV volatility forecaster.

    Maintains rolling windows of realized volatility at daily, weekly, and
    monthly horizons and produces one-step-ahead forecasts using log(RV):
        log_RV(t+1) = alpha + beta_d * log_RV_daily + beta_w * log_RV_weekly + beta_m * log_RV_monthly

    HAR-RV is the PRIMARY modifier (widest range: 0.50x - 1.40x).
    Justified by 2,100+ citations, proven across every asset class.
    """

    MIN_HISTORY = 22  # Minimum days of data before forecasting

    # 6-tier volatility modifier table (PRIMARY -- widest range)
    VOL_TIERS = [
        # (max_percentile, position_mult, stop_mult, label)
        (10,  1.40, 0.70, "very low vol"),
        (25,  1.20, 0.85, "low vol"),
        (50,  1.10, 0.95, "below avg vol"),
        (75,  0.90, 1.10, "above avg vol"),
        (90,  0.75, 1.30, "high vol"),
        (100, 0.50, 1.50, "extreme vol"),
    ]

    def __init__(self, alpha=0.0, beta_d=0.4, beta_w=0.35, beta_m=0.25):
        self.alpha = alpha
        self.beta_d = beta_d
        self.beta_w = beta_w
        self.beta_m = beta_m

        # Rolling windows for RV values
        self._daily_history = deque(maxlen=22)  # stores daily RV values
        self._forecast_history = deque(maxlen=252)  # ~1 year of trading days for percentile calc
        self._log_forecast_history = deque(maxlen=252)

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

    @staticmethod
    def compute_log_rv(five_min_returns: list) -> float:
        """
        Compute log(RV) from intraday 5-minute returns.
        Per Clements & Preve (2021): log transform prevents negative forecasts.

        Args:
            five_min_returns: List of 5-minute log returns for one day.

        Returns:
            log(RV), or -inf if RV is 0.
        """
        rv = sum(r ** 2 for r in five_min_returns) if five_min_returns else 0.0
        if rv <= 0:
            return float('-inf')
        return math.log(rv)

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
    def log_rv_daily(self) -> float:
        """log(RV) of current day."""
        rv = self.rv_daily
        if rv <= 0:
            return float('-inf')
        return math.log(rv)

    @property
    def log_rv_weekly(self) -> float:
        """log of 5-day average RV."""
        rv = self.rv_weekly
        if rv <= 0:
            return float('-inf')
        return math.log(rv)

    @property
    def log_rv_monthly(self) -> float:
        """log of 22-day average RV."""
        rv = self.rv_monthly
        if rv <= 0:
            return float('-inf')
        return math.log(rv)

    @property
    def has_enough_data(self) -> bool:
        """Whether we have at least MIN_HISTORY days of data."""
        return len(self._daily_history) >= self.MIN_HISTORY

    def forecast(self) -> float:
        """
        Produce one-step-ahead RV forecast using log(RV).

        log_RV(t+1) = alpha + beta_d * log_RV_daily + beta_w * log_RV_weekly + beta_m * log_RV_monthly
        RV(t+1) = exp(log_RV(t+1))

        Returns:
            Forecasted realized volatility (in original scale), or 0.0 if insufficient data.
        """
        if not self.has_enough_data:
            return 0.0

        log_d = self.log_rv_daily
        log_w = self.log_rv_weekly
        log_m = self.log_rv_monthly

        # If any log value is -inf (zero RV), fall back to raw forecast
        if any(math.isinf(v) for v in (log_d, log_w, log_m)):
            fc = (
                self.alpha
                + self.beta_d * self.rv_daily
                + self.beta_w * self.rv_weekly
                + self.beta_m * self.rv_monthly
            )
            self._forecast_history.append(fc)
            self._log_forecast_history.append(fc)
            return fc

        log_fc = (
            self.alpha
            + self.beta_d * log_d
            + self.beta_w * log_w
            + self.beta_m * log_m
        )

        self._log_forecast_history.append(log_fc)

        # Convert back from log scale
        fc = math.exp(log_fc) if math.isfinite(log_fc) else 0.0
        self._forecast_history.append(fc)
        return fc

    def get_volatility_modifier(self) -> dict:
        """
        Return position-size and stop-distance modifiers based on current
        volatility regime relative to historical distribution.

        Uses 6-tier percentile-based classification (PRIMARY modifier):
          0-10:   very low vol    -> position 1.40x, stop 0.70x
          10-25:  low vol         -> position 1.20x, stop 0.85x
          25-50:  below avg vol   -> position 1.10x, stop 0.95x
          50-75:  above avg vol   -> position 0.90x, stop 1.10x
          75-90:  high vol        -> position 0.75x, stop 1.30x
          90-100: extreme vol     -> position 0.50x, stop 1.50x
        """
        if not self.has_enough_data or not self._log_forecast_history:
            return {"position": 1.0, "stop": 1.0, "reason": "Insufficient data",
                    "percentile": None, "log_rv": None}

        current = self._log_forecast_history[-1]
        percentile = self._compute_percentile_log(current)

        for max_pct, pos_mult, stop_mult, label in self.VOL_TIERS:
            if percentile <= max_pct:
                log_rv_val = round(current, 2) if math.isfinite(current) else None
                reason = f"RV {int(percentile)}th percentile ({label})"
                if log_rv_val is not None:
                    reason += f", log_rv={log_rv_val}"
                return {
                    "position": pos_mult,
                    "stop": stop_mult,
                    "reason": reason,
                    "percentile": round(percentile, 1),
                    "log_rv": log_rv_val,
                }

        # Fallback (shouldn't reach here)
        return {"position": 1.0, "stop": 1.0, "reason": "Normal",
                "percentile": round(percentile, 1), "log_rv": None}

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

    def _compute_percentile_log(self, value: float) -> float:
        """Compute percentile rank within log forecast history."""
        if not self._log_forecast_history:
            return 50.0
        sorted_history = sorted(self._log_forecast_history)
        n = len(sorted_history)
        count_below = sum(1 for v in sorted_history if v < value)
        count_equal = sum(1 for v in sorted_history if v == value)
        percentile = ((count_below + 0.5 * count_equal) / n) * 100
        return percentile
