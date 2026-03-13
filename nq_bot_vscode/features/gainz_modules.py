"""
GainzAlgo Suite -- V1.3.3 Enhancement Modules
===============================================
Five additive modules that enhance entry quality without changing the core
PATH C+ dual-trigger architecture.  Each module computes features on BAR CLOSE
only (zero lookahead) and feeds into the existing aggregator as additional
signal sources with dynamic scoring.

Modules:
  1. VolatilityPercentileNormalizer -- percentile-ranked ATR for adaptive thresholds
  2. MomentumAccelerationModel (SAMSM) -- 2nd derivative momentum for deceleration/surge detection
  3. CycleSlopeTrendAnalyzer (CSTA) -- micro-cycle phase tracking within EMA trends
  4. CandleMicroReversalEvaluator (CSMRM) -- exhaustion candle progressions and pressure asymmetry
  5. AdaptiveConfidenceEngine -- vol-regime-responsive HC gate and cross-signal dependencies

CRITICAL INVARIANT: All computations use only COMPLETED bars (bars[:-1] or earlier).
The current bar (bars[-1]) is the bar being processed and hasn't closed yet in live.
This matches the causality model in features/engine.py.

Author: V1.3.3 GainzAlgo Framework Integration (Mar 2026)
"""

import math
import logging
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from collections import deque

logger = logging.getLogger(__name__)


# =====================================================================
#  MODULE 1: VOLATILITY PERCENTILE NORMALIZER
# =====================================================================

@dataclass
class VolatilityContext:
    """Volatility context snapshot -- how current vol compares to history."""
    atr_percentile: float = 50.0       # 0-100: where current ATR sits in lookback
    atr_z_score: float = 0.0           # Standard deviations from mean
    vol_regime: str = "normal"         # "compressed", "normal", "expanding", "extreme"
    adaptive_sweep_depth: float = 3.0  # Dynamic MIN_SWEEP_DEPTH based on vol percentile
    vol_expansion_rate: float = 0.0    # Rate of change of ATR (positive = expanding)


