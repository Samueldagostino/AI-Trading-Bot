"""
MarketContext -- Frozen snapshot of external market data from QuantData.
===========================================================================
Immutable dataclass created once per refresh cycle, consumed by all downstream
components. Currently LOG-ONLY -- does NOT affect confluence scoring.

Data sources: QuantData GEX, DEX, net options flow, dark pool, volatility skew.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass(frozen=True)
class MarketContext:
    """
    Frozen snapshot of external market data from QuantData.
    Immutable -- created once per refresh cycle, consumed by all downstream.
    """
    timestamp: datetime

    # Gamma Exposure
    gamma_regime: str = "neutral"       # "positive", "negative", "neutral"
    total_gex: float = 0.0              # Raw GEX value (millions)
    gamma_flip_level: float = 0.0       # Price where gamma flips sign
    nearest_wall_above: Optional[float] = None
    nearest_wall_below: Optional[float] = None

    # Net Options Flow
    flow_direction: str = "neutral"     # "bullish", "bearish", "neutral"
    net_premium: float = 0.0            # Net call - put premium
    call_premium: float = 0.0
    put_premium: float = 0.0

    # Dark Pool
    dark_bias: str = "neutral"          # "bullish", "bearish", "neutral"
    dark_pool_levels: Optional[List[float]] = None  # Significant dark pool levels

    # Volatility Skew
    skew_regime: str = "normal"         # "normal", "elevated", "extreme"
    skew_slope: float = 0.0

    # Data Quality
    source: str = "manual"              # "api", "manual", "default"
    age_seconds: float = 0.0            # How old this snapshot is

    def is_stale(self, max_age_minutes: float = 30) -> bool:
        """Check if snapshot is older than max_age_minutes."""
        return self.age_seconds > max_age_minutes * 60

    def is_favorable_for_momentum(self) -> bool:
        """
        Negative gamma + momentum = ideal environment.
        Negative gamma: dealers are short gamma, must hedge in the direction
        of the move → momentum follows through.
        Positive gamma: dealers absorb moves → mean-reversion (unfavorable).
        """
        return self.gamma_regime == "negative"

    def aligns_with_direction(self, direction: str) -> bool:
        """Check if flow direction aligns with trade direction."""
        if direction == "long":
            return self.flow_direction == "bullish"
        elif direction == "short":
            return self.flow_direction == "bearish"
        return False

    def to_dict(self) -> dict:
        """Serialize for JSON logging."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "gamma_regime": self.gamma_regime,
            "total_gex": self.total_gex,
            "gamma_flip_level": self.gamma_flip_level,
            "nearest_wall_above": self.nearest_wall_above,
            "nearest_wall_below": self.nearest_wall_below,
            "flow_direction": self.flow_direction,
            "net_premium": self.net_premium,
            "call_premium": self.call_premium,
            "put_premium": self.put_premium,
            "dark_bias": self.dark_bias,
            "dark_pool_levels": self.dark_pool_levels or [],
            "skew_regime": self.skew_regime,
            "skew_slope": self.skew_slope,
            "source": self.source,
            "age_seconds": self.age_seconds,
            "favorable_for_momentum": self.is_favorable_for_momentum(),
        }


class NQContextTranslator:
    """
    Translates SPY/QQQ options data to NQ/MNQ futures context.

    QQQ is the PRIMARY reference for NQ (QQQ tracks NDX, NQ is NDX futures).
    SPY is SECONDARY (confirms via cross-index correlation ~0.85).
    """

    # Approximate QQQ-to-NQ multiplier.
    # NQ ≈ QQQ × 40 (e.g., QQQ $500 ≈ NQ 20,000).
    # Used for gamma wall LEVELS only, not for trading.
    QQQ_TO_NQ_MULTIPLIER = 40.0

    def translate(
        self,
        qqq_context: MarketContext,
        spy_context: Optional[MarketContext] = None,
    ) -> MarketContext:
        """
        Produce a single NQ-relevant MarketContext from QQQ + SPY data.
        QQQ data takes priority. SPY confirms or conflicts.
        """
        import logging
        logger = logging.getLogger(__name__)

        nq_context = MarketContext(
            timestamp=qqq_context.timestamp,
            gamma_regime=qqq_context.gamma_regime,
            total_gex=qqq_context.total_gex,
            gamma_flip_level=self._qqq_to_nq_price(qqq_context.gamma_flip_level),
            nearest_wall_above=self._qqq_to_nq_price(qqq_context.nearest_wall_above),
            nearest_wall_below=self._qqq_to_nq_price(qqq_context.nearest_wall_below),
            flow_direction=qqq_context.flow_direction,
            net_premium=qqq_context.net_premium,
            call_premium=qqq_context.call_premium,
            put_premium=qqq_context.put_premium,
            dark_bias=qqq_context.dark_bias,
            dark_pool_levels=[
                self._qqq_to_nq_price(level)
                for level in (qqq_context.dark_pool_levels or [])
                if level is not None
            ] or None,
            skew_regime=qqq_context.skew_regime,
            skew_slope=qqq_context.skew_slope,
            source=qqq_context.source,
            age_seconds=qqq_context.age_seconds,
        )

        # If SPY context available and conflicts with QQQ, log warning
        if spy_context and spy_context.gamma_regime != qqq_context.gamma_regime:
            logger.warning(
                "SPY gamma (%s) conflicts with QQQ gamma (%s) -- using QQQ",
                spy_context.gamma_regime,
                qqq_context.gamma_regime,
            )

        return nq_context

    def _qqq_to_nq_price(self, qqq_price: Optional[float]) -> Optional[float]:
        """
        Approximate QQQ price to NQ futures price.
        Used for gamma wall LEVELS, not for trading decisions.
        """
        if qqq_price is None or qqq_price == 0.0:
            return None
        return round(qqq_price * self.QQQ_TO_NQ_MULTIPLIER, 2)
