"""
Session-Aware Volatility Scaling
=================================
Scales ATR-based measurements by intraday session period.

Based on the well-documented U-shaped intraday volatility pattern in
equity index futures (Andersen & Bollerslev 1997, Bollerslev, Cai & Song 2000).

Opening session:  highest vol  -- overnight information resolution, opening drive
Midday session:   lowest vol   -- liquidity trough, lunch lull
Closing session:  rising vol   -- gamma hedging, ETF rebalancing, institutional flow
ETH session:      very low vol -- thin overnight liquidity

Feature flag: SESSION_VOLATILITY_SCALING env var (default: "false").
When disabled, all scale factors return 1.0 (no-op).
"""

import os
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Feature flag -- default OFF for safety
SESSION_VOLATILITY_SCALING_ENABLED: bool = (
    os.getenv("SESSION_VOLATILITY_SCALING", "false").lower() == "true"
)


class SessionVolatilityScaler:
    """
    Scales ATR-based measurements by intraday session.

    Based on Andersen & Bollerslev (1997) U-shaped intraday
    volatility pattern for equity index futures.
    """

    SESSIONS = {
        "opening": {"start": time(9, 30), "end": time(10, 30)},
        "midday":  {"start": time(10, 30), "end": time(14, 0)},
        "closing": {"start": time(14, 0), "end": time(16, 0)},
        # ETH covers 18:00 → next day 09:29
        # Handled specially in get_session()
    }

    DEFAULT_SCALE_FACTORS = {
        "opening": 1.3,   # Vol is ~30% higher than average during open
        "midday":  0.75,  # Vol is ~25% lower than average during midday
        "closing": 1.1,   # Vol is ~10% higher than average into close
        "eth":     0.6,   # ETH vol is significantly lower
    }

    def __init__(self, scale_factors: Optional[dict] = None,
                 enabled: Optional[bool] = None):
        """
        Args:
            scale_factors: Override default scale factors. Keys must be
                           session names ("opening", "midday", "closing", "eth").
            enabled: Override the env-var feature flag. If None, reads from
                     SESSION_VOLATILITY_SCALING env var.
        """
        self.scale_factors = dict(self.DEFAULT_SCALE_FACTORS)
        if scale_factors:
            self.scale_factors.update(scale_factors)

        if enabled is not None:
            self.enabled = enabled
        else:
            self.enabled = SESSION_VOLATILITY_SCALING_ENABLED

    def get_session(self, timestamp: datetime) -> str:
        """Classify a bar timestamp into an intraday session.

        Args:
            timestamp: Bar timestamp (timezone-aware or naive UTC).

        Returns:
            One of "opening", "midday", "closing", or "eth".
        """
        if timestamp.tzinfo is None:
            # Assume UTC for naive timestamps
            from datetime import timezone
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        et_time = timestamp.astimezone(ET).time()

        # Weekend check -- treat as ETH
        et_dt = timestamp.astimezone(ET)
        if et_dt.weekday() >= 5:  # Saturday=5, Sunday=6
            return "eth"

        # RTH session checks (ordered by time)
        if time(9, 30) <= et_time < time(10, 30):
            return "opening"
        elif time(10, 30) <= et_time < time(14, 0):
            return "midday"
        elif time(14, 0) <= et_time < time(16, 0):
            return "closing"
        else:
            # Everything else is ETH: 16:00-16:30 maintenance,
            # 18:00-09:29 next day, pre-market, etc.
            return "eth"

    def get_scale_factor(self, timestamp: datetime) -> float:
        """Return the raw volatility multiplier for the current session.

        When the feature is disabled, always returns 1.0.
        """
        if not self.enabled:
            return 1.0
        session = self.get_session(timestamp)
        return self.scale_factors.get(session, 1.0)

    def scale_atr(self, atr_value: float, timestamp: datetime) -> float:
        """Scale an ATR value by the session volatility factor.

        Args:
            atr_value: Raw ATR-14 value.
            timestamp: Current bar timestamp.

        Returns:
            Scaled ATR. When disabled, returns atr_value unchanged.
        """
        return atr_value * self.get_scale_factor(timestamp)