class VolatilityPercentileNormalizer:
    """
    Ranks current ATR against a rolling historical window to provide
    percentile-based volatility context.

    Instead of using absolute ATR thresholds (which break when NQ moves
    from 18000 to 22000), percentile ranking tells us "current vol is
    at the 73rd percentile of recent history" -- universally meaningful.

    This enables:
    - Adaptive sweep depth (wider in high-vol, tighter in low-vol)
    - Vol-regime classification for adaptive confidence thresholds
    - Z-score for identifying vol breakouts (precursors to big moves)
    """

    def __init__(self, lookback: int = 500, update_interval: int = 1):
        self._atr_history: deque = deque(maxlen=lookback)
        self._lookback = lookback
        self._update_interval = update_interval
        self._call_count = 0

        # Calibrated sweep depth range (pts) -- scales with vol percentile
        # Low vol (10th pctl) → tight sweeps catch small moves
        # High vol (90th pctl) → wider sweeps needed to filter noise
        self._sweep_depth_min = 2.0   # Floor: minimum sweep depth in pts
        self._sweep_depth_max = 6.0   # Ceiling: max sweep depth in pts

    def update(self, atr_14: float) -> VolatilityContext:
        """
        Process a new ATR value and return volatility context.

        Args:
            atr_14: Current ATR-14 value from feature engine.

        Returns:
            VolatilityContext with percentile, z-score, regime, adaptive depth.

        CAUSALITY: Uses only historical ATR values. Current ATR is added
        to history AFTER computation (no contamination).
        """
        ctx = VolatilityContext()

        if not math.isfinite(atr_14) or atr_14 <= 0:
            self._atr_history.append(atr_14 if math.isfinite(atr_14) else 0.0)
            return ctx

        # Need minimum history for meaningful percentiles
        if len(self._atr_history) < 50:
            self._atr_history.append(atr_14)
            ctx.atr_percentile = 50.0
            ctx.vol_regime = "normal"
            ctx.adaptive_sweep_depth = (self._sweep_depth_min + self._sweep_depth_max) / 2
            return ctx

        history = np.array(self._atr_history)

        # ── Percentile rank ──
        # What % of historical ATR values is current ATR greater than?
        ctx.atr_percentile = round(
            float(np.sum(history < atr_14) / len(history) * 100), 1
        )

        # ── Z-score ──
        mean_atr = float(np.mean(history))
        std_atr = float(np.std(history))
        if std_atr > 0:
            ctx.atr_z_score = round((atr_14 - mean_atr) / std_atr, 2)

        # ── Vol regime classification ──
        if ctx.atr_percentile < 20:
            ctx.vol_regime = "compressed"    # Vol squeeze -- breakout imminent
        elif ctx.atr_percentile < 60:
            ctx.vol_regime = "normal"
        elif ctx.atr_percentile < 85:
            ctx.vol_regime = "expanding"     # Trend move or range expansion
        else:
            ctx.vol_regime = "extreme"       # Rare -- major move or news-driven

        # ── Adaptive sweep depth ──
        # Linear interpolation: 0th percentile → min depth, 100th → max depth
        t = ctx.atr_percentile / 100.0
        ctx.adaptive_sweep_depth = round(
            self._sweep_depth_min + t * (self._sweep_depth_max - self._sweep_depth_min), 2
        )

        # ── Vol expansion rate (momentum of volatility) ──
        if len(self._atr_history) >= 10:
            recent_5 = np.mean(list(self._atr_history)[-5:])
            older_5 = np.mean(list(self._atr_history)[-10:-5])
            if older_5 > 0:
                ctx.vol_expansion_rate = round((recent_5 - older_5) / older_5, 4)

        # Add current value to history AFTER computation (causality)
        self._atr_history.append(atr_14)

        return ctx


# =====================================================================
#  MODULE 2: MOMENTUM ACCELERATION MODEL (SAMSM)
# =====================================================================

@dataclass
class MomentumAcceleration:
    """Momentum acceleration snapshot -- velocity AND acceleration of price movement."""
    velocity: float = 0.0              # 1st derivative: rate of price change (pts/bar)
    acceleration: float = 0.0          # 2nd derivative: change in velocity
    momentum_phase: str = "neutral"    # "accelerating", "decelerating", "neutral", "reversing"
    surge_detected: bool = False       # Rapid acceleration beyond threshold
    exhaustion_detected: bool = False  # Deceleration after sustained move (reversal precursor)
    momentum_score: float = 0.0        # -1.0 to 1.0: negative = bearish momentum, positive = bullish


