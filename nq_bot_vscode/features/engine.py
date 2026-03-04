"""
NQ Feature Engine
==================
Computes institutional-grade trading features from NQ price data.
Every feature is computed strictly on past data — zero lookahead bias.

Features implemented:
- Order Blocks (OB) — bullish and bearish
- Fair Value Gaps (FVG) — standard and inverse
- Liquidity Sweeps — buy-side and sell-side
- VWAP + Standard Deviation Bands
- Order Flow / Delta Analysis
- ATR-based Volatility
- Market Structure (swing highs/lows, trend)
- Volume Profile (POC, Value Area)
"""

import logging
import math
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class Bar:
    """Single OHLCV bar with order flow data."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    bid_volume: int = 0
    ask_volume: int = 0
    delta: int = 0                 # ask_volume - bid_volume
    tick_count: int = 0
    vwap: float = 0.0
    session_type: Optional[str] = None  # "RTH" or "ETH", set by IBKRDataFeed

    def __post_init__(self):
        """Validate OHLC data integrity on construction."""
        # Guard against NaN/Inf in price fields
        for fld in ("open", "high", "low", "close"):
            val = getattr(self, fld)
            if not math.isfinite(val):
                logger.error("Bar has non-finite %s=%.4f at %s — clamping to close",
                             fld, val, self.timestamp)
                object.__setattr__(self, fld, self.close if math.isfinite(self.close) else 0.0)

        # Fix invalid OHLC: high must be >= low, high >= open/close, low <= open/close
        if self.high < self.low:
            logger.warning("Bar high (%.2f) < low (%.2f) at %s — swapping",
                           self.high, self.low, self.timestamp)
            object.__setattr__(self, "high", max(self.high, self.low))
            object.__setattr__(self, "low", min(self.high, self.low))

        actual_high = max(self.open, self.high, self.low, self.close)
        actual_low = min(self.open, self.high, self.low, self.close)
        if self.high < actual_high:
            object.__setattr__(self, "high", actual_high)
        if self.low > actual_low:
            object.__setattr__(self, "low", actual_low)

        # Guard against negative volume
        if self.volume < 0:
            logger.warning("Bar has negative volume (%d) at %s — setting to 0",
                           self.volume, self.timestamp)
            object.__setattr__(self, "volume", 0)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low


@dataclass
class OrderBlock:
    """Detected Order Block zone."""
    detected_at: datetime
    direction: str                 # 'bullish' or 'bearish'
    zone_high: float
    zone_low: float
    displacement_size: float       # How far price moved after OB
    bar_index: int                 # Index of the OB bar in the series
    is_valid: bool = True
    mitigated: bool = False


@dataclass
class FairValueGap:
    """Detected Fair Value Gap."""
    detected_at: datetime
    gap_type: str                  # 'bullish' or 'bearish'
    gap_high: float
    gap_low: float
    gap_size: float
    is_inverse: bool = False       # IFVG
    filled_pct: float = 0.0
    is_valid: bool = True


@dataclass
class LiquiditySweep:
    """Detected Liquidity Sweep event."""
    detected_at: datetime
    sweep_type: str                # 'buy_side' or 'sell_side'
    swept_level: float
    sweep_price: float             # The extreme of the sweep candle
    volume_at_sweep: int
    wick_ratio: float
    confirmed: bool = False        # Confirmed by subsequent displacement


@dataclass
class FeatureSnapshot:
    """Complete feature snapshot for a given bar timestamp."""
    timestamp: datetime
    
    # Volatility
    atr_14: float = 0.0
    realized_vol: float = 0.0
    
    # VWAP
    session_vwap: float = 0.0
    price_vs_vwap: float = 0.0
    vwap_upper_1: float = 0.0
    vwap_lower_1: float = 0.0
    vwap_upper_2: float = 0.0
    vwap_lower_2: float = 0.0
    
    # Order Flow
    cumulative_delta: int = 0
    delta_divergence: bool = False
    volume_imbalance: float = 0.0
    
    # Structure
    is_swing_high: bool = False
    is_swing_low: bool = False
    trend_direction: str = "none"
    trend_strength: float = 0.0
    
    # Active Zones
    active_order_blocks: List[OrderBlock] = field(default_factory=list)
    active_fvgs: List[FairValueGap] = field(default_factory=list)
    recent_sweeps: List[LiquiditySweep] = field(default_factory=list)
    
    # Regime / External
    vix_level: float = 0.0
    detected_regime: str = "unknown"

    # Proximity signals (is price near a key level?)
    near_bullish_ob: bool = False
    near_bearish_ob: bool = False
    inside_bullish_fvg: bool = False
    inside_bearish_fvg: bool = False
    recent_buy_sweep: bool = False
    recent_sell_sweep: bool = False

    # Structural invalidation levels for stop placement
    # These are price levels where the signal feature is negated
    structural_stop_long: Optional[float] = None    # Tightest stop price for LONG entries
    structural_stop_short: Optional[float] = None   # Tightest stop price for SHORT entries


class NQFeatureEngine:
    """
    Computes all NQ-specific features from a rolling window of bars.
    
    CRITICAL INVARIANT: All computations use only data available at or 
    before the current bar timestamp. No future data contamination.
    """

    def __init__(self, config):
        self.config = config.features
        self.risk_config = config.risk
        self._bars: List[Bar] = []
        self._order_blocks: List[OrderBlock] = []
        self._fvgs: List[FairValueGap] = []
        self._sweeps: List[LiquiditySweep] = []
        self._cumulative_delta: int = 0
        self._session_volume_price_sum: float = 0.0
        self._session_volume_sum: int = 0

    def update(self, bar: Bar) -> FeatureSnapshot:
        """
        Process a new bar and compute all features.
        Returns a complete feature snapshot for this bar.
        """
        self._bars.append(bar)
        
        # Keep rolling window manageable (last 500 bars)
        if len(self._bars) > 500:
            self._bars = self._bars[-500:]

        # Update cumulative delta
        self._cumulative_delta += bar.delta

        # Update session VWAP components
        self._session_volume_price_sum += bar.close * bar.volume
        self._session_volume_sum += bar.volume

        # Need minimum bars for most features
        if len(self._bars) < 20:
            return FeatureSnapshot(timestamp=bar.timestamp)

        snapshot = FeatureSnapshot(timestamp=bar.timestamp)

        # === Compute each feature group ===
        self._compute_atr(snapshot)
        self._compute_vwap(snapshot, bar)
        self._compute_order_flow(snapshot, bar)
        self._detect_swing_points(snapshot)
        self._compute_trend(snapshot)
        self._detect_order_blocks(snapshot, bar)
        self._detect_fvgs(snapshot, bar)
        self._detect_liquidity_sweeps(snapshot, bar)
        self._update_zone_validity(bar)
        self._compute_proximity_signals(snapshot, bar)

        return snapshot

    def reset_session(self) -> None:
        """Reset session-based calculations (call at session open)."""
        self._session_volume_price_sum = 0.0
        self._session_volume_sum = 0
        self._cumulative_delta = 0

    # ================================================================
    # ATR — Average True Range
    # ================================================================
    def _compute_atr(self, snapshot: FeatureSnapshot) -> None:
        """ATR-14 for volatility-adjusted sizing."""
        period = self.risk_config.atr_period # type: ignore
        if len(self._bars) < period + 1:
            return

        true_ranges = []
        for i in range(-period, 0):
            bar = self._bars[i]
            prev_bar = self._bars[i - 1]
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_bar.close),
                abs(bar.low - prev_bar.close),
            )
            true_ranges.append(tr)

        atr_val = float(np.mean(true_ranges))
        snapshot.atr_14 = round(atr_val, 2) if math.isfinite(atr_val) else 0.0

        # Realized volatility (annualized from 1-min returns)
        if len(self._bars) >= 20:
            closes = [b.close for b in self._bars[-21:]]
            # Guard against zero/negative closes that would produce NaN in log
            if all(c > 0 for c in closes):
                returns = [np.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
                rv = float(np.std(returns) * np.sqrt(390))
                snapshot.realized_vol = round(rv, 4) if math.isfinite(rv) else 0.0

    # ================================================================
    # VWAP + Deviation Bands
    # ================================================================
    def _compute_vwap(self, snapshot: FeatureSnapshot, bar: Bar) -> None:
        """Session VWAP with standard deviation bands."""
        if self._session_volume_sum == 0:
            return

        vwap = self._session_volume_price_sum / self._session_volume_sum
        if not math.isfinite(vwap):
            return
        snapshot.session_vwap = round(vwap, 2)
        snapshot.price_vs_vwap = round(bar.close - vwap, 2)

        # Compute VWAP standard deviation for bands
        if len(self._bars) >= 10:
            deviations = []
            for b in self._bars[-min(len(self._bars), 100):]:
                deviations.append((b.close - vwap) ** 2)
            std_dev = np.sqrt(np.mean(deviations))

            snapshot.vwap_upper_1 = round(vwap + std_dev, 2)
            snapshot.vwap_lower_1 = round(vwap - std_dev, 2)
            snapshot.vwap_upper_2 = round(vwap + 2 * std_dev, 2)
            snapshot.vwap_lower_2 = round(vwap - 2 * std_dev, 2)

    # ================================================================
    # Order Flow / Delta
    # ================================================================
    def _compute_order_flow(self, snapshot: FeatureSnapshot, bar: Bar) -> None:
        """Cumulative delta and volume imbalance."""
        snapshot.cumulative_delta = self._cumulative_delta

        # Volume imbalance: (ask_vol - bid_vol) / total_vol
        if bar.volume > 0:
            snapshot.volume_imbalance = round(
                (bar.ask_volume - bar.bid_volume) / bar.volume, 4
            )

        # Delta divergence: price makes new high but delta doesn't, or vice versa
        if len(self._bars) >= self.config.delta_lookback_bars:
            recent = self._bars[-self.config.delta_lookback_bars:]
            price_direction = recent[-1].close - recent[0].close
            delta_sum = sum(b.delta for b in recent)
            
            # Price up but delta negative (selling into strength)
            if price_direction > 0 and delta_sum < 0:
                snapshot.delta_divergence = True
            # Price down but delta positive (buying into weakness)
            elif price_direction < 0 and delta_sum > 0:
                snapshot.delta_divergence = True

    # ================================================================
    # Swing Points (Market Structure)
    # ================================================================
    def _detect_swing_points(self, snapshot: FeatureSnapshot) -> None:
        """Detect swing highs and lows using 5-bar lookback (confirmed)."""
        if len(self._bars) < 7:
            return
        
        # Check bar at index -3 (needs 2 bars on each side for confirmation)
        pivot_idx = -3
        pivot = self._bars[pivot_idx]
        left_bars = self._bars[pivot_idx - 2: pivot_idx]
        right_bars = self._bars[pivot_idx + 1: pivot_idx + 3]

        if not left_bars or not right_bars:
            return

        # Swing High: pivot high > highs on both sides
        if (pivot.high > max(b.high for b in left_bars) and 
            pivot.high > max(b.high for b in right_bars)):
            snapshot.is_swing_high = True

        # Swing Low: pivot low < lows on both sides
        if (pivot.low < min(b.low for b in left_bars) and 
            pivot.low < min(b.low for b in right_bars)):
            snapshot.is_swing_low = True

    # ================================================================
    # Trend Detection
    # ================================================================
    def _compute_trend(self, snapshot: FeatureSnapshot) -> None:
        """Simple trend detection using EMA crossover and structure."""
        if len(self._bars) < 50:
            snapshot.trend_direction = "none"
            snapshot.trend_strength = 0.0
            return

        closes = np.array([b.close for b in self._bars[-50:]])
        
        # EMA-8 vs EMA-21
        ema_8 = self._ema(closes, 8)
        ema_21 = self._ema(closes, 21)

        current_price = closes[-1]
        
        if ema_8 > ema_21 and current_price > ema_8:
            snapshot.trend_direction = "up"
        elif ema_8 < ema_21 and current_price < ema_8:
            snapshot.trend_direction = "down"
        else:
            snapshot.trend_direction = "none"

        # Trend strength: distance between EMAs normalized by ATR
        if snapshot.atr_14 > 0:
            ema_spread = abs(ema_8 - ema_21) / snapshot.atr_14
            if math.isfinite(ema_spread):
                snapshot.trend_strength = round(min(ema_spread, 1.0), 3)

    def _ema(self, data: np.ndarray, period: int) -> float:
        """Compute EMA and return latest value."""
        multiplier = 2 / (period + 1)
        ema_val = data[0]
        for price in data[1:]:
            ema_val = (price - ema_val) * multiplier + ema_val
        return ema_val

    # ================================================================
    # Order Blocks
    # ================================================================
    def _detect_order_blocks(self, snapshot: FeatureSnapshot, current_bar: Bar) -> None:
        """
        Detect Order Blocks using ICT methodology:
        - Bullish OB: Last bearish candle before a strong bullish displacement
        - Bearish OB: Last bullish candle before a strong bearish displacement
        
        Validation: The displacement away from the OB must exceed min ATR multiplier.
        """
        if len(self._bars) < 5 or snapshot.atr_14 == 0:
            return

        lookback = min(self.config.ob_lookback_bars, len(self._bars) - 3)
        min_displacement = snapshot.atr_14 * self.config.ob_min_displacement_atr

        for i in range(len(self._bars) - 3, max(len(self._bars) - lookback - 3, 2), -1):
            candidate = self._bars[i]
            next_bar = self._bars[i + 1]
            bar_after = self._bars[i + 2] if i + 2 < len(self._bars) else None

            if bar_after is None:
                continue

            # --- Bullish OB ---
            # Candidate is bearish, followed by strong bullish displacement
            if candidate.is_bearish:
                displacement = bar_after.close - candidate.low
                if displacement >= min_displacement:
                    ob = OrderBlock(
                        detected_at=candidate.timestamp,
                        direction="bullish",
                        zone_high=candidate.open,     # Top of bearish candle body
                        zone_low=candidate.low,        # Low of the OB candle
                        displacement_size=round(displacement, 2),
                        bar_index=i,
                    )
                    # Avoid duplicates
                    if not self._ob_exists(ob):
                        self._order_blocks.append(ob)

            # --- Bearish OB ---
            # Candidate is bullish, followed by strong bearish displacement
            if candidate.is_bullish:
                displacement = candidate.high - bar_after.close
                if displacement >= min_displacement:
                    ob = OrderBlock(
                        detected_at=candidate.timestamp,
                        direction="bearish",
                        zone_high=candidate.high,      # High of the OB candle
                        zone_low=candidate.close,      # Bottom of bullish candle body
                        displacement_size=round(displacement, 2),
                        bar_index=i,
                    )
                    if not self._ob_exists(ob):
                        self._order_blocks.append(ob)

    def _ob_exists(self, new_ob: OrderBlock) -> bool:
        """Check if an OB at this level already exists."""
        for ob in self._order_blocks:
            if (ob.direction == new_ob.direction and
                abs(ob.zone_high - new_ob.zone_high) < 2.0 and
                abs(ob.zone_low - new_ob.zone_low) < 2.0):
                return True
        return False

    # ================================================================
    # Fair Value Gaps (FVG) and Inverse FVG (IFVG)
    # ================================================================
    def _detect_fvgs(self, snapshot: FeatureSnapshot, current_bar: Bar) -> None:
        """
        Detect Fair Value Gaps:
        - Bullish FVG: bar[i-1].high < bar[i+1].low (gap up)
        - Bearish FVG: bar[i-1].low > bar[i+1].high (gap down)
        
        IFVG: A previously respected FVG that price returns through,
        inverting its polarity.
        """
        if len(self._bars) < 4:
            return

        # Check the 3-candle pattern ending at the second-to-last bar
        # (we need the current bar to confirm, but the FVG is from prior bars)
        bar_1 = self._bars[-3]  # Candle 1
        bar_2 = self._bars[-2]  # Candle 2 (middle — the "impulse")
        bar_3 = self._bars[-1]  # Candle 3 (current)

        min_gap = self.config.fvg_min_gap_ticks * 0.25  # Convert ticks to NQ points

        # --- Bullish FVG ---
        # Gap exists between candle 1's high and candle 3's low
        if bar_3.low > bar_1.high:
            gap_size = bar_3.low - bar_1.high
            if gap_size >= min_gap:
                fvg = FairValueGap(
                    detected_at=bar_2.timestamp,
                    gap_type="bullish",
                    gap_high=bar_3.low,
                    gap_low=bar_1.high,
                    gap_size=round(gap_size, 2),
                )
                if not self._fvg_exists(fvg):
                    self._fvgs.append(fvg)

        # --- Bearish FVG ---
        # Gap exists between candle 1's low and candle 3's high
        if bar_1.low > bar_3.high:
            gap_size = bar_1.low - bar_3.high
            if gap_size >= min_gap:
                fvg = FairValueGap(
                    detected_at=bar_2.timestamp,
                    gap_type="bearish",
                    gap_high=bar_1.low,
                    gap_low=bar_3.high,
                    gap_size=round(gap_size, 2),
                )
                if not self._fvg_exists(fvg):
                    self._fvgs.append(fvg)

        # --- Check for IFVG (Inverse Fair Value Gap) ---
        self._check_ifvg(current_bar)

    def _check_ifvg(self, current_bar: Bar) -> None:
        """
        Check if any existing FVG has been fully traded through,
        converting it to an Inverse FVG with flipped polarity.
        """
        for fvg in self._fvgs:
            if not fvg.is_valid or fvg.is_inverse:
                continue

            if fvg.gap_type == "bullish":
                # Price closes below the bullish FVG = IFVG (now bearish zone)
                if current_bar.close < fvg.gap_low:
                    ifvg = FairValueGap(
                        detected_at=current_bar.timestamp,
                        gap_type="bearish",        # Flipped polarity
                        gap_high=fvg.gap_high,
                        gap_low=fvg.gap_low,
                        gap_size=fvg.gap_size,
                        is_inverse=True,
                    )
                    fvg.is_valid = False            # Original FVG invalidated
                    self._fvgs.append(ifvg)
                    
            elif fvg.gap_type == "bearish":
                # Price closes above the bearish FVG = IFVG (now bullish zone)
                if current_bar.close > fvg.gap_high:
                    ifvg = FairValueGap(
                        detected_at=current_bar.timestamp,
                        gap_type="bullish",        # Flipped polarity
                        gap_high=fvg.gap_high,
                        gap_low=fvg.gap_low,
                        gap_size=fvg.gap_size,
                        is_inverse=True,
                    )
                    fvg.is_valid = False
                    self._fvgs.append(ifvg)

    def _fvg_exists(self, new_fvg: FairValueGap) -> bool:
        """Deduplicate FVGs at similar levels."""
        for fvg in self._fvgs:
            if (fvg.gap_type == new_fvg.gap_type and
                abs(fvg.gap_high - new_fvg.gap_high) < 1.0 and
                abs(fvg.gap_low - new_fvg.gap_low) < 1.0):
                return True
        return False

    # ================================================================
    # Liquidity Sweeps
    # ================================================================
    def _detect_liquidity_sweeps(self, snapshot: FeatureSnapshot, current_bar: Bar) -> None:
        """
        Detect liquidity sweeps:
        - Buy-side sweep: price wicks above recent swing high then closes below it
        - Sell-side sweep: price wicks below recent swing low then closes above it
        
        Confirmation: High wick ratio + volume spike.
        """
        lookback = min(self.config.sweep_lookback_bars, len(self._bars) - 1)
        if lookback < 5:
            return

        recent_bars = self._bars[-(lookback + 1):-1]  # Exclude current bar
        avg_volume = np.mean([b.volume for b in recent_bars]) if recent_bars else 1
        if avg_volume <= 0:
            avg_volume = 1
        vol_threshold = avg_volume * self.config.sweep_volume_spike_multiplier

        # Find recent swing highs and lows
        swing_highs = []
        swing_lows = []
        for i in range(2, len(recent_bars) - 2):
            b = recent_bars[i]
            if (b.high > recent_bars[i-1].high and b.high > recent_bars[i-2].high and
                b.high > recent_bars[i+1].high and b.high > recent_bars[i+2].high):
                swing_highs.append(b.high)
            if (b.low < recent_bars[i-1].low and b.low < recent_bars[i-2].low and
                b.low < recent_bars[i+1].low and b.low < recent_bars[i+2].low):
                swing_lows.append(b.low)

        if current_bar.range <= 0:
            return

        wick_ratio_upper = current_bar.upper_wick / current_bar.range
        wick_ratio_lower = current_bar.lower_wick / current_bar.range
        if not (math.isfinite(wick_ratio_upper) and math.isfinite(wick_ratio_lower)):
            return

        # --- Buy-side sweep (sweep of highs) ---
        for sh in swing_highs:
            if (current_bar.high > sh and current_bar.close < sh and
                wick_ratio_upper >= self.config.sweep_min_wick_ratio):
                sweep = LiquiditySweep(
                    detected_at=current_bar.timestamp,
                    sweep_type="buy_side",
                    swept_level=sh,
                    sweep_price=current_bar.high,
                    volume_at_sweep=current_bar.volume,
                    wick_ratio=round(wick_ratio_upper, 3),
                    confirmed=current_bar.volume >= vol_threshold,
                )
                self._sweeps.append(sweep)

        # --- Sell-side sweep (sweep of lows) ---
        for sl in swing_lows:
            if (current_bar.low < sl and current_bar.close > sl and
                wick_ratio_lower >= self.config.sweep_min_wick_ratio):
                sweep = LiquiditySweep(
                    detected_at=current_bar.timestamp,
                    sweep_type="sell_side",
                    swept_level=sl,
                    sweep_price=current_bar.low,
                    volume_at_sweep=current_bar.volume,
                    wick_ratio=round(wick_ratio_lower, 3),
                    confirmed=current_bar.volume >= vol_threshold,
                )
                self._sweeps.append(sweep)

    # ================================================================
    # Zone Validity Updates
    # ================================================================
    def _update_zone_validity(self, current_bar: Bar) -> None:
        """
        Expire old zones and mark mitigated order blocks / filled FVGs.
        """
        # Expire old OBs
        max_age = self.config.ob_max_age_bars
        for ob in self._order_blocks:
            if not ob.is_valid:
                continue
            bars_since = len(self._bars) - ob.bar_index
            if bars_since > max_age:
                ob.is_valid = False
                continue
            # Check mitigation (price returned to OB)
            if ob.direction == "bullish":
                if current_bar.low <= ob.zone_high:
                    ob.mitigated = True
            elif ob.direction == "bearish":
                if current_bar.high >= ob.zone_low:
                    ob.mitigated = True

        # Update FVG fill percentage
        for fvg in self._fvgs:
            if not fvg.is_valid:
                continue
            if fvg.gap_type == "bullish":
                if current_bar.low <= fvg.gap_high:
                    fill_depth = fvg.gap_high - max(current_bar.low, fvg.gap_low)
                    fvg.filled_pct = min(fill_depth / fvg.gap_size, 1.0) if fvg.gap_size > 0 else 1.0
            elif fvg.gap_type == "bearish":
                if current_bar.high >= fvg.gap_low:
                    fill_depth = min(current_bar.high, fvg.gap_high) - fvg.gap_low
                    fvg.filled_pct = min(fill_depth / fvg.gap_size, 1.0) if fvg.gap_size > 0 else 1.0

        # Expire old FVGs
        max_fvg_bars = self.config.fvg_max_age_bars
        self._fvgs = [f for f in self._fvgs if f.is_valid or f.filled_pct < 1.0]
        
        # Keep only recent sweeps (last 50 bars worth)
        if len(self._sweeps) > 50:
            self._sweeps = self._sweeps[-50:]

    # ================================================================
    # Proximity Signals
    # ================================================================
    def _compute_proximity_signals(self, snapshot: FeatureSnapshot, bar: Bar) -> None:
        """
        Determine if current price is near any active zones.
        These are the actionable signals that feed into trade decisions.
        """
        proximity_points = 5.0  # Within 5 NQ points of a zone

        # Active Order Blocks
        active_obs = [ob for ob in self._order_blocks if ob.is_valid and not ob.mitigated]
        snapshot.active_order_blocks = active_obs
        
        for ob in active_obs:
            if ob.direction == "bullish":
                if ob.zone_low - proximity_points <= bar.close <= ob.zone_high + proximity_points:
                    snapshot.near_bullish_ob = True
            elif ob.direction == "bearish":
                if ob.zone_low - proximity_points <= bar.close <= ob.zone_high + proximity_points:
                    snapshot.near_bearish_ob = True

        # Active FVGs
        active_fvgs = [f for f in self._fvgs if f.is_valid and f.filled_pct < 0.8]
        snapshot.active_fvgs = active_fvgs
        
        for fvg in active_fvgs:
            if fvg.gap_low <= bar.close <= fvg.gap_high:
                if fvg.gap_type == "bullish":
                    snapshot.inside_bullish_fvg = True
                elif fvg.gap_type == "bearish":
                    snapshot.inside_bearish_fvg = True

        # Recent sweeps (last 10 bars)
        recent_cutoff = len(self._bars) - 10
        recent_sweeps = [s for s in self._sweeps
                        if s.confirmed and self._bars.index(self._bars[-1]) - recent_cutoff < 10]
        snapshot.recent_sweeps = self._sweeps[-5:] if self._sweeps else []

        for sweep in self._sweeps[-5:]:
            if sweep.confirmed:
                if sweep.sweep_type == "buy_side":
                    snapshot.recent_buy_sweep = True
                elif sweep.sweep_type == "sell_side":
                    snapshot.recent_sell_sweep = True

        # --- Structural stop levels for stop placement ---
        long_structural_stops = []   # Prices below entry for LONG invalidation
        short_structural_stops = []  # Prices above entry for SHORT invalidation

        # OB structural stops (zone edge ± 3pts buffer)
        for ob in active_obs:
            if ob.direction == "bullish" and snapshot.near_bullish_ob:
                long_structural_stops.append(ob.zone_low - 3.0)
            elif ob.direction == "bearish" and snapshot.near_bearish_ob:
                short_structural_stops.append(ob.zone_high + 3.0)

        # FVG structural stops (FVG boundary ± 3pts buffer)
        for fvg in active_fvgs:
            if fvg.gap_type == "bullish" and snapshot.inside_bullish_fvg:
                long_structural_stops.append(fvg.gap_low - 3.0)
            elif fvg.gap_type == "bearish" and snapshot.inside_bearish_fvg:
                short_structural_stops.append(fvg.gap_high + 3.0)

        # Sweep structural stops (swept level ± 5pts buffer)
        for sweep in self._sweeps[-5:]:
            if sweep.confirmed:
                if sweep.sweep_type == "sell_side":
                    long_structural_stops.append(sweep.sweep_price - 5.0)
                elif sweep.sweep_type == "buy_side":
                    short_structural_stops.append(sweep.sweep_price + 5.0)

        # Tightest = highest price for LONG (closest to entry), lowest for SHORT
        if long_structural_stops:
            snapshot.structural_stop_long = round(max(long_structural_stops), 2)
        if short_structural_stops:
            snapshot.structural_stop_short = round(min(short_structural_stops), 2)
