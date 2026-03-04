"""
Institutional Modifier Layer — Phase 1
========================================
Overnight Bias + Pre-FOMC Drift modifiers as position sizing multipliers.

These modifiers are applied AFTER HTF gate approval and confluence score
calculation, BEFORE trade execution. They adjust position size, stop width,
and C2 runner trail width via multipliers.

Rules:
  - Modifiers multiply sequentially: final = base × overnight × fomc
  - Maximum total multiplier cap: 2.0x
  - Minimum total multiplier floor: 0.3x
  - Modifiers NEVER veto trades (except FOMC <0.5h stand-aside)

All thresholds sourced from V3 Integration Execution Plan.
"""

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict
from zoneinfo import ZoneInfo

from config.fomc_calendar import hours_until_next_fomc

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ── Modifier Result ─────────────────────────────────────────────────
@dataclass
class ModifierResult:
    """Result from a single modifier or the combined engine."""
    position_multiplier: float = 1.0
    stop_multiplier: float = 1.0
    runner_multiplier: float = 1.0
    stand_aside: bool = False
    stand_aside_reason: str = ""
    details: Dict = field(default_factory=dict)


# ── Constants — from V3 Integration Execution Plan ──────────────────
MAX_TOTAL_MULTIPLIER = 2.0
MIN_TOTAL_MULTIPLIER = 0.3

# Overnight Bias thresholds (bps)
OVERNIGHT_NEUTRAL_BPS = 50
OVERNIGHT_EXTREME_BPS = 120

# Overnight Bias multipliers by classification
OVERNIGHT_MULTIPLIERS = {
    "neutral": {"position": 1.0, "stop": 1.0, "runner": 1.0},
    "alignment_significant": {"position": 1.4, "stop": 1.0, "runner": 1.2},
    "alignment_extreme": {"position": 1.5, "stop": 1.0, "runner": 1.3},
    "conflict_significant": {"position": 0.6, "stop": 0.8, "runner": 0.8},
    "conflict_extreme": {"position": 0.4, "stop": 0.7, "runner": 0.7},
}

# FOMC Drift windows and multipliers
FOMC_WINDOWS = {
    "no_fomc": {"position": 1.0, "stop": 1.0, "runner": 1.0},
    "24h_to_4h": {"position": 1.1, "stop": 0.9, "runner": 1.0},
    "4h_to_0.5h": {"position": 1.15, "stop": 0.85, "runner": 1.0},
    # <0.5h = stand-aside (no multipliers, trade blocked)
}