class MomentumAccelerationModel:
    """
    Structural Acceleration & Momentum Shift Model (SAMSM).

    Tracks not just WHERE price is going (trend) but HOW FAST it's
    getting there and WHETHER it's speeding up or slowing down.

    Key insight: Exhaustion (deceleration after sustained move) is the
    #1 precursor to reversals.  Surge (sudden acceleration) precedes
    continuation breakouts.

    Computation:
    - Velocity: EMA-smoothed bar-to-bar price change
    - Acceleration: Velocity change (2nd derivative)
    - Phase: accelerating/decelerating/neutral/reversing
    - Surge: acceleration > 2σ of recent history
    - Exhaustion: deceleration + velocity still directional + sustained bars
    """

    def __init__(self, velocity_period: int = 5, accel_period: int = 3,
                 history_len: int = 100, surge_sigma: float = 2.0,
                 exhaustion_bars: int = 5):
        self._velocity_period = velocity_period
        self._accel_period = accel_period
        self._surge_sigma = surge_sigma
        self._exhaustion_bars = exhaustion_bars

        self._closes: deque = deque(maxlen=history_len + velocity_period + 10)
        self._velocities: deque = deque(maxlen=history_len)
        self._accelerations: deque = deque(maxlen=history_len)
        self._decel_counter: int = 0  # Consecutive deceleration bars

    def update(self, close: float) -> MomentumAcceleration:
        """
        Process a new bar close and compute momentum acceleration.

        CAUSALITY: close is the most recent COMPLETED bar's close.
        """
        result = MomentumAcceleration()
        self._closes.append(close)

        if len(self._closes) < self._velocity_period + 2:
            return result

        # ── Velocity: smoothed rate of change ──
        # EMA of bar-to-bar changes over velocity_period
        changes = []
        closes_list = list(self._closes)
        for i in range(-self._velocity_period, 0):
            changes.append(closes_list[i] - closes_list[i - 1])

        velocity = self._ema_of_list(changes)
        self._velocities.append(velocity)
        result.velocity = round(velocity, 4)

        if len(self._velocities) < self._accel_period + 1:
            return result

        # ── Acceleration: change in velocity (2nd derivative) ──
        vel_list = list(self._velocities)
        accel_changes = []
        for i in range(-self._accel_period, 0):
            accel_changes.append(vel_list[i] - vel_list[i - 1])

        acceleration = self._ema_of_list(accel_changes)
        self._accelerations.append(acceleration)
        result.acceleration = round(acceleration, 4)

        # ── Phase classification ──
        if len(self._accelerations) < 10:
            result.momentum_phase = "neutral"
            return result

        accel_arr = np.array(list(self._accelerations)[-50:])
        accel_std = float(np.std(accel_arr)) if len(accel_arr) > 5 else 1.0
        if accel_std <= 0:
            accel_std = 0.001

        # Phase logic:
        # - velocity > 0 + acceleration > 0 → bullish accelerating
        # - velocity > 0 + acceleration < 0 → bullish decelerating (exhaustion)
        # - velocity < 0 + acceleration < 0 → bearish accelerating
        # - velocity < 0 + acceleration > 0 → bearish decelerating (exhaustion)
        vel_threshold = 0.5  # Minimum velocity to be "directional"

        if abs(velocity) < vel_threshold:
            result.momentum_phase = "neutral"
            self._decel_counter = 0
        elif (velocity > 0 and acceleration > 0) or (velocity < 0 and acceleration < 0):
            result.momentum_phase = "accelerating"
            self._decel_counter = 0
        elif (velocity > 0 and acceleration < 0) or (velocity < 0 and acceleration > 0):
            self._decel_counter += 1
            if self._decel_counter >= self._exhaustion_bars:
                result.momentum_phase = "reversing"
            else:
                result.momentum_phase = "decelerating"
        else:
            result.momentum_phase = "neutral"
            self._decel_counter = 0

        # ── Surge detection: acceleration > 2σ ──
        if abs(acceleration) > self._surge_sigma * accel_std:
            result.surge_detected = True

        # ── Exhaustion detection: sustained deceleration after strong move ──
        if self._decel_counter >= self._exhaustion_bars and abs(velocity) > vel_threshold:
            result.exhaustion_detected = True

        # ── Momentum score: -1 to 1 ──
        # Combines velocity direction with acceleration magnitude
        vel_norm = np.clip(velocity / (accel_std * 3 + 0.001), -1.0, 1.0)
        accel_norm = np.clip(acceleration / (accel_std * 2 + 0.001), -1.0, 1.0)
        result.momentum_score = round(float(vel_norm * 0.6 + accel_norm * 0.4), 3)

        return result

    @staticmethod
    def _ema_of_list(values: list, alpha: float = 0.3) -> float:
        """Simple EMA over a list of values."""
        if not values:
            return 0.0
        ema = values[0]
        for v in values[1:]:
            ema = alpha * v + (1 - alpha) * ema
        return ema


# =====================================================================
#  MODULE 3: CYCLE-SLOPE TREND ANALYZER (CSTA)
# =====================================================================

