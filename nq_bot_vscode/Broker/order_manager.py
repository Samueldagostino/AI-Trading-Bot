"""
Order Manager — Bracket Orders with Safety Checks
====================================================
Manages order lifecycle for the 2-contract MNQ strategy:

  C1: Market entry + fixed take-profit at 1.5x R:R
  C2: Market entry + trailing stop

Safety checks BEFORE every order:
  - Position size <= 2 contracts (ABSOLUTE)
  - Daily loss limit not breached
  - Not in circuit breaker state

All order actions are logged to logs/order_log.json.

This module enforces 2 contract max INDEPENDENTLY of safety_rails.py.
"""

import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ABSOLUTE position size limit — not configurable
MAX_CONTRACTS = 2


class OrderManager:
    """
    Manages order submission, modification, and cancellation
    with mandatory safety checks.
    """

    def __init__(
        self,
        ibkr_client,
        max_daily_loss: float = 500.0,
        log_dir: str = "logs",
    ):
        self._client = ibkr_client
        self._max_daily_loss = max_daily_loss
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self._log_dir / "order_log.json"

        # State
        self._open_orders: Dict[int, dict] = {}
        self._daily_pnl: float = 0.0
        self._circuit_breaker: bool = False
        self._current_position_size: int = 0

    # ──────────────────────────────────────────────────────
    # SAFETY CHECKS
    # ──────────────────────────────────────────────────────

    def _check_safety(self, size: int) -> Optional[str]:
        """
        Run all safety checks before placing an order.

        Returns:
            None if safe, or a string describing why the order was blocked.
        """
        if self._circuit_breaker:
            return "Circuit breaker is active"

        if self._current_position_size + size > MAX_CONTRACTS:
            return (
                f"Position size would exceed {MAX_CONTRACTS} contracts "
                f"(current={self._current_position_size}, requested={size})"
            )

        if self._daily_pnl <= -self._max_daily_loss:
            return (
                f"Daily loss limit breached: "
                f"PnL=${self._daily_pnl:.2f}, limit=-${self._max_daily_loss:.2f}"
            )

        return None

    # ──────────────────────────────────────────────────────
    # ENTRY
    # ──────────────────────────────────────────────────────

    async def submit_entry(
        self,
        direction: str,
        size: int,
        stop_price: float,
        entry_price: Optional[float] = None,
    ) -> Optional[Dict]:
        """
        Place a bracket entry: entry + stop loss.

        For the 2-contract strategy:
          C1: market entry + fixed TP at 1.5x R:R
          C2: market entry + trailing stop (stop_price)

        Args:
            direction: 'LONG' or 'SHORT'
            size: Number of contracts (max 2)
            stop_price: Stop loss price
            entry_price: Reference entry price (for TP calc). If None,
                         uses market order and TP is set after fill.

        Returns:
            Dict with order IDs on success, None on safety block.
        """
        # Safety check
        reason = self._check_safety(size)
        if reason:
            logger.warning("Order BLOCKED: %s", reason)
            self._log_action("BLOCKED", {
                "direction": direction, "size": size,
                "stop_price": stop_price, "reason": reason,
            })
            return None

        action = "BUY" if direction == "LONG" else "SELL"
        exit_action = "SELL" if direction == "LONG" else "BUY"

        result = {"direction": direction, "size": size, "orders": {}}

        # Calculate R:R for C1 take-profit
        if entry_price and size >= 1:
            risk = abs(entry_price - stop_price)
            tp_offset = risk * 1.5  # 1.5x R:R

            if direction == "LONG":
                tp_price = round(entry_price + tp_offset, 2)
            else:
                tp_price = round(entry_price - tp_offset, 2)

            # C1: Market entry
            c1_entry_id = await self._client.place_order(
                action=action, quantity=1, order_type="MKT",
            )
            if c1_entry_id is not None:
                result["orders"]["c1_entry"] = c1_entry_id
                # C1: Take-profit
                c1_tp_id = await self._client.place_order(
                    action=exit_action, quantity=1, order_type="LMT",
                    limit_price=tp_price,
                )
                if c1_tp_id is not None:
                    result["orders"]["c1_tp"] = c1_tp_id
                # C1: Stop loss
                c1_stop_id = await self._client.place_order(
                    action=exit_action, quantity=1, order_type="STP",
                    stop_price=stop_price,
                )
                if c1_stop_id is not None:
                    result["orders"]["c1_stop"] = c1_stop_id

        # C2: Market entry + trailing stop
        if size >= 2:
            c2_entry_id = await self._client.place_order(
                action=action, quantity=1, order_type="MKT",
            )
            if c2_entry_id is not None:
                result["orders"]["c2_entry"] = c2_entry_id
                # C2: Stop loss (trailing stop managed via modify_stop)
                c2_stop_id = await self._client.place_order(
                    action=exit_action, quantity=1, order_type="STP",
                    stop_price=stop_price,
                )
                if c2_stop_id is not None:
                    result["orders"]["c2_stop"] = c2_stop_id
        elif size == 1 and not entry_price:
            # Single contract, no entry_price — simple market + stop
            entry_id = await self._client.place_order(
                action=action, quantity=1, order_type="MKT",
            )
            if entry_id is not None:
                result["orders"]["entry"] = entry_id
                stop_id = await self._client.place_order(
                    action=exit_action, quantity=1, order_type="STP",
                    stop_price=stop_price,
                )
                if stop_id is not None:
                    result["orders"]["stop"] = stop_id

        # Track
        self._current_position_size += size
        for label, oid in result["orders"].items():
            self._open_orders[oid] = {
                "label": label, "direction": direction,
                "action": action, "time": time.time(),
            }

        self._log_action("ENTRY", result)
        logger.info(
            "Entry submitted: %s %d contracts, stop=%.2f, orders=%s",
            direction, size, stop_price, list(result["orders"].keys()),
        )
        return result

    # ──────────────────────────────────────────────────────
    # EXIT
    # ──────────────────────────────────────────────────────

    async def submit_exit(self, order_id: int, exit_type: str) -> bool:
        """
        Submit an exit for a specific order.

        Args:
            order_id: The order to exit
            exit_type: 'TP1_HIT', 'STOP_HIT', 'TRAIL_STOP', 'MANUAL'
        """
        success = await self._client.cancel_order(order_id)
        if success:
            self._current_position_size = max(0, self._current_position_size - 1)
            self._open_orders.pop(order_id, None)
            self._log_action("EXIT", {
                "order_id": order_id, "exit_type": exit_type,
            })
            logger.info("Exit submitted: orderId=%d, type=%s", order_id, exit_type)
        return success

    # ──────────────────────────────────────────────────────
    # MODIFY STOP (for trailing)
    # ──────────────────────────────────────────────────────

    async def modify_stop(self, order_id: int, new_stop_price: float) -> bool:
        """
        Modify a stop order price (for C2 trailing stop adjustment).

        Cancels the old stop and places a new one at the new price.
        """
        info = self._open_orders.get(order_id)
        if not info:
            logger.warning("modify_stop: orderId=%d not found", order_id)
            return False

        direction = info["direction"]
        exit_action = "SELL" if direction == "LONG" else "BUY"

        # Cancel old stop
        cancelled = await self._client.cancel_order(order_id)
        if not cancelled:
            logger.error("modify_stop: failed to cancel old stop orderId=%d", order_id)
            return False

        # Place new stop
        new_id = await self._client.place_order(
            action=exit_action, quantity=1, order_type="STP",
            stop_price=new_stop_price,
        )
        if new_id is None:
            logger.error("modify_stop: failed to place new stop")
            return False

        # Update tracking
        self._open_orders.pop(order_id, None)
        self._open_orders[new_id] = {
            "label": info["label"], "direction": direction,
            "action": exit_action, "time": time.time(),
        }

        self._log_action("MODIFY_STOP", {
            "old_order_id": order_id, "new_order_id": new_id,
            "new_stop_price": new_stop_price,
        })
        logger.info(
            "Stop modified: orderId %d -> %d @ %.2f",
            order_id, new_id, new_stop_price,
        )
        return True

    # ──────────────────────────────────────────────────────
    # EMERGENCY
    # ──────────────────────────────────────────────────────

    async def cancel_all(self) -> int:
        """
        Emergency: cancel all open orders.

        Returns:
            Number of orders cancelled.
        """
        cancelled = 0
        for oid in list(self._open_orders.keys()):
            try:
                success = await self._client.cancel_order(oid)
                if success:
                    cancelled += 1
            except Exception as e:
                logger.error("cancel_all: failed for orderId=%d: %s", oid, e)

        self._open_orders.clear()
        self._current_position_size = 0

        self._log_action("CANCEL_ALL", {"cancelled": cancelled})
        logger.warning("EMERGENCY cancel_all: %d orders cancelled", cancelled)
        return cancelled

    # ──────────────────────────────────────────────────────
    # QUERIES
    # ──────────────────────────────────────────────────────

    def get_open_orders(self) -> list:
        """Get list of tracked open orders."""
        return [
            {"order_id": oid, **info}
            for oid, info in self._open_orders.items()
        ]

    # ──────────────────────────────────────────────────────
    # STATE MANAGEMENT
    # ──────────────────────────────────────────────────────

    def record_pnl(self, pnl: float) -> None:
        """Record trade PnL for daily loss tracking."""
        if not math.isfinite(pnl):
            logger.critical("NaN/Inf PnL received — activating circuit breaker")
            self._circuit_breaker = True
            self._log_action("CIRCUIT_BREAKER", {"reason": "NaN/Inf PnL", "pnl": str(pnl)})
            return
        self._daily_pnl += pnl

    def trip_circuit_breaker(self) -> None:
        """Activate circuit breaker — blocks all new orders."""
        self._circuit_breaker = True
        logger.critical("Circuit breaker ACTIVATED — all orders blocked")

    def reset_circuit_breaker(self) -> None:
        """Reset circuit breaker (manual only)."""
        self._circuit_breaker = False
        logger.info("Circuit breaker reset")

    def update_position_size(self, size: int) -> None:
        """Update tracked position size (e.g. after fill confirmation)."""
        self._current_position_size = size

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def current_position_size(self) -> int:
        return self._current_position_size

    @property
    def circuit_breaker_active(self) -> bool:
        return self._circuit_breaker

    # ──────────────────────────────────────────────────────
    # LOGGING
    # ──────────────────────────────────────────────────────

    def _log_action(self, action: str, details: dict) -> None:
        """Append order action to order_log.json."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            **details,
        }

        try:
            existing = []
            if self._log_path.exists():
                text = self._log_path.read_text().strip()
                if text:
                    existing = json.loads(text)
            existing.append(entry)
            self._log_path.write_text(json.dumps(existing, indent=2, default=str))
        except Exception as e:
            logger.error("Failed to write order log: %s", e)
