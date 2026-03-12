"""
FVG Detector -- Fair Value Gap Detection & Lifecycle
=====================================================
Real-time detection of Fair Value Gaps (FVG) and Inverse Fair Value Gaps
(IFVG) for the Universal Confirmation Layer.

Uses only completed candles -- zero look-ahead bias.

FVG Lifecycle:
  UNFILLED -> price enters zone -> PARTIALLY_FILLED
  PARTIALLY_FILLED -> price touches CE (midpoint) -> FILLED
  Any -> price closes through zone -> VIOLATED (removed)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class FairValueGap:
    """A detected Fair Value Gap zone."""
    fvg_high: float
    fvg_low: float
    fvg_midpoint: float               # CE (consequent encroachment)
    formation_bar: int                 # bar index when formed
    formation_time: datetime
    direction: str                     # "bullish" | "bearish"
    is_inverse: bool                   # FVG against prevailing trend
    status: str = "UNFILLED"           # "UNFILLED" | "PARTIALLY_FILLED" | "FILLED" | "VIOLATED"
    size_points: float = 0.0           # fvg_high - fvg_low


class FVGDetector:
    """
    Real-time FVG/IFVG detection and lifecycle tracking.

    Called once per bar via update(). Scans the last 3 completed candles
    for new FVGs, then updates the lifecycle of all active FVGs.
    """

    MIN_FVG_SIZE: float = 2.0          # points -- ignore micro-gaps
    MAX_ACTIVE_PER_DIRECTION: int = 20
    EXPIRY_BARS: int = 500             # remove if never revisited

    def __init__(self):
        self._bullish_fvgs: List[FairValueGap] = []
        self._bearish_fvgs: List[FairValueGap] = []
        self._bars: list = []          # rolling window of recent bars
        self._total_detected: int = 0
        self._total_violated: int = 0
        self._total_filled: int = 0

    def update(self, bar, bar_index: int, trend_direction: str) -> List[FairValueGap]:
        """
        Process a new completed bar.

        1. Append bar to rolling window.
        2. Detect new FVGs from the last 3 candles.
        3. Update lifecycle of all active FVGs.
        4. Expire stale FVGs.

        Args:
            bar: A Bar object with open, high, low, close.
            bar_index: Monotonically increasing bar counter.
            trend_direction: "up", "down", or "none" from NQFeatureEngine.

        Returns:
            List of newly detected FairValueGap objects (may be empty).
        """
        self._bars.append(bar)
        if len(self._bars) > 100:
            self._bars = self._bars[-100:]

        new_fvgs = []

        # Need at least 3 completed candles to detect FVG
        if len(self._bars) >= 3:
            new_fvgs = self._detect_fvgs(bar_index, trend_direction)

        # Update lifecycle of all active FVGs
        self._update_lifecycle(bar, bar_index)

        return new_fvgs

    def get_active_fvgs(self, direction: str = None) -> List[FairValueGap]:
        """Return active (non-violated) FVGs, optionally filtered by direction."""
        if direction == "bullish":
            return [f for f in self._bullish_fvgs if f.status != "VIOLATED"]
        elif direction == "bearish":
            return [f for f in self._bearish_fvgs if f.status != "VIOLATED"]
        else:
            bullish = [f for f in self._bullish_fvgs if f.status != "VIOLATED"]
            bearish = [f for f in self._bearish_fvgs if f.status != "VIOLATED"]
            return bullish + bearish

    def check_zone_interaction(self, bar) -> List[Tuple[FairValueGap, str]]:
        """
        Check if the current bar interacts with any active FVG zones.

        Returns list of (FairValueGap, interaction_type) tuples where
        interaction_type is one of: "ENTER", "HOLD", "VIOLATE".
        """
        interactions = []
        for fvg in self._bullish_fvgs + self._bearish_fvgs:
            if fvg.status == "VIOLATED":
                continue
            interaction = self._classify_interaction(fvg, bar)
            if interaction:
                interactions.append((fvg, interaction))
        return interactions

    def get_stats(self) -> Dict:
        """Return summary statistics."""
        active_bullish = len([f for f in self._bullish_fvgs if f.status != "VIOLATED"])
        active_bearish = len([f for f in self._bearish_fvgs if f.status != "VIOLATED"])
        return {
            "active_bullish": active_bullish,
            "active_bearish": active_bearish,
            "total_active": active_bullish + active_bearish,
            "total_detected": self._total_detected,
            "total_violated": self._total_violated,
            "total_filled": self._total_filled,
        }

    # ================================================================
    # DETECTION
    # ================================================================
    def _detect_fvgs(self, bar_index: int, trend_direction: str) -> List[FairValueGap]:
        """Scan the last 3 completed candles for FVG patterns."""
        candle_0 = self._bars[-3]  # oldest of the 3
        candle_1 = self._bars[-2]  # middle (impulse)
        candle_2 = self._bars[-1]  # most recent

        new_fvgs = []

        # Bullish FVG: candle[0].high < candle[2].low
        if candle_0.high < candle_2.low:
            gap_size = candle_2.low - candle_0.high
            if gap_size >= self.MIN_FVG_SIZE:
                fvg_low = candle_0.high
                fvg_high = candle_2.low
                is_inverse = (trend_direction == "down")
                fvg = FairValueGap(
                    fvg_high=fvg_high,
                    fvg_low=fvg_low,
                    fvg_midpoint=round((fvg_high + fvg_low) / 2, 2),
                    formation_bar=bar_index,
                    formation_time=candle_1.timestamp,
                    direction="bullish",
                    is_inverse=is_inverse,
                    size_points=round(gap_size, 2),
                )
                if not self._duplicate_exists(fvg):
                    self._bullish_fvgs.append(fvg)
                    self._total_detected += 1
                    new_fvgs.append(fvg)
                    self._enforce_max_active("bullish")

        # Bearish FVG: candle[0].low > candle[2].high
        if candle_0.low > candle_2.high:
            gap_size = candle_0.low - candle_2.high
            if gap_size >= self.MIN_FVG_SIZE:
                fvg_high = candle_0.low
                fvg_low = candle_2.high
                is_inverse = (trend_direction == "up")
                fvg = FairValueGap(
                    fvg_high=fvg_high,
                    fvg_low=fvg_low,
                    fvg_midpoint=round((fvg_high + fvg_low) / 2, 2),
                    formation_bar=bar_index,
                    formation_time=candle_1.timestamp,
                    direction="bearish",
                    is_inverse=is_inverse,
                    size_points=round(gap_size, 2),
                )
                if not self._duplicate_exists(fvg):
                    self._bearish_fvgs.append(fvg)
                    self._total_detected += 1
                    new_fvgs.append(fvg)
                    self._enforce_max_active("bearish")

        return new_fvgs

    def _duplicate_exists(self, new_fvg: FairValueGap) -> bool:
        """Check if an FVG at very similar levels already exists."""
        target_list = self._bullish_fvgs if new_fvg.direction == "bullish" else self._bearish_fvgs
        for existing in target_list:
            if existing.status == "VIOLATED":
                continue
            if (abs(existing.fvg_high - new_fvg.fvg_high) < 1.0 and
                    abs(existing.fvg_low - new_fvg.fvg_low) < 1.0):
                return True
        return False

    def _enforce_max_active(self, direction: str) -> None:
        """Evict oldest FVGs if we exceed the max per direction."""
        target_list = self._bullish_fvgs if direction == "bullish" else self._bearish_fvgs
        active = [f for f in target_list if f.status != "VIOLATED"]
        if len(active) > self.MAX_ACTIVE_PER_DIRECTION:
            # Sort by formation_bar ascending -- oldest first
            active.sort(key=lambda f: f.formation_bar)
            to_remove = len(active) - self.MAX_ACTIVE_PER_DIRECTION
            for fvg in active[:to_remove]:
                fvg.status = "VIOLATED"
                self._total_violated += 1

    # ================================================================
    # LIFECYCLE
    # ================================================================
    def _update_lifecycle(self, bar, bar_index: int) -> None:
        """Update status of all active FVGs based on current bar."""
        for fvg in self._bullish_fvgs + self._bearish_fvgs:
            if fvg.status == "VIOLATED":
                continue

            # Skip lifecycle check on the formation bar -- the bar that
            # forms the FVG naturally overlaps it
            if fvg.formation_bar == bar_index:
                continue

            # Expiry check
            if bar_index - fvg.formation_bar > self.EXPIRY_BARS:
                fvg.status = "VIOLATED"
                self._total_violated += 1
                continue

            # Violation check: bar closes through zone
            if fvg.direction == "bullish" and bar.close < fvg.fvg_low:
                fvg.status = "VIOLATED"
                self._total_violated += 1
                continue
            elif fvg.direction == "bearish" and bar.close > fvg.fvg_high:
                fvg.status = "VIOLATED"
                self._total_violated += 1
                continue

            # Status progression
            if fvg.status == "UNFILLED":
                # Check if bar enters the zone
                if bar.low <= fvg.fvg_high and bar.high >= fvg.fvg_low:
                    fvg.status = "PARTIALLY_FILLED"

            if fvg.status == "PARTIALLY_FILLED":
                # Check if bar touches the CE midpoint
                if fvg.direction == "bullish":
                    if bar.low <= fvg.fvg_midpoint:
                        fvg.status = "FILLED"
                        self._total_filled += 1
                elif fvg.direction == "bearish":
                    if bar.high >= fvg.fvg_midpoint:
                        fvg.status = "FILLED"
                        self._total_filled += 1

        # Clean up violated FVGs to save memory
        self._bullish_fvgs = [f for f in self._bullish_fvgs if f.status != "VIOLATED"]
        self._bearish_fvgs = [f for f in self._bearish_fvgs if f.status != "VIOLATED"]

    def _classify_interaction(self, fvg: FairValueGap, bar) -> Optional[str]:
        """Classify how a bar interacts with an FVG zone."""
        bar_enters_zone = (bar.low <= fvg.fvg_high and bar.high >= fvg.fvg_low)

        if not bar_enters_zone:
            return None

        # Violation: closes through zone
        if fvg.direction == "bullish" and bar.close < fvg.fvg_low:
            return "VIOLATE"
        if fvg.direction == "bearish" and bar.close > fvg.fvg_high:
            return "VIOLATE"

        # HOLD: enters zone but closes within or on the right side
        if fvg.direction == "bullish":
            if bar.close >= fvg.fvg_low:
                return "HOLD"
        elif fvg.direction == "bearish":
            if bar.close <= fvg.fvg_high:
                return "HOLD"

        return "ENTER"