@dataclass
class CycleSlopeResult:
    """Micro-cycle phase and slope analysis."""
    cycle_phase: str = "unknown"       # "impulse_up", "correction_down", "impulse_down", "correction_up", "consolidation"
    slope_angle: float = 0.0           # Normalized slope of current phase (-1 to 1)
    phase_duration: int = 0            # Bars in current phase
    phase_strength: float = 0.0        # How strong is current phase (0-1)
    trend_maturity: float = 0.0        # 0=fresh, 1=mature/exhausted
    cycle_score: float = 0.0           # -1 (bearish) to +1 (bullish), weighted by phase


class CycleSlopeTrendAnalyzer:
    """
    Cycle-Slope Trend Analysis (CSTA).

    Tracks the PHASE within a trend, not just the trend direction:
    - Impulse up → Correction down → Impulse up → Correction down (uptrend)
    - Impulse down → Correction up → Impulse down → Correction up (downtrend)
    - Consolidation (neither impulse nor correction -- potential breakout)

    Uses dual-EMA slope analysis to detect phase transitions:
    - Fast EMA slope (8-period): captures micro-movements
    - Slow EMA slope (21-period): captures macro-direction
    - Phase = relationship between fast and slow slopes

    Key value: Entering on correction_down (in uptrend) or correction_up
    (in downtrend) gives better R:R than entering during impulse.
    """

    def __init__(self, fast_period: int = 8, slow_period: int = 21,
                 slope_lookback: int = 3, history_len: int = 200):
        self._fast_period = fast_period
        self._slow_period = slow_period
        self._slope_lookback = slope_lookback

        self._closes: deque = deque(maxlen=history_len)
        self._fast_emas: deque = deque(maxlen=history_len)
        self._slow_emas: deque = deque(maxlen=history_len)

        self._current_phase: str = "unknown"
        self._phase_bar_count: int = 0
        self._phase_transitions: int = 0  # Total transitions (maturity proxy)
        self._last_impulse_strength: float = 0.0

    def update(self, close: float, atr: float = 1.0) -> CycleSlopeResult:
        """
        Process a new completed bar close and compute cycle-slope.

        CAUSALITY: Uses only completed bars.
        """
        result = CycleSlopeResult()
        self._closes.append(close)

        if len(self._closes) < self._slow_period + self._slope_lookback + 1:
            return result

        closes_arr = np.array(self._closes)

        # ── Compute EMAs ──
        fast_ema = self._compute_ema(closes_arr, self._fast_period)
        slow_ema = self._compute_ema(closes_arr, self._slow_period)
        self._fast_emas.append(fast_ema)
        self._slow_emas.append(slow_ema)

        if len(self._fast_emas) < self._slope_lookback + 1:
            return result

        # ── Compute slopes (normalized by ATR) ──
        safe_atr = max(atr, 0.01)
        fast_emas_list = list(self._fast_emas)
        slow_emas_list = list(self._slow_emas)

        fast_slope = (fast_emas_list[-1] - fast_emas_list[-self._slope_lookback - 1]) / (self._slope_lookback * safe_atr)
        slow_slope = (slow_emas_list[-1] - slow_emas_list[-self._slope_lookback - 1]) / (self._slope_lookback * safe_atr)

        result.slope_angle = round(float(np.clip(fast_slope, -1.0, 1.0)), 3)

        # ── Phase classification ──
        slope_threshold = 0.05  # Minimum normalized slope to be "directional"

        old_phase = self._current_phase

        if abs(fast_slope) < slope_threshold and abs(slow_slope) < slope_threshold:
            self._current_phase = "consolidation"
        elif slow_slope > slope_threshold:
            # Macro trend is up
            if fast_slope > slope_threshold:
                self._current_phase = "impulse_up"
            elif fast_slope < -slope_threshold:
                self._current_phase = "correction_down"
            else:
                self._current_phase = "consolidation"
        elif slow_slope < -slope_threshold:
            # Macro trend is down
            if fast_slope < -slope_threshold:
                self._current_phase = "impulse_down"
            elif fast_slope > slope_threshold:
                self._current_phase = "correction_up"
            else:
                self._current_phase = "consolidation"
        else:
            self._current_phase = "consolidation"

        # Track phase duration
        if self._current_phase != old_phase:
            self._phase_bar_count = 1
            self._phase_transitions += 1
            if "impulse" in old_phase:
                self._last_impulse_strength = abs(fast_slope)
        else:
            self._phase_bar_count += 1

        result.cycle_phase = self._current_phase
        result.phase_duration = self._phase_bar_count

        # ── Phase strength: how decisively are we in this phase? ──
        if "impulse" in self._current_phase:
            result.phase_strength = round(min(abs(fast_slope) / 0.3, 1.0), 3)
        elif "correction" in self._current_phase:
            # Correction strength = how much of impulse was retraced
            if self._last_impulse_strength > 0:
                retrace_ratio = abs(fast_slope) / self._last_impulse_strength
                result.phase_strength = round(min(retrace_ratio, 1.0), 3)
        else:
            result.phase_strength = round(min(1.0 - abs(fast_slope) / 0.1, 1.0), 3)

        # ── Trend maturity: 0=fresh, 1=exhausted ──
        # Based on number of phase transitions and duration
        if self._phase_transitions > 0:
            result.trend_maturity = round(min(self._phase_transitions / 8.0, 1.0), 3)

        # ── Cycle score: -1 (bearish) to +1 (bullish) ──
        # Positive during impulse_up or correction_up, negative for down phases
        phase_direction = {
            "impulse_up": 1.0,
            "correction_down": -0.3,    # Mild bearish (still in uptrend context)
            "impulse_down": -1.0,
            "correction_up": 0.3,       # Mild bullish (still in downtrend context)
            "consolidation": 0.0,
        }
        base_score = phase_direction.get(self._current_phase, 0.0)
        result.cycle_score = round(base_score * result.phase_strength, 3)

        return result

    @staticmethod
    def _compute_ema(data: np.ndarray, period: int) -> float:
        """Compute EMA over full array, return latest value."""
        multiplier = 2.0 / (period + 1)
        ema = float(data[0])
        for val in data[1:]:
            ema = (float(val) - ema) * multiplier + ema
        return ema


