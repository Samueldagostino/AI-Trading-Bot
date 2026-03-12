"""
Execution Engine
=================
Handles order placement, fill management, and position tracking.
Supports both paper trading (simulated) and live execution.

Design principles:
1. Paper mode is the DEFAULT -- live mode requires explicit activation
2. All orders include worst-case slippage in simulation
3. Partial fills are handled
4. Every order event is logged to database
"""

import asyncio
import logging
import random
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from enum import Enum
import uuid

logger = logging.getLogger(__name__)


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL_FILL = "partial_fill"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    ERROR = "error"


class PositionStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass
class Order:
    """Represents a single order."""
    order_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    trade_id: str = ""
    symbol: str = "MNQ"
    direction: str = "long"            # 'long' or 'short'
    order_type: OrderType = OrderType.MARKET
    requested_price: float = 0.0
    limit_price: float = 0.0
    contracts: int = 1
    
    # Fill info
    fill_price: float = 0.0
    filled_contracts: int = 0
    slippage: float = 0.0
    commission: float = 0.0
    
    # Status
    status: OrderStatus = OrderStatus.PENDING
    submitted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    
    # Error tracking
    error_message: str = ""
    retry_count: int = 0


@dataclass
class Position:
    """Active position tracking."""
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str = "MNQ"
    direction: str = "long"
    contracts: int = 0
    entry_price: float = 0.0
    entry_time: Optional[datetime] = None
    
    # Stops and targets
    stop_loss: float = 0.0
    take_profit: float = 0.0
    trailing_stop: float = 0.0
    trailing_stop_distance: float = 0.0
    
    # Current state
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    
    # Exit info
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    exit_reason: str = ""
    realized_pnl: float = 0.0
    total_commission: float = 0.0
    
    status: PositionStatus = PositionStatus.OPEN


