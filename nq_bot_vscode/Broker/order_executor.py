"""
IBKR Order Execution Adapter
==============================
Places MNQ orders through the IBKR Client Portal Gateway REST API.

Supports:
  - Market and Limit orders
  - 2-contract scale-out: C1 (fixed target) + C2 (runner with trailing stop)
  - Paper trading mode only (live raises NotImplementedError)

Safety rails — HARD BLOCKS that cannot be overridden:
  1. Max 2 contracts per order (MNQ)
  2. Max 4 open positions at any time
  3. No orders outside RTH unless config explicitly allows ETH
  4. Daily loss limit check before every order ($500 default)
  5. Kill switch: daily P&L hits -$1000 -> halt all trading, cancel open orders

Every order attempt is logged with timestamp, direction, size, price,
and rejection reason if blocked.
"""

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from Broker.ibkr_client_portal import IBKRClient, IBKRConfig, SessionType, get_session_type
from monitoring.execution_analytics import ExecutionAnalytics

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# SAFETY CONSTANTS — DO NOT CHANGE
# ═══════════════════════════════════════════════════════════════
MAX_CONTRACTS_PER_ORDER = 2
MAX_OPEN_POSITIONS = 4
DAILY_LOSS_LIMIT_DOLLARS = 500.0
KILL_SWITCH_THRESHOLD_DOLLARS = 1000.0
MNQ_POINT_VALUE = 2.0       # $2.00 per point for Micro E-mini Nasdaq


# ═══════════════════════════════════════════════════════════════
# ENUMS
# ═══════════════════════════════════════════════════════════════

class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class IBKROrderType(Enum):
    MARKET = "MKT"
    LIMIT = "LMT"