# =====================================================================
#  MODULE 4: CANDLE-STRUCTURE MICRO-REVERSAL EVALUATOR (CSMRM)
# =====================================================================

@dataclass
class MicroReversalResult:
    """Candle-structure micro-reversal evaluation."""
    exhaustion_pattern: str = "none"   # "hammer", "shooting_star", "doji", "engulfing", "pin_bar", "none"
    pressure_asymmetry: float = 0.0    # -1 (sell pressure) to +1 (buy pressure)
    reversal_score: float = 0.0        # 0 (no reversal signal) to 1 (strong reversal)
    reversal_direction: str = "none"   # "bullish", "bearish", "none"
    pattern_progression: str = "none"  # "forming", "confirmed", "none"
    consecutive_rejection_bars: int = 0  # Bars showing same-direction rejection


class CandleMicroReversalEvaluator:
    """
    Candle-Structure Micro-Reversal Metric (CSMRM).

    Evaluates individual and multi-bar candle patterns for exhaustion
    and reversal signals.  This goes beyond the basic wick_ratio used
    in the sweep detector to identify:

    1. Single-bar exhaustion: hammer, shooting star, doji, pin bar
    2. Multi-bar progressions: hammer → engulfing confirmation
    3. Pressure asymmetry: ratio of upper vs lower wick (rejection signature)
    4. Consecutive rejection: multiple bars rejecting from same direction

    Key insight: A single rejection candle is noise. Three consecutive
    bars rejecting from highs (upper wick > 60% of range) is a strong
    bearish reversal signal.
    """

    def __init__(self, rejection_threshold: float = 0.55,
                 body_doji_threshold: float = 0.15,
                 min_range_atr_ratio: float = 0.3):
        self._rejection_threshold = rejection_threshold  # Wick ratio to count as rejection
        self._body_doji_threshold = body_doji_threshold  # Body/range < this → doji
        self._min_range_atr_ratio = min_range_atr_ratio  # Min candle range vs ATR

        self._recent_bars: deque = deque(maxlen=10)
        self._upper_rejection_count: int = 0
        self._lower_rejection_count: int = 0

    def update(self, bar_open: float, bar_high: float, bar_low: float,
               bar_close: float, atr: float) -> MicroReversalResult:
        """
        Evaluate candle structure for reversal signals.

        CAUSALITY: bar_open/high/low/close are from the most recent
        COMPLETED bar only.  No future data.
        """
        result = MicroReversalResult()
        bar_range = bar_high - bar_low

        if bar_range <= 0 or atr <= 0:
            self._recent_bars.append(None)
            return result

        # ── Core candle metrics ──
        body = abs(bar_close - bar_open)
        upper_wick = bar_high - max(bar_open, bar_close)
        lower_wick = min(bar_open, bar_close) - bar_low
        body_ratio = body / bar_range
        upper_wick_ratio = upper_wick / bar_range
        lower_wick_ratio = lower_wick / bar_range
        is_bullish = bar_close > bar_open

        # Store for multi-bar analysis
        bar_data = {
            "open": bar_open, "high": bar_high, "low": bar_low, "close": bar_close,
            "body": body, "range": bar_range, "body_ratio": body_ratio,
            "upper_wick_ratio": upper_wick_ratio, "lower_wick_ratio": lower_wick_ratio,
            "is_bullish": is_bullish, "atr_ratio": bar_range / atr,
        }
        self._recent_bars.append(bar_data)

        # Skip insignificant candles (range < 30% of ATR)
        if bar_range / atr < self._min_range_atr_ratio:
            return result

        # ── 1. Single-bar exhaustion patterns ──

        # Doji: body < 15% of range (indecision)
        if body_ratio < self._body_doji_threshold:
            result.exhaustion_pattern = "doji"
            result.reversal_score = 0.3

        # Hammer: small body at top, long lower wick (bullish reversal)
        elif lower_wick_ratio > self._rejection_threshold and upper_wick_ratio < 0.2:
            result.exhaustion_pattern = "hammer"
            result.reversal_direction = "bullish"
            result.reversal_score = 0.5 + lower_wick_ratio * 0.3

        # Shooting star: small body at bottom, long upper wick (bearish reversal)
        elif upper_wick_ratio > self._rejection_threshold and lower_wick_ratio < 0.2:
            result.exhaustion_pattern = "shooting_star"
            result.reversal_direction = "bearish"
            result.reversal_score = 0.5 + upper_wick_ratio * 0.3

        # Pin bar: very long wick one direction, tiny body (strong rejection)
        elif (upper_wick_ratio > 0.65 or lower_wick_ratio > 0.65) and body_ratio < 0.25:
            result.exhaustion_pattern = "pin_bar"
            if lower_wick_ratio > upper_wick_ratio:
                result.reversal_direction = "bullish"
            else:
                result.reversal_direction = "bearish"
            result.reversal_score = 0.7

        # ── 2. Pressure asymmetry ──
        # Positive = buy pressure (lower wick dominant = buyers stepping in)
        # Negative = sell pressure (upper wick dominant = sellers rejecting)
        if upper_wick + lower_wick > 0:
            result.pressure_asymmetry = round(
                (lower_wick - upper_wick) / (upper_wick + lower_wick), 3
            )

        # ── 3. Consecutive rejection tracking ──
        if upper_wick_ratio > self._rejection_threshold:
            self._upper_rejection_count += 1
            self._lower_rejection_count = 0
        elif lower_wick_ratio > self._rejection_threshold:
            self._lower_rejection_count += 1
            self._upper_rejection_count = 0
        else:
            self._upper_rejection_count = max(0, self._upper_rejection_count - 1)
            self._lower_rejection_count = max(0, self._lower_rejection_count - 1)

        if self._upper_rejection_count >= 2:
            result.consecutive_rejection_bars = self._upper_rejection_count
            result.reversal_direction = "bearish"
            # Boost score for consecutive rejections
            result.reversal_score = max(result.reversal_score,
                                        min(0.5 + self._upper_rejection_count * 0.15, 0.9))
        elif self._lower_rejection_count >= 2:
            result.consecutive_rejection_bars = self._lower_rejection_count
            result.reversal_direction = "bullish"
            result.reversal_score = max(result.reversal_score,
                                        min(0.5 + self._lower_rejection_count * 0.15, 0.9))

        # ── 4. Multi-bar engulfing confirmation ──
        if len(self._recent_bars) >= 2:
            prev = self._recent_bars[-2]
            if prev is not None and bar_data["atr_ratio"] >= 0.5:
                # Bullish engulfing: prev was bearish, current is bullish and body > prev body
                if not prev["is_bullish"] and is_bullish and body > prev["body"]:
                    if bar_close > prev["open"] and bar_open < prev["close"]:
                        result.exhaustion_pattern = "engulfing"
                        result.reversal_direction = "bullish"
                        result.reversal_score = max(result.reversal_score, 0.65)
                        result.pattern_progression = "confirmed"

                # Bearish engulfing: prev was bullish, current is bearish and body > prev body
                elif prev["is_bullish"] and not is_bullish and body > prev["body"]:
                    if bar_close < prev["open"] and bar_open > prev["close"]:
                        result.exhaustion_pattern = "engulfing"
                        result.reversal_direction = "bearish"
                        result.reversal_score = max(result.reversal_score, 0.65)
                        result.pattern_progression = "confirmed"

        # ── 5. Pattern progression (hammer → engulfing = confirmed) ──
        if len(self._recent_bars) >= 2 and result.exhaustion_pattern != "engulfing":
            prev = self._recent_bars[-2]
            if prev is not None:
                # Previous bar was a hammer/pin → current bar confirms direction
                if prev.get("lower_wick_ratio", 0) > self._rejection_threshold and is_bullish:
                    result.pattern_progression = "confirmed" if body_ratio > 0.5 else "forming"
                elif prev.get("upper_wick_ratio", 0) > self._rejection_threshold and not is_bullish:
                    result.pattern_progression = "confirmed" if body_ratio > 0.5 else "forming"

        result.reversal_score = round(min(result.reversal_score, 1.0), 3)
        return result


