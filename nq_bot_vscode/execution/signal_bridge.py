"""
Signal-to-Execution Bridge
============================
Pure translation layer between the signal pipeline and the
IBKR order executor.  Does NOT modify signal generation logic.

Flow:
  process_bar() -> signal pipeline -> TradeDecision
                                        ↓
                                   SignalBridge.translate()
                                        ↓
                                   BridgeResult (ScaleOutParams + metadata)
                                        ↓
                           IBKROrderExecutor.place_scale_out_entry()

Responsibilities:
  1. Accept validated trade decisions from the signal pipeline
  2. Compute stop/target prices from ATR using RiskConfig values
  3. Map direction -> OrderSide, package as scale-out entry params
  4. Attach signal score + HTF bias state as audit metadata
  5. Belt-and-suspenders safety gates (score, HTF, stop distance)
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from config.settings import RiskConfig
from config.constants import HIGH_CONVICTION_MIN_SCORE, HIGH_CONVICTION_MAX_STOP_PTS

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# CONSTANTS — imported from config/constants.py (single source of truth)
# ═══════════════════════════════════════════════════════════════
MIN_SIGNAL_SCORE = HIGH_CONVICTION_MIN_SCORE
MAX_STOP_DISTANCE_PTS = HIGH_CONVICTION_MAX_STOP_PTS


# ═══════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════

@dataclass
class TradeDecision:
    """
    Output of the signal pipeline, input to the bridge.

    Represents a validated trade decision from the aggregator
    scoring + HTF gate.  The bridge does NOT re-run signal
    generation — it only translates.
    """
    direction: str               # "long" or "short"
    entry_price: float           # market price at decision time
    signal_score: float          # combined score (0.0–1.0)
    atr: float                   # current ATR for stop/target calc
    htf_bias: str                # "bullish", "bearish", "neutral"
    htf_allows_long: bool
    htf_allows_short: bool
    entry_source: str            # "signal", "sweep", "confluence"
    market_regime: str           # regime label at entry time
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass
class ScaleOutParams:
    """
    Parameters ready for IBKROrderExecutor.place_scale_out_entry().

    Maps 1:1 to the method signature:
        place_scale_out_entry(direction, limit_price, stop_loss, c1_take_profit)
    """
    direction: str               # "long" or "short"
    limit_price: float           # 0.0 = market order
    stop_loss: float             # computed from ATR × atr_multiplier_stop
    c1_take_profit: float        # computed from ATR × atr_multiplier_target


@dataclass
class BridgeResult:
    """
    Output of SignalBridge.translate().

    If approved, ``params`` is populated and ready to unpack
    into place_scale_out_entry().  ``metadata`` is always
    populated for audit logging regardless of approval.
    """
    approved: bool
    rejection_reason: str = ""
    params: Optional[ScaleOutParams] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════
# SIGNAL BRIDGE
# ═══════════════════════════════════════════════════════════════

class SignalBridge:
    """
    Translates trade decisions into order parameters.

    This is a pure translation layer.  It does NOT:
      - Generate or modify signals
      - Run the aggregator or HTF engine
      - Touch the order executor directly

    It DOES:
      - Validate decisions (belt-and-suspenders safety gates)
      - Compute stop/target prices from ATR + config
      - Package parameters for place_scale_out_entry()
      - Attach audit metadata (score, bias, source)
    """

    def __init__(self, risk_config: Optional[RiskConfig] = None):
        self._config = risk_config or RiskConfig()
        self._translations: int = 0
        self._rejections: int = 0

    def translate(self, decision: TradeDecision) -> BridgeResult:
        """
        Translate a trade decision into scale-out entry parameters.

        Returns a BridgeResult with ``approved=True`` and
        populated ``params`` if the decision passes all safety
        gates, or ``approved=False`` with a reason if not.

        Metadata is always populated for audit logging.
        """
        metadata = self._build_metadata(decision)

        # ── NaN GUARD: NaN comparisons return False, bypassing gates ──
        if not math.isfinite(decision.signal_score):
            return self._reject("signal_score is NaN/Inf", metadata)
        if not math.isfinite(decision.atr):
            return self._reject("ATR is NaN/Inf", metadata)

        # ── SAFETY GATE 1: direction must be valid ──
        if decision.direction not in ("long", "short"):
            return self._reject(
                f"invalid direction: {decision.direction!r}",
                metadata,
            )

        # ── SAFETY GATE 2: minimum signal score ──
        if decision.signal_score < MIN_SIGNAL_SCORE:
            return self._reject(
                f"score {decision.signal_score:.3f} < "
                f"min {MIN_SIGNAL_SCORE}",
                metadata,
            )

        # ── SAFETY GATE 3: HTF allows this direction ──
        if decision.direction == "long" and not decision.htf_allows_long:
            return self._reject(
                f"HTF blocks long (bias={decision.htf_bias})",
                metadata,
            )
        if decision.direction == "short" and not decision.htf_allows_short:
            return self._reject(
                f"HTF blocks short (bias={decision.htf_bias})",
                metadata,
            )

        # ── SAFETY GATE 4: ATR must be positive ──
        if decision.atr <= 0:
            return self._reject(
                f"ATR must be positive, got {decision.atr}",
                metadata,
            )

        # ── COMPUTE STOP / TARGET ──
        stop_distance = round(
            decision.atr * self._config.atr_multiplier_stop, 2
        )
        target_distance = round(
            decision.atr * self._config.atr_multiplier_target, 2
        )

        # Enforce minimum R:R ratio
        if stop_distance > 0:
            rr = target_distance / stop_distance
            if rr < self._config.min_rr_ratio:
                target_distance = round(
                    stop_distance * self._config.min_rr_ratio, 2
                )

        # ── SAFETY GATE 5: max stop distance ──
        if stop_distance > MAX_STOP_DISTANCE_PTS:
            return self._reject(
                f"stop {stop_distance:.1f}pts > "
                f"max {MAX_STOP_DISTANCE_PTS}pts",
                metadata,
            )

        # ── COMPUTE PRICES ──
        if decision.direction == "long":
            stop_loss = round(decision.entry_price - stop_distance, 2)
            c1_take_profit = round(
                decision.entry_price + target_distance, 2
            )
        else:
            stop_loss = round(decision.entry_price + stop_distance, 2)
            c1_take_profit = round(
                decision.entry_price - target_distance, 2
            )

        params = ScaleOutParams(
            direction=decision.direction,
            limit_price=0.0,           # market entry
            stop_loss=stop_loss,
            c1_take_profit=c1_take_profit,
        )

        # Enrich metadata with computed values
        metadata["stop_distance_pts"] = stop_distance
        metadata["target_distance_pts"] = target_distance
        metadata["rr_ratio"] = round(
            target_distance / stop_distance, 2
        ) if stop_distance > 0 else 0.0
        metadata["stop_loss"] = stop_loss
        metadata["c1_take_profit"] = c1_take_profit

        self._translations += 1
        logger.info(
            "BRIDGE APPROVED: %s entry=%.2f stop=%.2f "
            "target=%.2f score=%.3f source=%s htf=%s",
            decision.direction.upper(),
            decision.entry_price,
            stop_loss,
            c1_take_profit,
            decision.signal_score,
            decision.entry_source,
            decision.htf_bias,
        )

        return BridgeResult(
            approved=True,
            params=params,
            metadata=metadata,
        )

    # ──────────────────────────────────────────────────────────
    # PROPERTIES
    # ──────────────────────────────────────────────────────────

    @property
    def translations(self) -> int:
        return self._translations

    @property
    def rejections(self) -> int:
        return self._rejections

    # ──────────────────────────────────────────────────────────
    # INTERNALS
    # ──────────────────────────────────────────────────────────

    def _build_metadata(self, decision: TradeDecision) -> Dict[str, Any]:
        """Build audit metadata dict from a trade decision."""
        return {
            "signal_score": decision.signal_score,
            "htf_bias": decision.htf_bias,
            "htf_allows_long": decision.htf_allows_long,
            "htf_allows_short": decision.htf_allows_short,
            "entry_source": decision.entry_source,
            "market_regime": decision.market_regime,
            "atr": decision.atr,
            "direction": decision.direction,
            "entry_price": decision.entry_price,
            "timestamp": decision.timestamp.isoformat(),
        }

    def _reject(
        self, reason: str, metadata: Dict[str, Any]
    ) -> BridgeResult:
        """Log and return a rejected bridge result."""
        self._rejections += 1
        logger.info("BRIDGE REJECTED: %s", reason)
        return BridgeResult(
            approved=False,
            rejection_reason=reason,
            metadata=metadata,
        )
