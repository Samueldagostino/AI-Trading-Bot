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

from monitoring.alerting import get_alert_manager
from monitoring.alert_templates import AlertTemplates

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
        self._last_regime: str = "unknown"
        self._high_vix_alerted: bool = False

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
            self._fire_regime_alerts("crash", current_vix)
            return "crash"

        # 2. EVENT_DRIVEN — near major news
        if near_news_event:
            self._fire_regime_alerts("event_driven", current_vix)
            return "event_driven"

        # 3. LOW_LIQUIDITY — overnight or thin volume
        if is_overnight and current_volume < avg_volume * 0.3:
            self._fire_regime_alerts("low_liquidity", current_vix)
            return "low_liquidity"

        # 4. HIGH_VOLATILITY — elevated VIX or ATR expansion
        avg_atr = np.mean(self._atr_history[-20:]) if len(self._atr_history) >= 20 else current_atr
        atr_expansion = current_atr / avg_atr if avg_atr > 0 else 1.0

        if current_vix > 25 or atr_expansion > 1.5:
            self._fire_regime_alerts("high_volatility", current_vix)
            return "high_volatility"

        # 5. TRENDING — strong directional momentum
        if trend_strength > 0.5:
            if trend_direction == "up":
                self._fire_regime_alerts("trending_up", current_vix)
                return "trending_up"
            elif trend_direction == "down":
                self._fire_regime_alerts("trending_down", current_vix)
                return "trending_down"

        # 6. RANGING — low trend strength, contained moves
        if trend_strength < 0.3 and atr_expansion < 1.2:
            regime = "ranging"
        else:
            regime = "unknown"

        # Fire alerts on regime change or high VIX
        self._fire_regime_alerts(regime, current_vix)
        return regime

    def _fire_regime_alerts(self, regime: str, current_vix: float) -> None:
        """Fire alerts on regime change or high VIX threshold crossing."""
        mgr = get_alert_manager()
        if not mgr:
            return

        # Alert on regime change (rate-limited by AlertManager)
        if regime != self._last_regime:
            mgr.enqueue(AlertTemplates.custom_alert(
                event_type="regime_change",
                title="Regime Change",
                message=f"{self._last_regime} -> {regime}",
                data={"old_regime": self._last_regime, "new_regime": regime, "vix": current_vix},
            ))
            self._last_regime = regime

        # Alert when VIX crosses above 25 (only once per crossing)
        if current_vix > 25 and not self._high_vix_alerted:
            mgr.enqueue(AlertTemplates.high_vix_alert(
                vix_level=current_vix,
                max_vix=self.config.risk.max_vix_for_trading,
            ))
            self._high_vix_alerted = True
        elif current_vix <= 25:
            self._high_vix_alerted = False

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