# =====================================================================
#  MODULE 5: ADAPTIVE CONFIDENCE ENGINE
# =====================================================================

@dataclass
class AdaptiveThresholds:
    """Dynamically adjusted confidence thresholds based on market context."""
    hc_gate: float = 0.75             # Adaptive HC gate (base is 0.75)
    min_signals: int = 2              # Adaptive min signals aligned
    score_boost: float = 0.0          # Context-dependent score adjustment
    gate_reason: str = ""             # Why the gate was adjusted
    cross_signal_boost: float = 0.0   # Boost from signal interdependence


class AdaptiveConfidenceEngine:
    """
    Dynamically adjusts the High-Conviction gate and signal scoring
    based on volatility regime, momentum phase, and cycle position.

    Instead of a static 0.75 HC gate:
    - Compressed vol + consolidation → LOWER gate (0.70) to catch breakouts early
    - Extreme vol + accelerating → RAISE gate (0.80) to avoid noise
    - Correction phase in trend → LOWER gate (0.72) for better entry on pullback
    - Exhaustion detected → BOOST reversal signals (+0.05)

    Also implements cross-signal dependencies:
    - Sweep + exhaustion candle + deceleration → synergy boost
    - OB + FVG overlap + correction phase → synergy boost
    """

    # Gate adjustment ranges (safety bounds -- never go below 0.70 or above 0.82)
    GATE_FLOOR = 0.70
    GATE_CEILING = 0.82
    BASE_GATE = 0.75

    def compute(
        self,
        vol_ctx: VolatilityContext,
        momentum: MomentumAcceleration,
        cycle: CycleSlopeResult,
        reversal: MicroReversalResult,
        regime: str,
    ) -> AdaptiveThresholds:
        """
        Compute adaptive thresholds based on all GainzAlgo module outputs.

        Returns adjusted gate and score modifiers.
        """
        result = AdaptiveThresholds()
        gate = self.BASE_GATE
        reasons = []

        # ── Vol-regime gate adjustment ──
        if vol_ctx.vol_regime == "compressed":
            gate -= 0.03  # Lower gate to catch breakouts (vol squeeze → big move)
            reasons.append("vol_compressed(-0.03)")
        elif vol_ctx.vol_regime == "extreme":
            gate += 0.05  # Raise gate to avoid noise
            reasons.append("vol_extreme(+0.05)")
        elif vol_ctx.vol_regime == "expanding" and vol_ctx.vol_expansion_rate > 0.1:
            gate += 0.02  # Slightly raise during rapid expansion
            reasons.append("vol_expanding(+0.02)")

        # ── Momentum phase gate adjustment ──
        if momentum.exhaustion_detected:
            gate -= 0.02  # Lower gate for reversal entries at exhaustion
            reasons.append("exhaustion(-0.02)")
        elif momentum.surge_detected and momentum.momentum_phase == "accelerating":
            gate -= 0.01  # Slight ease for continuation on surge
            reasons.append("surge(-0.01)")

        # ── Cycle phase gate adjustment ──
        if cycle.cycle_phase in ("correction_down", "correction_up"):
            gate -= 0.03  # Lower gate for pullback entries (best R:R)
            reasons.append(f"correction(-0.03)")
        elif cycle.trend_maturity > 0.7:
            gate += 0.03  # Raise gate in mature/exhausted trend
            reasons.append("mature_trend(+0.03)")

        # ── Regime adjustment ──
        if regime == "ranging":
            gate += 0.02  # Tighter gate in ranging (avoid chop)
            reasons.append("ranging(+0.02)")
        elif regime in ("trending_up", "trending_down"):
            gate -= 0.01  # Slight ease in confirmed trend
            reasons.append("trending(-0.01)")

        # Clamp to safety bounds
        gate = round(max(self.GATE_FLOOR, min(self.GATE_CEILING, gate)), 3)
        result.hc_gate = gate
        result.gate_reason = " | ".join(reasons) if reasons else "default"

        # ── Cross-signal synergy boosts ──
        boost = 0.0

        # Exhaustion candle + momentum deceleration = strong reversal signal
        if (reversal.reversal_score > 0.5 and
            momentum.momentum_phase in ("decelerating", "reversing")):
            boost += 0.05
            reasons.append("candle+decel_synergy(+0.05)")

        # Correction phase + reversal pattern = high-probability pullback entry
        if (cycle.cycle_phase in ("correction_down", "correction_up") and
            reversal.pattern_progression == "confirmed"):
            boost += 0.04
            reasons.append("correction+pattern_synergy(+0.04)")

        # Vol compression + consolidation = breakout imminent (boost any signal)
        if vol_ctx.vol_regime == "compressed" and cycle.cycle_phase == "consolidation":
            boost += 0.03
            reasons.append("vol_squeeze+consolidation(+0.03)")

        # Surge + impulse phase = strong continuation
        if momentum.surge_detected and "impulse" in cycle.cycle_phase:
            boost += 0.03
            reasons.append("surge+impulse_synergy(+0.03)")

        result.cross_signal_boost = round(min(boost, 0.10), 3)  # Cap at +0.10
        result.score_boost = result.cross_signal_boost

        return result
