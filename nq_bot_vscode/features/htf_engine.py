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

from config.constants import HTF_STRENGTH_GATE, HTF_STALENESS_LIMITS

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
    STRENGTH_GATE = HTF_STRENGTH_GATE  # Imported from config/constants.py — single source of truth
    _STALENESS_LIMITS = HTF_STALENESS_LIMITS  # Imported from config/constants.py

    def __init__(self, config=None, timeframes: List[str] = None):
        self.config = config
        self.timeframes = timeframes or ["5m", "15m", "30m", "1H", "4H", "1D"]
        self._bars: Dict[str, List[HTFBar]] = {tf: [] for tf in self.timeframes}
        self._biases: Dict[str, str] = {}
        self._last_result: Optional[HTFBiasResult] = None
        self._total_updates = 0
        self._last_update_time: Dict[str, datetime] = {}
        self._stale_warned: set = set()

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
        self._last_update_time[timeframe] = bar.timestamp
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
                    "ingested — staleness check would be silently skipped.  "
                    "Pass the current bar timestamp."
                )
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
            direction = "bullish"
        elif bearish > bullish:
            direction = "bearish"
        else:
            direction = "neutral"

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

    def _check_staleness(self, now: datetime) -> set:
        """Return set of timeframe labels whose last bar is stale."""
        stale = set()
        for tf in self.timeframes:
            last = self._last_update_time.get(tf)
            if last is None:
                continue  # Never received data — handled by "no bars" path
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
                        "(limit %d min) — bias downgraded to neutral",
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
        lines.append("=" * 40)
        return "\n".join(lines)