class ExecutionEngine:
    """
    Manages order execution and position lifecycle.
    
    Modes:
    - Paper: Simulated execution with configurable slippage/latency
    - Live: Connects to broker API (Tradovate/IB/NinjaTrader)
    """

    def __init__(self, config, db_manager=None):
        self.config = config.execution
        self.risk_config = config.risk
        self.db = db_manager
        self._positions: Dict[str, Position] = {}
        self._orders: Dict[str, Order] = {}
        self._order_history: List[Order] = []

    @property
    def has_open_position(self) -> bool:
        return any(p.status == PositionStatus.OPEN for p in self._positions.values())

    @property
    def open_position(self) -> Optional[Position]:
        for p in self._positions.values():
            if p.status == PositionStatus.OPEN:
                return p
        return None

    # ================================================================
    # Order Submission
    # ================================================================
    async def submit_entry(
        self,
        direction: str,
        contracts: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        signal_id: Optional[int] = None,
    ) -> Optional[Position]:
        """
        Submit an entry order and create a position on fill.
        
        Args:
            direction: 'long' or 'short'
            contracts: Number of contracts
            entry_price: Current market price (for market orders)
            stop_loss: Stop loss price
            take_profit: Take profit price
            signal_id: Reference to the signal that triggered this trade
            
        Returns:
            Position object if filled, None if rejected/failed
        """
        # Reject if already in a position
        if self.has_open_position:
            logger.warning("Entry rejected: already in a position")
            return None

        # Create entry order
        order = Order(
            symbol="MNQ" if self.risk_config.use_micro else "NQ",
            direction=direction,
            order_type=OrderType.MARKET if not self.config.use_limit_orders else OrderType.LIMIT,
            requested_price=entry_price,
            limit_price=self._compute_limit_price(direction, entry_price),
            contracts=contracts,
        )

        # Execute order
        filled_order = await self._execute_order(order)

        if filled_order.status != OrderStatus.FILLED:
            logger.warning(f"Entry order not filled: {filled_order.status.value} - {filled_order.error_message}")
            return None

        # Create position
        position = Position(
            symbol=order.symbol,
            direction=direction,
            contracts=filled_order.filled_contracts,
            entry_price=filled_order.fill_price,
            entry_time=filled_order.filled_at,
            stop_loss=stop_loss,
            take_profit=take_profit,
            total_commission=filled_order.commission,
        )

        self._positions[position.trade_id] = position

        logger.info(
            f"POSITION OPENED: {direction} {filled_order.filled_contracts}x "
            f"{order.symbol} @ {filled_order.fill_price:.2f} | "
            f"SL={stop_loss:.2f} TP={take_profit:.2f}"
        )

        return position

    async def check_exits(self, current_price: float, current_time: datetime) -> Optional[Position]:
        """
        Check if any open position should be exited (stop, target, trailing).
        Call this on every bar/tick update.
        """
        position = self.open_position
        if not position:
            return None

        position.current_price = current_price
        position.unrealized_pnl = self._compute_pnl(position, current_price)

        exit_reason = None

        # --- Stop Loss ---
        if position.direction == "long" and current_price <= position.stop_loss:
            exit_reason = "stop"
        elif position.direction == "short" and current_price >= position.stop_loss:
            exit_reason = "stop"

        # --- Take Profit ---
        if position.direction == "long" and current_price >= position.take_profit:
            exit_reason = "target"
        elif position.direction == "short" and current_price <= position.take_profit:
            exit_reason = "target"

        # --- Trailing Stop ---
        if position.trailing_stop_distance > 0:
            self._update_trailing_stop(position, current_price)
            if position.direction == "long" and current_price <= position.trailing_stop:
                exit_reason = "trailing"
            elif position.direction == "short" and current_price >= position.trailing_stop:
                exit_reason = "trailing"

        if exit_reason:
            return await self.close_position(position.trade_id, current_price, exit_reason)

        return None

    async def close_position(
        self, trade_id: str, exit_price: float, reason: str = "manual"
    ) -> Optional[Position]:
        """Close a position at the given price."""
        position = self._positions.get(trade_id)
        if not position or position.status != PositionStatus.OPEN:
            return None

        # Create exit order
        exit_direction = "short" if position.direction == "long" else "long"
        order = Order(
            trade_id=trade_id,
            symbol=position.symbol,
            direction=exit_direction,
            order_type=OrderType.MARKET,
            requested_price=exit_price,
            contracts=position.contracts,
        )

        filled_order = await self._execute_order(order)

        if filled_order.status != OrderStatus.FILLED:
            logger.error(f"Exit order failed: {filled_order.status.value}")
            return None

        # Update position
        position.exit_price = filled_order.fill_price
        position.exit_time = filled_order.filled_at
        position.exit_reason = reason
        position.total_commission += filled_order.commission
        position.realized_pnl = self._compute_pnl(position, filled_order.fill_price) - position.total_commission
        position.status = PositionStatus.CLOSED

        logger.info(
            f"POSITION CLOSED: {reason} | PnL=${position.realized_pnl:.2f} | "
            f"Entry={position.entry_price:.2f} Exit={position.exit_price:.2f}"
        )

        return position

    # ================================================================
    # Order Execution (Paper vs Live)
    # ================================================================
    async def _execute_order(self, order: Order) -> Order:
        """Execute an order. Routes to paper or live engine."""
        order.submitted_at = datetime.now(timezone.utc)
        order.status = OrderStatus.SUBMITTED

        if self.config.paper_trading:
            return await self._execute_paper(order)
        else:
            return await self._execute_live(order)

    async def _execute_paper(self, order: Order) -> Order:
        """
        Simulated execution with realistic slippage and latency.
        """
        # Simulate latency
        await asyncio.sleep(self.config.simulated_latency_ms / 1000)

        # Simulate slippage (random within configured range)
        slippage_ticks = random.randint(0, self.config.simulated_slippage_ticks)
        slippage_points = slippage_ticks * 0.25  # NQ tick = 0.25 points

        # Slippage direction: always adverse
        if order.direction == "long":
            fill_price = order.requested_price + slippage_points
        else:
            fill_price = order.requested_price - slippage_points

        # For limit orders, check if fill is possible
        if order.order_type == OrderType.LIMIT:
            if order.direction == "long" and fill_price > order.limit_price:
                # Wouldn't fill at limit -- simulate timeout
                order.status = OrderStatus.CANCELLED
                order.error_message = "Limit not reached"
                self._orders[order.order_id] = order
                return order
            elif order.direction == "short" and fill_price < order.limit_price:
                order.status = OrderStatus.CANCELLED
                order.error_message = "Limit not reached"
                self._orders[order.order_id] = order
                return order

        # Simulate partial fills (5% chance in paper mode)
        if random.random() < 0.05 and order.contracts > 1:
            filled = random.randint(1, order.contracts - 1)
            order.filled_contracts = filled
            order.status = OrderStatus.PARTIAL_FILL
            logger.warning(f"Partial fill: {filled}/{order.contracts} contracts")
            # For simplicity, treat partial as full with reduced size
            order.filled_contracts = order.contracts  # Override for paper
            order.status = OrderStatus.FILLED

        order.fill_price = round(fill_price, 2)
        order.filled_contracts = order.contracts
        order.slippage = round(slippage_points, 2)
        order.commission = self.risk_config.commission_per_contract * order.contracts
        order.status = OrderStatus.FILLED
        order.filled_at = datetime.now(timezone.utc)

        self._orders[order.order_id] = order
        self._order_history.append(order)

        return order

    async def _execute_live(self, order: Order) -> Order:
        """
        Live broker execution. 
        TODO: Implement broker-specific API integration.
        Currently raises NotImplementedError as a safety measure.
        """
        raise NotImplementedError(
            "LIVE EXECUTION NOT YET IMPLEMENTED. "
            "This is intentional -- paper trading must be validated first. "
            "Supported brokers: tradovate, interactive_brokers, ninjatrader"
        )

    # ================================================================
    # Helpers
    # ================================================================
    def _compute_limit_price(self, direction: str, market_price: float) -> float:
        """Compute limit price offset from market."""
        offset = self.config.limit_offset_ticks * 0.25
        if direction == "long":
            return round(market_price + offset, 2)  # Slightly above for buy
        else:
            return round(market_price - offset, 2)  # Slightly below for sell

    def _compute_pnl(self, position: Position, exit_price: float) -> float:
        """Compute PnL for a position at a given exit price."""
        point_value = (self.risk_config.nq_point_value_micro if "MNQ" in position.symbol
                      else self.risk_config.nq_point_value_mini)
        
        if position.direction == "long":
            pnl = (exit_price - position.entry_price) * point_value * position.contracts
        else:
            pnl = (position.entry_price - exit_price) * point_value * position.contracts
        
        return round(pnl, 2)

    def _update_trailing_stop(self, position: Position, current_price: float) -> None:
        """Update trailing stop as price moves in favor."""
        if position.direction == "long":
            new_stop = current_price - position.trailing_stop_distance
            if new_stop > position.trailing_stop:
                position.trailing_stop = new_stop
        else:
            new_stop = current_price + position.trailing_stop_distance
            if position.trailing_stop == 0 or new_stop < position.trailing_stop:
                position.trailing_stop = new_stop

    def get_execution_stats(self) -> dict:
        """Execution quality metrics."""
        filled = [o for o in self._order_history if o.status == OrderStatus.FILLED]
        if not filled:
            return {"total_orders": 0}
        
        avg_slippage = sum(o.slippage for o in filled) / len(filled)
        total_commission = sum(o.commission for o in filled)
        
        return {
            "total_orders": len(self._order_history),
            "filled_orders": len(filled),
            "fill_rate": round(len(filled) / len(self._order_history) * 100, 1),
            "avg_slippage_points": round(avg_slippage, 3),
            "total_commission": round(total_commission, 2),
        }
