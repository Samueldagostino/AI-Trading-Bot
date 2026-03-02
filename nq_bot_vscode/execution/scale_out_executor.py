"""
Scale-Out Execution Engine
============================
Manages the 2-contract scale-out lifecycle.

THE STRATEGY:
  Entry:  2 MNQ contracts at same price
  C1:     Trail-from-profit exit (Variant C)
          Once unrealized profit >= 3.0pts, activate a 2.5pt trailing stop
          from the high-water mark. Fallback: exit at market after 12 bars
          if trailing never activates.
  C2:     Runner -> stop to breakeven+1 after C1 exits, then trail

LIFECYCLE:
  1. SIGNAL  -> Risk approved -> Enter 2 MNQ
  2. PHASE_1 -> Both contracts open, initial stop on both, C1 trailing armed
  3. C1_EXIT -> Trailing stop hit (or 12-bar fallback) -> close C1, C2 stop -> BE+1
  4. RUNNING -> C2 trailing with ATR-based or fixed trail
  5. C2_EXIT -> C2 hits trailing stop, time stop, or max target
  6. DONE    -> Record PnL, update risk engine

Win-win math ($2/point MNQ):
  C1 trails 5pts profit -> $10
  C2 at breakeven+1 = $2
  Total minimum win: $12
  C1 trails 10pts + C2 runs 80pts = $20 + $160 = $180

Worst case (both stopped):
  Stop 20pts = 2 contracts × 20 × $2 = $80 loss
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from enum import Enum
import uuid

logger = logging.getLogger(__name__)


class ScaleOutPhase(Enum):
    """Current phase of the scale-out trade."""
    PENDING = "pending"          # Signal received, not yet entered
    ENTERING = "entering"        # Entry orders submitted
    PHASE_1 = "phase_1"          # Both contracts open, awaiting C1 target
    C1_HIT = "c1_hit"            # C1 closed at target, C2 stop moved to BE
    RUNNING = "running"          # C2 trailing
    CLOSING = "closing"          # Exit in progress
    DONE = "done"                # Trade complete
    ERROR = "error"


@dataclass
class ContractLeg:
    """One leg of the scale-out trade."""
    leg_id: str = ""
    leg_number: int = 0          # 1 or 2
    contracts: int = 1
    
    # Entry
    entry_price: float = 0.0
    entry_time: Optional[datetime] = None
    entry_order_id: Optional[int] = None
    
    # Stops & Targets
    stop_price: float = 0.0
    target_price: float = 0.0    # C1 has fixed target, C2 may not
    stop_order_id: Optional[int] = None
    target_order_id: Optional[int] = None
    
    # Exit
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    exit_reason: str = ""        # "target", "stop", "trailing", "breakeven", "time", "manual"
    
    # PnL
    gross_pnl: float = 0.0
    commission: float = 0.0
    net_pnl: float = 0.0
    
    # State
    is_open: bool = False
    is_filled: bool = False


@dataclass
class ScaleOutTrade:
    """Complete 2-contract scale-out trade."""
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    direction: str = "long"
    symbol: str = "MNQ"
    
    # Legs
    c1: ContractLeg = field(default_factory=lambda: ContractLeg(leg_number=1))
    c2: ContractLeg = field(default_factory=lambda: ContractLeg(leg_number=2))
    
    # Shared
    initial_stop: float = 0.0
    entry_price: float = 0.0
    entry_time: Optional[datetime] = None
    
    # Phase tracking
    phase: ScaleOutPhase = ScaleOutPhase.PENDING
    phase_history: List[dict] = field(default_factory=list)
    
    # Context
    signal_score: float = 0.0
    market_regime: str = "unknown"
    atr_at_entry: float = 0.0
    
    # C1 trail-from-profit tracking (Variant C)
    c1_bars_elapsed: int = 0      # Bars since entry
    c1_best_price: float = 0.0    # C1 high-water mark for trailing
    c1_trailing_active: bool = False  # True once profit >= threshold

    # Trailing stop state (for C2)
    c2_trailing_stop: float = 0.0
    c2_best_price: float = 0.0    # Best favorable price seen since entry
    
    # Aggregate PnL
    total_gross_pnl: float = 0.0
    total_commission: float = 0.0
    total_net_pnl: float = 0.0
    
    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None

    def _set_phase(self, new_phase: ScaleOutPhase) -> None:
        self.phase_history.append({
            "from": self.phase.value,
            "to": new_phase.value,
            "time": datetime.now(timezone.utc).isoformat(),
        })
        self.phase = new_phase


class ScaleOutExecutor:
    """
    Manages the full lifecycle of 2-contract scale-out trades.
    
    Works in two modes:
    - Paper: Simulated fills (default)
    - Live: Routes through TradovateClient
    """

    def __init__(self, config, tradovate_client=None):
        self.config = config
        self.scale_config = config.scale_out
        self.risk_config = config.risk
        self.broker = tradovate_client
        
        self._active_trade: Optional[ScaleOutTrade] = None
        self._trade_history: List[ScaleOutTrade] = []

    @property
    def has_active_trade(self) -> bool:
        return (self._active_trade is not None and 
                self._active_trade.phase not in (ScaleOutPhase.DONE, ScaleOutPhase.ERROR))

    @property
    def active_trade(self) -> Optional[ScaleOutTrade]:
        return self._active_trade

    # ================================================================
    # ENTRY
    # ================================================================
    async def enter_trade(
        self,
        direction: str,
        entry_price: float,
        stop_distance: float,
        atr: float,
        signal_score: float = 0.0,
        regime: str = "unknown",
    ) -> Optional[ScaleOutTrade]:
        """
        Enter a new 2-contract scale-out trade.

        C1 exits via time-based rule (10 bars if profitable).
        C2 trails as runner. Both share initial stop.

        Args:
            direction: "long" or "short"
            entry_price: Current market price
            stop_distance: Distance to stop in NQ points
            atr: Current ATR for trailing stop calculation
            signal_score: Confluence score from signal engine
            regime: Current market regime
        """
        if self.has_active_trade:
            logger.warning("Cannot enter: active trade exists")
            return None

        # Compute stop price
        if direction == "long":
            stop_price = entry_price - stop_distance
        else:
            stop_price = entry_price + stop_distance

        # Create trade object
        trade = ScaleOutTrade(
            direction=direction,
            entry_price=entry_price,
            initial_stop=round(stop_price, 2),
            signal_score=signal_score,
            market_regime=regime,
            atr_at_entry=atr,
        )

        # C1 setup — trail-from-profit exit (Variant C)
        trade.c1.entry_price = entry_price
        trade.c1.stop_price = round(stop_price, 2)
        trade.c1.target_price = 0  # No fixed target — trail from profit
        trade.c1.contracts = self.scale_config.c1_contracts

        # C2 setup — no fixed target, will trail
        trade.c2.entry_price = entry_price
        trade.c2.stop_price = round(stop_price, 2)
        trade.c2.target_price = 0  # No fixed target
        trade.c2.contracts = self.scale_config.c2_contracts

        trade._set_phase(ScaleOutPhase.ENTERING)

        # Execute entry
        if self.config.execution.paper_trading:
            await self._paper_enter(trade, entry_price)
        else:
            await self._live_enter(trade)

        self._active_trade = trade

        c1_thresh = self.scale_config.c1_profit_threshold_pts
        c1_trail = self.scale_config.c1_trail_distance_pts
        logger.info(
            f"SCALE-OUT ENTRY: {direction.upper()} 2x MNQ @ {entry_price:.2f} | "
            f"Stop: {stop_price:.2f} | C1: trail from +{c1_thresh}pts (trail {c1_trail}pts) | "
            f"Score: {signal_score:.2f} | Regime: {regime}"
        )

        return trade

    async def _paper_enter(self, trade: ScaleOutTrade, price: float) -> None:
        """Simulated entry fill."""
        import random
        
        # Simulate slippage
        slippage = random.randint(0, self.config.execution.simulated_slippage_ticks) * 0.25
        if trade.direction == "long":
            fill_price = price + slippage
        else:
            fill_price = price - slippage

        fill_price = round(fill_price, 2)
        now = datetime.now(timezone.utc)

        for leg in [trade.c1, trade.c2]:
            leg.entry_price = fill_price
            leg.entry_time = now
            leg.is_filled = True
            leg.is_open = True
            leg.commission = self.risk_config.commission_per_contract

        trade.entry_price = fill_price
        trade.entry_time = now
        trade.c1_best_price = fill_price
        trade.c2_best_price = fill_price
        trade._set_phase(ScaleOutPhase.PHASE_1)

    async def _live_enter(self, trade: ScaleOutTrade) -> None:
        """Live entry through Tradovate."""
        if not self.broker:
            logger.error("No broker client configured for live trading")
            trade._set_phase(ScaleOutPhase.ERROR)
            return

        result = await self.broker.place_scale_out_entry(
            direction=trade.direction,
            c2_initial_stop=trade.initial_stop,
        )

        if result.get("success"):
            trade._set_phase(ScaleOutPhase.PHASE_1)
            # Store order IDs for modification later
            c1_order = result.get("c1_order", {})
            c2_order = result.get("c2_order", {})
            trade.c1.entry_order_id = c1_order.get("orderId") or c1_order.get("id")
            trade.c2.entry_order_id = c2_order.get("orderId") or c2_order.get("id")
        else:
            logger.error("Live entry failed")
            trade._set_phase(ScaleOutPhase.ERROR)

    # ================================================================
    # TICK-BY-TICK MANAGEMENT
    # ================================================================
    async def update(self, current_price: float, current_time: datetime) -> Optional[dict]:
        """
        Call on every price update (each bar close or tick).
        Manages the full scale-out lifecycle.
        
        Returns dict describing any action taken, or None.
        """
        trade = self._active_trade
        if not trade or trade.phase in (ScaleOutPhase.DONE, ScaleOutPhase.ERROR, ScaleOutPhase.PENDING):
            return None

        action = None

        # ---- PHASE 1: Both contracts open, watching for C1 target or stop ----
        if trade.phase == ScaleOutPhase.PHASE_1:
            action = await self._manage_phase_1(trade, current_price, current_time)

        # ---- C1 EXITED / RUNNING: C2 trailing ----
        elif trade.phase in (ScaleOutPhase.C1_HIT, ScaleOutPhase.RUNNING):
            action = await self._manage_runner(trade, current_price, current_time)

        return action

    async def _manage_phase_1(self, trade: ScaleOutTrade, price: float, time: datetime) -> Optional[dict]:
        """
        Phase 1: Both contracts open (Variant C — trail from profit).

        Logic:
          1. Check initial stop (both contracts)
          2. Track C1 high-water mark (HWM)
          3. Once unrealized profit >= c1_profit_threshold_pts, activate trailing
          4. Trailing stop = HWM - c1_trail_distance_pts (long) / HWM + dist (short)
          5. Fallback: if trailing never activates within c1_max_bars_fallback bars
             and C1 is profitable, exit at market
        """
        direction = trade.direction
        cfg = self.scale_config

        # --- Check STOP (both contracts) ---
        stop_hit = False
        if direction == "long" and price <= trade.initial_stop:
            stop_hit = True
        elif direction == "short" and price >= trade.initial_stop:
            stop_hit = True

        if stop_hit:
            return await self._close_all(trade, price, time, "stop")

        # --- Count bars ---
        trade.c1_bars_elapsed += 1

        # --- Update C1 HWM ---
        if direction == "long":
            trade.c1_best_price = max(trade.c1_best_price, price)
        else:
            if trade.c1_best_price == 0:
                trade.c1_best_price = price
            trade.c1_best_price = min(trade.c1_best_price, price)

        # --- Compute unrealized C1 profit ---
        if direction == "long":
            unrealized = price - trade.entry_price
        else:
            unrealized = trade.entry_price - price

        # --- Activate trailing once profit >= threshold ---
        if unrealized >= cfg.c1_profit_threshold_pts and not trade.c1_trailing_active:
            trade.c1_trailing_active = True

        # --- Check trailing stop ---
        if trade.c1_trailing_active:
            if direction == "long":
                trail_stop = trade.c1_best_price - cfg.c1_trail_distance_pts
                triggered = price <= trail_stop
            else:
                trail_stop = trade.c1_best_price + cfg.c1_trail_distance_pts
                triggered = price >= trail_stop

            if triggered:
                return await self._close_c1_to_runner(
                    trade, round(trail_stop, 2), time, "c1_trail_from_profit"
                )

        # --- Fallback: max bars without trailing activation ---
        if trade.c1_bars_elapsed >= cfg.c1_max_bars_fallback and not trade.c1_trailing_active:
            if unrealized > 0:
                return await self._close_c1_to_runner(
                    trade, round(price, 2), time,
                    f"time_{cfg.c1_max_bars_fallback}bars_fallback"
                )

        # Update best price for C2 tracking
        if direction == "long":
            trade.c2_best_price = max(trade.c2_best_price, price)
        else:
            trade.c2_best_price = min(trade.c2_best_price, price) if trade.c2_best_price > 0 else price

        return None

    async def _close_c1_to_runner(
        self, trade: ScaleOutTrade, exit_price: float, time: datetime, reason: str
    ) -> dict:
        """Close C1 and transition C2 to runner phase with BE stop."""
        direction = trade.direction

        # Close C1
        trade.c1.exit_price = exit_price
        trade.c1.exit_time = time
        trade.c1.exit_reason = reason
        trade.c1.is_open = False
        trade.c1.gross_pnl = self._compute_leg_pnl(trade.c1, trade.direction)
        trade.c1.net_pnl = trade.c1.gross_pnl - trade.c1.commission

        # Move C2 stop to breakeven + buffer
        if self.scale_config.c2_move_stop_to_breakeven:
            buffer = self.scale_config.c2_breakeven_buffer_points
            if direction == "long":
                new_stop = trade.entry_price + buffer
            else:
                new_stop = trade.entry_price - buffer
            trade.c2.stop_price = round(new_stop, 2)

            if not self.config.execution.paper_trading and self.broker:
                if trade.c2.stop_order_id:
                    await self.broker.modify_stop(trade.c2.stop_order_id, new_stop)

        trade._set_phase(ScaleOutPhase.C1_HIT)
        trade.c2_best_price = exit_price

        c1_pts = abs(exit_price - trade.entry_price)
        logger.info(
            f"C1 EXIT ({reason}) @ bar {trade.c1_bars_elapsed} | "
            f"Price: {exit_price:.2f} ({c1_pts:.1f}pts) | "
            f"C1 PnL: ${trade.c1.net_pnl:.2f} | "
            f"C2 stop moved to BE+1: {trade.c2.stop_price:.2f}"
        )

        trade._set_phase(ScaleOutPhase.RUNNING)

        return {
            "action": "c1_time_exit",
            "c1_pnl": trade.c1.net_pnl,
            "c1_bars": trade.c1_bars_elapsed,
            "c2_new_stop": trade.c2.stop_price,
            "price": exit_price,
        }

    # Archived: Original Time-10 C1 exit (kept for A/B testing)
    async def _manage_phase_1_time10(self, trade: ScaleOutTrade, price: float, time: datetime) -> Optional[dict]:
        """
        ARCHIVED: Original Time-10 bars C1 exit.
        Use for A/B testing by swapping: executor._manage_phase_1 = executor._manage_phase_1_time10
        """
        direction = trade.direction
        stop_hit = False
        if direction == "long" and price <= trade.initial_stop:
            stop_hit = True
        elif direction == "short" and price >= trade.initial_stop:
            stop_hit = True
        if stop_hit:
            return await self._close_all(trade, price, time, "stop")

        trade.c1_bars_elapsed += 1
        c1_exit_bars = self.scale_config.c1_time_exit_bars
        if trade.c1_bars_elapsed >= c1_exit_bars:
            c1_in_profit = (price > trade.entry_price) if direction == "long" else (price < trade.entry_price)
            if c1_in_profit:
                return await self._close_c1_to_runner(
                    trade, round(price, 2), time, f"time_{c1_exit_bars}bars"
                )

        if direction == "long":
            trade.c2_best_price = max(trade.c2_best_price, price)
        else:
            trade.c2_best_price = min(trade.c2_best_price, price) if trade.c2_best_price > 0 else price
        return None

    async def _manage_runner(self, trade: ScaleOutTrade, price: float, time: datetime) -> Optional[dict]:
        """
        Manage C2 (runner contract):
        - Update trailing stop
        - Check stop hit
        - Check time stop
        - Check max target
        """
        direction = trade.direction
        cfg = self.scale_config

        # --- Update best price ---
        if direction == "long":
            trade.c2_best_price = max(trade.c2_best_price, price)
        else:
            if trade.c2_best_price == 0:
                trade.c2_best_price = price
            trade.c2_best_price = min(trade.c2_best_price, price)

        # --- Update trailing stop ---
        if cfg.c2_trailing_stop_enabled:
            new_trail = self._compute_trailing_stop(trade, price)
            
            # Trail only moves in favorable direction
            if direction == "long" and new_trail > trade.c2.stop_price:
                trade.c2.stop_price = round(new_trail, 2)
                trade.c2_trailing_stop = trade.c2.stop_price
                
                if not self.config.execution.paper_trading and self.broker and trade.c2.stop_order_id:
                    await self.broker.modify_stop(trade.c2.stop_order_id, trade.c2.stop_price)
                    
            elif direction == "short" and (new_trail < trade.c2.stop_price or trade.c2.stop_price == 0):
                trade.c2.stop_price = round(new_trail, 2)
                trade.c2_trailing_stop = trade.c2.stop_price

                if not self.config.execution.paper_trading and self.broker and trade.c2.stop_order_id:
                    await self.broker.modify_stop(trade.c2.stop_order_id, trade.c2.stop_price)

        # --- Check C2 STOP ---
        stop_hit = False
        if direction == "long" and price <= trade.c2.stop_price:
            stop_hit = True
        elif direction == "short" and price >= trade.c2.stop_price:
            stop_hit = True

        if stop_hit:
            exit_reason = "trailing" if trade.c2_trailing_stop > 0 else "breakeven"
            return await self._close_c2(trade, trade.c2.stop_price, time, exit_reason)

        # --- Check MAX TARGET ---
        points_from_entry = abs(price - trade.entry_price)
        if points_from_entry >= cfg.c2_max_target_points:
            return await self._close_c2(trade, price, time, "max_target")

        # --- Check TIME STOP ---
        if trade.entry_time:
            elapsed_minutes = (time - trade.entry_time).total_seconds() / 60
            if elapsed_minutes >= cfg.c2_time_stop_minutes:
                return await self._close_c2(trade, price, time, "time_stop")

        return None

    def _compute_trailing_stop(self, trade: ScaleOutTrade, price: float) -> float:
        """Compute new trailing stop price for C2."""
        cfg = self.scale_config

        if cfg.c2_trailing_stop_type == "atr":
            distance = trade.atr_at_entry * cfg.c2_trailing_atr_multiplier
        elif cfg.c2_trailing_stop_type == "fixed":
            distance = cfg.c2_trailing_fixed_points
        else:
            distance = cfg.c2_trailing_fixed_points  # Default fallback

        if trade.direction == "long":
            return trade.c2_best_price - distance
        else:
            return trade.c2_best_price + distance

    # ================================================================
    # EXIT LOGIC
    # ================================================================
    async def _close_c2(self, trade: ScaleOutTrade, price: float, time: datetime, reason: str) -> dict:
        """Close C2 (runner) and finalize trade."""
        trade.c2.exit_price = round(price, 2)
        trade.c2.exit_time = time
        trade.c2.exit_reason = reason
        trade.c2.is_open = False
        trade.c2.gross_pnl = self._compute_leg_pnl(trade.c2, trade.direction)
        trade.c2.net_pnl = trade.c2.gross_pnl - trade.c2.commission

        return self._finalize_trade(trade, time)

    async def _close_all(self, trade: ScaleOutTrade, price: float, time: datetime, reason: str) -> dict:
        """Close both contracts (stop hit)."""
        for leg in [trade.c1, trade.c2]:
            if leg.is_open:
                leg.exit_price = round(price, 2)
                leg.exit_time = time
                leg.exit_reason = reason
                leg.is_open = False
                leg.gross_pnl = self._compute_leg_pnl(leg, trade.direction)
                leg.net_pnl = leg.gross_pnl - leg.commission

        return self._finalize_trade(trade, time)

    def _finalize_trade(self, trade: ScaleOutTrade, time: datetime) -> dict:
        """Compute final PnL and archive trade."""
        trade.total_gross_pnl = trade.c1.gross_pnl + trade.c2.gross_pnl
        trade.total_commission = trade.c1.commission + trade.c2.commission
        trade.total_net_pnl = trade.total_gross_pnl - trade.total_commission
        trade.closed_at = time
        trade._set_phase(ScaleOutPhase.DONE)

        self._trade_history.append(trade)
        self._active_trade = None

        c1_pts = abs(trade.c1.exit_price - trade.c1.entry_price) if trade.c1.exit_price else 0
        c2_pts = abs(trade.c2.exit_price - trade.c2.entry_price) if trade.c2.exit_price else 0

        logger.info(
            f"TRADE CLOSED: {trade.direction.upper()} | "
            f"C1: {trade.c1.exit_reason} ({c1_pts:.1f}pts ${trade.c1.net_pnl:.2f}) | "
            f"C2: {trade.c2.exit_reason} ({c2_pts:.1f}pts ${trade.c2.net_pnl:.2f}) | "
            f"TOTAL: ${trade.total_net_pnl:.2f}"
        )

        return {
            "action": "trade_closed",
            "trade_id": trade.trade_id,
            "direction": trade.direction,
            "entry_price": trade.entry_price,
            "c1_exit_price": trade.c1.exit_price,
            "c1_exit_reason": trade.c1.exit_reason,
            "c1_pnl": trade.c1.net_pnl,
            "c2_exit_price": trade.c2.exit_price,
            "c2_exit_reason": trade.c2.exit_reason,
            "c2_pnl": trade.c2.net_pnl,
            "total_pnl": trade.total_net_pnl,
            "regime": trade.market_regime,
            "phase_history": trade.phase_history,
        }

    # ================================================================
    # EMERGENCY
    # ================================================================
    async def emergency_flatten(self, price: float) -> Optional[dict]:
        """Emergency close of active trade at market."""
        if not self.has_active_trade:
            return None
        
        trade = self._active_trade
        now = datetime.now(timezone.utc)

        # Close any open legs
        if trade.c1.is_open:
            trade.c1.exit_price = price
            trade.c1.exit_time = now
            trade.c1.exit_reason = "emergency"
            trade.c1.is_open = False
            trade.c1.gross_pnl = self._compute_leg_pnl(trade.c1, trade.direction)
            trade.c1.net_pnl = trade.c1.gross_pnl - trade.c1.commission

        if trade.c2.is_open:
            trade.c2.exit_price = price
            trade.c2.exit_time = now
            trade.c2.exit_reason = "emergency"
            trade.c2.is_open = False
            trade.c2.gross_pnl = self._compute_leg_pnl(trade.c2, trade.direction)
            trade.c2.net_pnl = trade.c2.gross_pnl - trade.c2.commission

        # If live, flatten at broker level too
        if not self.config.execution.paper_trading and self.broker:
            await self.broker.flatten_position()

        return self._finalize_trade(trade, now)

    # ================================================================
    # HELPERS
    # ================================================================
    def _compute_leg_pnl(self, leg: ContractLeg, direction: str) -> float:
        """Compute gross PnL for one leg in dollars."""
        if not leg.exit_price or not leg.entry_price:
            return 0.0
        
        point_value = self.risk_config.nq_point_value_micro  # Always MNQ
        
        if direction == "long":
            points = leg.exit_price - leg.entry_price
        else:
            points = leg.entry_price - leg.exit_price

        return round(points * point_value * leg.contracts, 2)

    def get_trade_history(self) -> List[dict]:
        """Return trade history as list of dicts."""
        return [
            {
                "trade_id": t.trade_id,
                "direction": t.direction,
                "entry_price": t.entry_price,
                "entry_time": t.entry_time.isoformat() if t.entry_time else None,
                "c1_pnl": t.c1.net_pnl,
                "c1_exit_reason": t.c1.exit_reason,
                "c2_pnl": t.c2.net_pnl,
                "c2_exit_reason": t.c2.exit_reason,
                "total_pnl": t.total_net_pnl,
                "regime": t.market_regime,
                "signal_score": t.signal_score,
                "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            }
            for t in self._trade_history
        ]

    def get_stats(self) -> dict:
        """Aggregate statistics for all completed trades."""
        trades = self._trade_history
        if not trades:
            return {"total_trades": 0}

        pnls = [t.total_net_pnl for t in trades]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p < 0]
        
        # C1 vs C2 breakdown
        c1_pnls = [t.c1.net_pnl for t in trades]
        c2_pnls = [t.c2.net_pnl for t in trades]
        
        # How often C2 ran for more than C1
        c2_outperformed = sum(1 for t in trades if t.c2.net_pnl > t.c1.net_pnl)

        return {
            "total_trades": len(trades),
            "total_pnl": round(sum(pnls), 2),
            "win_rate": round(len(winners) / len(trades) * 100, 1),
            "avg_winner": round(sum(winners) / len(winners), 2) if winners else 0,
            "avg_loser": round(sum(losers) / len(losers), 2) if losers else 0,
            "profit_factor": round(abs(sum(winners) / sum(losers)), 2) if losers and sum(losers) != 0 else float('inf'),
            "largest_win": round(max(pnls), 2),
            "largest_loss": round(min(pnls), 2),
            "c1_total_pnl": round(sum(c1_pnls), 2),
            "c2_total_pnl": round(sum(c2_pnls), 2),
            "c2_outperformed_c1_pct": round(c2_outperformed / len(trades) * 100, 1),
            "avg_c1_points": round(
                sum(abs(t.c1.exit_price - t.c1.entry_price) for t in trades if t.c1.exit_price) / len(trades), 1
            ),
            "avg_c2_points": round(
                sum(abs(t.c2.exit_price - t.c2.entry_price) for t in trades if t.c2.exit_price) / len(trades), 1
            ),
        }
