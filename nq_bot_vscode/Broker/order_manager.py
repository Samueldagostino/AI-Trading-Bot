"""
IBKR Order Manager — 2-Contract Scale-Out Execution
======================================================
Manages the full order lifecycle for the MNQ 2-contract strategy via TWS API.

Architecture:
  C1: Fixed target exit (limit order at c1_target)
  C2: Trailing stop runner (dynamically updated stop order)

Order flow:
  1. Signal APPROVED -> submit_entry()
  2. Limit entry with 2-tick chase (0.50 pts)
  3. 5-second fill timeout -> cancel if unfilled
  4. On fill: stop-loss + C1 target + C2 trail monitor
  5. manage_c1_exit() / manage_c2_trail() on every bar
  6. close_all_positions() for emergency flatten

Safety rails enforced BEFORE every order:
  - 2 contract ABSOLUTE maximum
  - No pyramiding (one position at a time)
  - Daily loss limit ($500)
  - Consecutive loss check (5 in a row)
  - Stale data check (300s heartbeat)
  - Kill switch (10% drawdown)
  - EOD forced close (3:55 PM ET)
  - Market halt detection (60s no data)
  - Direction assertion
  - Modifier clamping (0.1-3.0)

All order events logged to logs/order_events.json.
Trade summaries logged to logs/trade_decisions.json.
"""

import asyncio
import json
import logging
import math
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# CONSTANTS — SAFETY LIMITS (NOT CONFIGURABLE)
# ═══════════════════════════════════════════════════════════════
MAX_CONTRACTS = 2                   # ABSOLUTE MAXIMUM — no exceptions
MNQ_POINT_VALUE = 2.0               # $2.00 per point per contract
MNQ_TICK_SIZE = 0.25                # Minimum price increment
ENTRY_CHASE_TICKS = 2               # 2 ticks = 0.50 pts chase allowance
ENTRY_TIMEOUT_SECONDS = 5.0         # Cancel entry if not filled
ORDER_WATCHDOG_SECONDS = 30.0       # Cancel any order stuck > 30s
DAILY_LOSS_LIMIT = 500.0            # Reject new entries if daily PnL <= -$500
MAX_CONSECUTIVE_LOSSES = 5          # Reject after 5 consecutive losses
HEARTBEAT_STALE_SECONDS = 300.0     # Reject if last bar > 300s ago
KILL_SWITCH_DRAWDOWN_PCT = 10.0     # Close all if drawdown > 10% from peak
SLIPPAGE_WARN_PTS = 5.0             # Log WARNING if slippage > 5 pts
SLIPPAGE_CRITICAL_PTS = 10.0        # Log CRITICAL if slippage > 10 pts
MODIFIER_MIN = 0.1                  # Floor for modifier total
MODIFIER_MAX = 3.0                  # Cap for modifier total
BAD_TICK_MAX_CHANGE_PCT = 2.0       # Max % change per bar
MARKET_HALT_SECONDS = 60.0          # Suspect halt if no data for 60s during RTH
MARKET_HALT_RESUME_WAIT = 30.0      # Wait 30s after halt resumes before trading
C2_TRAIL_RETRY_DELAY = 1.0          # Wait 1s before retrying trail modification

# EOD forced close times (Eastern Time)
ET_TZ = ZoneInfo("America/New_York")
EOD_NO_NEW_ENTRIES_TIME = "15:45"   # No new entries after 3:45 PM ET
EOD_WARNING_TIME = "15:50"          # Warning at 3:50 PM ET
EOD_CLOSE_TIME = "15:55"            # Force close at 3:55 PM ET


def _generate_trade_id() -> str:
    """Generate a unique trade ID."""
    return f"T{int(time.time() * 1000) % 1000000:06d}"


