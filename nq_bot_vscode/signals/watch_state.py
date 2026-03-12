"""
Watch State Manager -- Universal Confirmation Layer v2
=====================================================
UCL v2 manages watch states for wide-stop sweep signals that score >= 0.75
but get blocked by the 30pt max stop gate.  Post-sweep confirmation produces
a tighter stop from the confirmation level.

Lifecycle:
  Signal fires (score >= 0.75, stop > 30pt, sweep source)
      |
  WatchStateManager.add_watch()
      |
      +-- [each bar] --> check invalidation -> cancel if breached
      |                  check expiry -> remove if expired
      |                  evaluate confirmations -> update confirmations_met
      |
      +-- ALL confirmations met --> emit ConfirmedSignal
      |                            --> HC gate (boosted score, tight stop)
      |
      +-- Expired / Invalidated --> silently removed, logged
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from config.constants import (
    UCL_CONFIRMATION_BOOST,
    UCL_FVG_BOOST,
    UCL_FAST_CONFIRM_BOOST,
    UCL_HTF_ALIGN_BOOST,
)

logger = logging.getLogger(__name__)


@dataclass
class WatchState:
    """A signal awaiting market confirmation."""
    watch_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    setup_type: str = ""              # "sweep" | "wide_stop_sweep" | "break_retest" | "ob_reaction" | "fvg_tap"
    direction: str = ""               # "LONG" | "SHORT"
    trigger_bar: int = 0              # bar index when the signal fired
    trigger_price: float = 0.0        # price at signal time
    key_level: float = 0.0            # the structural level being watched
    invalidation_price: float = 0.0   # if price reaches here -> cancel watch
    expiry_bars: int = 60             # auto-cancel if no confirmation within N bars
    bars_elapsed: int = 0             # bars since creation
    confirmation_conditions: List[str] = field(default_factory=list)
    confirmations_met: Dict[str, bool] = field(default_factory=dict)
    metadata: Dict = field(default_factory=dict)
    base_score: float = 0.0           # original signal score at trigger time
    created_at: Optional[datetime] = None

    @property
    def is_confirmed(self) -> bool:
        """True when all confirmation conditions have been met."""
        if not self.confirmations_met:
            return False
        return all(self.confirmations_met.values())


@dataclass
class ConfirmedSignal:
    """A watch state that has been fully confirmed by market structure."""
    watch_id: str
    setup_type: str
    direction: str                     # "LONG" | "SHORT"
    base_score: float
    boosted_score: float
    bars_to_confirm: int
    trigger_price: float
    confirmation_price: float          # current price at confirmation
    key_level: float
    stop_distance: float = 0.0        # tight post-confirmation stop (from metadata)
    metadata: Dict = field(default_factory=dict)
    confirmations: Dict[str, bool] = field(default_factory=dict)
    has_fvg_confluence: bool = False
    htf_aligned: bool = False


class WatchStateManager:
    """
    Manages watch states and evaluates confirmation conditions each bar.

    Rules:
      - Max 3 concurrent watches.
      - One watch per (direction, setup_type) pair.
      - Each bar: decrement expiry, check invalidation, evaluate conditions.
      - When all conditions met: emit ConfirmedSignal with boosted score.
    """

    MAX_ACTIVE_WATCHES: int = 3
    FAST_CONFIRM_THRESHOLD: int = 20   # bars -- below this, fast-confirm boost applies

    def __init__(self):
        self._watches: List[WatchState] = []
        self._stats = {
            "created": 0,
            "confirmed": 0,
            "expired": 0,
            "invalidated": 0,
        }

    def add_watch(self, watch: WatchState) -> bool:
        """
        Add a new watch state.

        Enforces:
          - Uniqueness: replaces existing watch with same (direction, setup_type).
          - Max capacity: evicts oldest if at MAX_ACTIVE_WATCHES.

        Returns True if the watch was added.
        """
        # Remove existing watch with same (direction, setup_type)
        self._watches = [
            w for w in self._watches
            if not (w.direction == watch.direction and w.setup_type == watch.setup_type)
        ]

        # Evict oldest if at capacity
        if len(self._watches) >= self.MAX_ACTIVE_WATCHES:
            evicted = self._watches.pop(0)
            logger.debug(
                f"UCL: evicted oldest watch {evicted.watch_id} "
                f"({evicted.setup_type}/{evicted.direction}) for capacity"
            )

        # Initialize confirmations_met from conditions
        if not watch.confirmations_met:
            watch.confirmations_met = {c: False for c in watch.confirmation_conditions}

        self._watches.append(watch)
        self._stats["created"] += 1
        logger.debug(
            f"UCL: watch added {watch.watch_id} | "
            f"{watch.setup_type}/{watch.direction} | "
            f"score={watch.base_score:.3f} | "
            f"level={watch.key_level:.2f} | "
            f"expiry={watch.expiry_bars} bars"
        )
        return True

    def update(self, bar, fvg_detector, htf_bias=None) -> List[ConfirmedSignal]:
        """
        Evaluate all active watches against the current bar.

        Args:
            bar: Current execution-TF bar (Bar object).
            fvg_detector: FVGDetector instance for FVG-based confirmations.
            htf_bias: Optional HTFBiasResult for HTF alignment boost.

        Returns:
            List of ConfirmedSignal objects for watches that completed.
        """
        confirmed_signals = []
        to_remove = []

        for watch in self._watches:
            watch.bars_elapsed += 1

            # --- Expiry check ---
            if watch.bars_elapsed >= watch.expiry_bars:
                to_remove.append(watch)
                self._stats["expired"] += 1
                logger.debug(
                    f"UCL: watch expired {watch.watch_id} | "
                    f"{watch.setup_type}/{watch.direction} after {watch.bars_elapsed} bars"
                )
                continue

            # --- Invalidation check ---
            if self._is_invalidated(watch, bar):
                to_remove.append(watch)
                self._stats["invalidated"] += 1
                logger.debug(
                    f"UCL: watch invalidated {watch.watch_id} | "
                    f"{watch.setup_type}/{watch.direction} | "
                    f"close={bar.close:.2f} vs invalidation={watch.invalidation_price:.2f}"
                )
                continue

            # --- Evaluate confirmation conditions ---
            self._evaluate_conditions(watch, bar, fvg_detector)

            # --- Check if fully confirmed ---
            if watch.is_confirmed:
                # Compute boosted score
                has_fvg = self._has_fvg_confluence(watch, fvg_detector)
                htf_aligned = self._is_htf_aligned(watch, htf_bias)
                boosted_score = self._compute_boosted_score(
                    watch, has_fvg, htf_aligned
                )

                # Use tight confirmed stop from metadata if available
                confirmed_stop = watch.metadata.get("confirmed_stop_distance", 0.0)

                signal = ConfirmedSignal(
                    watch_id=watch.watch_id,
                    setup_type=watch.setup_type,
                    direction=watch.direction,
                    base_score=watch.base_score,
                    boosted_score=boosted_score,
                    bars_to_confirm=watch.bars_elapsed,
                    trigger_price=watch.trigger_price,
                    confirmation_price=bar.close,
                    key_level=watch.key_level,
                    stop_distance=confirmed_stop,
                    metadata=watch.metadata,
                    confirmations=dict(watch.confirmations_met),
                    has_fvg_confluence=has_fvg,
                    htf_aligned=htf_aligned,
                )
                confirmed_signals.append(signal)
                to_remove.append(watch)
                self._stats["confirmed"] += 1
                logger.info(
                    f"UCL CONFIRMED: {watch.setup_type}/{watch.direction} | "
                    f"score {watch.base_score:.3f} -> {boosted_score:.3f} | "
                    f"bars={watch.bars_elapsed} | id={watch.watch_id}"
                )

        # Remove completed/expired/invalidated watches
        for w in to_remove:
            if w in self._watches:
                self._watches.remove(w)

        return confirmed_signals

    def cancel(self, watch_id: str) -> None:
        """Cancel a specific watch by ID."""
        self._watches = [w for w in self._watches if w.watch_id != watch_id]

    def get_active_watches(self) -> List[WatchState]:
        """Return all active watch states."""
        return list(self._watches)

    def get_stats(self) -> Dict:
        """Return watch state statistics."""
        return {
            **self._stats,
            "active": len(self._watches),
            "active_types": [
                f"{w.setup_type}/{w.direction}" for w in self._watches
            ],
        }

    # ================================================================
    # INVALIDATION
    # ================================================================
    def _is_invalidated(self, watch: WatchState, bar) -> bool:
        """Check if current bar invalidates the watch."""
        if watch.direction == "LONG":
            return bar.close < watch.invalidation_price
        elif watch.direction == "SHORT":
            return bar.close > watch.invalidation_price
        return False

    # ================================================================
    # CONDITION EVALUATION
    # ================================================================
    def _evaluate_conditions(self, watch: WatchState, bar, fvg_detector) -> None:
        """Evaluate all unmet confirmation conditions for a watch."""
        if watch.setup_type in ("sweep", "wide_stop_sweep"):
            self._evaluate_sweep_conditions(watch, bar, fvg_detector)
        elif watch.setup_type == "fvg_tap":
            self._evaluate_fvg_tap_conditions(watch, bar)

    def _evaluate_sweep_conditions(self, watch: WatchState, bar, fvg_detector) -> None:
        """
        Sweep confirmation: RECLAIM -> FVG_FORM -> FVG_TAP
        Conditions must be met in order.
        """
        # RECLAIM: price closes back above/below swept level
        if not watch.confirmations_met.get("RECLAIM", False):
            if watch.direction == "LONG" and bar.close > watch.key_level:
                watch.confirmations_met["RECLAIM"] = True
                logger.debug(f"UCL {watch.watch_id}: RECLAIM confirmed (close={bar.close:.2f} > level={watch.key_level:.2f})")
            elif watch.direction == "SHORT" and bar.close < watch.key_level:
                watch.confirmations_met["RECLAIM"] = True
                logger.debug(f"UCL {watch.watch_id}: RECLAIM confirmed (close={bar.close:.2f} < level={watch.key_level:.2f})")
            return  # Must reclaim before checking FVG

        # FVG_FORM: a new FVG forms in the recovery direction
        if not watch.confirmations_met.get("FVG_FORM", False):
            fvg_dir = "bullish" if watch.direction == "LONG" else "bearish"
            active = fvg_detector.get_active_fvgs(fvg_dir)
            # Look for FVGs formed after the trigger
            for fvg in active:
                if fvg.formation_bar > watch.trigger_bar:
                    watch.confirmations_met["FVG_FORM"] = True
                    watch.metadata["confirmed_fvg_high"] = fvg.fvg_high
                    watch.metadata["confirmed_fvg_low"] = fvg.fvg_low
                    watch.metadata["confirmed_fvg_midpoint"] = fvg.fvg_midpoint
                    logger.debug(
                        f"UCL {watch.watch_id}: FVG_FORM confirmed | "
                        f"FVG {fvg.fvg_low:.2f}-{fvg.fvg_high:.2f}"
                    )
                    break
            return  # Must have FVG before checking tap

        # FVG_TAP: price returns to FVG zone and holds
        if not watch.confirmations_met.get("FVG_TAP", False):
            fvg_high = watch.metadata.get("confirmed_fvg_high", 0)
            fvg_low = watch.metadata.get("confirmed_fvg_low", 0)
            if fvg_high == 0 and fvg_low == 0:
                return

            # Bar enters FVG zone
            bar_enters = (bar.low <= fvg_high and bar.high >= fvg_low)
            if bar_enters:
                # HOLD check: doesn't close through the zone
                if watch.direction == "LONG" and bar.close >= fvg_low:
                    watch.confirmations_met["FVG_TAP"] = True
                    logger.debug(f"UCL {watch.watch_id}: FVG_TAP confirmed (hold above {fvg_low:.2f})")
                elif watch.direction == "SHORT" and bar.close <= fvg_high:
                    watch.confirmations_met["FVG_TAP"] = True
                    logger.debug(f"UCL {watch.watch_id}: FVG_TAP confirmed (hold below {fvg_high:.2f})")

    def _evaluate_fvg_tap_conditions(self, watch: WatchState, bar) -> None:
        """
        FVG Tap confirmation: ENTER_ZONE -> HOLD -> CONTINUATION
        """
        fvg_high = watch.metadata.get("fvg_high", 0)
        fvg_low = watch.metadata.get("fvg_low", 0)

        # ENTER_ZONE: price enters FVG zone
        if not watch.confirmations_met.get("ENTER_ZONE", False):
            if bar.low <= fvg_high and bar.high >= fvg_low:
                watch.confirmations_met["ENTER_ZONE"] = True
                logger.debug(f"UCL {watch.watch_id}: ENTER_ZONE confirmed")
            return

        # HOLD: candle closes within or on the right side of the FVG
        if not watch.confirmations_met.get("HOLD", False):
            if bar.low <= fvg_high and bar.high >= fvg_low:
                if watch.direction == "LONG" and bar.close >= fvg_low:
                    watch.confirmations_met["HOLD"] = True
                    watch.metadata["hold_close"] = bar.close
                    logger.debug(f"UCL {watch.watch_id}: HOLD confirmed")
                elif watch.direction == "SHORT" and bar.close <= fvg_high:
                    watch.confirmations_met["HOLD"] = True
                    watch.metadata["hold_close"] = bar.close
                    logger.debug(f"UCL {watch.watch_id}: HOLD confirmed")
            return

        # CONTINUATION: next candle moves in expected direction
        if not watch.confirmations_met.get("CONTINUATION", False):
            hold_close = watch.metadata.get("hold_close", 0)
            if hold_close == 0:
                return
            if watch.direction == "LONG" and bar.close > hold_close:
                watch.confirmations_met["CONTINUATION"] = True
                logger.debug(f"UCL {watch.watch_id}: CONTINUATION confirmed")
            elif watch.direction == "SHORT" and bar.close < hold_close:
                watch.confirmations_met["CONTINUATION"] = True
                logger.debug(f"UCL {watch.watch_id}: CONTINUATION confirmed")

    # ================================================================
    # SCORE BOOST
    # ================================================================
    def _compute_boosted_score(
        self, watch: WatchState, has_fvg: bool, htf_aligned: bool
    ) -> float:
        """Compute the boosted score for a confirmed watch."""
        score = watch.base_score

        # Base confirmation boost
        score += UCL_CONFIRMATION_BOOST

        # FVG confluence
        if has_fvg:
            score += UCL_FVG_BOOST

        # Fast confirmation
        if watch.bars_elapsed < self.FAST_CONFIRM_THRESHOLD:
            score += UCL_FAST_CONFIRM_BOOST

        # HTF alignment
        if htf_aligned:
            score += UCL_HTF_ALIGN_BOOST

        return round(min(score, 1.0), 3)

    def _has_fvg_confluence(self, watch: WatchState, fvg_detector) -> bool:
        """Check if there's FVG confluence at the confirmation point."""
        if watch.setup_type in ("sweep", "wide_stop_sweep"):
            # For sweeps, FVG_FORM already implies confluence
            return watch.confirmations_met.get("FVG_FORM", False)
        elif watch.setup_type == "fvg_tap":
            return True  # FVG tap is inherently FVG-confluent
        return False

    def _is_htf_aligned(self, watch: WatchState, htf_bias) -> bool:
        """Check if HTF bias aligns with the watch direction."""
        if htf_bias is None:
            return False
        if watch.direction == "LONG":
            return htf_bias.htf_allows_long
        elif watch.direction == "SHORT":
            return htf_bias.htf_allows_short
        return False
