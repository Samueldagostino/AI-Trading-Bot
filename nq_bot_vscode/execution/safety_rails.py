"""
Safety Rails — Hard Circuit Breakers for Paper/Live Trading
=============================================================
Four independent safety mechanisms that HALT trading when limits are breached.
These are HARD LIMITS — they cannot be overridden by modifiers or signals.

Circuit Breakers:
  1. MaxDailyLossCircuitBreaker — halt if daily PnL drops below threshold
  2. MaxConsecutiveLossesBreaker — halt after N consecutive losses
  3. MaxPositionSizeGuard — hard cap on position size (absolute, no exceptions)
  4. HeartbeatMonitor — halt if no data received within timeout

All breakers log events to logs/safety_rail_events.json.
All breakers support manual reset via reset_breaker().
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

@dataclass
class SafetyRailsConfig:
    """Configuration for all safety rails."""
    max_daily_loss: float = 500.0          # Dollars — HALT if daily PnL drops below -this
    max_consecutive_losses: int = 5        # HALT after this many consecutive losses
    max_position_size: int = 2             # ABSOLUTE cap — never exceed (MNQ contracts)
    heartbeat_alert_seconds: float = 60.0  # ALERT if no data for this long during market hours
    heartbeat_halt_seconds: float = 300.0  # HALT if no data for this long
    log_dir: str = ""                      # Directory for safety_rail_events.json


# ═══════════════════════════════════════════════════════════════
# SAFETY RAIL EVENT LOG
# ═══════════════════════════════════════════════════════════════

class SafetyRailEventLog:
    """Append-only JSONL logger for safety rail events."""

    def __init__(self, log_dir: str):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self._log_dir / "safety_rail_events.json"

    def log_event(self, breaker_name: str, event_type: str, details: Dict) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "breaker": breaker_name,
            "event": event_type,
            **details,
        }
        try:
            with open(self._log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            logger.warning("Failed to write safety rail event: %s", e)


# ═══════════════════════════════════════════════════════════════
# 1. MAX DAILY LOSS CIRCUIT BREAKER
# ═══════════════════════════════════════════════════════════════

class MaxDailyLossCircuitBreaker:
    """
    HALT all trading if daily PnL drops below -max_daily_loss.

    Requires manual restart (reset_breaker) after triggering.
    """

    def __init__(self, max_daily_loss: float = 500.0, event_log: Optional[SafetyRailEventLog] = None):
        self.max_daily_loss = max_daily_loss
        self._event_log = event_log
        self._tripped = False
        self._daily_pnl = 0.0
        self._trip_time: Optional[str] = None

    @property
    def is_tripped(self) -> bool:
        return self._tripped

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    def update_pnl(self, trade_pnl: float) -> bool:
        """
        Record a trade's PnL. Returns True if breaker trips.

        Args:
            trade_pnl: PnL from a single completed trade (dollars).

        Returns:
            True if breaker just tripped, False otherwise.
        """
        if self._tripped:
            return False  # Already tripped

        self._daily_pnl += trade_pnl

        if self._daily_pnl <= -self.max_daily_loss:
            self._tripped = True
            self._trip_time = datetime.now(timezone.utc).isoformat()
            logger.critical(
                "CIRCUIT BREAKER: Max daily loss exceeded — "
                "daily PnL $%.2f <= -$%.2f — HALTING ALL TRADING",
                self._daily_pnl, self.max_daily_loss,
            )
            if self._event_log:
                self._event_log.log_event("MaxDailyLoss", "TRIPPED", {
                    "daily_pnl": self._daily_pnl,
                    "threshold": -self.max_daily_loss,
                })
            return True
        return False

    def check(self) -> bool:
        """Returns True if trading is allowed, False if halted."""
        return not self._tripped

    def reset(self) -> None:
        """Manual reset — requires human decision to resume trading."""
        if self._tripped:
            logger.warning("MaxDailyLossCircuitBreaker RESET manually")
            if self._event_log:
                self._event_log.log_event("MaxDailyLoss", "RESET", {
                    "daily_pnl_at_reset": self._daily_pnl,
                })
        self._tripped = False
        self._daily_pnl = 0.0
        self._trip_time = None

    def reset_daily(self) -> None:
        """Reset daily PnL counter (called at session boundary). Does NOT clear a trip."""
        if not self._tripped:
            self._daily_pnl = 0.0

    def get_status(self) -> Dict:
        return {
            "breaker": "MaxDailyLoss",
            "tripped": self._tripped,
            "daily_pnl": self._daily_pnl,
            "threshold": -self.max_daily_loss,
            "trip_time": self._trip_time,
        }


# ═══════════════════════════════════════════════════════════════
# 2. MAX CONSECUTIVE LOSSES BREAKER
# ═══════════════════════════════════════════════════════════════

class MaxConsecutiveLossesBreaker:
    """
    HALT and alert after N consecutive losing trades.

    Requires manual reset after triggering.
    """

    def __init__(self, max_consecutive: int = 5, event_log: Optional[SafetyRailEventLog] = None):
        self.max_consecutive = max_consecutive
        self._event_log = event_log
        self._tripped = False
        self._consecutive_losses = 0
        self._trip_time: Optional[str] = None

    @property
    def is_tripped(self) -> bool:
        return self._tripped

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    def record_trade(self, pnl: float) -> bool:
        """
        Record a trade result. Returns True if breaker trips.

        Args:
            pnl: Trade PnL in dollars.

        Returns:
            True if breaker just tripped.
        """
        if self._tripped:
            return False

        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0
            return False

        if self._consecutive_losses >= self.max_consecutive:
            self._tripped = True
            self._trip_time = datetime.now(timezone.utc).isoformat()
            logger.critical(
                "CIRCUIT BREAKER: %d consecutive losses — HALTING AND ALERTING",
                self._consecutive_losses,
            )
            if self._event_log:
                self._event_log.log_event("MaxConsecutiveLosses", "TRIPPED", {
                    "consecutive_losses": self._consecutive_losses,
                    "threshold": self.max_consecutive,
                })
            return True
        return False

    def check(self) -> bool:
        """Returns True if trading is allowed."""
        return not self._tripped

    def reset(self) -> None:
        """Manual reset."""
        if self._tripped:
            logger.warning("MaxConsecutiveLossesBreaker RESET manually")
            if self._event_log:
                self._event_log.log_event("MaxConsecutiveLosses", "RESET", {
                    "consecutive_at_reset": self._consecutive_losses,
                })
        self._tripped = False
        self._consecutive_losses = 0
        self._trip_time = None

    def get_status(self) -> Dict:
        return {
            "breaker": "MaxConsecutiveLosses",
            "tripped": self._tripped,
            "consecutive_losses": self._consecutive_losses,
            "threshold": self.max_consecutive,
            "trip_time": self._trip_time,
        }


# ═══════════════════════════════════════════════════════════════
# 3. MAX POSITION SIZE GUARD
# ═══════════════════════════════════════════════════════════════

class MaxPositionSizeGuard:
    """
    ABSOLUTE hard cap on position size.

    Even if modifiers calculate higher, this guard caps at max_contracts.
    This is NOT a circuit breaker — it silently clamps, never halts.
    Max position of 2 contracts is ABSOLUTE — no exceptions.
    """

    def __init__(self, max_contracts: int = 2, event_log: Optional[SafetyRailEventLog] = None):
        self.max_contracts = max_contracts
        self._event_log = event_log
        self._clamp_count = 0

    def clamp(self, requested_contracts: int) -> int:
        """
        Clamp position size to max. Returns the allowed size.

        Args:
            requested_contracts: Desired position size.

        Returns:
            min(requested, max_contracts)
        """
        if requested_contracts > self.max_contracts:
            self._clamp_count += 1
            logger.warning(
                "POSITION SIZE GUARD: Clamped %d -> %d contracts (HARD LIMIT)",
                requested_contracts, self.max_contracts,
            )
            if self._event_log:
                self._event_log.log_event("MaxPositionSize", "CLAMPED", {
                    "requested": requested_contracts,
                    "allowed": self.max_contracts,
                    "total_clamps": self._clamp_count,
                })
            return self.max_contracts
        return requested_contracts

    def check(self, contracts: int) -> bool:
        """Returns True if the position size is within limits."""
        return contracts <= self.max_contracts

    def get_status(self) -> Dict:
        return {
            "breaker": "MaxPositionSize",
            "max_contracts": self.max_contracts,
            "clamp_count": self._clamp_count,
        }


# ═══════════════════════════════════════════════════════════════
# 4. HEARTBEAT MONITOR
# ═══════════════════════════════════════════════════════════════

class HeartbeatMonitor:
    """
    Monitor data feed liveness during market hours.

    - No data for alert_seconds (60s): ALERT
    - No data for halt_seconds (300s): HALT

    Call heartbeat() on every received bar/tick.
    Call check() periodically (e.g., every second in main loop).
    """

    def __init__(
        self,
        alert_seconds: float = 60.0,
        halt_seconds: float = 300.0,
        event_log: Optional[SafetyRailEventLog] = None,
    ):
        self.alert_seconds = alert_seconds
        self.halt_seconds = halt_seconds
        self._event_log = event_log
        self._last_heartbeat: float = time.monotonic()
        self._tripped = False
        self._alert_sent = False
        self._trip_time: Optional[str] = None
        self._is_market_hours = False

    @property
    def is_tripped(self) -> bool:
        return self._tripped

    @property
    def seconds_since_last(self) -> float:
        return time.monotonic() - self._last_heartbeat

    def heartbeat(self) -> None:
        """Call on every bar/tick received."""
        self._last_heartbeat = time.monotonic()
        self._alert_sent = False

    def set_market_hours(self, is_market_hours: bool) -> None:
        """Update whether we're currently in market hours."""
        self._is_market_hours = is_market_hours

    def check(self) -> bool:
        """
        Check heartbeat status. Returns True if trading is allowed.

        Should be called periodically (every 1-5 seconds).
        Only triggers during market hours.
        """
        if self._tripped:
            return False

        if not self._is_market_hours:
            return True

        elapsed = self.seconds_since_last

        # HALT threshold
        if elapsed >= self.halt_seconds:
            self._tripped = True
            self._trip_time = datetime.now(timezone.utc).isoformat()
            logger.critical(
                "HEARTBEAT HALT: No data for %.0fs (threshold: %.0fs) — HALTING",
                elapsed, self.halt_seconds,
            )
            if self._event_log:
                self._event_log.log_event("Heartbeat", "HALT", {
                    "seconds_since_last": round(elapsed, 1),
                    "threshold": self.halt_seconds,
                })
            return False

        # ALERT threshold
        if elapsed >= self.alert_seconds and not self._alert_sent:
            self._alert_sent = True
            logger.warning(
                "HEARTBEAT ALERT: No data for %.0fs (alert threshold: %.0fs)",
                elapsed, self.alert_seconds,
            )
            if self._event_log:
                self._event_log.log_event("Heartbeat", "ALERT", {
                    "seconds_since_last": round(elapsed, 1),
                    "threshold": self.alert_seconds,
                })

        return True

    def reset(self) -> None:
        """Manual reset."""
        if self._tripped:
            logger.warning("HeartbeatMonitor RESET manually")
            if self._event_log:
                self._event_log.log_event("Heartbeat", "RESET", {})
        self._tripped = False
        self._alert_sent = False
        self._last_heartbeat = time.monotonic()
        self._trip_time = None

    def get_status(self) -> Dict:
        return {
            "breaker": "Heartbeat",
            "tripped": self._tripped,
            "seconds_since_last": round(self.seconds_since_last, 1),
            "alert_threshold": self.alert_seconds,
            "halt_threshold": self.halt_seconds,
            "is_market_hours": self._is_market_hours,
            "trip_time": self._trip_time,
        }