class OrderManager:
    """
    Manages order lifecycle for the 2-contract scale-out system.

    Coordinates entry, stop-loss, C1 target, C2 trailing stop,
    and enforces all safety rails before every order submission.
    """

    def __init__(self, ib_client, config=None, log_dir: str = "logs"):
        """
        Args:
            ib_client: IBKRClient instance (from Broker.ibkr_client)
            config: Optional config dict with overrides
            log_dir: Directory for order/trade logs
        """
        self.ib = ib_client._ib             # ib_insync IB instance
        self.contract = ib_client._contract  # Qualified MNQ contract
        self._ib_client = ib_client
        self._config = config or {}
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)

        # State
        self._active_orders: Dict[int, dict] = {}
        self._active_positions: Dict[str, dict] = {}
        self._order_log: List[dict] = []
        self._trade_history: List[dict] = []
        self._processed_exec_ids: Set[str] = set()

        # Safety state
        self._daily_pnl: float = 0.0
        self._peak_equity: float = self._config.get("account_size", 50000.0)
        self._current_equity: float = self._peak_equity
        self._consecutive_losses: int = 0
        self._trade_results: List[float] = []
        self._last_bar_time: float = time.monotonic()
        self._slippage_total: float = 0.0
        self._slippage_count: int = 0

        # Order-in-flight guard
        self._order_in_flight: bool = False

        # Market halt detection
        self._market_halt_suspected: bool = False
        self._market_halt_resume_time: float = 0.0

        # EOD state
        self._eod_closed: bool = False

        # Watchdog timers
        self._watchdog_timers: Dict[int, threading.Timer] = {}

        # Wire up IBKR event callbacks
        self.ib.orderStatusEvent += self._on_order_status
        self.ib.execDetailsEvent += self._on_execution
        self.ib.errorEvent += self._on_error
        self.ib.disconnectedEvent += self._on_disconnect

        logger.info("OrderManager initialized (max_contracts=%d, daily_loss_limit=$%.0f)",
                     MAX_CONTRACTS, DAILY_LOSS_LIMIT)

    # ══════════════════════════════════════════════════════════
    # SAFETY CHECKS
    # ══════════════════════════════════════════════════════════

    def _check_safety(self, signal: dict) -> Optional[str]:
        """
        Run ALL safety checks before order submission.
        Returns None if safe, or rejection reason string.
        """
        # a. Position size check
        current_count = sum(p.get("contracts", 0) for p in self._active_positions.values())
        requested = 2 if signal.get("modifier_total", 1.0) >= 0.6 else 1
        if current_count + requested > MAX_CONTRACTS:
            return "SAFETY_POSITION_SIZE"

        # b. Daily loss check
        if self._daily_pnl <= -DAILY_LOSS_LIMIT:
            return "SAFETY_DAILY_LOSS"

        # c. Consecutive loss check
        if self._consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            return "SAFETY_CONSEC_LOSS"

        # d. No pyramiding
        if len(self._active_positions) > 0:
            return "SAFETY_ALREADY_IN_POSITION"

        # e. Order in flight
        if self._order_in_flight:
            return "REJECTED_ORDER_IN_FLIGHT"

        # f. Heartbeat / stale data check
        elapsed = time.monotonic() - self._last_bar_time
        if elapsed > HEARTBEAT_STALE_SECONDS:
            return "SAFETY_STALE_DATA"

        # g. Kill switch — drawdown check
        if self._peak_equity > 0:
            drawdown_pct = ((self._peak_equity - self._current_equity) / self._peak_equity) * 100
            if drawdown_pct > KILL_SWITCH_DRAWDOWN_PCT:
                return "SAFETY_KILL_SWITCH"

        # h. Market halt
        if self._market_halt_suspected:
            return "SAFETY_MARKET_HALT"

        # i. Market halt resume wait
        if self._market_halt_resume_time > 0:
            if time.monotonic() < self._market_halt_resume_time:
                return "SAFETY_MARKET_HALT_RESUMING"

        # j. EOD check
        if self._eod_closed:
            return "SAFETY_EOD_CLOSED"

        et_now = datetime.now(ET_TZ)
        no_entry_h, no_entry_m = map(int, EOD_NO_NEW_ENTRIES_TIME.split(":"))
        if et_now.hour > no_entry_h or (et_now.hour == no_entry_h and et_now.minute >= no_entry_m):
            return "SAFETY_EOD_NO_NEW_ENTRIES"

        # k. Direction assertion
        direction = signal.get("direction", "")
        if direction not in ("LONG", "SHORT"):
            return "DIRECTION_MISMATCH_CRITICAL"

        # l. NaN guard on critical values
        for field_name in ("entry_price", "stop_price", "c1_target"):
            val = signal.get(field_name, 0)
            if not math.isfinite(val) or val <= 0:
                return f"SAFETY_INVALID_{field_name.upper()}"

        return None

    # ══════════════════════════════════════════════════════════
    # BAD TICK FILTER
    # ══════════════════════════════════════════════════════════

    _last_bar_close: float = 0.0

    def validate_bar(self, bar) -> bool:
        """
        Validate incoming bar data. Returns True if valid.
        Filters out bad ticks before processing.
        """
        o, h, l, c, v = bar.open, bar.high, bar.low, bar.close, bar.volume

        # Basic sanity
        if c <= 0 or o <= 0 or h <= 0 or l <= 0:
            self._log_event("BAD_TICK_FILTERED", details="Price <= 0")
            return False
        if v < 0:
            self._log_event("BAD_TICK_FILTERED", details="Negative volume")
            return False
        if h < l:
            self._log_event("BAD_TICK_FILTERED", details="High < Low")
            return False
        if h < o or h < c:
            self._log_event("BAD_TICK_FILTERED", details="High < Open or High < Close")
            return False
        if l > o or l > c:
            self._log_event("BAD_TICK_FILTERED", details="Low > Open or Low > Close")
            return False

        # Price change check against previous bar
        if self._last_bar_close > 0:
            change_pct = abs(c - self._last_bar_close) / self._last_bar_close * 100
            if change_pct > BAD_TICK_MAX_CHANGE_PCT:
                self._log_event("BAD_TICK_FILTERED",
                                details=f"Price change {change_pct:.2f}% > {BAD_TICK_MAX_CHANGE_PCT}%")
                return False

        self._last_bar_close = c
        return True

    def on_bar_received(self, bar) -> None:
        """Called on every bar to update heartbeat and market halt detection."""
        now = time.monotonic()
        self._last_bar_time = now

        # Check if resuming from halt
        if self._market_halt_suspected:
            self._market_halt_suspected = False
            self._market_halt_resume_time = now + MARKET_HALT_RESUME_WAIT
            self._log_event("MARKET_HALT_RESUMED")
            logger.info("Market halt resumed — waiting %.0fs before allowing new entries",
                        MARKET_HALT_RESUME_WAIT)

    def check_market_halt(self) -> bool:
        """Check if market data appears halted. Call periodically."""
        elapsed = time.monotonic() - self._last_bar_time
        if elapsed > MARKET_HALT_SECONDS and not self._market_halt_suspected:
            self._market_halt_suspected = True
            self._log_event("MARKET_HALT_SUSPECTED",
                            details=f"No data for {elapsed:.0f}s")
            logger.warning("MARKET HALT SUSPECTED — no data for %.0fs", elapsed)
            return True
        return self._market_halt_suspected

    # ══════════════════════════════════════════════════════════
    # EOD FORCED CLOSE
    # ══════════════════════════════════════════════════════════

    async def check_eod(self) -> None:
        """Check end-of-day times and force close if needed."""
        et_now = datetime.now(ET_TZ)
        h, m = et_now.hour, et_now.minute

        warn_h, warn_m = map(int, EOD_WARNING_TIME.split(":"))
        close_h, close_m = map(int, EOD_CLOSE_TIME.split(":"))

        # Warning
        if (h == warn_h and m == warn_m):
            logger.warning("EOD_CLOSE_APPROACHING — positions must be flat by %s ET", EOD_CLOSE_TIME)
            self._log_event("EOD_CLOSE_APPROACHING")

        # Force close
        if (h > close_h or (h == close_h and m >= close_m)):
            if self._active_positions and not self._eod_closed:
                logger.critical("EOD FORCED CLOSE at %02d:%02d ET", h, m)
                await self.close_all_positions(reason="EOD_FORCED_CLOSE")
                self._eod_closed = True

    def reset_eod(self) -> None:
        """Reset EOD state for new trading session."""
        self._eod_closed = False

    # ══════════════════════════════════════════════════════════
    # ENTRY
    # ══════════════════════════════════════════════════════════

    async def submit_entry(self, signal: dict) -> Optional[dict]:
        """
        Submit a new trade based on an approved signal.

        Args:
            signal: dict with keys:
                direction: "LONG" or "SHORT"
                entry_price: float
                stop_price: float
                c1_target: float
                c2_trail_distance: float
                modifier_total: float
                confluence_score: float
                reason: str

        Returns:
            Trade dict on success, None on safety block.
        """
        # Clamp modifier total
        raw_modifier = signal.get("modifier_total", 1.0)
        modifier_total = max(MODIFIER_MIN, min(MODIFIER_MAX, raw_modifier))
        if raw_modifier != modifier_total:
            logger.warning("Modifier clamped: %.2f -> %.2f (range %.1f-%.1f)",
                           raw_modifier, modifier_total, MODIFIER_MIN, MODIFIER_MAX)
        signal["modifier_total"] = modifier_total

        # Safety checks
        rejection = self._check_safety(signal)
        if rejection:
            logger.warning("Order REJECTED: %s", rejection)
            self._log_event("ENTRY_REJECTED", trade_id="",
                            direction=signal.get("direction", ""),
                            details=rejection)
            return None

        direction = signal["direction"]
        entry_price = signal["entry_price"]
        stop_price = signal["stop_price"]
        c1_target = signal["c1_target"]
        c2_trail_distance = signal.get("c2_trail_distance", 15.0)

        # Direction assertion
        action = "BUY" if direction == "LONG" else "SELL"
        reverse_action = "SELL" if direction == "LONG" else "BUY"

        # Double-check direction assertion
        if (direction == "LONG" and action != "BUY") or \
           (direction == "SHORT" and action != "SELL"):
            logger.critical("DIRECTION_MISMATCH_CRITICAL: direction=%s action=%s", direction, action)
            self._log_event("DIRECTION_MISMATCH_CRITICAL", direction=direction)
            return None

        # Determine contract count
        num_contracts = 2 if modifier_total >= 0.6 else 1
        num_contracts = min(num_contracts, MAX_CONTRACTS)

        # Calculate limit price with chase
        chase = ENTRY_CHASE_TICKS * MNQ_TICK_SIZE  # 0.50 pts
        if action == "BUY":
            limit_price = round(entry_price + chase, 2)
        else:
            limit_price = round(entry_price - chase, 2)

        trade_id = _generate_trade_id()

        logger.info("ENTRY SUBMITTING: %s %s %d MNQ @ %.2f (limit=%.2f, stop=%.2f, c1_target=%.2f)",
                     trade_id, direction, num_contracts, entry_price, limit_price, stop_price, c1_target)

        # Set order in flight
        self._order_in_flight = True

        try:
            # Import order types
            from ib_insync import LimitOrder, StopOrder, MarketOrder

            # Submit limit entry order
            entry_order = LimitOrder(action, num_contracts, limit_price)
            entry_order.tif = "GTC"
            entry_order.outsideRth = False

            entry_trade = self.ib.placeOrder(self.contract, entry_order)
            entry_order_id = entry_trade.order.orderId

            self._log_event("ENTRY_SUBMITTED", trade_id=trade_id,
                            direction=direction, price=entry_price,
                            quantity=num_contracts, order_id=entry_order_id,
                            details=f"Limit={limit_price}")

            # Start watchdog timer
            self._start_watchdog(entry_order_id, ORDER_WATCHDOG_SECONDS)

            # Wait for fill with timeout
            filled_qty = 0
            fill_price = 0.0
            start_time = time.monotonic()

            while time.monotonic() - start_time < ENTRY_TIMEOUT_SECONDS:
                self.ib.sleep(0.1)  # ib_insync event pump

                status = entry_trade.orderStatus.status
                filled_qty = int(entry_trade.orderStatus.filled)
                fill_price = entry_trade.orderStatus.avgFillPrice

                if status == "Filled":
                    break
                if status in ("Cancelled", "Inactive"):
                    break

            # Cancel watchdog
            self._cancel_watchdog(entry_order_id)

            # Handle timeout / partial fill
            if filled_qty == 0:
                # No fill — cancel and abort
                try:
                    self.ib.cancelOrder(entry_trade.order)
                except Exception:
                    pass
                self._order_in_flight = False
                self._log_event("ENTRY_TIMEOUT", trade_id=trade_id,
                                direction=direction, quantity=num_contracts,
                                details=f"0/{num_contracts} filled")
                logger.info("ENTRY TIMEOUT: %s — 0/%d contracts filled", trade_id, num_contracts)
                return None

            if filled_qty < num_contracts:
                # Partial fill — cancel remainder, treat as C1-only
                try:
                    self.ib.cancelOrder(entry_trade.order)
                except Exception:
                    pass
                num_contracts = filled_qty
                self._log_event("PARTIAL_FILL", trade_id=trade_id,
                                direction=direction, quantity=filled_qty,
                                details=f"{filled_qty}/{num_contracts} contracts filled, C1-only mode")
                logger.warning("PARTIAL FILL: %s — %d contracts filled, switching to C1-only",
                               trade_id, filled_qty)

            # Calculate slippage
            slippage = abs(fill_price - entry_price)
            self._track_slippage(slippage, trade_id)

            # Build trade record
            now = datetime.now(timezone.utc)
            c1_only = (num_contracts == 1)

            trade = {
                "trade_id": trade_id,
                "direction": direction,
                "action": action,
                "reverse_action": reverse_action,
                "contracts": num_contracts,
                "entry_price": round(fill_price, 2),
                "signal_price": round(entry_price, 2),
                "entry_time": now.isoformat(),
                "entry_order_id": entry_order_id,
                "stop_price": round(stop_price, 2),
                "c1_target": round(c1_target, 2),
                "c2_trail_distance": c2_trail_distance,
                "modifier_total": modifier_total,
                "confluence_score": signal.get("confluence_score", 0),
                "reason": signal.get("reason", ""),
                "slippage_entry": round(slippage, 2),
                # C1 state
                "c1_status": "OPEN",
                "c1_fill_price": round(fill_price, 2),
                "c1_fill_qty": 1,
                "c1_exit_price": 0.0,
                "c1_exit_time": "",
                "c1_pnl": 0.0,
                # C2 state
                "c2_status": "TRAILING" if not c1_only else "SKIPPED",
                "c2_fill_price": round(fill_price, 2) if not c1_only else 0.0,
                "c2_fill_qty": 1 if not c1_only else 0,
                "c2_exit_price": 0.0,
                "c2_exit_time": "",
                "c2_pnl": 0.0,
                "c2_trail_stop": 0.0,
                # Stop state
                "stop_order_id": None,
                "stop_status": "PENDING",
                "stop_qty": num_contracts,
                # Tracking
                "max_favorable_excursion": 0.0,
                "max_adverse_excursion": 0.0,
                "max_price_since_entry": fill_price if direction == "LONG" else float("inf"),
                "min_price_since_entry": fill_price if direction == "SHORT" else float("inf"),
                "c1_target_order_id": None,
                "total_filled": num_contracts,
                "total_target": 2,
            }

            # Place stop-loss order
            stop_placed = await self._place_stop_order(trade, stop_price, num_contracts)
            if not stop_placed:
                # CRITICAL: stop rejected — emergency flatten
                logger.critical("STOP REJECTED — emergency flatten for %s", trade_id)
                await self._emergency_flatten_trade(trade, "STOP_REJECTED_EMERGENCY_FLATTEN")
                self._order_in_flight = False
                return None

            # Place C1 take-profit order (1 contract)
            c1_tp_placed = await self._place_c1_target(trade)

            # Initialize C2 trailing stop
            if not c1_only:
                if direction == "LONG":
                    trade["c2_trail_stop"] = round(fill_price - c2_trail_distance, 2)
                else:
                    trade["c2_trail_stop"] = round(fill_price + c2_trail_distance, 2)

            # Record in active positions
            self._active_positions[trade_id] = trade

            self._log_event("ENTRY_FILLED", trade_id=trade_id,
                            direction=direction, price=fill_price,
                            quantity=num_contracts, fill_price=fill_price,
                            slippage=slippage, order_id=entry_order_id,
                            details=f"Stop={stop_price}, C1_target={c1_target}")

            # Log to trade_decisions.json
            self._log_trade_decision(trade, "ENTRY")

            logger.info("ENTRY FILLED: %s %s %d MNQ @ %.2f (signal=%.2f, slip=%.2f)",
                         trade_id, direction, num_contracts, fill_price, entry_price, slippage)

            self._order_in_flight = False
            return trade

        except Exception as e:
            self._order_in_flight = False
            logger.error("Entry submission error: %s", e)
            self._log_event("ORDER_ERROR", trade_id=trade_id,
                            details=str(e))
            return None

    # ══════════════════════════════════════════════════════════
    # STOP ORDER PLACEMENT
    # ══════════════════════════════════════════════════════════

    async def _place_stop_order(self, trade: dict, stop_price: float, quantity: int) -> bool:
        """Place stop-loss order. Returns True on success."""
        from ib_insync import StopOrder

        reverse_action = trade["reverse_action"]
        try:
            stop_order = StopOrder(reverse_action, quantity, round(stop_price, 2))
            stop_order.tif = "GTC"
            stop_trade = self.ib.placeOrder(self.contract, stop_order)
            trade["stop_order_id"] = stop_trade.order.orderId
            trade["stop_status"] = "WORKING"
            trade["stop_qty"] = quantity

            self._log_event("STOP_PLACED", trade_id=trade["trade_id"],
                            price=stop_price, quantity=quantity,
                            order_id=stop_trade.order.orderId)
            return True
        except Exception as e:
            logger.error("Stop order placement failed: %s", e)

            # Retry with 2 points wider
            try:
                direction = trade["direction"]
                wider_stop = stop_price - 2.0 if direction == "LONG" else stop_price + 2.0
                stop_order = StopOrder(reverse_action, quantity, round(wider_stop, 2))
                stop_order.tif = "GTC"
                stop_trade = self.ib.placeOrder(self.contract, stop_order)
                trade["stop_order_id"] = stop_trade.order.orderId
                trade["stop_status"] = "WORKING"
                trade["stop_qty"] = quantity
                logger.info("Stop placed on retry with wider price: %.2f", wider_stop)
                return True
            except Exception as e2:
                logger.critical("Stop retry also failed: %s — MUST FLATTEN", e2)
                return False

    async def _place_c1_target(self, trade: dict) -> bool:
        """Place C1 take-profit limit order for 1 contract."""
        from ib_insync import LimitOrder

        reverse_action = trade["reverse_action"]
        c1_target = trade["c1_target"]

        try:
            tp_order = LimitOrder(reverse_action, 1, round(c1_target, 2))
            tp_order.tif = "GTC"
            tp_trade = self.ib.placeOrder(self.contract, tp_order)
            trade["c1_target_order_id"] = tp_trade.order.orderId

            self._log_event("C1_TARGET_PLACED", trade_id=trade["trade_id"],
                            price=c1_target, quantity=1,
                            order_id=tp_trade.order.orderId)
            return True
        except Exception as e:
            logger.error("C1 target order failed: %s", e)
            return False

    # ══════════════════════════════════════════════════════════
    # C1 EXIT MANAGEMENT
    # ══════════════════════════════════════════════════════════

    def manage_c1_exit(self, trade: dict) -> bool:
        """
        Check if C1 fixed target has been filled.
        Returns True if C1 exited.
        """
        if trade["c1_status"] != "OPEN":
            return trade["c1_status"] == "FILLED"

        # Check if C1 target order filled (via callbacks)
        c1_order_id = trade.get("c1_target_order_id")
        if c1_order_id is None:
            return False

        # Check order status from ib_insync
        for ib_trade in self.ib.openTrades():
            if ib_trade.order.orderId == c1_order_id:
                if ib_trade.orderStatus.status == "Filled":
                    fill_price = ib_trade.orderStatus.avgFillPrice
                    self._handle_c1_fill(trade, fill_price)
                    return True
                return False

        # Order not in open trades — check if it completed
        for ib_trade in self.ib.trades():
            if ib_trade.order.orderId == c1_order_id:
                if ib_trade.orderStatus.status == "Filled":
                    fill_price = ib_trade.orderStatus.avgFillPrice
                    self._handle_c1_fill(trade, fill_price)
                    return True

        return False

    def _handle_c1_fill(self, trade: dict, fill_price: float) -> None:
        """Process C1 target fill."""
        direction = trade["direction"]
        entry_price = trade["entry_price"]

        # Calculate C1 PnL
        if direction == "LONG":
            c1_pnl = (fill_price - entry_price) * MNQ_POINT_VALUE
        else:
            c1_pnl = (entry_price - fill_price) * MNQ_POINT_VALUE

        # Verify PnL sign
        c1_pnl = self._verify_pnl_sign(direction, entry_price, fill_price, c1_pnl)

        trade["c1_status"] = "FILLED"
        trade["c1_exit_price"] = round(fill_price, 2)
        trade["c1_exit_time"] = datetime.now(timezone.utc).isoformat()
        trade["c1_pnl"] = round(c1_pnl, 2)

        # Modify stop order: reduce quantity to 1 (C2 only)
        if trade["c2_status"] == "TRAILING":
            self._modify_stop_quantity(trade, 1)

        self._log_event("C1_TARGET_FILLED", trade_id=trade["trade_id"],
                        direction=direction, fill_price=fill_price,
                        details=f"C1 PnL=${c1_pnl:.2f}")

        logger.info("C1 TARGET FILLED: %s @ %.2f, PnL=$%.2f",
                     trade["trade_id"], fill_price, c1_pnl)

    def _modify_stop_quantity(self, trade: dict, new_qty: int) -> None:
        """Modify stop order quantity (when C1 fills)."""
        from ib_insync import StopOrder

        stop_order_id = trade.get("stop_order_id")
        if stop_order_id is None:
            return

        reverse_action = trade["reverse_action"]
        stop_price = trade["stop_price"]

        try:
            # Find and modify the existing stop order
            for ib_trade in self.ib.openTrades():
                if ib_trade.order.orderId == stop_order_id:
                    ib_trade.order.totalQuantity = new_qty
                    self.ib.placeOrder(self.contract, ib_trade.order)
                    trade["stop_qty"] = new_qty
                    trade["stop_status"] = "MODIFIED"
                    logger.info("Stop quantity modified to %d for %s",
                                new_qty, trade["trade_id"])
                    return

            # Order not found — place new stop
            new_stop = StopOrder(reverse_action, new_qty, round(stop_price, 2))
            new_stop.tif = "GTC"
            new_trade = self.ib.placeOrder(self.contract, new_stop)
            trade["stop_order_id"] = new_trade.order.orderId
            trade["stop_qty"] = new_qty
            trade["stop_status"] = "MODIFIED"

        except Exception as e:
            logger.error("Failed to modify stop quantity: %s", e)

    # ══════════════════════════════════════════════════════════
    # C2 TRAILING STOP
    # ══════════════════════════════════════════════════════════

    async def manage_c2_trail(self, trade: dict, current_price: float) -> bool:
        """
        Update C2 trailing stop based on price movement.
        Returns True if C2 exited.
        """
        if trade["c2_status"] != "TRAILING":
            return trade["c2_status"] == "FILLED"

        direction = trade["direction"]
        trail_distance = trade["c2_trail_distance"]
        current_trail_stop = trade["c2_trail_stop"]

        # Update MFE/MAE tracking
        entry_price = trade["entry_price"]
        if direction == "LONG":
            excursion = current_price - entry_price
            trade["max_favorable_excursion"] = max(trade["max_favorable_excursion"], excursion)
            trade["max_adverse_excursion"] = max(trade["max_adverse_excursion"], -excursion)

            # Update high-water mark
            trade["max_price_since_entry"] = max(trade["max_price_since_entry"], current_price)

            # Calculate new trail stop
            calculated_stop = round(trade["max_price_since_entry"] - trail_distance, 2)

            # MONOTONIC: only move UP
            new_stop = max(current_trail_stop, calculated_stop)

            # Check if price crossed trail stop
            if current_price <= new_stop:
                await self._handle_c2_exit(trade, current_price)
                return True

        else:  # SHORT
            excursion = entry_price - current_price
            trade["max_favorable_excursion"] = max(trade["max_favorable_excursion"], excursion)
            trade["max_adverse_excursion"] = max(trade["max_adverse_excursion"], -excursion)

            # Update low-water mark
            if trade["min_price_since_entry"] == float("inf"):
                trade["min_price_since_entry"] = current_price
            trade["min_price_since_entry"] = min(trade["min_price_since_entry"], current_price)

            # Calculate new trail stop
            calculated_stop = round(trade["min_price_since_entry"] + trail_distance, 2)

            # MONOTONIC: only move DOWN
            new_stop = min(current_trail_stop, calculated_stop)

            # Check if price crossed trail stop
            if current_price >= new_stop:
                await self._handle_c2_exit(trade, current_price)
                return True

        # Update stop order on IBKR if stop moved
        if new_stop != current_trail_stop:
            trade["c2_trail_stop"] = round(new_stop, 2)
            success = await self._modify_trail_stop(trade, new_stop)
            if success:
                self._log_event("C2_TRAIL_UPDATED", trade_id=trade["trade_id"],
                                price=new_stop,
                                details=f"Trail moved to {new_stop:.2f}")

        return False

    async def _modify_trail_stop(self, trade: dict, new_stop_price: float) -> bool:
        """Modify the C2 trailing stop on IBKR with retry."""
        from ib_insync import StopOrder

        stop_order_id = trade.get("stop_order_id")
        reverse_action = trade["reverse_action"]

        if stop_order_id is None:
            return False

        # Attempt 1: modify existing order
        try:
            for ib_trade in self.ib.openTrades():
                if ib_trade.order.orderId == stop_order_id:
                    ib_trade.order.auxPrice = round(new_stop_price, 2)
                    self.ib.placeOrder(self.contract, ib_trade.order)
                    return True
        except Exception as e:
            logger.warning("C2 trail modify attempt 1 failed: %s", e)

        # Attempt 2: retry after delay
        await asyncio.sleep(C2_TRAIL_RETRY_DELAY)
        try:
            for ib_trade in self.ib.openTrades():
                if ib_trade.order.orderId == stop_order_id:
                    ib_trade.order.auxPrice = round(new_stop_price, 2)
                    self.ib.placeOrder(self.contract, ib_trade.order)
                    return True
        except Exception as e:
            logger.warning("C2 trail modify attempt 2 failed: %s", e)

        # Attempt 3: cancel old stop, place new
        try:
            for ib_trade in self.ib.openTrades():
                if ib_trade.order.orderId == stop_order_id:
                    self.ib.cancelOrder(ib_trade.order)
                    break

            new_stop_order = StopOrder(reverse_action, 1, round(new_stop_price, 2))
            new_stop_order.tif = "GTC"
            new_trade = self.ib.placeOrder(self.contract, new_stop_order)
            trade["stop_order_id"] = new_trade.order.orderId
            logger.info("C2 trail: placed new stop order at %.2f", new_stop_price)
            return True
        except Exception as e:
            logger.critical("C2 trail: all attempts failed — market closing C2: %s", e)
            await self._handle_c2_exit(trade, new_stop_price)
            return False

    async def _handle_c2_exit(self, trade: dict, exit_price: float) -> None:
        """Process C2 trailing stop exit."""
        direction = trade["direction"]
        entry_price = trade["entry_price"]

        # Calculate C2 PnL
        if direction == "LONG":
            c2_pnl = (exit_price - entry_price) * MNQ_POINT_VALUE
        else:
            c2_pnl = (entry_price - exit_price) * MNQ_POINT_VALUE

        c2_pnl = self._verify_pnl_sign(direction, entry_price, exit_price, c2_pnl)

        trade["c2_status"] = "FILLED"
        trade["c2_exit_price"] = round(exit_price, 2)
        trade["c2_exit_time"] = datetime.now(timezone.utc).isoformat()
        trade["c2_pnl"] = round(c2_pnl, 2)

        self._log_event("C2_TRAIL_FILLED", trade_id=trade["trade_id"],
                        direction=direction, fill_price=exit_price,
                        details=f"C2 PnL=${c2_pnl:.2f}")

        logger.info("C2 TRAIL FILLED: %s @ %.2f, PnL=$%.2f",
                     trade["trade_id"], exit_price, c2_pnl)

        # Check if trade is fully closed
        self._check_trade_complete(trade)

    # ══════════════════════════════════════════════════════════
    # STOP LOSS FILL
    # ══════════════════════════════════════════════════════════

    def _handle_stop_fill(self, trade: dict, fill_price: float, filled_qty: int) -> None:
        """Process stop-loss fill — closes remaining contracts."""
        direction = trade["direction"]
        entry_price = trade["entry_price"]

        # Handle partial stop fill
        if filled_qty < trade.get("stop_qty", trade["contracts"]):
            remaining = trade["stop_qty"] - filled_qty
            logger.critical("PARTIAL STOP FILL: %d/%d — submitting market order for %d",
                            filled_qty, trade["stop_qty"], remaining)
            self._log_event("PARTIAL_STOP_FILL", trade_id=trade["trade_id"],
                            quantity=filled_qty,
                            details=f"Market order for {remaining} remaining")
            # Submit market order for remainder
            try:
                from ib_insync import MarketOrder
                mkt_order = MarketOrder(trade["reverse_action"], remaining)
                self.ib.placeOrder(self.contract, mkt_order)
            except Exception as e:
                logger.critical("Failed to submit market order for partial stop remainder: %s", e)

        # Calculate PnL for each unfilled leg
        if direction == "LONG":
            pnl_per = (fill_price - entry_price) * MNQ_POINT_VALUE
        else:
            pnl_per = (entry_price - fill_price) * MNQ_POINT_VALUE

        # Close C1 if still open
        if trade["c1_status"] == "OPEN":
            c1_pnl = self._verify_pnl_sign(direction, entry_price, fill_price, pnl_per)
            trade["c1_status"] = "FILLED"
            trade["c1_exit_price"] = round(fill_price, 2)
            trade["c1_exit_time"] = datetime.now(timezone.utc).isoformat()
            trade["c1_pnl"] = round(c1_pnl, 2)

        # Close C2 if still open
        if trade["c2_status"] == "TRAILING":
            c2_pnl = self._verify_pnl_sign(direction, entry_price, fill_price, pnl_per)
            trade["c2_status"] = "FILLED"
            trade["c2_exit_price"] = round(fill_price, 2)
            trade["c2_exit_time"] = datetime.now(timezone.utc).isoformat()
            trade["c2_pnl"] = round(c2_pnl, 2)

        trade["stop_status"] = "FILLED"

        self._log_event("STOP_FILLED", trade_id=trade["trade_id"],
                        direction=direction, fill_price=fill_price,
                        quantity=filled_qty,
                        details=f"Stop hit at {fill_price:.2f}")

        logger.info("STOP FILLED: %s @ %.2f", trade["trade_id"], fill_price)

        self._check_trade_complete(trade)

    # ══════════════════════════════════════════════════════════
    # TRADE COMPLETION
    # ══════════════════════════════════════════════════════════

    def _check_trade_complete(self, trade: dict) -> None:
        """Check if all legs of a trade are closed and finalize."""
        c1_done = trade["c1_status"] in ("FILLED", "SKIPPED", "CANCELLED")
        c2_done = trade["c2_status"] in ("FILLED", "SKIPPED", "CANCELLED")

        if not (c1_done and c2_done):
            return

        # Calculate total PnL
        total_pnl = trade["c1_pnl"] + trade["c2_pnl"]
        trade["total_pnl"] = round(total_pnl, 2)

        # Calculate hold duration
        try:
            entry_time = datetime.fromisoformat(trade["entry_time"])
            now = datetime.now(timezone.utc)
            trade["hold_duration_seconds"] = int((now - entry_time).total_seconds())
        except (ValueError, TypeError):
            trade["hold_duration_seconds"] = 0

        # Update daily PnL
        self._daily_pnl += total_pnl
        self._current_equity += total_pnl
        self._peak_equity = max(self._peak_equity, self._current_equity)

        # Track consecutive losses
        self._trade_results.append(total_pnl)
        if total_pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        # Remove from active positions
        trade_id = trade["trade_id"]
        self._active_positions.pop(trade_id, None)
        self._trade_history.append(trade)

        # Cancel any remaining orders for this trade
        self._cancel_trade_orders(trade)

        # Log trade summary
        self._log_trade_summary(trade)
        self._log_trade_decision(trade, "CLOSED")

        logger.info("TRADE COMPLETE: %s | PnL=$%.2f (C1=$%.2f, C2=$%.2f) | Daily=$%.2f",
                     trade_id, total_pnl, trade["c1_pnl"], trade["c2_pnl"], self._daily_pnl)

    # ══════════════════════════════════════════════════════════
    # EMERGENCY CLOSE
    # ══════════════════════════════════════════════════════════

    async def close_all_positions(self, reason: str = "MANUAL") -> dict:
        """
        Emergency close all open positions.
        Cancels all pending orders, submits market orders to flatten.
        """
        from ib_insync import MarketOrder

        result = {"reason": reason, "trades_closed": 0, "orders_cancelled": 0}

        logger.critical("CLOSE ALL POSITIONS: reason=%s", reason)
        self._log_event("EMERGENCY_CLOSE", details=reason)

        # Cancel all pending orders
        for ib_trade in self.ib.openTrades():
            try:
                self.ib.cancelOrder(ib_trade.order)
                result["orders_cancelled"] += 1
            except Exception as e:
                logger.error("Failed to cancel order %d: %s", ib_trade.order.orderId, e)

        # Market close all positions
        for trade_id, trade in list(self._active_positions.items()):
            try:
                reverse_action = trade["reverse_action"]
                remaining = trade["contracts"]

                # Determine remaining quantity
                if trade["c1_status"] == "FILLED":
                    remaining -= 1
                if trade["c2_status"] == "FILLED":
                    remaining -= 1

                if remaining > 0:
                    mkt_order = MarketOrder(reverse_action, remaining)
                    self.ib.placeOrder(self.contract, mkt_order)
                    result["trades_closed"] += 1

                    # Get approximate current price for PnL
                    ticker = self.ib.reqMktData(self.contract, '', False, False)
                    self.ib.sleep(0.5)
                    current_price = ticker.last if ticker.last and ticker.last > 0 else trade["entry_price"]
                    try:
                        self.ib.cancelMktData(self.contract)
                    except Exception:
                        pass

                    # Close trade legs
                    self._finalize_emergency_close(trade, current_price, reason)

            except Exception as e:
                logger.error("Failed to close trade %s: %s", trade_id, e)

        self._order_in_flight = False
        return result

    async def _emergency_flatten_trade(self, trade: dict, reason: str) -> None:
        """Emergency flatten a single trade."""
        from ib_insync import MarketOrder

        try:
            remaining = trade["contracts"]
            reverse_action = trade["reverse_action"]
            mkt_order = MarketOrder(reverse_action, remaining)
            self.ib.placeOrder(self.contract, mkt_order)

            self._log_event(reason, trade_id=trade["trade_id"],
                            direction=trade["direction"],
                            quantity=remaining)

        except Exception as e:
            logger.critical("Emergency flatten failed for %s: %s", trade["trade_id"], e)

    def _finalize_emergency_close(self, trade: dict, price: float, reason: str) -> None:
        """Finalize trade on emergency close."""
        direction = trade["direction"]
        entry_price = trade["entry_price"]

        if direction == "LONG":
            pnl_per = (price - entry_price) * MNQ_POINT_VALUE
        else:
            pnl_per = (entry_price - price) * MNQ_POINT_VALUE

        if trade["c1_status"] == "OPEN":
            trade["c1_status"] = "FILLED"
            trade["c1_exit_price"] = round(price, 2)
            trade["c1_exit_time"] = datetime.now(timezone.utc).isoformat()
            trade["c1_pnl"] = round(pnl_per, 2)

        if trade["c2_status"] == "TRAILING":
            trade["c2_status"] = "FILLED"
            trade["c2_exit_price"] = round(price, 2)
            trade["c2_exit_time"] = datetime.now(timezone.utc).isoformat()
            trade["c2_pnl"] = round(pnl_per, 2)

        trade["stop_status"] = "CANCELLED"
        self._check_trade_complete(trade)

    def _cancel_trade_orders(self, trade: dict) -> None:
        """Cancel any remaining open orders for a trade."""
        order_ids = [
            trade.get("stop_order_id"),
            trade.get("c1_target_order_id"),
        ]
        for oid in order_ids:
            if oid is not None:
                try:
                    for ib_trade in self.ib.openTrades():
                        if ib_trade.order.orderId == oid:
                            self.ib.cancelOrder(ib_trade.order)
                            break
                except Exception:
                    pass

    # ══════════════════════════════════════════════════════════
    # QUERIES
    # ══════════════════════════════════════════════════════════

    def get_active_positions(self) -> list:
        """Return current open positions for dashboard."""
        result = []
        for trade_id, trade in self._active_positions.items():
            try:
                entry_time = datetime.fromisoformat(trade["entry_time"])
                hold_seconds = int((datetime.now(timezone.utc) - entry_time).total_seconds())
            except (ValueError, TypeError):
                hold_seconds = 0

            result.append({
                "id": trade_id,
                "direction": trade["direction"],
                "contracts": trade["contracts"],
                "entry_price": trade["entry_price"],
                "entry_time": trade["entry_time"],
                "current_price": self._last_bar_close,
                "unrealized_pnl": self._calc_unrealized_pnl(trade),
                "c1_status": trade["c1_status"],
                "c1_target": trade["c1_target"],
                "c2_trail_stop": trade.get("c2_trail_stop", 0),
                "modifier_total": trade["modifier_total"],
                "hold_duration_seconds": hold_seconds,
            })
        return result

    def get_order_log(self) -> list:
        """Return all order events."""
        return list(self._order_log)

    def get_trade_history(self) -> list:
        """Return completed trade history."""
        return list(self._trade_history)

    def get_trade_metrics(self) -> dict:
        """Return aggregate trade metrics for dashboard."""
        wins = sum(1 for t in self._trade_history if t.get("total_pnl", 0) > 0)
        losses = sum(1 for t in self._trade_history if t.get("total_pnl", 0) < 0)
        total = len(self._trade_history)

        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / total * 100) if total > 0 else 0.0,
            "daily_pnl": round(self._daily_pnl, 2),
            "total_pnl": round(sum(t.get("total_pnl", 0) for t in self._trade_history), 2),
            "current_equity": round(self._current_equity, 2),
            "peak_equity": round(self._peak_equity, 2),
            "consecutive_losses": self._consecutive_losses,
            "avg_slippage": round(self._slippage_total / max(1, self._slippage_count), 2),
            "active_positions": len(self._active_positions),
            "order_in_flight": self._order_in_flight,
        }

    # ══════════════════════════════════════════════════════════
    # IBKR EVENT CALLBACKS
    # ══════════════════════════════════════════════════════════

    def _on_order_status(self, trade) -> None:
        """Handle IBKR order status changes."""
        status = trade.orderStatus.status
        order_id = trade.order.orderId
        filled = int(trade.orderStatus.filled)
        avg_fill = trade.orderStatus.avgFillPrice

        logger.debug("Order status: id=%d status=%s filled=%d avg=%.2f",
                      order_id, status, filled, avg_fill)

        if status == "Filled":
            # Check if this is a stop fill for any active trade
            for t_id, t in list(self._active_positions.items()):
                if t.get("stop_order_id") == order_id:
                    self._handle_stop_fill(t, avg_fill, filled)
                    return
                if t.get("c1_target_order_id") == order_id:
                    self._handle_c1_fill(t, avg_fill)
                    return

    def _on_execution(self, trade, fill) -> None:
        """Handle IBKR execution details — deduplicate by execId."""
        exec_id = fill.execution.execId
        if exec_id in self._processed_exec_ids:
            logger.debug("Duplicate execution ignored: %s", exec_id)
            return
        self._processed_exec_ids.add(exec_id)

        order_id = fill.execution.orderId
        fill_price = fill.execution.price
        filled_qty = int(fill.execution.shares)

        logger.info("Execution: orderId=%d price=%.2f qty=%d execId=%s",
                     order_id, fill_price, filled_qty, exec_id)

    def _on_error(self, reqId, errorCode, errorString, contract) -> None:
        """Handle IBKR error events."""
        # Filter info messages
        if errorCode in (2104, 2106, 2158, 2119):
            return

        logger.error("IBKR error [reqId=%d, code=%d]: %s", reqId, errorCode, errorString)

        # Order rejected
        if errorCode == 201:
            self._log_event("ORDER_REJECTED", order_id=reqId,
                            details=f"Error 201: {errorString}")
            # Check if this is a stop rejection — CRITICAL
            for t_id, t in list(self._active_positions.items()):
                if t.get("stop_order_id") == reqId:
                    logger.critical("STOP ORDER REJECTED for %s — emergency flatten!", t_id)
                    asyncio.get_event_loop().create_task(
                        self._emergency_flatten_trade(t, "STOP_REJECTED_EMERGENCY_FLATTEN"))
                    return

        # Order cancelled (expected for timeouts)
        if errorCode == 202:
            self._log_event("ORDER_CANCELLED", order_id=reqId,
                            details=f"Error 202: {errorString}")

    def _on_disconnect(self) -> None:
        """Handle TWS disconnection."""
        logger.critical("TWS DISCONNECTED — stop orders remain on server")
        self._log_event("TWS_DISCONNECTED",
                        details="Stop orders survive on IBKR server")

    # ══════════════════════════════════════════════════════════
    # RECONNECT RECONCILIATION
    # ══════════════════════════════════════════════════════════

    async def reconcile_after_reconnect(self) -> dict:
        """
        After TWS reconnect, reconcile local state with IBKR.
        Query positions and open orders, compare with internal state.
        """
        result = {"matched": True, "discrepancies": []}

        try:
            # Query IBKR for current positions
            ibkr_positions = self.ib.positions()
            mnq_position = 0
            for pos in ibkr_positions:
                if pos.contract.symbol == "MNQ":
                    mnq_position = int(pos.position)

            # Query open orders
            ibkr_orders = self.ib.openOrders()

            # Check local state
            local_contracts = sum(
                t.get("contracts", 0) for t in self._active_positions.values()
                if t.get("c1_status") == "OPEN" or t.get("c2_status") == "TRAILING"
            )

            if mnq_position != 0 and len(self._active_positions) == 0:
                # IBKR has position but bot doesn't know about it
                result["matched"] = False
                result["discrepancies"].append({
                    "type": "GHOST_POSITION",
                    "ibkr_qty": mnq_position,
                    "local_qty": 0,
                    "action": "ALERT — closing unknown position",
                })
                logger.critical("GHOST POSITION: IBKR shows %d MNQ but bot has no record",
                                mnq_position)
                # Close the ghost position
                from ib_insync import MarketOrder
                action = "SELL" if mnq_position > 0 else "BUY"
                mkt_order = MarketOrder(action, abs(mnq_position))
                self.ib.placeOrder(self.contract, mkt_order)

            elif mnq_position == 0 and len(self._active_positions) > 0:
                # Bot thinks it has position but IBKR doesn't
                result["matched"] = False
                for t_id, trade in list(self._active_positions.items()):
                    result["discrepancies"].append({
                        "type": "POSITION_CLOSED_WHILE_DISCONNECTED",
                        "trade_id": t_id,
                        "action": "Updating local state — stop was likely hit",
                    })
                    logger.warning("Position %s was closed while disconnected", t_id)
                    # Reconstruct — assume stopped out at stop price
                    stop_price = trade.get("stop_price", trade["entry_price"])
                    self._finalize_emergency_close(trade, stop_price, "DISCONNECTED_STOP_OUT")

            self._log_event("RECONNECT_RECONCILIATION",
                            details=json.dumps(result, default=str))
            logger.info("Reconnect reconciliation: matched=%s discrepancies=%d",
                        result["matched"], len(result["discrepancies"]))

        except Exception as e:
            logger.error("Reconciliation failed: %s", e)
            result["matched"] = False
            result["discrepancies"].append({"type": "ERROR", "details": str(e)})

        return result

    # ══════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════

    def _calc_unrealized_pnl(self, trade: dict) -> float:
        """Calculate unrealized PnL for a trade."""
        if self._last_bar_close <= 0:
            return 0.0

        direction = trade["direction"]
        entry_price = trade["entry_price"]
        current = self._last_bar_close

        # Count remaining open contracts
        remaining = 0
        if trade["c1_status"] == "OPEN":
            remaining += 1
        if trade["c2_status"] == "TRAILING":
            remaining += 1

        if direction == "LONG":
            pnl = (current - entry_price) * MNQ_POINT_VALUE * remaining
        else:
            pnl = (entry_price - current) * MNQ_POINT_VALUE * remaining

        return round(pnl, 2)

    def _verify_pnl_sign(self, direction: str, entry: float, exit: float, pnl: float) -> float:
        """Verify PnL sign is correct for the direction."""
        if direction == "LONG":
            expected_positive = exit > entry
        else:
            expected_positive = entry > exit

        if expected_positive and pnl < 0:
            logger.error("PNL_SIGN_ERROR: direction=%s entry=%.2f exit=%.2f pnl=%.2f — recalculating",
                         direction, entry, exit, pnl)
            if direction == "LONG":
                return round((exit - entry) * MNQ_POINT_VALUE, 2)
            else:
                return round((entry - exit) * MNQ_POINT_VALUE, 2)

        if not expected_positive and pnl > 0 and entry != exit:
            logger.error("PNL_SIGN_ERROR: direction=%s entry=%.2f exit=%.2f pnl=%.2f — recalculating",
                         direction, entry, exit, pnl)
            if direction == "LONG":
                return round((exit - entry) * MNQ_POINT_VALUE, 2)
            else:
                return round((entry - exit) * MNQ_POINT_VALUE, 2)

        return pnl

    def _track_slippage(self, slippage: float, trade_id: str) -> None:
        """Track slippage and alert if excessive."""
        self._slippage_total += slippage
        self._slippage_count += 1

        if slippage > SLIPPAGE_CRITICAL_PTS:
            logger.critical("CRITICAL SLIPPAGE: %.2f pts on %s (threshold: %.0f)",
                            slippage, trade_id, SLIPPAGE_CRITICAL_PTS)
        elif slippage > SLIPPAGE_WARN_PTS:
            logger.warning("HIGH SLIPPAGE: %.2f pts on %s (threshold: %.0f)",
                           slippage, trade_id, SLIPPAGE_WARN_PTS)

    # ══════════════════════════════════════════════════════════
    # WATCHDOG
    # ══════════════════════════════════════════════════════════

    def _start_watchdog(self, order_id: int, timeout: float) -> None:
        """Start a watchdog timer that cancels an order after timeout."""
        def _timeout():
            logger.warning("ORDER_WATCHDOG_TIMEOUT: orderId=%d after %.0fs", order_id, timeout)
            self._log_event("ORDER_WATCHDOG_TIMEOUT", order_id=order_id)
            try:
                for ib_trade in self.ib.openTrades():
                    if ib_trade.order.orderId == order_id:
                        self.ib.cancelOrder(ib_trade.order)
                        break
            except Exception as e:
                logger.error("Watchdog cancel failed: %s", e)
            self._order_in_flight = False

        timer = threading.Timer(timeout, _timeout)
        timer.daemon = True
        timer.start()
        self._watchdog_timers[order_id] = timer

    def _cancel_watchdog(self, order_id: int) -> None:
        """Cancel a watchdog timer."""
        timer = self._watchdog_timers.pop(order_id, None)
        if timer:
            timer.cancel()

    # ══════════════════════════════════════════════════════════
    # LOGGING
    # ══════════════════════════════════════════════════════════

    def _log_event(self, event: str, trade_id: str = "", direction: str = "",
                   price: float = 0, quantity: int = 0, fill_price: float = 0,
                   slippage: float = 0, order_id: int = 0, details: str = "") -> None:
        """Log an order event to order_events.json."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "trade_id": trade_id,
            "direction": direction,
            "price": price,
            "quantity": quantity,
            "fill_price": fill_price,
            "slippage": slippage,
            "order_id": order_id,
            "details": details,
        }
        self._order_log.append(entry)

        try:
            log_path = self._log_dir / "order_events.json"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as e:
            logger.warning("Failed to write order event log: %s", e)

    def _log_trade_summary(self, trade: dict) -> None:
        """Log trade summary when fully closed."""
        summary = {
            "trade_id": trade["trade_id"],
            "direction": trade["direction"],
            "entry_price": trade["entry_price"],
            "entry_time": trade["entry_time"],
            "c1_exit_price": trade.get("c1_exit_price", 0),
            "c1_exit_time": trade.get("c1_exit_time", ""),
            "c1_pnl": trade.get("c1_pnl", 0),
            "c2_exit_price": trade.get("c2_exit_price", 0),
            "c2_exit_time": trade.get("c2_exit_time", ""),
            "c2_pnl": trade.get("c2_pnl", 0),
            "total_pnl": trade.get("total_pnl", 0),
            "hold_duration_seconds": trade.get("hold_duration_seconds", 0),
            "modifier_at_entry": trade.get("modifier_total", 1.0),
            "slippage_entry": trade.get("slippage_entry", 0),
            "max_favorable_excursion": trade.get("max_favorable_excursion", 0),
            "max_adverse_excursion": trade.get("max_adverse_excursion", 0),
        }

        try:
            log_path = self._log_dir / "trade_decisions.json"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(summary, default=str) + "\n")
        except OSError as e:
            logger.warning("Failed to write trade summary: %s", e)

    def _log_trade_decision(self, trade: dict, action: str) -> None:
        """Log trade decision for audit trail."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "trade_id": trade["trade_id"],
            "direction": trade["direction"],
            "contracts": trade["contracts"],
            "entry_price": trade["entry_price"],
            "stop_price": trade["stop_price"],
            "c1_target": trade["c1_target"],
            "modifier_total": trade["modifier_total"],
            "confluence_score": trade.get("confluence_score", 0),
        }
        if action == "CLOSED":
            entry["total_pnl"] = trade.get("total_pnl", 0)
            entry["c1_pnl"] = trade.get("c1_pnl", 0)
            entry["c2_pnl"] = trade.get("c2_pnl", 0)

        try:
            log_path = self._log_dir / "trade_decisions.json"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as e:
            logger.warning("Failed to write trade decision: %s", e)

    # ══════════════════════════════════════════════════════════
    # STATE PERSISTENCE
    # ══════════════════════════════════════════════════════════

    def save_state(self) -> None:
        """Save current state to paper_trading_state.json."""
        state = {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "daily_pnl": round(self._daily_pnl, 2),
            "current_equity": round(self._current_equity, 2),
            "peak_equity": round(self._peak_equity, 2),
            "consecutive_losses": self._consecutive_losses,
            "trade_count": len(self._trade_history),
            "active_positions": {
                k: {
                    "trade_id": v["trade_id"],
                    "direction": v["direction"],
                    "contracts": v["contracts"],
                    "entry_price": v["entry_price"],
                    "entry_time": v["entry_time"],
                    "stop_price": v["stop_price"],
                    "c1_target": v["c1_target"],
                    "c1_status": v["c1_status"],
                    "c2_status": v["c2_status"],
                    "c2_trail_stop": v.get("c2_trail_stop", 0),
                }
                for k, v in self._active_positions.items()
            },
            "slippage_avg": round(self._slippage_total / max(1, self._slippage_count), 2),
        }

        try:
            state_path = self._log_dir / "order_manager_state.json"
            tmp_path = state_path.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            import os
            os.replace(str(tmp_path), str(state_path))
        except OSError as e:
            logger.warning("Failed to save order manager state: %s", e)

    def load_state(self) -> bool:
        """Load state from disk on startup."""
        state_path = self._log_dir / "order_manager_state.json"
        if not state_path.exists():
            return False

        try:
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)

            self._daily_pnl = state.get("daily_pnl", 0.0)
            self._current_equity = state.get("current_equity", self._peak_equity)
            self._peak_equity = state.get("peak_equity", self._peak_equity)
            self._consecutive_losses = state.get("consecutive_losses", 0)

            logger.info("Order manager state restored: daily_pnl=$%.2f equity=$%.2f",
                        self._daily_pnl, self._current_equity)
            return True
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load order manager state: %s", e)
            return False

    def reset_daily(self) -> None:
        """Reset daily state for new session."""
        self._daily_pnl = 0.0
        self._consecutive_losses = 0
        self._eod_closed = False
        self._trade_results.clear()
        logger.info("Order manager daily reset complete")
