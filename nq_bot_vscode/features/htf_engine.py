"""
HTF Bias Engine
================
Multi-timeframe directional consensus for gating execution-TF entries.

Tracks higher-timeframe bars (15m, 30m, 1H, 4H, 1D) and derives
a directional bias used to filter trades on the execution timeframe.
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from config.constants import (
    HTF_STRENGTH_GATE,
    HTF_STALENESS_LIMITS,
    HTF_HYSTERESIS_MARGIN,
    HTF_HYSTERESIS_CONFIRM_BARS,
)

logger = logging.getLogger(__name__)


@dataclass
class HTFBar:
    """Single higher-timeframe OHLCV bar."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


@dataclass
class HTFBiasResult:
    """Consensus bias from all HTF timeframes."""
    consensus_direction: str = "neutral"   # "bullish", "bearish", "neutral"
    consensus_strength: float = 0.0        # 0.0 - 1.0
    htf_allows_long: bool = False          # FAIL-SAFE: default blocks trades
    htf_allows_short: bool = False         # FAIL-SAFE: default blocks trades
    timestamp: Optional[datetime] = None
    tf_biases: Dict[str, str] = field(default_factory=dict)


class HTFBiasEngine:
    """
    Tracks higher-timeframe bars and computes directional consensus.

    Each timeframe keeps a rolling window of recent bars.  A simple
    trend detection (close vs open over the window) determines per-TF
    bias, and the consensus across all TFs gates execution entries.
    """

    WINDOW = 20  # bars per timeframe to retain
    STRENGTH_GATE = HTF_STRENGTH_GATE  # Imported from config/constants.py -- single source of truth
    _STALENESS_LIMITS = HTF_STALENESS_LIMITS  # Imported from config/constants.py

    # ── Hysteresis: prevent flip-flopping on choppy days ──
    # Two-stage protection:
    #   Stage 1 (margin): opposing direction must exceed HYSTERESIS_MARGIN
    #           strength to even START the confirmation counter.
    #   Stage 2 (hold):   opposing direction must HOLD for CONFIRM_BARS
    #           consecutive get_bias() calls before the lock actually flips.
    # A single fakeout candle won't override a persistent trend.
    HYSTERESIS_MARGIN = HTF_HYSTERESIS_MARGIN
    CONFIRM_BARS = HTF_HYSTERESIS_CONFIRM_BARS

    # Map timeframe labels to their period in minutes (for completion-time tracking)
    _TF_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1H": 60, "4H": 240, "1D": 1440}

    def __init__(self, config=None, timeframes: List[str] = None,
                 backtest_mode: bool = False):
        self.config = config
        self.timeframes = timeframes or ["5m", "15m", "30m", "1H", "4H", "1D"]
        self.backtest_mode = backtest_mode  # Skip staleness checks in backtests
        self._bars: Dict[str, List[HTFBar]] = {tf: [] for tf in self.timeframes}
        self._biases: Dict[str, str] = {}
        self._last_result: Optional[HTFBiasResult] = None
        self._total_updates = 0
        self._last_update_time: Dict[str, datetime] = {}
        self._stale_warned: set = set()
        self._locked_direction: str = "neutral"  # Sticky directional lock
        self._lock_strength: float = 0.0         # Strength when lock was set
        self._flip_candidate: str = "neutral"    # Direction trying to flip TO
        self._flip_confirm_count: int = 0        # Consecutive bars candidate held

    def update_bar(self, timeframe: str, bar: HTFBar) -> None:
        """Ingest a new HTF bar and recompute bias for that TF."""
        # Skip timeframes not in our configured list
        if timeframe not in self.timeframes:
            return
        if timeframe not in self._bars:
            self._bars[timeframe] = []
        self._bars[timeframe].append(bar)
        if len(self._bars[timeframe]) > self.WINDOW:
            self._bars[timeframe] = self._bars[timeframe][-self.WINDOW:]
        self._biases[timeframe] = self._compute_tf_bias(timeframe)
        # Store bar COMPLETION time (start + period) for accurate staleness tracking.
        # A 5m bar starting at 09:05 represents data through 09:10, so staleness
        # should measure from 09:10 — not 09:05.
        tf_min = self._TF_MINUTES.get(timeframe, 0)
        self._last_update_time[timeframe] = bar.timestamp + timedelta(minutes=tf_min)
        self._total_updates += 1

    def get_bias(self, timestamp: datetime = None) -> HTFBiasResult:
        """Return current multi-TF consensus.

        Stale timeframes (no bar update within the staleness limit)
        are downgraded to "neutral" so they don't contribute a
        directional vote.  A warning is logged once per stale TF.

        Args:
            timestamp: Current time for staleness checks.  Required for
                       live trading.  May be None only during initial
                       warmup when no bars have been processed yet.

        Raises:
            ValueError: If timestamp is None and bars have been ingested
                        (staleness check would be silently skipped).
        """
        if timestamp is None:
            if self._total_updates > 0:
                raise ValueError(
                    "get_bias() called without timestamp after bars have been "
                    "ingested -- staleness check would be silently skipped.  "
                    "Pass the current bar timestamp."
                )
            stale_tfs = set()
        elif self.backtest_mode:
            # In backtest mode, HTF bars are pre-built and fed causally by
            # HTFScheduler — staleness is an artifact of CSV data gaps
            # (overnight, settlement breaks), not a broken data feed.
            stale_tfs = set()
        else:
            stale_tfs = self._check_staleness(timestamp)

        bullish = 0
        bearish = 0
        total = 0
        effective_biases: Dict[str, str] = {}
        for tf, bias in self._biases.items():
            if tf in stale_tfs:
                effective_biases[tf] = "neutral"
            else:
                effective_biases[tf] = bias
            total += 1
            if effective_biases[tf] == "bullish":
                bullish += 1
            elif effective_biases[tf] == "bearish":
                bearish += 1

        if total == 0:
            return HTFBiasResult(timestamp=timestamp)

        strength = max(bullish, bearish) / total
        if bullish > bearish:
            raw_direction = "bullish"
        elif bearish > bullish:
            raw_direction = "bearish"
        else:
            raw_direction = "neutral"

        # ── Hysteresis: two-stage anti-flip-flop ──────────────────────
        # Stage 1 — margin check: does the raw signal OPPOSE the current
        #   lock strongly enough to even start the confirmation timer?
        # Stage 2 — hold check: has the opposing signal persisted for
        #   CONFIRM_BARS consecutive calls?  If not, lock stays put.
        direction = self._apply_hysteresis(raw_direction, strength)

        result = HTFBiasResult(
            consensus_direction=direction,
            consensus_strength=round(strength, 3),
            htf_allows_long=(direction != "bearish" or strength < self.STRENGTH_GATE),
            htf_allows_short=(direction != "bullish" or strength < self.STRENGTH_GATE),
            timestamp=timestamp,
            tf_biases=effective_biases,
        )
        self._last_result = result
        return result

    def _apply_hysteresis(self, raw_direction: str, strength: float) -> str:
        """Apply two-stage hysteresis to prevent bias flip-flopping.

        Stage 1 (margin):  The opposing direction must exceed
                           HYSTERESIS_MARGIN strength to start the timer.
        Stage 2 (hold):    The opposing direction must persist for
                           CONFIRM_BARS consecutive get_bias() calls
                           before the lock flips.

        Returns the effective (possibly locked) direction.
        """
        locked = self._locked_direction

        # ── First bias ever: just lock it immediately ──
        if locked == "neutral":
            if raw_direction != "neutral":
                self._locked_direction = raw_direction
                self._lock_strength = strength
                self._flip_candidate = "neutral"
                self._flip_confirm_count = 0
                logger.info(
                    "HTF bias lock INITIAL: %s (strength=%.2f)",
                    raw_direction, strength,
                )
            return raw_direction

        # ── Same direction as lock: reinforce it ──
        if raw_direction == locked:
            self._lock_strength = strength
            self._flip_candidate = "neutral"
            self._flip_confirm_count = 0
            return locked

        # ── Neutral raw: not strong enough to flip, keep the lock ──
        # Neutral doesn't start a flip timer — it's indecision, not opposition.
        if raw_direction == "neutral":
            self._flip_candidate = "neutral"
            self._flip_confirm_count = 0
            return locked

        # ── Opposing direction detected ──
        # Stage 1: Is the opposing signal strong enough to even consider?
        if strength < self.HYSTERESIS_MARGIN:
            # Too weak — reset any in-progress flip attempt
            self._flip_candidate = "neutral"
            self._flip_confirm_count = 0
            return locked

        # Stage 2: Opposing signal is strong enough — run confirmation timer
        if raw_direction == self._flip_candidate:
            # Same flip candidate as last call — increment counter
            self._flip_confirm_count += 1
        else:
            # New flip candidate — restart the timer
            self._flip_candidate = raw_direction
            self._flip_confirm_count = 1

        if self._flip_confirm_count >= self.CONFIRM_BARS:
            # Confirmed flip: opposing direction held for enough bars
            old_lock = self._locked_direction
            self._locked_direction = raw_direction
            self._lock_strength = strength
            self._flip_candidate = "neutral"
            self._flip_confirm_count = 0
            logger.info(
                "HTF bias lock FLIPPED: %s → %s after %d confirming bars "
                "(strength=%.2f)",
                old_lock, raw_direction, self.CONFIRM_BARS, strength,
            )
            return raw_direction

        # Not yet confirmed — keep the existing lock
        logger.debug(
            "HTF hysteresis: %s attempting flip to %s (%d/%d confirms, "
            "strength=%.2f) — lock stays %s",
            locked, raw_direction, self._flip_confirm_count,
            self.CONFIRM_BARS, strength, locked,
        )
        return locked

    def _check_staleness(self, now: datetime) -> set:
        """Return set of timeframe labels whose last bar is stale."""
        stale = set()
        for tf in self.timeframes:
            last = self._last_update_time.get(tf)
            if last is None:
                continue  # Never received data -- handled by "no bars" path
            limit_minutes = self._STALENESS_LIMITS.get(tf, 180)
            # Make both timestamps offset-aware for comparison
            if now.tzinfo is None:
                now_aware = now.replace(tzinfo=timezone.utc)
            else:
                now_aware = now
            if last.tzinfo is None:
                last_aware = last.replace(tzinfo=timezone.utc)
            else:
                last_aware = last
            age = now_aware - last_aware
            if age > timedelta(minutes=limit_minutes):
                if tf not in self._stale_warned:
                    logger.warning(
                        "HTF staleness: %s last bar is %.1f min old "
                        "(limit %d min) -- bias downgraded to neutral",
                        tf, age.total_seconds() / 60, limit_minutes,
                    )
                    self._stale_warned.add(tf)
                stale.add(tf)
            else:
                # Clear warning flag so it can warn again next time it goes stale
                self._stale_warned.discard(tf)
        return stale

    def _compute_tf_bias(self, timeframe: str) -> str:
        """Simple trend: compare latest close to open of lookback window."""
        bars = self._bars.get(timeframe, [])
        if len(bars) < 3:
            return "neutral"
        lookback = min(10, len(bars))
        recent = bars[-lookback:]
        net = recent[-1].close - recent[0].open
        avg_range = sum(b.high - b.low for b in recent) / lookback
        if avg_range == 0 or not math.isfinite(avg_range):
            return "neutral"
        if not math.isfinite(net):
            return "neutral"
        ratio = net / avg_range
        if not math.isfinite(ratio):
            return "neutral"
        if ratio > 0.5:
            return "bullish"
        elif ratio < -0.5:
            return "bearish"
        return "neutral"

    def get_summary(self) -> str:
        """Return human-readable summary for logging."""
        lines = ["HTF BIAS ENGINE SUMMARY", "=" * 40]
        lines.append(f"  Total bar updates: {self._total_updates}")
        for tf in self.timeframes:
            count = len(self._bars.get(tf, []))
            bias = self._biases.get(tf, "n/a")
            last = self._last_update_time.get(tf)
            age_str = ""
            if last is not None:
                age_str = f" | last={last.strftime('%H:%M')}"
            lines.append(f"  {tf:>4s}: {count:>4d} bars | bias={bias}{age_str}")
        if self._last_result:
            r = self._last_result
            lines.append(f"  Consensus: {r.consensus_direction} ({r.consensus_strength:.2f})")
            lines.append(f"  Allows long={r.htf_allows_long}  short={r.htf_allows_short}")
        lines.append(f"  Hysteresis lock: {self._locked_direction} "
                     f"(flip candidate={self._flip_candidate}, "
                     f"confirms={self._flip_confirm_count}/{self.CONFIRM_BARS})")
        lines.append("=" * 40)
        return "\n".join(lines)