class OrderState(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


# ═══════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════

@dataclass
class OrderRequest:
    """Immutable description of a requested order."""
    side: OrderSide
    order_type: IBKROrderType
    contracts: int
    limit_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    tag: str = ""                # "C1", "C2", or free-form label


@dataclass
class OrderRecord:
    """Audit record for every order attempt (accepted or rejected)."""
    timestamp: datetime
    side: str
    order_type: str
    contracts: int
    price: float                 # limit price or 0.0 for market
    tag: str = ""
    accepted: bool = False
    rejection_reason: str = ""
    broker_order_id: str = ""
    state: OrderState = OrderState.PENDING
    fill_price: float = 0.0


@dataclass
class OpenPosition:
    """Tracks a single open position for limit enforcement."""
    broker_order_id: str
    side: str
    contracts: int
    entry_price: float
    tag: str = ""
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ExecutorConfig:
    """Tunable knobs — safety limits are NOT here (they are constants)."""
    allow_eth: bool = False
    paper_mode: bool = True


@dataclass
class ExecutorState:
    """Mutable runtime state for the executor."""
    daily_pnl: float = 0.0
    daily_trades: int = 0
    daily_blocked: int = 0
    is_halted: bool = False
    halt_reason: str = ""
    open_positions: List[OpenPosition] = field(default_factory=list)
    order_log: List[OrderRecord] = field(default_factory=list)
    session_date: str = ""


# ═══════════════════════════════════════════════════════════════
# IBKR ORDER EXECUTOR
# ═══════════════════════════════════════════════════════════════

class IBKROrderExecutor:
    """
    IBKR order execution adapter with mandatory safety rails.

    Every public method that touches orders passes through
    ``_run_safety_checks()`` first.  There is no code path
    that bypasses these checks.
    """

    def __init__(
        self,
        client: IBKRClient,
        config: Optional[ExecutorConfig] = None,
        execution_analytics: Optional[ExecutionAnalytics] = None,
    ):
        self._client = client
        self._config = config or ExecutorConfig()
        self._state = ExecutorState()
        self._on_fill: Optional[Callable] = None
        self._analytics = execution_analytics
        # Persistent order log file
        self._log_dir = Path(__file__).resolve().parent.parent / "logs"
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._order_log_path = self._log_dir / "order_log.json"

    # ──────────────────────────────────────────────────────────
    # PUBLIC API
    # ──────────────────────────────────────────────────────────

    async def place_order(self, request: OrderRequest) -> OrderRecord:
        """
        Place a single order after passing ALL safety checks.

        Returns an ``OrderRecord`` — always.  If the order was
        blocked, ``record.accepted`` is False and
        ``record.rejection_reason`` explains why.
        """
        record = OrderRecord(
            timestamp=datetime.now(timezone.utc),
            side=request.side.value,
            order_type=request.order_type.value,
            contracts=request.contracts,
            price=request.limit_price,
            tag=request.tag,
        )

        # ── SAFETY GATE (no bypass path) ──
        rejection = self._run_safety_checks(request)
        if rejection:
            record.accepted = False
            record.rejection_reason = rejection
            record.state = OrderState.REJECTED
            self._state.daily_blocked += 1
            self._log_order(record)
            # Analytics: record rejection (non-blocking)
            if self._analytics:
                record.broker_order_id = record.broker_order_id or f"REJECTED-{int(time.time() * 1000)}"
                self._analytics.record_order_sent(
                    order_id=record.broker_order_id,
                    side=request.side.value,
                    size=request.contracts,
                    expected_price=request.limit_price,
                    timestamp=record.timestamp,
                    order_type=request.order_type.value.lower(),
                )
                self._analytics.record_rejection(
                    order_id=record.broker_order_id,
                    reason=rejection,
                    timestamp=record.timestamp,
                )
            return record

        # ── ROUTE TO PAPER OR LIVE ──
        if self._config.paper_mode:
            return await self._execute_paper(request, record)

        return await self._execute_live(request, record)

    async def place_scale_out_entry(
        self,
        direction: str,
        limit_price: float = 0.0,
        stop_loss: float = 0.0,
        c1_take_profit: float = 0.0,
        instrument: str = "MNQ",
    ) -> Dict[str, OrderRecord]:
        """
        Place the standard 2-contract scale-out entry.

        C1: 1 contract with fixed take-profit target.
        C2: 1 contract as runner (trailing stop managed externally).
        Both share the same initial stop-loss.
        """
        # ── INSTRUMENT VALIDATION GATE ──
        rejection = self._check_instrument_validated(instrument)
        if rejection:
            now = datetime.now(timezone.utc)
            rejected = OrderRecord(
                timestamp=now, side=direction, order_type="MKT",
                contracts=2, price=limit_price, tag="BLOCKED",
                accepted=False, rejection_reason=rejection,
                state=OrderState.REJECTED,
            )
            self._log_order(rejected)
            return {"c1": rejected, "c2": rejected}

        side = OrderSide.BUY if direction == "long" else OrderSide.SELL
        order_type = (IBKROrderType.LIMIT if limit_price > 0
                      else IBKROrderType.MARKET)

        c1_request = OrderRequest(
            side=side,
            order_type=order_type,
            contracts=1,
            limit_price=limit_price,
            stop_loss=stop_loss,
            take_profit=c1_take_profit,
            tag="C1",
        )
        c2_request = OrderRequest(
            side=side,
            order_type=order_type,
            contracts=1,
            limit_price=limit_price,
            stop_loss=stop_loss,
            take_profit=0.0,
            tag="C2",
        )

        c1_record = await self.place_order(c1_request)
        # Only place C2 if C1 was accepted
        if c1_record.accepted:
            c2_record = await self.place_order(c2_request)
        else:
            # Mirror the rejection for C2
            c2_record = OrderRecord(
                timestamp=datetime.now(timezone.utc),
                side=side.value,
                order_type=order_type.value,
                contracts=1,
                price=limit_price,
                tag="C2",
                accepted=False,
                rejection_reason=f"C1 rejected: {c1_record.rejection_reason}",
                state=OrderState.REJECTED,
            )
            self._log_order(c2_record)

        return {"c1": c1_record, "c2": c2_record}

    async def modify_stop(
        self, broker_order_id: str, new_stop_price: float
    ) -> bool:
        """Modify an existing stop order price (for trailing stops)."""
        if self._config.paper_mode:
            logger.info(
                "PAPER modify_stop order_id=%s new_stop=%.2f",
                broker_order_id, new_stop_price,
            )
            return True
        raise NotImplementedError(
            "LIVE modify_stop NOT IMPLEMENTED — paper trading only."
        )

    async def cancel_order(self, broker_order_id: str, reason: str = "manual") -> bool:
        """Cancel a single open order."""
        if self._config.paper_mode:
            logger.info("PAPER cancel_order order_id=%s", broker_order_id)
            # Analytics: record cancellation (non-blocking)
            if self._analytics:
                self._analytics.record_cancel(
                    order_id=broker_order_id,
                    reason=reason,
                    timestamp=datetime.now(timezone.utc),
                )
            return True

        raise NotImplementedError(
            "LIVE cancel_order NOT IMPLEMENTED — paper trading only."
        )

    async def cancel_all_open_orders(self) -> int:
        """Cancel every open order.  Used by kill switch."""
        if self._config.paper_mode:
            count = len(self._state.open_positions)
            self._state.open_positions.clear()
            logger.critical("PAPER cancel_all: cleared %d positions", count)
            return count

        raise NotImplementedError(
            "LIVE cancel_all NOT IMPLEMENTED — paper trading only."
        )

    async def emergency_flatten(self, reason: str) -> None:
        """Halt trading, cancel all orders, flatten positions."""
        logger.critical("EMERGENCY FLATTEN: %s", reason)
        self._state.is_halted = True
        self._state.halt_reason = f"emergency: {reason}"
        await self.cancel_all_open_orders()

    def record_fill(
        self,
        broker_order_id: str,
        fill_price: float,
        tag: str = "",
    ) -> None:
        """Record a fill and update the open-position ledger."""
        pos = OpenPosition(
            broker_order_id=broker_order_id,
            side="",
            contracts=1,
            entry_price=fill_price,
            tag=tag,
        )
        self._state.open_positions.append(pos)
        self._state.daily_trades += 1

    def record_trade_pnl(self, pnl: float) -> None:
        """
        Record realised P&L and check kill switch.

        Called by the higher-level scale-out executor when a
        leg closes.
        """
        # NaN guard — if PnL is NaN, the kill switch comparison will
        # silently return False, allowing unlimited losses.
        if not math.isfinite(pnl):
            logger.critical("NaN/Inf PnL received — activating kill switch")
            self._state.is_halted = True
            self._state.halt_reason = "KILL SWITCH: NaN/Inf PnL — data integrity failure"
            self._schedule_cancel_all()
            return

        self._state.daily_pnl += pnl

        # ── KILL SWITCH ──
        if self._state.daily_pnl <= -KILL_SWITCH_THRESHOLD_DOLLARS:
            self._state.is_halted = True
            self._state.halt_reason = (
                f"KILL SWITCH: daily P&L ${self._state.daily_pnl:.2f} "
                f"hit -${KILL_SWITCH_THRESHOLD_DOLLARS:.2f} threshold"
            )
            logger.critical(self._state.halt_reason)
            self._schedule_cancel_all()

    def close_position(self, broker_order_id: str) -> None:
        """Remove a position from the open-position ledger."""
        self._state.open_positions = [
            p for p in self._state.open_positions
            if p.broker_order_id != broker_order_id
        ]

    def on_fill(self, callback: Callable) -> None:
        """Register a callback fired on every fill."""
        self._on_fill = callback

    def reset_daily(self) -> None:
        """Reset daily counters (called at session open)."""
        self._state.daily_pnl = 0.0
        self._state.daily_trades = 0
        self._state.daily_blocked = 0
        self._state.is_halted = False
        self._state.halt_reason = ""
        self._state.session_date = (
            datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )

    # ──────────────────────────────────────────────────────────
    # PROPERTIES
    # ──────────────────────────────────────────────────────────

    @property
    def state(self) -> ExecutorState:
        return self._state

    @property
    def is_halted(self) -> bool:
        return self._state.is_halted

    @property
    def open_position_count(self) -> int:
        return len(self._state.open_positions)

    @property
    def daily_pnl(self) -> float:
        return self._state.daily_pnl

    def get_status(self) -> dict:
        """Return executor health snapshot."""
        return {
            "paper_mode": self._config.paper_mode,
            "is_halted": self._state.is_halted,
            "halt_reason": self._state.halt_reason,
            "daily_pnl": self._state.daily_pnl,
            "daily_trades": self._state.daily_trades,
            "daily_blocked": self._state.daily_blocked,
            "open_positions": self.open_position_count,
            "allow_eth": self._config.allow_eth,
        }

    # ──────────────────────────────────────────────────────────
    # SAFETY CHECKS — THE ONLY GATE
    # ──────────────────────────────────────────────────────────

    def _run_safety_checks(self, request: OrderRequest) -> Optional[str]:
        """
        Run every safety rail.  Returns None if clear, or a
        rejection reason string.

        HARD BLOCKS — these cannot be bypassed, overridden, or
        disabled.  Every order passes through this single gate.
        """
        # 0. NaN/Inf guard — NaN comparisons return False, bypassing gates
        if not math.isfinite(self._state.daily_pnl):
            self._state.is_halted = True
            self._state.halt_reason = "KILL SWITCH: daily P&L is NaN/Inf — data integrity failure"
            logger.critical(self._state.halt_reason)
            return f"KILL_SWITCH: daily P&L is NaN/Inf"

        if not isinstance(request.contracts, int) or request.contracts <= 0:
            return f"INVALID_CONTRACTS: {request.contracts!r} (must be positive int)"

        # 1. Kill switch / halt
        if self._state.is_halted:
            return f"HALTED: {self._state.halt_reason}"

        # 2. Max contracts per order
        if request.contracts > MAX_CONTRACTS_PER_ORDER:
            return (
                f"MAX_CONTRACTS_PER_ORDER: requested {request.contracts}, "
                f"limit is {MAX_CONTRACTS_PER_ORDER}"
            )

        # 3. Max open positions
        if len(self._state.open_positions) >= MAX_OPEN_POSITIONS:
            return (
                f"MAX_OPEN_POSITIONS: {len(self._state.open_positions)} "
                f"open, limit is {MAX_OPEN_POSITIONS}"
            )

        # 4. Session check (RTH only unless ETH explicitly allowed)
        if not self._config.allow_eth:
            session = get_session_type(datetime.now(timezone.utc))
            if session == SessionType.ETH:
                return "ETH_BLOCKED: orders outside RTH require allow_eth=True"

        # 5. Kill switch threshold (checked before daily loss limit
        #    because it is the more severe condition and must set
        #    is_halted; belt-and-suspenders with record_trade_pnl)
        if self._state.daily_pnl <= -KILL_SWITCH_THRESHOLD_DOLLARS:
            self._state.is_halted = True
            self._state.halt_reason = (
                f"KILL SWITCH at order time: "
                f"P&L ${self._state.daily_pnl:.2f}"
            )
            logger.critical(self._state.halt_reason)
            return f"KILL_SWITCH: P&L ${self._state.daily_pnl:.2f}"

        # 6. Daily loss limit
        if self._state.daily_pnl <= -DAILY_LOSS_LIMIT_DOLLARS:
            return (
                f"DAILY_LOSS_LIMIT: P&L ${self._state.daily_pnl:.2f} "
                f"exceeds -${DAILY_LOSS_LIMIT_DOLLARS:.2f}"
            )

        return None

    # ──────────────────────────────────────────────────────────
    # EXECUTION BACKENDS
    # ──────────────────────────────────────────────────────────

    async def _execute_paper(
        self, request: OrderRequest, record: OrderRecord
    ) -> OrderRecord:
        """Simulate order placement in paper mode."""
        record.accepted = True
        record.state = OrderState.FILLED
        record.broker_order_id = f"PAPER-{int(time.time() * 1000)}"
        record.fill_price = (
            request.limit_price if request.limit_price > 0
            else self._get_last_price()
        )

        # Analytics: record order sent (non-blocking)
        if self._analytics:
            expected = request.limit_price if request.limit_price > 0 else record.fill_price
            direction = self._infer_direction(request.side.value, request.tag)
            self._analytics.record_order_sent(
                order_id=record.broker_order_id,
                side=request.side.value,
                size=request.contracts,
                expected_price=expected,
                timestamp=record.timestamp,
                order_type=request.order_type.value.lower(),
                direction=direction,
            )

        self._state.daily_trades += 1
        self._state.open_positions.append(
            OpenPosition(
                broker_order_id=record.broker_order_id,
                side=request.side.value,
                contracts=request.contracts,
                entry_price=record.fill_price,
                tag=request.tag,
            )
        )

        logger.info(
            "PAPER FILL: %s %d×MNQ %s @ %.2f [%s] -> %s",
            request.side.value,
            request.contracts,
            request.order_type.value,
            record.fill_price,
            request.tag,
            record.broker_order_id,
        )

        # Analytics: record fill (non-blocking)
        if self._analytics:
            self._analytics.record_fill(
                order_id=record.broker_order_id,
                fill_price=record.fill_price,
                fill_size=request.contracts,
                fill_timestamp=datetime.now(timezone.utc),
            )

        if self._on_fill:
            self._on_fill(record)

        self._log_order(record)
        return record

    async def _execute_live(
        self, request: OrderRequest, record: OrderRecord
    ) -> OrderRecord:
        """
        Live broker execution via IBKR Client Portal Gateway.

        NOT IMPLEMENTED — paper trading must be validated first.
        Phase 3 will implement:
          POST /iserver/account/{accountId}/orders
          with confirmation reply handling.
        """
        raise NotImplementedError(
            "LIVE EXECUTION NOT YET IMPLEMENTED. "
            "This is intentional — paper trading must be validated first. "
            "See Phase 3 of the IBKR integration roadmap."
        )

    # ──────────────────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _check_instrument_validated(instrument: str) -> Optional[str]:
        """
        Check if an instrument has been backtested and validated.

        Returns None if clear, or a rejection reason string.
        Instruments without 200+ trade backtest validation are blocked
        unless ALLOW_UNVALIDATED=true is set (for paper testing only).
        """
        try:
            from config.instruments import InstrumentSpec
            spec = InstrumentSpec.from_symbol(instrument)
        except (ValueError, ImportError):
            return f"UNKNOWN_INSTRUMENT: '{instrument}' not found in InstrumentSpec registry"

        if not spec.validated:
            if os.getenv("ALLOW_UNVALIDATED", "false").lower() == "true":
                logger.warning(
                    "OVERRIDE: %s is unvalidated but ALLOW_UNVALIDATED=true. "
                    "Use for PAPER TESTING ONLY.", instrument
                )
                return None
            logger.error(
                "BLOCKED: %s has not been backtested. "
                "Run 200+ trade validation before deploying. "
                "Set ALLOW_UNVALIDATED=true to override (PAPER ONLY).",
                instrument,
            )
            return (
                f"UNVALIDATED_INSTRUMENT: {instrument} has not been backtested. "
                f"Run 200+ trade validation before deploying. "
                f"Set ALLOW_UNVALIDATED=true to override (PAPER ONLY)."
            )
        return None

    def _schedule_cancel_all(self) -> None:
        """Schedule cancel_all_open_orders on the event loop.

        Called from synchronous methods (record_trade_pnl) that cannot
        await.  This ensures the kill switch immediately cancels orders
        rather than waiting for the next place_order call to notice
        is_halted.
        """
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.cancel_all_open_orders())
        except RuntimeError:
            # No running event loop — the is_halted flag will block
            # the next place_order call.
            pass

    def _get_last_price(self) -> float:
        """Best-effort current price from the IBKR data feed."""
        prices = self._client.get_current_price()
        if prices:
            return prices.get("last", 0.0)
        return 0.0

    @staticmethod
    def _infer_direction(side: str, tag: str) -> str:
        """Infer order direction for analytics from side and tag."""
        tag_lower = tag.lower()
        side_upper = side.upper()
        if "exit" in tag_lower or "close" in tag_lower:
            return "long_exit" if side_upper == "SELL" else "short_exit"
        else:
            return "long_entry" if side_upper == "BUY" else "short_entry"

    def _log_order(self, record: OrderRecord) -> None:
        """Log every order attempt (accepted or rejected) to file and memory."""
        status = "ACCEPTED" if record.accepted else "REJECTED"
        logger.info(
            "ORDER %s: ts=%s side=%s contracts=%d type=%s price=%.2f "
            "tag=%s reason=%s broker_id=%s",
            status,
            record.timestamp.isoformat(),
            record.side,
            record.contracts,
            record.order_type,
            record.price,
            record.tag,
            record.rejection_reason or "—",
            record.broker_order_id or "—",
        )
        self._state.order_log.append(record)
        # Persist to JSONL file
        entry = {
            "timestamp": record.timestamp.isoformat(),
            "action": status,
            "side": record.side,
            "order_type": record.order_type,
            "contracts": record.contracts,
            "price": record.price,
            "tag": record.tag,
            "rejection_reason": record.rejection_reason,
            "broker_order_id": record.broker_order_id,
            "fill_price": record.fill_price,
        }
        try:
            with open(self._order_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except OSError as e:
            logger.warning("Failed to write order log: %s", e)