# ═══════════════════════════════════════════════════════════════
# SAFETY RAILS MANAGER
# ═══════════════════════════════════════════════════════════════

class SafetyRails:
    """
    Unified manager for all safety rails.

    Provides a single check_all() method and centralized reset.
    """

    def __init__(self, config: Optional[SafetyRailsConfig] = None):
        if config is None:
            config = SafetyRailsConfig()

        log_dir = config.log_dir
        if not log_dir:
            log_dir = str(Path(__file__).resolve().parent.parent / "logs")

        self._event_log = SafetyRailEventLog(log_dir)

        self.daily_loss = MaxDailyLossCircuitBreaker(
            max_daily_loss=config.max_daily_loss,
            event_log=self._event_log,
        )
        self.consecutive_losses = MaxConsecutiveLossesBreaker(
            max_consecutive=config.max_consecutive_losses,
            event_log=self._event_log,
        )
        self.position_size = MaxPositionSizeGuard(
            max_contracts=config.max_position_size,
            event_log=self._event_log,
        )
        self.heartbeat = HeartbeatMonitor(
            alert_seconds=config.heartbeat_alert_seconds,
            halt_seconds=config.heartbeat_halt_seconds,
            event_log=self._event_log,
        )

    def check_all(self) -> bool:
        """
        Check all circuit breakers. Returns True if trading is allowed.

        Does NOT check position size (that's a clamp, not a halt).
        """
        if not self.daily_loss.check():
            return False
        if not self.consecutive_losses.check():
            return False
        if not self.heartbeat.check():
            return False
        return True

    def record_trade(self, pnl: float) -> bool:
        """
        Record a completed trade across all relevant breakers.

        Returns True if any breaker tripped.
        """
        tripped = False
        if self.daily_loss.update_pnl(pnl):
            tripped = True
        if self.consecutive_losses.record_trade(pnl):
            tripped = True
        return tripped

    def clamp_position_size(self, requested: int) -> int:
        """Clamp position size through the guard."""
        return self.position_size.clamp(requested)

    def on_bar_received(self) -> None:
        """Call on every bar to update heartbeat."""
        self.heartbeat.heartbeat()

    def set_market_hours(self, is_market_hours: bool) -> None:
        """Update market hours state for heartbeat monitor."""
        self.heartbeat.set_market_hours(is_market_hours)

    def reset_breaker(self, breaker_name: str) -> bool:
        """
        Reset a specific breaker by name.

        Valid names: "daily_loss", "consecutive_losses", "heartbeat"
        Returns True if breaker was found and reset.
        """
        breakers = {
            "daily_loss": self.daily_loss,
            "consecutive_losses": self.consecutive_losses,
            "heartbeat": self.heartbeat,
        }
        breaker = breakers.get(breaker_name)
        if breaker is None:
            logger.warning("Unknown breaker: %s", breaker_name)
            return False
        breaker.reset()
        return True

    def reset_all(self) -> None:
        """Reset all circuit breakers."""
        self.daily_loss.reset()
        self.consecutive_losses.reset()
        self.heartbeat.reset()

    def reset_daily(self) -> None:
        """Reset daily counters (session boundary). Does NOT clear trips."""
        self.daily_loss.reset_daily()

    def get_status(self) -> Dict:
        """Get status of all safety rails."""
        any_tripped = (
            self.daily_loss.is_tripped
            or self.consecutive_losses.is_tripped
            or self.heartbeat.is_tripped
        )
        return {
            "trading_allowed": not any_tripped,
            "any_tripped": any_tripped,
            "daily_loss": self.daily_loss.get_status(),
            "consecutive_losses": self.consecutive_losses.get_status(),
            "position_size": self.position_size.get_status(),
            "heartbeat": self.heartbeat.get_status(),
        }
