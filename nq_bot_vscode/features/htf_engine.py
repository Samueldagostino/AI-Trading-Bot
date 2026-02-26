"""
HTF Bias Engine
================
Multi-timeframe directional consensus for gating execution-TF entries.

Tracks higher-timeframe bars (15m, 30m, 1H, 4H, 1D) and derives
a directional bias used to filter trades on the execution timeframe.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

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
    htf_allows_long: bool = True
    htf_allows_short: bool = True
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

    def __init__(self, config=None, timeframes: List[str] = None):
        self.config = config
        self.timeframes = timeframes or ["5m", "15m", "30m", "1H", "4H", "1D"]
        self._bars: Dict[str, List[HTFBar]] = {tf: [] for tf in self.timeframes}
        self._biases: Dict[str, str] = {}
        self._last_result: Optional[HTFBiasResult] = None
        self._total_updates = 0

    def update_bar(self, timeframe: str, bar: HTFBar) -> None:
        """Ingest a new HTF bar and recompute bias for that TF."""
        if timeframe not in self._bars:
            self._bars[timeframe] = []
        self._bars[timeframe].append(bar)
        if len(self._bars[timeframe]) > self.WINDOW:
            self._bars[timeframe] = self._bars[timeframe][-self.WINDOW:]
        self._biases[timeframe] = self._compute_tf_bias(timeframe)
        self._total_updates += 1

    def get_bias(self, timestamp: datetime = None) -> HTFBiasResult:
        """Return current multi-TF consensus."""
        bullish = 0
        bearish = 0
        total = 0
        for tf, bias in self._biases.items():
            total += 1
            if bias == "bullish":
                bullish += 1
            elif bias == "bearish":
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
            htf_allows_long=(direction != "bearish" or strength < 0.7),
            htf_allows_short=(direction != "bullish" or strength < 0.7),
            timestamp=timestamp,
            tf_biases=dict(self._biases),
        )
        self._last_result = result
        return result

    def _compute_tf_bias(self, timeframe: str) -> str:
        """Simple trend: compare latest close to open of lookback window."""
        bars = self._bars.get(timeframe, [])
        if len(bars) < 3:
            return "neutral"
        lookback = min(10, len(bars))
        recent = bars[-lookback:]
        net = recent[-1].close - recent[0].open
        avg_range = sum(b.high - b.low for b in recent) / lookback
        if avg_range == 0:
            return "neutral"
        ratio = net / avg_range
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
            lines.append(f"  {tf:>4s}: {count:>4d} bars | bias={bias}")
        if self._last_result:
            r = self._last_result
            lines.append(f"  Consensus: {r.consensus_direction} ({r.consensus_strength:.2f})")
            lines.append(f"  Allows long={r.htf_allows_long}  short={r.htf_allows_short}")
        lines.append("=" * 40)
        return "\n".join(lines)
