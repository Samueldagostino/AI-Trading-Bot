"""
Regime Detection Engine
========================
Classifies current NQ market regime for adaptive strategy behavior.

Regimes:
- TRENDING_UP / TRENDING_DOWN: Sustained directional movement
- RANGING: Mean-reverting, bounded price action
- HIGH_VOLATILITY: Elevated VIX, wide ranges, fast moves
- LOW_LIQUIDITY: Overnight/holiday thin markets
- EVENT_DRIVEN: Near FOMC/CPI/NFP or other scheduled catalysts
- CRASH: Extreme selling, circuit breaker territory
"""

import logging
import numpy as np
from datetime import datetime, timezone
from typing import Optional, List

logger = logging.getLogger(__name__)


class RegimeDetector:
    """
    Multi-factor regime classification for NQ.
    
    Uses:
    - VIX level and rate of change
    - ATR vs historical ATR (volatility expansion/contraction)
    - Trend strength (EMA slope, ADX-like)
    - Volume relative to average
    - Time of day (overnight vs regular session)
    - Economic calendar proximity
    """

    def __init__(self, config):
        self.config = config
        self._atr_history: List[float] = []
        self._vix_history: List[float] = []

    def classify(
        self,
        current_atr: float,
        current_vix: float,
        trend_direction: str,
        trend_strength: float,
        current_volume: int,
        avg_volume: float,
        is_overnight: bool,
        near_news_event: bool,
        price_change_pct: float = 0.0,
    ) -> str:
        """
        Classify current market regime.
        Returns one of the MarketRegime enum values as a string.
        """
        self._atr_history.append(current_atr)
        self._vix_history.append(current_vix)
        
        # Keep rolling history
        if len(self._atr_history) > 100:
            self._atr_history = self._atr_history[-100:]
        if len(self._vix_history) > 100:
            self._vix_history = self._vix_history[-100:]

        # === Priority checks (order matters) ===

        # 1. CRASH — extreme conditions
        if current_vix > 40 or price_change_pct < -3.0:
            logger.warning(f"CRASH regime detected: VIX={current_vix}, change={price_change_pct:.1f}%")
            return "crash"

        # 2. EVENT_DRIVEN — near major news
        if near_news_event:
            return "event_driven"

        # 3. LOW_LIQUIDITY — overnight or thin volume
        if is_overnight and current_volume < avg_volume * 0.3:
            return "low_liquidity"

        # 4. HIGH_VOLATILITY — elevated VIX or ATR expansion
        avg_atr = np.mean(self._atr_history[-20:]) if len(self._atr_history) >= 20 else current_atr
        atr_expansion = current_atr / avg_atr if avg_atr > 0 else 1.0
        
        if current_vix > 25 or atr_expansion > 1.5:
            return "high_volatility"

        # 5. TRENDING — strong directional momentum
        if trend_strength > 0.5:
            if trend_direction == "up":
                return "trending_up"
            elif trend_direction == "down":
                return "trending_down"

        # 6. RANGING — low trend strength, contained moves
        if trend_strength < 0.3 and atr_expansion < 1.2:
            return "ranging"

        return "unknown"

    def get_regime_adjustments(self, regime: str) -> dict:
        """
        Return strategy adjustments for the current regime.
        These adjustments modify signal interpretation and sizing.
        """
        adjustments = {
            "trending_up": {
                "favor_direction": "long",
                "size_multiplier": 1.0,
                "use_trailing_stop": True,
                "widen_targets": True,
                "tighten_stops": False,
                "description": "Trending up — favor longs, use trailing stops",
            },
            "trending_down": {
                "favor_direction": "short",
                "size_multiplier": 1.0,
                "use_trailing_stop": True,
                "widen_targets": True,
                "tighten_stops": False,
                "description": "Trending down — favor shorts, use trailing stops",
            },
            "ranging": {
                "favor_direction": "none",
                "size_multiplier": 0.75,
                "use_trailing_stop": False,
                "widen_targets": False,
                "tighten_stops": True,
                "description": "Ranging — reduced size, tighter targets, mean-reversion focus",
            },
            "high_volatility": {
                "favor_direction": "none",
                "size_multiplier": 0.5,
                "use_trailing_stop": True,
                "widen_targets": True,
                "tighten_stops": False,
                "description": "High volatility — half size, wider stops to survive noise",
            },
            "low_liquidity": {
                "favor_direction": "none",
                "size_multiplier": 0.5,
                "use_trailing_stop": False,
                "widen_targets": False,
                "tighten_stops": True,
                "description": "Low liquidity — half size, avoid large orders",
            },
            "event_driven": {
                "favor_direction": "none",
                "size_multiplier": 0.0,    # No new trades near events
                "use_trailing_stop": False,
                "widen_targets": False,
                "tighten_stops": True,
                "description": "Event-driven — no new trades, manage existing only",
            },
            "crash": {
                "favor_direction": "none",
                "size_multiplier": 0.0,    # Do not trade during crashes
                "use_trailing_stop": False,
                "widen_targets": False,
                "tighten_stops": True,
                "description": "CRASH — stop all trading, preserve capital",
            },
            "unknown": {
                "favor_direction": "none",
                "size_multiplier": 0.5,
                "use_trailing_stop": False,
                "widen_targets": False,
                "tighten_stops": True,
                "description": "Unknown regime — reduced size, conservative approach",
            },
        }

        return adjustments.get(regime, adjustments["unknown"])
