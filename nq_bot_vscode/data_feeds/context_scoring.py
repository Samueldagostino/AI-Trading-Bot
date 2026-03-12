"""
Context Scorer — Converts MarketContext into confluence score adjustments.
==========================================================================

STATUS: DISABLED. Enable only after analyze_quantdata_correlation.py
confirms statistical significance at p < 0.05.

To enable: set QUANTDATA_SCORING=true in .env AND change ENABLED below.
The .env flag alone is NOT sufficient — this is a safety measure to prevent
accidental activation before validation.
"""

import logging
from typing import Optional

from data_feeds.market_context import MarketContext

logger = logging.getLogger(__name__)


class ContextScorer:
    """
    Converts MarketContext into confluence score adjustments.

    STATUS: DISABLED. Enable only after analyze_quantdata_correlation.py
    confirms statistical significance at p < 0.05.

    To enable: set QUANTDATA_SCORING=true in .env
    """

    # HARD-CODED OFF. Change only after validation with 100+ paper trades.
    ENABLED = False

    # Scoring adjustments (will be calibrated from correlation analysis)
    GAMMA_NEGATIVE_BOOST = 0.10     # Add to score when gamma is negative
    GAMMA_POSITIVE_PENALTY = -0.05  # Subtract when gamma is positive
    FLOW_ALIGNED_BOOST = 0.05       # Add when flow matches trade direction
    DARK_POOL_ALIGNED_BOOST = 0.03  # Add when dark pool bias matches

    def score_adjustment(
        self,
        context: Optional[MarketContext],
        direction: str,
    ) -> float:
        """
        Calculate total score adjustment from market context.
        Returns 0.0 if disabled or no context available.
        """
        if not self.ENABLED:
            return 0.0

        if context is None:
            return 0.0

        adjustment = 0.0

        # Gamma regime adjustment
        if context.gamma_regime == "negative":
            adjustment += self.GAMMA_NEGATIVE_BOOST
        elif context.gamma_regime == "positive":
            adjustment += self.GAMMA_POSITIVE_PENALTY

        # Flow alignment
        if context.aligns_with_direction(direction):
            adjustment += self.FLOW_ALIGNED_BOOST

        # Dark pool alignment
        direction_as_bias = "bullish" if direction == "long" else "bearish"
        if context.dark_bias == direction_as_bias:
            adjustment += self.DARK_POOL_ALIGNED_BOOST

        return round(adjustment, 3)
