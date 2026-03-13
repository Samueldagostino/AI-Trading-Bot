"""
Signal Aggregator
==================
Combines multiple independent signal sources into a single
trade decision with confidence scoring.

Signal Sources:
1. Discord bias (weighted at 25%)
2. Technical features -- OB, FVG, sweeps, VWAP, delta (weighted at 50%)
3. ML model predictions (weighted at 25%) -- placeholder for future

CRITICAL RULE: A trade requires minimum 3 independent signals
agreeing on direction. Discord alone NEVER triggers a trade.
"""

import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional, List, Dict, TYPE_CHECKING
from enum import Enum

if TYPE_CHECKING:
    from nq_bot_vscode.ml.predictor import MLPredictor

logger = logging.getLogger(__name__)


class SignalDirection(Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


@dataclass
class IndividualSignal:
    """A single signal from one source."""
    name: str
    direction: SignalDirection
    strength: float          # 0.0 to 1.0
    source_category: str     # 'discord', 'technical', 'ml'
    timestamp: datetime
    metadata: dict = field(default_factory=dict)


@dataclass
class AggregatedSignal:
    """Final aggregated trade signal with confluence score."""
    timestamp: datetime
    direction: SignalDirection
    
    # Scores
    discord_score: float
    technical_score: float
    ml_score: float
    combined_score: float
    
    # Contributing signals
    contributing_signals: List[IndividualSignal]
    num_signals_aligned: int
    
    # Trade parameters (filled by risk engine)
    should_trade: bool
    rejection_reason: str = ""

    # Structural stop price (tightest from contributing signals)
    structural_stop_price: Optional[float] = None


class SignalAggregator:
    """
    Aggregates signals from all sources and computes confluence score.
    
    The aggregator does NOT make risk decisions -- it only determines
    direction and confidence. The risk engine decides sizing and approval.
    """

    def __init__(self, config, ml_predictor: Optional["MLPredictor"] = None):
        self.config = config.signals
        self._ml_predictor = ml_predictor
        self._last_signal_time: Optional[datetime] = None
        self._signal_history: List[AggregatedSignal] = []
        self._htf_blocked_count: int = 0

    def aggregate(
        self,
        discord_signal: Optional[object] = None,
        feature_snapshot: Optional[object] = None,
        ml_prediction: Optional[dict] = None,
        htf_bias: Optional[object] = None,
        current_time: Optional[datetime] = None,
        adaptive_hc_gate: Optional[float] = None,
        cross_signal_boost: float = 0.0,
    ) -> Optional[AggregatedSignal]:
        """
        Aggregate all available signals into a single trade decision.
        
        Args:
            discord_signal: DiscordBiasSignal from the listener
            feature_snapshot: FeatureSnapshot from the feature engine
            ml_prediction: Dict with 'direction' and 'confidence' keys
            current_time: Current timestamp
            
        Returns:
            AggregatedSignal if actionable, None if no trade
        """
        if current_time is None:
            current_time = datetime.now(timezone.utc)

        # Auto-generate ML prediction when predictor is available
        if ml_prediction is None and self._ml_predictor is not None:
            try:
                pred = self._ml_predictor.predict(
                    feature_snapshot, htf_bias,
                )
                if pred.direction != "neutral":
                    ml_prediction = {
                        "direction": pred.direction,
                        "confidence": pred.confidence,
                    }
            except Exception as e:
                logger.debug("ML prediction failed: %s", e)

        signals: List[IndividualSignal] = []

        # === 1. Discord Signals ===
        if discord_signal and discord_signal.bias in ("bullish", "bearish"):
            direction = (SignalDirection.LONG if discord_signal.bias == "bullish" 
                        else SignalDirection.SHORT)
            signals.append(IndividualSignal(
                name="discord_bias",
                direction=direction,
                strength=discord_signal.confidence,
                source_category="discord",
                timestamp=discord_signal.timestamp,
                metadata={
                    "author": discord_signal.author_name,
                    "reliability": discord_signal.author_reliability,
                    "keywords": discord_signal.matched_keywords,
                },
            ))

        # === 2. Technical Signals ===
        if feature_snapshot:
            tech_signals = self._extract_technical_signals(feature_snapshot, current_time)
            signals.extend(tech_signals)

        # === 3. ML Signals ===
        if ml_prediction:
            direction = (SignalDirection.LONG if ml_prediction.get("direction") == "long"
                        else SignalDirection.SHORT if ml_prediction.get("direction") == "short"
                        else SignalDirection.NEUTRAL)
            if direction != SignalDirection.NEUTRAL:
                signals.append(IndividualSignal(
                    name="ml_model",
                    direction=direction,
                    strength=ml_prediction.get("confidence", 0.5),
                    source_category="ml",
                    timestamp=current_time,
                    metadata=ml_prediction,
                ))

        # === Not enough signals -- no trade ===
        if len(signals) < 2:
            return None

        # === Count alignment ===
        long_signals = [s for s in signals if s.direction == SignalDirection.LONG]
        short_signals = [s for s in signals if s.direction == SignalDirection.SHORT]

        long_count = len(long_signals)
        short_count = len(short_signals)

        # Determine dominant direction
        if long_count > short_count and long_count >= self.config.min_signals_aligned:
            direction = SignalDirection.LONG
            aligned_signals = long_signals
        elif short_count > long_count and short_count >= self.config.min_signals_aligned:
            direction = SignalDirection.SHORT
            aligned_signals = short_signals
        else:
            # No clear consensus -- no trade
            return None

        # === Compute weighted scores by category ===
        discord_scores = [s.strength for s in aligned_signals if s.source_category == "discord"]
        tech_scores = [s.strength for s in aligned_signals if s.source_category == "technical"]
        ml_scores = [s.strength for s in aligned_signals if s.source_category == "ml"]

        discord_avg = sum(discord_scores) / len(discord_scores) if discord_scores else 0.0
        tech_avg = sum(tech_scores) / len(tech_scores) if tech_scores else 0.0
        ml_avg = sum(ml_scores) / len(ml_scores) if ml_scores else 0.0

        # Weighted combination
        combined = (
            discord_avg * self.config.discord_weight +
            tech_avg * self.config.technical_weight +
            ml_avg * self.config.ml_weight
        )

        # Normalize: if a category is missing, redistribute its weight
        active_weight = 0.0
        if discord_scores:
            active_weight += self.config.discord_weight
        if tech_scores:
            active_weight += self.config.technical_weight
        if ml_scores:
            active_weight += self.config.ml_weight
        
        if active_weight > 0:
            combined = combined / active_weight  # Re-normalize to 0-1

        # === V1.3.3: Apply cross-signal synergy boost ===
        if cross_signal_boost > 0:
            combined += cross_signal_boost

        combined = round(min(combined, 1.0), 3)

        # === Minimum confluence check ===
        # V1.3.3: Use adaptive HC gate if provided, else static config value
        effective_min_score = (
            adaptive_hc_gate if adaptive_hc_gate is not None
            else self.config.min_confluence_score
        )
        should_trade = (
            combined >= effective_min_score and
            len(aligned_signals) >= self.config.min_signals_aligned
        )

        rejection_reason = ""
        if not should_trade:
            if combined < effective_min_score:
                rejection_reason = f"Confluence score too low: {combined:.3f} < {effective_min_score:.3f}"
            elif len(aligned_signals) < self.config.min_signals_aligned:
                rejection_reason = f"Insufficient aligned signals: {len(aligned_signals)} < {self.config.min_signals_aligned}"

        # === HARD RULE: Discord alone cannot trigger ===
        if should_trade and len(tech_scores) == 0 and len(ml_scores) == 0:
            should_trade = False
            rejection_reason = "Discord signal alone -- requires technical or ML confluence"

        # === HTF BIAS GATE ===
        # CRITICAL: fail-safe -- if HTF data is unavailable, block all trades.
        # The HTF gate provides 84% of the system's edge; trading without it
        # is trading blind.  Never default to allowing trades.
        if should_trade:
            if htf_bias is None:
                should_trade = False
                rejection_reason = (
                    "HTF data unavailable -- blocking trade (fail-safe)"
                )
                self._htf_blocked_count += 1
                logger.warning("HTF FAIL-SAFE: no HTF data, blocking %s signal", direction.value)
            elif direction == SignalDirection.LONG and not htf_bias.htf_allows_long:
                should_trade = False
                rejection_reason = (
                    f"HTF bias blocks long: {htf_bias.consensus_direction} "
                    f"({htf_bias.consensus_strength:.2f})"
                )
                self._htf_blocked_count += 1
            elif direction == SignalDirection.SHORT and not htf_bias.htf_allows_short:
                should_trade = False
                rejection_reason = (
                    f"HTF bias blocks short: {htf_bias.consensus_direction} "
                    f"({htf_bias.consensus_strength:.2f})"
                )
                self._htf_blocked_count += 1

        # Find tightest structural stop from contributing signals
        structural_stops = [
            s.metadata.get("structural_stop_price")
            for s in aligned_signals
            if s.metadata.get("structural_stop_price") is not None
        ]
        tightest_structural = None
        if structural_stops:
            if direction == SignalDirection.LONG:
                tightest_structural = max(structural_stops)  # Highest = closest to entry
            else:
                tightest_structural = min(structural_stops)  # Lowest = closest to entry

        signal = AggregatedSignal(
            timestamp=current_time,
            direction=direction,
            discord_score=round(discord_avg, 3),
            technical_score=round(tech_avg, 3),
            ml_score=round(ml_avg, 3),
            combined_score=combined,
            contributing_signals=aligned_signals,
            num_signals_aligned=len(aligned_signals),
            should_trade=should_trade,
            rejection_reason=rejection_reason,
            structural_stop_price=tightest_structural,
        )

        self._signal_history.append(signal)
        
        if should_trade:
            logger.info(
                f"TRADE SIGNAL: {direction.value} | score={combined:.3f} | "
                f"aligned={len(aligned_signals)} | "
                f"discord={discord_avg:.2f} tech={tech_avg:.2f} ml={ml_avg:.2f}"
            )
        
        return signal

    def _extract_technical_signals(
        self, snapshot, current_time: datetime
    ) -> List[IndividualSignal]:
        """
        Convert FeatureSnapshot into individual directional signals.
        Each feature is an independent signal source.
        """
        signals = []

        # --- Order Block proximity ---
        if snapshot.near_bullish_ob:
            signals.append(IndividualSignal(
                name="bullish_order_block",
                direction=SignalDirection.LONG,
                strength=0.75,
                source_category="technical",
                timestamp=current_time,
                metadata={"feature": "OB", "type": "bullish",
                          "structural_stop_price": snapshot.structural_stop_long},
            ))

        if snapshot.near_bearish_ob:
            signals.append(IndividualSignal(
                name="bearish_order_block",
                direction=SignalDirection.SHORT,
                strength=0.75,
                source_category="technical",
                timestamp=current_time,
                metadata={"feature": "OB", "type": "bearish",
                          "structural_stop_price": snapshot.structural_stop_short},
            ))

        # --- Fair Value Gap ---
        if snapshot.inside_bullish_fvg:
            signals.append(IndividualSignal(
                name="bullish_fvg",
                direction=SignalDirection.LONG,
                strength=0.70,
                source_category="technical",
                timestamp=current_time,
                metadata={"feature": "FVG", "type": "bullish",
                          "structural_stop_price": snapshot.structural_stop_long},
            ))

        if snapshot.inside_bearish_fvg:
            signals.append(IndividualSignal(
                name="bearish_fvg",
                direction=SignalDirection.SHORT,
                strength=0.70,
                source_category="technical",
                timestamp=current_time,
                metadata={"feature": "FVG", "type": "bearish",
                          "structural_stop_price": snapshot.structural_stop_short},
            ))

        # --- Liquidity Sweeps ---
        # Buy-side sweep (swept highs) = bearish signal (smart money sold)
        if snapshot.recent_buy_sweep:
            signals.append(IndividualSignal(
                name="buy_side_sweep",
                direction=SignalDirection.SHORT,
                strength=0.80,
                source_category="technical",
                timestamp=current_time,
                metadata={"feature": "sweep", "type": "buy_side",
                          "structural_stop_price": snapshot.structural_stop_short},
            ))

        # Sell-side sweep (swept lows) = bullish signal (smart money bought)
        if snapshot.recent_sell_sweep:
            signals.append(IndividualSignal(
                name="sell_side_sweep",
                direction=SignalDirection.LONG,
                strength=0.80,
                source_category="technical",
                timestamp=current_time,
                metadata={"feature": "sweep", "type": "sell_side",
                          "structural_stop_price": snapshot.structural_stop_long},
            ))

        # --- VWAP ---
        if snapshot.price_vs_vwap != 0 and snapshot.session_vwap > 0:
            if snapshot.price_vs_vwap < -5:  # Price well below VWAP -- mean reversion long
                signals.append(IndividualSignal(
                    name="vwap_below",
                    direction=SignalDirection.LONG,
                    strength=min(abs(snapshot.price_vs_vwap) / 20, 0.8),
                    source_category="technical",
                    timestamp=current_time,
                    metadata={"feature": "VWAP", "deviation": snapshot.price_vs_vwap},
                ))
            elif snapshot.price_vs_vwap > 5:  # Price well above VWAP -- mean reversion short
                signals.append(IndividualSignal(
                    name="vwap_above",
                    direction=SignalDirection.SHORT,
                    strength=min(abs(snapshot.price_vs_vwap) / 20, 0.8),
                    source_category="technical",
                    timestamp=current_time,
                    metadata={"feature": "VWAP", "deviation": snapshot.price_vs_vwap},
                ))

        # --- Delta Divergence ---
        if snapshot.delta_divergence:
            # If price up but delta negative, bearish divergence
            if snapshot.cumulative_delta < 0:
                signals.append(IndividualSignal(
                    name="delta_divergence_bearish",
                    direction=SignalDirection.SHORT,
                    strength=0.65,
                    source_category="technical",
                    timestamp=current_time,
                    metadata={"feature": "delta", "cumulative": snapshot.cumulative_delta},
                ))
            else:
                signals.append(IndividualSignal(
                    name="delta_divergence_bullish",
                    direction=SignalDirection.LONG,
                    strength=0.65,
                    source_category="technical",
                    timestamp=current_time,
                    metadata={"feature": "delta", "cumulative": snapshot.cumulative_delta},
                ))

        # --- Trend alignment (confirmation signal, not primary) ---
        if snapshot.trend_direction == "up" and snapshot.trend_strength > 0.3:
            signals.append(IndividualSignal(
                name="trend_up",
                direction=SignalDirection.LONG,
                strength=0.5 + snapshot.trend_strength * 0.3,
                source_category="technical",
                timestamp=current_time,
                metadata={"feature": "trend", "strength": snapshot.trend_strength},
            ))
        elif snapshot.trend_direction == "down" and snapshot.trend_strength > 0.3:
            signals.append(IndividualSignal(
                name="trend_down",
                direction=SignalDirection.SHORT,
                strength=0.5 + snapshot.trend_strength * 0.3,
                source_category="technical",
                timestamp=current_time,
                metadata={"feature": "trend", "strength": snapshot.trend_strength},
            ))

        # === V1.3.3 GainzAlgo signals (additive -- no changes to above) ===
        try:
            from config.constants import (
                GAINZ_MODULES_ENABLED,
                GAINZ_EXHAUSTION_SIGNAL_STRENGTH,
                GAINZ_MOMENTUM_SIGNAL_STRENGTH,
                GAINZ_CYCLE_SIGNAL_STRENGTH,
            )
            if GAINZ_MODULES_ENABLED:
                gainz_signals = self._extract_gainz_signals(
                    snapshot, current_time,
                    GAINZ_EXHAUSTION_SIGNAL_STRENGTH,
                    GAINZ_MOMENTUM_SIGNAL_STRENGTH,
                    GAINZ_CYCLE_SIGNAL_STRENGTH,
                )
                signals.extend(gainz_signals)
        except (ImportError, AttributeError):
            pass  # Graceful degradation if constants not yet defined

        return signals

    def _extract_gainz_signals(
        self, snapshot, current_time: datetime,
        exhaustion_strength: float,
        momentum_strength: float,
        cycle_strength: float,
    ) -> List[IndividualSignal]:
        """
        Extract V1.3.3 GainzAlgo signals from the enhanced FeatureSnapshot.

        These signals participate in the normal aggregation pipeline:
        they add confluence for entries but cannot independently trigger
        trades (that requires sweep or aggregator standalone).
        """
        signals = []

        # --- Candle Micro-Reversal (CSMRM) ---
        # Exhaustion patterns with reversal direction are directional signals
        if (getattr(snapshot, "reversal_score", 0) > 0.5 and
            getattr(snapshot, "reversal_direction", "none") != "none"):

            direction = (SignalDirection.LONG
                        if snapshot.reversal_direction == "bullish"
                        else SignalDirection.SHORT)

            # Strength scales with reversal_score (0.5-1.0) and pattern quality
            base_strength = exhaustion_strength
            if getattr(snapshot, "exhaustion_pattern", "") == "engulfing":
                base_strength += 0.05  # Engulfing confirmation is stronger
            if getattr(snapshot, "consecutive_rejections", 0) >= 3:
                base_strength += 0.05  # Multiple rejections = very strong

            signals.append(IndividualSignal(
                name=f"csmrm_{snapshot.reversal_direction}",
                direction=direction,
                strength=min(base_strength, 0.85),
                source_category="technical",
                timestamp=current_time,
                metadata={
                    "feature": "CSMRM",
                    "pattern": getattr(snapshot, "exhaustion_pattern", "none"),
                    "reversal_score": getattr(snapshot, "reversal_score", 0),
                    "rejections": getattr(snapshot, "consecutive_rejections", 0),
                    "structural_stop_price": (
                        snapshot.structural_stop_long if direction == SignalDirection.LONG
                        else snapshot.structural_stop_short
                    ),
                },
            ))

        # --- Momentum Acceleration (SAMSM) ---
        # Exhaustion = deceleration after sustained move → reversal signal
        if getattr(snapshot, "momentum_exhaustion", False):
            # Direction: exhaustion in positive velocity → bearish signal (move slowing)
            vel = getattr(snapshot, "momentum_velocity", 0)
            if abs(vel) > 0.3:  # Meaningful velocity threshold
                direction = SignalDirection.SHORT if vel > 0 else SignalDirection.LONG
                signals.append(IndividualSignal(
                    name="samsm_exhaustion",
                    direction=direction,
                    strength=momentum_strength,
                    source_category="technical",
                    timestamp=current_time,
                    metadata={
                        "feature": "SAMSM",
                        "phase": getattr(snapshot, "momentum_phase", "unknown"),
                        "velocity": vel,
                        "acceleration": getattr(snapshot, "momentum_acceleration", 0),
                    },
                ))

        # Surge = sudden acceleration → continuation signal
        elif getattr(snapshot, "momentum_surge", False):
            vel = getattr(snapshot, "momentum_velocity", 0)
            if abs(vel) > 0.3:
                direction = SignalDirection.LONG if vel > 0 else SignalDirection.SHORT
                signals.append(IndividualSignal(
                    name="samsm_surge",
                    direction=direction,
                    strength=momentum_strength,
                    source_category="technical",
                    timestamp=current_time,
                    metadata={
                        "feature": "SAMSM",
                        "phase": "surge",
                        "velocity": vel,
                    },
                ))

        # --- Cycle-Slope (CSTA) ---
        # Correction phases in trending market = pullback entry opportunity
        phase = getattr(snapshot, "cycle_phase", "unknown")
        phase_strength = getattr(snapshot, "cycle_phase_duration", 0)

        if phase == "correction_down" and phase_strength >= 2:
            # Correction in uptrend → buy the dip
            signals.append(IndividualSignal(
                name="csta_correction_buy",
                direction=SignalDirection.LONG,
                strength=cycle_strength,
                source_category="technical",
                timestamp=current_time,
                metadata={
                    "feature": "CSTA",
                    "phase": phase,
                    "duration": phase_strength,
                    "cycle_score": getattr(snapshot, "cycle_score", 0),
                },
            ))
        elif phase == "correction_up" and phase_strength >= 2:
            # Correction in downtrend → sell the rally
            signals.append(IndividualSignal(
                name="csta_correction_sell",
                direction=SignalDirection.SHORT,
                strength=cycle_strength,
                source_category="technical",
                timestamp=current_time,
                metadata={
                    "feature": "CSTA",
                    "phase": phase,
                    "duration": phase_strength,
                    "cycle_score": getattr(snapshot, "cycle_score", 0),
                },
            ))

        return signals

    def get_signal_history(self, last_n: int = 50) -> List[AggregatedSignal]:
        """Return recent signal history for monitoring."""
        return self._signal_history[-last_n:]

    def get_signal_stats(self) -> dict:
        """Signal generation statistics."""
        total = len(self._signal_history)
        trade_signals = [s for s in self._signal_history if s.should_trade]
        return {
            "total_signals_evaluated": total,
            "trade_signals_generated": len(trade_signals),
            "signal_rate": round(len(trade_signals) / total * 100, 1) if total > 0 else 0,
            "avg_confluence_score": (
                round(sum(s.combined_score for s in trade_signals) / len(trade_signals), 3)
                if trade_signals else 0
            ),
            "htf_blocked_signals": self._htf_blocked_count,
            "htf_block_rate": (
                round(self._htf_blocked_count / total * 100, 1) if total > 0 else 0
            ),
        }