# =====================================================================
#  OVERNIGHT BIAS MODIFIER
# =====================================================================
class OvernightBiasModifier:
    """
    Calculates overnight gap in bps, classifies alignment/conflict with
    HTF bias, and returns position/stop/runner multipliers.

    Input: previous day close (4PM ET), current day open (9:30AM ET),
           HTF bias direction.
    """

    def __init__(self):
        self._prev_day_close: Optional[float] = None
        self._prev_close_date: Optional[object] = None
        self._current_day_open: Optional[float] = None
        self._current_open_date: Optional[object] = None
        self._overnight_bps: float = 0.0
        self._classification: str = "neutral"
        self._last_compute_date: Optional[object] = None

    def update_bar(self, bar) -> None:
        """Track session close (4PM ET) and RTH open (9:30AM ET) from bar stream."""
        et_time = bar.timestamp.astimezone(ET)
        current_date = et_time.date()
        h, m = et_time.hour, et_time.minute
        t = h + m / 60.0

        # Capture session close — last bar in the 15:58–16:02 window
        if 15 + 58 / 60 <= t <= 16 + 2 / 60:
            self._prev_day_close = bar.close
            self._prev_close_date = current_date

        # Capture RTH open — first bar at 9:30 AM ET (new day only)
        if 9.5 <= t < 9.5 + 4 / 60:  # 9:30–9:34 window (covers 2m bar)
            if self._current_open_date != current_date:
                self._current_day_open = bar.open
                self._current_open_date = current_date

                # Compute overnight bias if we have previous close
                if (self._prev_day_close is not None
                        and self._prev_close_date != current_date):
                    self._overnight_bps = (
                        (self._current_day_open - self._prev_day_close)
                        / self._prev_day_close
                    ) * 10000
                    self._last_compute_date = current_date

    def calculate(self, htf_bias_direction: Optional[str]) -> ModifierResult:
        """Return multipliers based on overnight bias vs HTF direction."""
        abs_bps = abs(self._overnight_bps)

        # Below threshold = neutral
        if abs_bps < OVERNIGHT_NEUTRAL_BPS:
            self._classification = "neutral"
            mults = OVERNIGHT_MULTIPLIERS["neutral"]
            return ModifierResult(
                position_multiplier=mults["position"],
                stop_multiplier=mults["stop"],
                runner_multiplier=mults["runner"],
                details={
                    "overnight_bps": round(self._overnight_bps, 2),
                    "abs_bps": round(abs_bps, 2),
                    "classification": "neutral",
                    "htf_direction": htf_bias_direction or "unknown",
                },
            )

        # Determine overnight direction
        overnight_direction = "bullish" if self._overnight_bps > 0 else "bearish"

        # Determine alignment with HTF bias
        htf_dir = (htf_bias_direction or "neutral").lower()
        if htf_dir == "neutral" or htf_dir not in ("bullish", "bearish"):
            # Neutral HTF = treat as neutral modifier
            self._classification = "neutral"
            mults = OVERNIGHT_MULTIPLIERS["neutral"]
        elif overnight_direction == htf_dir:
            # Aligned
            if abs_bps >= OVERNIGHT_EXTREME_BPS:
                self._classification = "alignment_extreme"
            else:
                self._classification = "alignment_significant"
            mults = OVERNIGHT_MULTIPLIERS[self._classification]
        else:
            # Conflict
            if abs_bps >= OVERNIGHT_EXTREME_BPS:
                self._classification = "conflict_extreme"
            else:
                self._classification = "conflict_significant"
            mults = OVERNIGHT_MULTIPLIERS[self._classification]

        return ModifierResult(
            position_multiplier=mults["position"],
            stop_multiplier=mults["stop"],
            runner_multiplier=mults["runner"],
            details={
                "overnight_bps": round(self._overnight_bps, 2),
                "abs_bps": round(abs_bps, 2),
                "overnight_direction": overnight_direction,
                "htf_direction": htf_dir,
                "classification": self._classification,
                "prev_close": self._prev_day_close,
                "current_open": self._current_day_open,
            },
        )

    @property
    def overnight_bps(self) -> float:
        return self._overnight_bps

    @property
    def classification(self) -> str:
        return self._classification


# =====================================================================
#  FOMC DRIFT MODIFIER
# =====================================================================
class FOMCDriftModifier:
    """
    Checks hours until next FOMC announcement and returns multipliers
    per window:
      - No FOMC within 24h: 1.0x all
      - 24–4h before FOMC: position 1.1x, stop 0.9x
      - 4–0.5h before FOMC: position 1.15x, stop 0.85x
      - <0.5h before FOMC: STAND ASIDE (block trade)
    """

    def calculate(self, current_time: datetime) -> ModifierResult:
        """Return multipliers based on proximity to next FOMC."""
        hours = hours_until_next_fomc(current_time)

        # No upcoming FOMC or > 24h away
        if hours is None or hours > 24.0:
            mults = FOMC_WINDOWS["no_fomc"]
            return ModifierResult(
                position_multiplier=mults["position"],
                stop_multiplier=mults["stop"],
                runner_multiplier=mults["runner"],
                details={
                    "hours_until_fomc": round(hours, 2) if hours is not None else None,
                    "window": "no_fomc",
                },
            )

        # < 0.5h = STAND ASIDE
        if hours <= 0.5:
            return ModifierResult(
                stand_aside=True,
                stand_aside_reason=f"FOMC in {hours:.2f}h — stand aside",
                details={
                    "hours_until_fomc": round(hours, 2),
                    "window": "stand_aside",
                },
            )

        # 4h–0.5h window
        if hours <= 4.0:
            mults = FOMC_WINDOWS["4h_to_0.5h"]
            window = "4h_to_0.5h"
        # 24h–4h window
        else:
            mults = FOMC_WINDOWS["24h_to_4h"]
            window = "24h_to_4h"

        return ModifierResult(
            position_multiplier=mults["position"],
            stop_multiplier=mults["stop"],
            runner_multiplier=mults["runner"],
            details={
                "hours_until_fomc": round(hours, 2),
                "window": window,
            },
        )


# =====================================================================
#  INSTITUTIONAL MODIFIER ENGINE
# =====================================================================
class InstitutionalModifierEngine:
    """
    Orchestrates all institutional modifiers. Applies them sequentially
    and enforces 2.0x max cap and 0.3x min floor on total multiplier.

    Usage in process_bar():
        engine.update_bar(bar)  # called every bar for state tracking
        result = engine.calculate(current_time, htf_bias_direction)
        if result.stand_aside:
            return None  # block trade
        raw_stop *= result.stop_multiplier
        atr_for_runner = atr * result.runner_multiplier
    """

    def __init__(self, log_dir: Optional[str] = None):
        self.overnight = OvernightBiasModifier()
        self.fomc = FOMCDriftModifier()
        self._enabled = True

        # JSON logging
        if log_dir is None:
            log_dir = str(Path(__file__).resolve().parent.parent / "logs")
        self._log_path = Path(log_dir) / "institutional_modifiers_log.json"
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    def update_bar(self, bar) -> None:
        """Update stateful modifiers with each bar (for session tracking)."""
        if not self._enabled:
            return
        self.overnight.update_bar(bar)

    def calculate(
        self,
        current_time: datetime,
        htf_bias_direction: Optional[str],
    ) -> ModifierResult:
        """
        Calculate combined modifier multipliers.

        Returns a ModifierResult with position/stop/runner multipliers
        and stand_aside flag.
        """
        if not self._enabled:
            return ModifierResult()

        # Get individual modifier results
        overnight_result = self.overnight.calculate(htf_bias_direction)
        fomc_result = self.fomc.calculate(current_time)

        # Check stand-aside (only FOMC <0.5h can trigger)
        if fomc_result.stand_aside:
            combined = ModifierResult(
                stand_aside=True,
                stand_aside_reason=fomc_result.stand_aside_reason,
                details={
                    "overnight": overnight_result.details,
                    "fomc": fomc_result.details,
                    "action": "stand_aside",
                },
            )
            self._log_calculation(current_time, overnight_result, fomc_result, combined)
            return combined

        # Sequential multiplication
        raw_position = overnight_result.position_multiplier * fomc_result.position_multiplier
        raw_stop = overnight_result.stop_multiplier * fomc_result.stop_multiplier
        raw_runner = overnight_result.runner_multiplier * fomc_result.runner_multiplier

        # Enforce cap (2.0x) and floor (0.3x)
        combined = ModifierResult(
            position_multiplier=max(MIN_TOTAL_MULTIPLIER, min(MAX_TOTAL_MULTIPLIER, raw_position)),
            stop_multiplier=max(MIN_TOTAL_MULTIPLIER, min(MAX_TOTAL_MULTIPLIER, raw_stop)),
            runner_multiplier=max(MIN_TOTAL_MULTIPLIER, min(MAX_TOTAL_MULTIPLIER, raw_runner)),
            details={
                "overnight": overnight_result.details,
                "fomc": fomc_result.details,
                "raw_position": round(raw_position, 4),
                "raw_stop": round(raw_stop, 4),
                "raw_runner": round(raw_runner, 4),
                "capped_position": round(max(MIN_TOTAL_MULTIPLIER, min(MAX_TOTAL_MULTIPLIER, raw_position)), 4),
                "capped_stop": round(max(MIN_TOTAL_MULTIPLIER, min(MAX_TOTAL_MULTIPLIER, raw_stop)), 4),
                "capped_runner": round(max(MIN_TOTAL_MULTIPLIER, min(MAX_TOTAL_MULTIPLIER, raw_runner)), 4),
                "action": "applied",
            },
        )

        self._log_calculation(current_time, overnight_result, fomc_result, combined)
        return combined

    def _log_calculation(
        self,
        current_time: datetime,
        overnight: ModifierResult,
        fomc: ModifierResult,
        combined: ModifierResult,
    ) -> None:
        """Append structured JSON log entry for this modifier calculation."""
        entry = {
            "timestamp": current_time.isoformat(),
            "stand_aside": combined.stand_aside,
            "stand_aside_reason": combined.stand_aside_reason,
            "position_multiplier": round(combined.position_multiplier, 4),
            "stop_multiplier": round(combined.stop_multiplier, 4),
            "runner_multiplier": round(combined.runner_multiplier, 4),
            "overnight": {
                "classification": overnight.details.get("classification", "n/a"),
                "bps": overnight.details.get("overnight_bps", 0),
                "position": round(overnight.position_multiplier, 4),
                "stop": round(overnight.stop_multiplier, 4),
                "runner": round(overnight.runner_multiplier, 4),
            },
            "fomc": {
                "window": fomc.details.get("window", "n/a"),
                "hours_until": fomc.details.get("hours_until_fomc"),
                "position": round(fomc.position_multiplier, 4),
                "stop": round(fomc.stop_multiplier, 4),
                "runner": round(fomc.runner_multiplier, 4) if not fomc.stand_aside else 0,
            },
        }

        try:
            with open(self._log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            logger.warning(f"Failed to write institutional modifier log: {e}")
