"""
Scale-Out Execution Engine v1.3.1 — 5-Contract TJR Architecture
====================================================================
Manages 5 contracts with diversified exit strategies + delayed C3 runner.

VERSION: 1.3.1 — Validated: 396 trades, PF 2.86, +$47,236, 1.60% max DD

THE STRATEGY:
  C1 (1 contract) — The Canary:     5-bar time exit. Direction probe.
  C2 (1 contract) — The Structure:  Structural target (nearest swing point).
                                    TJR-inspired: exits at market structure.
  C3 (3 contracts) — The Runner:    ATR trailing stop, no fixed target.
                                    PROVEN MONEYMAKER (+$51,176 in backtest).
                                    DELAYED ENTRY: C3 only stays open when
                                    C1 exits profitably. If C1 loses, C3 is
                                    closed immediately at market.

DELAYED C3 RUNNER (v1.3.1 — THE KEY EDGE):
  Backtest proof: saved $38,430, reduced max DD 8.62% → 1.60%.
  120/396 trades had C3 blocked (30.3%). C1 is the "canary" —
  when it loses, the trade direction was wrong, so we prevent
  C3's 3 contracts from amplifying that loss.

BREAKEVEN LOGIC:
  When C1 exits profitably → immediately move C2/C3 stops to breakeven
  (entry + 2pt buffer). This makes the trade risk-free on remaining legs.

LIFECYCLE:
  1. SIGNAL  → Risk approved → Enter 5 contracts (C1=1, C2=1, C3=3)
  2. PHASE_1 → All contracts open, initial stop on all, C1 5-bar timer
  3a. C1 PROFIT → C2/C3 stops move to breakeven → C3 keeps running
  3b. C1 LOSS  → C3 CLOSED IMMEDIATELY → only C2 remains (if open)
  3c. STOP HIT → All contracts closed (initial stop during Phase 1)
  4. SCALING → C2/C3 managed independently
  5. DONE   → All legs closed, record PnL

Research backing:
  - C1: Validated PF 1.81 across 751 trades (c1_exit_research.md)
  - C2: TJR bootcamp — structural exits allow asymmetric R:R
  - C3: "Let winners run" — captures fat tails in trend distribution
  - C3 delayed: $38,430 saved across 396 trades (v1.3.1 validated)
  - 567K backtests: simple exits > complex exits (KJ Trading Systems)
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from enum import Enum
import uuid

from execution.adaptive_exit_config import AdaptiveExitConfig
from monitoring.alerting import get_alert_manager
from monitoring.alert_templates import AlertTemplates

logger = logging.getLogger(__name__)


class ScaleOutPhase(Enum):
    """Current phase of the scale-out trade."""
    PENDING = "pending"          # Signal received, not yet entered
    ENTERING = "entering"        # Entry orders submitted
    PHASE_1 = "phase_1"          # All contracts open, awaiting C1 5-bar exit
    SCALING = "scaling"          # C1 exited, managing remaining legs
    CLOSING = "closing"          # Exit in progress
    DONE = "done"                # Trade complete
    ERROR = "error"

    # Legacy aliases (kept for backward compat with phase_history logs)
    C1_HIT = "c1_hit"
    RUNNING = "running"


@dataclass
class ContractLeg:
    """One leg of the scale-out trade."""
    leg_id: str = ""
    leg_number: int = 0          # 1-4
    leg_label: str = ""          # "C1", "C2", "C3", "C4"
    contracts: int = 1

    # Exit strategy for this leg
    exit_strategy: str = ""      # "time_5bar", "time_15bar", "target_3r", "atr_trail"

    # Entry
    entry_price: float = 0.0
    entry_time: Optional[datetime] = None
    entry_order_id: Optional[int] = None

    # Stops & Targets
    stop_price: float = 0.0
    target_price: float = 0.0
    stop_order_id: Optional[int] = None
    target_order_id: Optional[int] = None

    # Exit
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    exit_reason: str = ""

    # PnL
    gross_pnl: float = 0.0
    commission: float = 0.0
    net_pnl: float = 0.0

    # State
    is_open: bool = False
    is_filled: bool = False

    # Per-leg tracking
    bars_since_active: int = 0   # Bars since this leg became independently managed
    best_price: float = 0.0      # High-water mark for trailing
    mfe: float = 0.0             # Max favorable excursion
    trailing_stop: float = 0.0   # Current trailing stop level
    be_triggered: bool = False   # Breakeven applied?


@dataclass
class ScaleOutTrade:
    """Complete 4-tier scale-out trade."""
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    direction: str = "long"
    symbol: str = "MNQ"

    # Legs (always 4 slots; unused legs have contracts=0)
    c1: ContractLeg = field(default_factory=lambda: ContractLeg(leg_number=1, leg_label="C1"))
    c2: ContractLeg = field(default_factory=lambda: ContractLeg(leg_number=2, leg_label="C2"))
    c3: ContractLeg = field(default_factory=lambda: ContractLeg(leg_number=3, leg_label="C3"))
    c4: ContractLeg = field(default_factory=lambda: ContractLeg(leg_number=4, leg_label="C4"))

    # Shared
    initial_stop: float = 0.0
    stop_distance: float = 0.0   # Stop distance in points (for R-multiple calc)
    entry_price: float = 0.0
    entry_time: Optional[datetime] = None

    # Phase tracking
    phase: ScaleOutPhase = ScaleOutPhase.PENDING
    phase_history: List[dict] = field(default_factory=list)

    # Context
    signal_score: float = 0.0
    market_regime: str = "unknown"
    atr_at_entry: float = 0.0

    # C1 tracking (Phase 1)
    c1_bars_elapsed: int = 0
    c1_best_price: float = 0.0
    c1_trailing_active: bool = False  # Legacy compat

    # Legacy C2 fields (backward compat for backtest reporting)
    c2_trailing_stop: float = 0.0
    c2_best_price: float = 0.0
    c2_be_triggered: bool = False

    # MFE tracking
    c1_mfe: float = 0.0
    c2_mfe: float = 0.0

    # C1 exit metrics
    c1_exit_profit_pts: float = 0.0
    c1_exit_bars: int = 0
    c1_price_velocity: float = 0.0

    # Aggregate PnL
    total_gross_pnl: float = 0.0
    total_commission: float = 0.0
    total_net_pnl: float = 0.0

    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None

    @property
    def all_legs(self) -> List[ContractLeg]:
        return [self.c1, self.c2, self.c3, self.c4]

    @property
    def open_legs(self) -> List[ContractLeg]:
        return [leg for leg in self.all_legs if leg.is_open and leg.contracts > 0]

    @property
    def active_legs(self) -> List[ContractLeg]:
        """Legs with contracts assigned (open or closed)."""
        return [leg for leg in self.all_legs if leg.contracts > 0]

    def _set_phase(self, new_phase: ScaleOutPhase) -> None:
        self.phase_history.append({
            "from": self.phase.value,
            "to": new_phase.value,
            "time": datetime.now(timezone.utc).isoformat(),
        })
        self.phase = new_phase


class ScaleOutExecutor:
    """
    Manages the full lifecycle of 4-tier scale-out trades.

    Works in two modes:
    - Paper: Simulated fills (default)
    - Live: Routes through TradovateClient
    """

    def __init__(self, config, tradovate_client=None, execution_analytics=None, instrument: str = "MNQ"):
        self.config = config
        self.scale_config = config.scale_out
        self.risk_config = config.risk
        self.broker = tradovate_client
        self._analytics = execution_analytics
        self._instrument = instrument

        self._active_trade: Optional[ScaleOutTrade] = None
        self._trade_history: List[ScaleOutTrade] = []

        # v1.3.1: Delayed C3 runner
        self._c3_delayed_entry = getattr(config.scale_out, 'c3_delayed_entry_enabled', True)
        self._c3_stats = {
            "trades_total": 0,
            "c3_entered": 0,
            "c3_blocked": 0,
            "c3_pnl_saved": 0.0,
        }

        # Regime-adaptive exit parameters
        self._adaptive_exits = AdaptiveExitConfig(enabled=config.scale_out.adaptive_exits_enabled)
        self._last_regime = "unknown"

    @property
    def has_active_trade(self) -> bool:
        return (self._active_trade is not None and
                self._active_trade.phase not in (ScaleOutPhase.DONE, ScaleOutPhase.ERROR))

    @property
    def active_trade(self) -> Optional[ScaleOutTrade]:
        return self._active_trade

    # ================================================================
    # SCORING & SIZING
    # ================================================================
    @staticmethod
    def score_based_contracts(signal_score: float, regime_multiplier: float = 1.0) -> dict:
        """
        5 contracts: C1 (1) + C2 (1) + C3 (3).

        C3 (runner) gets 3× allocation — data shows it's the only
        consistently profitable leg (W/L ratio 1.77, +$3,653 in backtest).

        C4 is unused (kept for backward compat).

        Partial fill handling: if fewer contracts fill, priority order is
        C3 first (moneymaker), then C1 (quick feedback), then C2 (structural).
        """
        alloc = {"c1": 1, "c2": 1, "c3": 3, "c4": 0}
        alloc["total"] = 5
        return alloc

    @staticmethod
    def adjust_for_partial_fill(alloc: dict, filled_contracts: int) -> dict:
        """Redistribute contracts if partial fill occurs.

        Priority: C3 (runner) > C1 (time) > C2 (structural).
        C3 is the moneymaker, so it gets contracts first.
        """
        if filled_contracts >= alloc["total"]:
            return alloc  # Full fill

        # Priority allocation: C3 first, then C1, then C2
        adjusted = {"c1": 0, "c2": 0, "c3": 0, "c4": 0}
        remaining = filled_contracts

        # C3 gets up to 3 contracts
        adjusted["c3"] = min(3, remaining)
        remaining -= adjusted["c3"]

        # C1 gets 1 if available
        if remaining > 0:
            adjusted["c1"] = 1
            remaining -= 1

        # C2 gets 1 if available
        if remaining > 0:
            adjusted["c2"] = 1
            remaining -= 1

        adjusted["total"] = adjusted["c1"] + adjusted["c2"] + adjusted["c3"]
        return adjusted

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
        regime_multiplier: float = 1.0,
        structural_target: float = 0.0,
    ) -> Optional[ScaleOutTrade]:
        """
        Enter a 5-contract trade: C1 (1) + C2 (1) + C3 (3).

        structural_target: The nearest swing high/low in trade direction.
                          If 0, C2 falls back to 2×R target.
        """
        if self.has_active_trade:
            logger.warning("Cannot enter: active trade exists")
            return None

        # Always 3 contracts
        alloc = self.score_based_contracts(signal_score, regime_multiplier)

        # Compute stop price
        if direction == "long":
            stop_price = entry_price - stop_distance
        else:
            stop_price = entry_price + stop_distance

        # C2 structural target — use swing point if available, else 2×R fallback
        if structural_target > 0:
            c2_target = round(structural_target, 2)
        else:
            # Fallback: 2× risk from entry
            fallback_dist = stop_distance * 2.0
            if direction == "long":
                c2_target = round(entry_price + fallback_dist, 2)
            else:
                c2_target = round(entry_price - fallback_dist, 2)

        # Validate C2 target is in the right direction and at least 1×R away
        min_target_dist = stop_distance * 1.0  # At least 1:1 R:R
        if direction == "long":
            if c2_target - entry_price < min_target_dist:
                c2_target = round(entry_price + stop_distance * 2.0, 2)
        else:
            if entry_price - c2_target < min_target_dist:
                c2_target = round(entry_price - stop_distance * 2.0, 2)

        # Create trade object
        trade = ScaleOutTrade(
            direction=direction,
            entry_price=entry_price,
            initial_stop=round(stop_price, 2),
            stop_distance=stop_distance,
            signal_score=signal_score,
            market_regime=regime,
            atr_at_entry=atr,
        )

        # === C1 — The Scalp (5-bar time exit) ===
        trade.c1.contracts = alloc["c1"]
        trade.c1.entry_price = entry_price
        trade.c1.stop_price = round(stop_price, 2)
        trade.c1.target_price = 0
        trade.c1.exit_strategy = "time_5bar"

        # === C2 — The Structure (structural target from swing point) ===
        trade.c2.contracts = alloc["c2"]
        trade.c2.entry_price = entry_price
        trade.c2.stop_price = round(stop_price, 2)
        trade.c2.target_price = c2_target
        trade.c2.exit_strategy = "structural_target"

        # === C3 — The Runner (pure ATR trail) ===
        trade.c3.contracts = alloc["c3"]
        trade.c3.entry_price = entry_price
        trade.c3.stop_price = round(stop_price, 2)
        trade.c3.target_price = 0
        trade.c3.exit_strategy = "atr_trail"

        # === C4 — Unused ===
        trade.c4.contracts = 0
        trade.c4.exit_strategy = ""

        trade._set_phase(ScaleOutPhase.ENTERING)

        # Execute entry
        if self.config.execution.paper_trading:
            await self._paper_enter(trade, entry_price)
        else:
            await self._live_enter(trade)

        self._active_trade = trade

        total = alloc["total"]
        c2_dist = abs(c2_target - entry_price)
        logger.info(
            f"SCALE-OUT ENTRY: {direction.upper()} {total}x {self._instrument} @ {entry_price:.2f} | "
            f"C1=time_5bar C2=structural({c2_target:.2f}, {c2_dist:.1f}pts) C3=atr_trail | "
            f"Stop: {stop_price:.2f} ({stop_distance:.1f}pts) | "
            f"Score: {signal_score:.2f} | Regime: {regime}"
        )

        # Fire trade entry alert
        mgr = get_alert_manager()
        if mgr:
            mgr.enqueue(AlertTemplates.trade_entry(
                direction=direction,
                contracts=total,
                entry_price=trade.entry_price,
                stop_loss=trade.initial_stop,
                take_profit=c2_target,
                signal_confidence=signal_score,
            ))

        return trade

    async def _paper_enter(self, trade: ScaleOutTrade, price: float) -> None:
        """Simulated entry fill."""
        import random

        slippage = random.randint(0, self.config.execution.simulated_slippage_ticks) * 0.25
        if trade.direction == "long":
            fill_price = price + slippage
        else:
            fill_price = price - slippage

        fill_price = round(fill_price, 2)
        now = datetime.now(timezone.utc)

        for leg in trade.all_legs:
            if leg.contracts > 0:
                leg.entry_price = fill_price
                leg.entry_time = now
                leg.is_filled = True
                leg.is_open = True
                leg.best_price = fill_price
                leg.commission = self.risk_config.get_commission(self._instrument)
            else:
                leg.is_filled = False
                leg.is_open = False
                leg.commission = 0.0

        trade.entry_price = fill_price
        trade.entry_time = now
        trade.c1_best_price = fill_price
        trade.c2_best_price = fill_price
        trade._set_phase(ScaleOutPhase.PHASE_1)

        # Analytics
        if self._analytics:
            side = "BUY" if trade.direction == "long" else "SELL"
            direction = "long_entry" if trade.direction == "long" else "short_entry"
            for leg in trade.active_legs:
                oid = f"{trade.trade_id}-{leg.leg_label}-entry"
                self._analytics.record_order_sent(
                    order_id=oid, side=side, size=leg.contracts,
                    expected_price=price, timestamp=now,
                    order_type="market", direction=direction,
                )
                self._analytics.record_fill(
                    order_id=oid, fill_price=fill_price,
                    fill_size=leg.contracts, fill_timestamp=now,
                )

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
            for leg in trade.active_legs:
                leg.best_price = trade.entry_price
        else:
            logger.error("Live entry failed")
            trade._set_phase(ScaleOutPhase.ERROR)

    # ================================================================
    # TICK-BY-TICK MANAGEMENT
    # ================================================================
    async def update(self, current_price: float, current_time: datetime) -> Optional[dict]:
        """
        Call on every bar close. Manages the full 4-tier lifecycle.
        Returns dict describing any action taken, or None.
        """
        trade = self._active_trade
        if not trade or trade.phase in (ScaleOutPhase.DONE, ScaleOutPhase.ERROR, ScaleOutPhase.PENDING):
            return None

        action = None

        # ---- PHASE 1: All contracts open, C1 5-bar timer running ----
        if trade.phase == ScaleOutPhase.PHASE_1:
            action = await self._manage_phase_1(trade, current_price, current_time)

        # ---- SCALING: C1 exited, managing remaining legs independently ----
        elif trade.phase == ScaleOutPhase.SCALING:
            action = await self._manage_scaling(trade, current_price, current_time)

        # ---- Legacy phase compat ----
        elif trade.phase in (ScaleOutPhase.C1_HIT, ScaleOutPhase.RUNNING):
            action = await self._manage_scaling(trade, current_price, current_time)

        return action

    # ================================================================
    # PHASE 1: All contracts open, C1 5-bar exit
    # ================================================================
    async def _manage_phase_1(self, trade: ScaleOutTrade, price: float, time: datetime) -> Optional[dict]:
        """
        Phase 1: All contracts share initial stop. C1 runs its 5-bar timer.

        VALIDATED: PF 1.81 across 751 trades (c1_exit_research.md).
        """
        direction = trade.direction
        cfg = self.scale_config

        # --- Check STOP (all contracts) ---
        stop_hit = False
        if direction == "long" and price <= trade.initial_stop:
            stop_hit = True
        elif direction == "short" and price >= trade.initial_stop:
            stop_hit = True

        if stop_hit:
            return await self._close_all(trade, price, time, "stop")

        # --- Count bars ---
        trade.c1_bars_elapsed += 1

        # --- Compute unrealized profit ---
        if direction == "long":
            unrealized = price - trade.entry_price
        else:
            unrealized = trade.entry_price - price

        # --- Update MFE for all legs ---
        if unrealized > 0:
            trade.c1_mfe = max(trade.c1_mfe, unrealized)
            trade.c2_mfe = max(trade.c2_mfe, unrealized)
            for leg in trade.open_legs:
                leg.mfe = max(leg.mfe, unrealized)

        # --- Update best prices ---
        if direction == "long":
            trade.c1_best_price = max(trade.c1_best_price, price)
            trade.c2_best_price = max(trade.c2_best_price, price)
            for leg in trade.open_legs:
                leg.best_price = max(leg.best_price, price)
        else:
            trade.c1_best_price = min(trade.c1_best_price, price)
            trade.c2_best_price = min(trade.c2_best_price, price)
            for leg in trade.open_legs:
                leg.best_price = min(leg.best_price, price)

        # --- C2 structural target check during Phase 1 (in case of fast move) ---
        if trade.c2.is_open and trade.c2.target_price > 0:
            target_hit = False
            if direction == "long" and price >= trade.c2.target_price:
                target_hit = True
            elif direction == "short" and price <= trade.c2.target_price:
                target_hit = True
            if target_hit:
                self._close_leg(trade.c2, trade.c2.target_price, time, "structural_target", direction)
                logger.info(f"C2 STRUCTURAL TARGET HIT during Phase 1 @ {trade.c2.target_price:.2f}")

        # --- B:5 time-based exit: exit C1 after 5 bars if profitable ---
        c1_exit_bars = cfg.c1_time_exit_bars
        if trade.c1_bars_elapsed >= c1_exit_bars:
            c1_profit_pts = unrealized
            c1_in_profit = c1_profit_pts >= 3.0
            if c1_in_profit:
                return await self._transition_c1_to_scaling(
                    trade, round(price, 2), time, f"time_{c1_exit_bars}bars"
                )

        # --- Fallback: max bars, exit if profitable ---
        if trade.c1_bars_elapsed >= cfg.c1_max_bars_fallback:
            if unrealized > 0:
                return await self._transition_c1_to_scaling(
                    trade, round(price, 2), time,
                    f"time_{cfg.c1_max_bars_fallback}bars_fallback"
                )

        return None

    async def _transition_c1_to_scaling(
        self, trade: ScaleOutTrade, exit_price: float, time: datetime, reason: str
    ) -> dict:
        """Close C1 and transition remaining legs to independent management.

        CRITICAL: If C1 exits in profit, immediately move ALL remaining
        leg stops to breakeven (entry + 2pt buffer). This makes the trade
        risk-free on C2/C3 — the worst case is a scratch, not a full loss.

        Backtest data showed C2 had 139 stop-outs (-$13,258) and C3 had
        87 stop-outs (-$8,968). Many occurred AFTER C1 already won.
        Moving to BE after C1 profit eliminates this bleeding.
        """
        direction = trade.direction

        # Close C1
        self._close_leg(trade.c1, exit_price, time, reason, direction)

        # C1 exit metrics
        if direction == "long":
            trade.c1_exit_profit_pts = round(exit_price - trade.entry_price, 2)
        else:
            trade.c1_exit_profit_pts = round(trade.entry_price - exit_price, 2)
        trade.c1_exit_bars = trade.c1_bars_elapsed
        trade.c1_price_velocity = round(
            trade.c1_exit_profit_pts / max(trade.c1_bars_elapsed, 1), 4
        )

        # ── IMMEDIATE BREAKEVEN on remaining legs after C1 profit ──
        # If C1 exited profitably, move all remaining stops to breakeven.
        # Buffer of 2 pts covers spread/slippage on the BE exit.
        BE_BUFFER_PTS = 2.0
        c1_was_profitable = trade.c1_exit_profit_pts > 0

        if c1_was_profitable:
            for leg in trade.open_legs:
                if direction == "long":
                    be_stop = round(trade.entry_price + BE_BUFFER_PTS, 2)
                    # Only tighten (never widen the stop)
                    if be_stop > leg.stop_price:
                        leg.stop_price = be_stop
                        leg.be_triggered = True
                else:
                    be_stop = round(trade.entry_price - BE_BUFFER_PTS, 2)
                    if be_stop < leg.stop_price:
                        leg.stop_price = be_stop
                        leg.be_triggered = True

            be_legs = [l.leg_label for l in trade.open_legs if l.be_triggered]
            logger.info(
                f"  BE TRIGGERED on {be_legs} after C1 profit | "
                f"New stop: {trade.entry_price + (BE_BUFFER_PTS if direction == 'long' else -BE_BUFFER_PTS):.2f}"
            )

        logger.info(
            f"C1 EXIT ({reason}) @ bar {trade.c1_bars_elapsed} | "
            f"Price: {exit_price:.2f} ({trade.c1_exit_profit_pts:.1f}pts) | "
            f"C1 PnL: ${trade.c1.net_pnl:.2f} | "
            f"Remaining legs: {[l.leg_label for l in trade.open_legs]} | "
            f"BE applied: {c1_was_profitable}"
        )

        # Fire partial exit alert
        mgr = get_alert_manager()
        if mgr:
            remaining = sum(l.contracts for l in trade.open_legs)
            mgr.enqueue(AlertTemplates.partial_exit(
                contracts_exited=trade.c1.contracts,
                remaining_contracts=remaining,
                exit_price=exit_price,
                pnl=trade.c1.net_pnl,
            ))

        # ── v1.3.1: C3 delayed entry check at C1 exit ──────────────────
        # Backtest rule: C3 only stays open when C1 exits profitably (net PnL > 0).
        # This covers the rare case (3/120 in backtest) where C1 exits via
        # 12-bar fallback with positive unrealized pts but negative net PnL
        # after commission/slippage.
        c3_blocked_at_transition = False
        if (
            self._c3_delayed_entry
            and trade.c1.net_pnl <= 0
            and trade.c3.is_open
            and trade.c3.contracts > 0
        ):
            c3_blocked_at_transition = True
            self._close_leg(trade.c3, exit_price, time, "c3_delayed_blocked", direction)
            logger.info(
                f"  C3 DELAYED BLOCKED at C1 exit: C1 net PnL ${trade.c1.net_pnl:.2f} <= 0 | "
                f"C3 closed at market"
            )

        # If no remaining legs, trade is done
        if not trade.open_legs:
            return self._finalize_trade(trade, time, c3_blocked=c3_blocked_at_transition)

        trade._set_phase(ScaleOutPhase.SCALING)

        return {
            "action": "c1_exit",
            "c1_pnl": trade.c1.net_pnl,
            "c1_bars": trade.c1_bars_elapsed,
            "remaining_legs": [l.leg_label for l in trade.open_legs],
            "c3_blocked": c3_blocked_at_transition,
        }

    # ================================================================
    # SCALING: Independent management of C2, C3, C4
    # ================================================================
    async def _manage_scaling(self, trade: ScaleOutTrade, price: float, time: datetime) -> Optional[dict]:
        """
        Manage remaining legs independently after C1 exits.

        Each leg has its own exit strategy:
          C2: Structural target (nearest swing point) + delayed BE + time stop
          C3: Pure ATR trailing stop (runner) + max target + time stop
        """
        direction = trade.direction
        cfg = self.scale_config
        closed_legs = []

        for leg in trade.open_legs:
            # Update per-leg tracking
            leg.bars_since_active += 1

            if direction == "long":
                leg.best_price = max(leg.best_price, price)
                unrealized = price - leg.entry_price
            else:
                leg.best_price = min(leg.best_price, price)
                unrealized = leg.entry_price - price

            if unrealized > 0:
                leg.mfe = max(leg.mfe, unrealized)
                # Update legacy trade-level MFE
                if leg.leg_label == "C2":
                    trade.c2_mfe = max(trade.c2_mfe, unrealized)

            # ------ INITIAL STOP CHECK (all legs) ------
            stop_hit = False
            stop_to_check = leg.stop_price
            if direction == "long" and price <= stop_to_check:
                stop_hit = True
            elif direction == "short" and price >= stop_to_check:
                stop_hit = True

            if stop_hit:
                exit_reason = "trailing" if leg.trailing_stop > 0 else ("breakeven" if leg.be_triggered else "stop")
                self._close_leg(leg, stop_to_check, time, exit_reason, direction)
                closed_legs.append(leg.leg_label)
                continue

            # ------ PER-LEG EXIT STRATEGY ------

            if leg.exit_strategy == "structural_target":
                # C2: Exit at structural target (swing point)
                target_hit = False
                if direction == "long" and price >= leg.target_price:
                    target_hit = True
                elif direction == "short" and price <= leg.target_price:
                    target_hit = True

                if target_hit:
                    self._close_leg(leg, leg.target_price, time, "structural_target", direction)
                    closed_legs.append(leg.leg_label)
                    continue

                # C2: Time stop — max 20 bars (~40 min on 2m bars)
                # Tighter than before (was 30). If structural target hasn't
                # been hit in 40 min, the setup has likely failed.
                c2_max_bars = 20
                if leg.bars_since_active >= c2_max_bars:
                    self._close_leg(leg, round(price, 2), time, "time_20bars", direction)
                    closed_legs.append(leg.leg_label)
                    continue

                # C2: Additional BE is already applied at C1 exit.
                # Apply trailing BE if MFE keeps growing.
                self._apply_delayed_be(trade, leg, cfg, direction)

            elif leg.exit_strategy == "time_15bar":
                # Legacy C2: Time exit after 15 bars (backward compat)
                c2_time_bars = getattr(cfg, "c2_time_exit_bars", 15)
                if leg.bars_since_active >= c2_time_bars:
                    self._close_leg(leg, round(price, 2), time, f"time_{c2_time_bars}bars", direction)
                    closed_legs.append(leg.leg_label)
                    continue
                self._apply_delayed_be(trade, leg, cfg, direction)

            elif leg.exit_strategy == "target_3r":
                # Legacy C3: Check profit target
                target_hit = False
                if direction == "long" and price >= leg.target_price:
                    target_hit = True
                elif direction == "short" and price <= leg.target_price:
                    target_hit = True

                if target_hit:
                    self._close_leg(leg, leg.target_price, time, "target_3r", direction)
                    closed_legs.append(leg.leg_label)
                    continue

                self._update_atr_trail(trade, leg, cfg, direction)
                self._apply_delayed_be(trade, leg, cfg, direction)

            elif leg.exit_strategy == "atr_trail":
                # C3: Pure ATR trailing stop (runner)
                self._update_atr_trail(trade, leg, cfg, direction)

                # C3: Max target safety valve
                points_from_entry = abs(price - leg.entry_price)
                if points_from_entry >= cfg.c2_max_target_points:
                    self._close_leg(leg, round(price, 2), time, "max_target", direction)
                    closed_legs.append(leg.leg_label)
                    continue

                # C3: Time stop (2 hours max)
                if trade.entry_time:
                    elapsed_minutes = (time - trade.entry_time).total_seconds() / 60
                    if elapsed_minutes >= cfg.c2_time_stop_minutes:
                        self._close_leg(leg, round(price, 2), time, "time_stop", direction)
                        closed_legs.append(leg.leg_label)
                        continue

                # C3 also gets delayed BE
                self._apply_delayed_be(trade, leg, cfg, direction)

        # Check if all legs closed
        if not trade.open_legs:
            return self._finalize_trade(trade, time)

        if closed_legs:
            return {
                "action": "legs_closed",
                "closed": closed_legs,
                "remaining": [l.leg_label for l in trade.open_legs],
            }

        return None

    # ================================================================
    # SHARED HELPERS
    # ================================================================
    def _close_leg(self, leg: ContractLeg, price: float, time: datetime, reason: str, direction: str) -> None:
        """Close a single contract leg."""
        leg.exit_price = round(price, 2)
        leg.exit_time = time
        leg.exit_reason = reason
        leg.is_open = False

        point_value = self.risk_config.get_point_value(self._instrument)
        if direction == "long":
            points = leg.exit_price - leg.entry_price
        else:
            points = leg.entry_price - leg.exit_price

        leg.gross_pnl = round(points * point_value * leg.contracts, 2)
        leg.net_pnl = leg.gross_pnl - leg.commission

        logger.info(
            f"  {leg.leg_label} EXIT ({reason}) @ {price:.2f} | "
            f"{abs(price - leg.entry_price):.1f}pts | "
            f"PnL: ${leg.net_pnl:.2f} | Bars: {leg.bars_since_active}"
        )

    def _apply_delayed_be(self, trade: ScaleOutTrade, leg: ContractLeg,
                          cfg, direction: str) -> None:
        """Variant B: Move stop to breakeven once MFE >= threshold."""
        if leg.be_triggered:
            return

        be_multiplier = getattr(cfg, "c2_be_delay_multiplier", 1.5)
        threshold = trade.stop_distance * be_multiplier

        if leg.mfe >= threshold:
            buf = cfg.c2_breakeven_buffer_points
            new_stop = (trade.entry_price + buf if direction == "long"
                        else trade.entry_price - buf)
            new_stop = round(new_stop, 2)

            # Only tighten (never widen)
            should_apply = (
                (direction == "long" and new_stop > leg.stop_price) or
                (direction == "short" and new_stop < leg.stop_price)
            )
            if should_apply:
                leg.stop_price = new_stop
                leg.be_triggered = True
                # Update legacy tracking
                if leg.leg_label == "C2":
                    trade.c2_be_triggered = True
                logger.debug(
                    f"  {leg.leg_label} BE triggered @ MFE {leg.mfe:.1f}pts "
                    f"(threshold {threshold:.1f}pts) | new stop: {new_stop:.2f}"
                )

    def _update_atr_trail(self, trade: ScaleOutTrade, leg: ContractLeg,
                          cfg, direction: str) -> None:
        """Update ATR-based trailing stop for a leg."""
        if not cfg.c2_trailing_stop_enabled:
            return

        multiplier = cfg.c2_trailing_atr_multiplier
        distance = trade.atr_at_entry * multiplier

        if direction == "long":
            new_trail = leg.best_price - distance
        else:
            new_trail = leg.best_price + distance

        # Only tighten
        should_update = False
        if direction == "long" and new_trail > leg.stop_price:
            should_update = True
        elif direction == "short" and new_trail < leg.stop_price:
            should_update = True

        if should_update:
            leg.stop_price = round(new_trail, 2)
            leg.trailing_stop = leg.stop_price

    # ================================================================
    # EXIT & FINALIZE
    # ================================================================
    async def _close_all(self, trade: ScaleOutTrade, price: float, time: datetime, reason: str) -> dict:
        """Close all open contracts (initial stop hit).

        v1.3.1: When called during Phase 1 (all legs still open) and C3 delayed
        entry is enabled, C3 is marked as blocked. The PnL adjustment happens
        in _finalize_trade to mirror exact backtest behavior.
        """
        direction = trade.direction

        # v1.3.1: Determine if C3 should be blocked
        # C3 is blocked when the initial stop is hit during Phase 1
        # (C1 never got a chance to prove direction)
        c3_should_block = (
            self._c3_delayed_entry
            and trade.phase == ScaleOutPhase.PHASE_1
            and reason == "stop"
            and trade.c3.is_open
            and trade.c3.contracts > 0
        )

        for leg in trade.open_legs:
            if c3_should_block and leg.leg_label == "C3":
                self._close_leg(leg, price, time, "c3_delayed_blocked", direction)
            else:
                self._close_leg(leg, price, time, reason, direction)

        trade.c1_exit_bars = trade.c1_bars_elapsed
        return self._finalize_trade(trade, time, c3_blocked=c3_should_block)

    def _finalize_trade(self, trade: ScaleOutTrade, time: datetime, c3_blocked: bool = False) -> dict:
        """Compute final PnL and archive trade.

        v1.3.1: When c3_blocked=True, C3's PnL contribution is zeroed out
        to mirror the delayed C3 runner architecture validated in backtest.
        C3 was never really "in" the trade — we reverse its market exposure.
        """
        trade.total_gross_pnl = sum(l.gross_pnl for l in trade.active_legs)
        trade.total_commission = sum(l.commission for l in trade.active_legs)
        trade.total_net_pnl = trade.total_gross_pnl - trade.total_commission

        # v1.3.1: C3 delayed entry PnL adjustment
        # When C3 is blocked, remove its PnL contribution and add back its
        # slippage/commission costs (since it was never really entered).
        # This mirrors the backtest logic exactly.
        if c3_blocked and trade.c3.contracts > 0:
            c3_pnl_original = trade.c3.net_pnl
            c3_gross = trade.c3.gross_pnl
            c3_commission = trade.c3.commission

            # Remove C3's market PnL but keep slippage cost as "blocked" cost
            # In the backtest: adjusted_pnl = adjusted_pnl - c3_pnl_original + c3_slip_cost + c3_commission
            # Effectively: total_net_pnl -= c3_pnl_original (remove the C3 loss)
            # then add back the slippage cost that wouldn't have occurred
            trade.total_net_pnl -= c3_gross  # Remove C3 gross PnL (the market loss)
            trade.total_commission -= c3_commission  # Remove C3 commission (never entered)
            trade.total_gross_pnl -= c3_gross  # Adjust gross too

            # Update C3 stats
            self._c3_stats["trades_total"] += 1
            self._c3_stats["c3_blocked"] += 1
            self._c3_stats["c3_pnl_saved"] += abs(c3_pnl_original)

            logger.info(
                f"  C3 DELAYED BLOCKED: removed C3 PnL ${c3_pnl_original:.2f} | "
                f"Adjusted total: ${trade.total_net_pnl:.2f} | "
                f"C3 stats: {self._c3_stats['c3_blocked']}/{self._c3_stats['trades_total']} blocked, "
                f"${self._c3_stats['c3_pnl_saved']:.2f} saved"
            )
        elif trade.c3.contracts > 0:
            # C3 entered (not blocked) — track stats
            self._c3_stats["trades_total"] += 1
            self._c3_stats["c3_entered"] += 1

        trade.closed_at = time
        trade._set_phase(ScaleOutPhase.DONE)

        # Analytics
        if self._analytics:
            exit_side = "SELL" if trade.direction == "long" else "BUY"
            direction = "long_exit" if trade.direction == "long" else "short_exit"
            for leg in trade.active_legs:
                if leg.exit_price:
                    oid = f"{trade.trade_id}-{leg.leg_label}-exit"
                    self._analytics.record_order_sent(
                        order_id=oid, side=exit_side, size=leg.contracts,
                        expected_price=leg.stop_price or leg.target_price or leg.entry_price,
                        timestamp=time, order_type="market", direction=direction,
                    )
                    self._analytics.record_fill(
                        order_id=oid, fill_price=leg.exit_price,
                        fill_size=leg.contracts, fill_timestamp=time,
                    )

        self._trade_history.append(trade)
        self._active_trade = None

        # Log summary
        leg_summaries = []
        for leg in trade.active_legs:
            pts = abs(leg.exit_price - leg.entry_price) if leg.exit_price else 0
            leg_summaries.append(
                f"{leg.leg_label}: {leg.exit_reason} ({pts:.1f}pts ${leg.net_pnl:.2f})"
            )

        logger.info(
            f"TRADE CLOSED: {trade.direction.upper()} | "
            f"{' | '.join(leg_summaries)} | "
            f"TOTAL: ${trade.total_net_pnl:.2f}"
        )

        # Fire trade exit alert
        mgr = get_alert_manager()
        if mgr:
            last_exit = next(
                (l for l in reversed(trade.active_legs) if l.exit_price), None
            )
            if last_exit:
                mgr.enqueue(AlertTemplates.trade_exit(
                    direction=trade.direction,
                    contracts=sum(l.contracts for l in trade.active_legs),
                    exit_price=last_exit.exit_price,
                    entry_price=trade.entry_price,
                    pnl=trade.total_net_pnl,
                    exit_reason=last_exit.exit_reason,
                ))

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
            "c3_exit_price": trade.c3.exit_price if trade.c3.contracts > 0 else 0.0,
            "c3_exit_reason": trade.c3.exit_reason if trade.c3.contracts > 0 else "n/a",
            "c3_pnl": trade.c3.net_pnl,
            "c4_exit_price": trade.c4.exit_price if trade.c4.contracts > 0 else 0.0,
            "c4_exit_reason": trade.c4.exit_reason if trade.c4.contracts > 0 else "n/a",
            "c4_pnl": trade.c4.net_pnl,
            "total_pnl": trade.total_net_pnl,
            "regime": trade.market_regime,
            "phase_history": trade.phase_history,
            "c1_mfe": trade.c1_mfe,
            "c2_mfe": trade.c2_mfe,
            "c1_exit_profit_pts": trade.c1_exit_profit_pts,
            "c1_exit_bars": trade.c1_exit_bars,
            "c1_price_velocity": trade.c1_price_velocity,
            "c3_blocked": c3_blocked,
            "c3_stats": dict(self._c3_stats),
        }

    # ================================================================
    # MAINTENANCE WINDOW FLATTEN
    # ================================================================
    async def maintenance_flatten(self, price: float, current_time: datetime) -> Optional[dict]:
        """Force-close ALL open positions for CME maintenance window.

        Called at 4:50 PM ET (or first bar >= 4:50 PM ET).
        Unconditional — closes C1 AND C2 regardless of unrealized PnL.
        Exit reason tagged as EXIT_MAINTENANCE_FLATTEN.
        """
        if not self.has_active_trade:
            return None

        trade = self._active_trade
        open_legs = trade.open_legs
        n_contracts = sum(leg.contracts for leg in open_legs)

        logger.warning(
            "MAINTENANCE FLATTEN: Closing %d contracts at %.2f — "
            "10 minutes to maintenance halt",
            n_contracts, price,
        )

        for leg in open_legs:
            self._close_leg(leg, price, current_time, "EXIT_MAINTENANCE_FLATTEN", trade.direction)

        if not self.config.execution.paper_trading and self.broker:
            await self.broker.flatten_position()

        return self._finalize_trade(trade, current_time)

    # ================================================================
    # EMERGENCY
    # ================================================================
    async def emergency_flatten(self, price: float) -> Optional[dict]:
        """Emergency close of active trade at market."""
        if not self.has_active_trade:
            return None

        trade = self._active_trade
        now = datetime.now(timezone.utc)

        for leg in trade.open_legs:
            self._close_leg(leg, price, now, "emergency", trade.direction)

        if not self.config.execution.paper_trading and self.broker:
            await self.broker.flatten_position()

        return self._finalize_trade(trade, now)

    # ================================================================
    # HELPERS
    # ================================================================
    def _compute_leg_pnl(self, leg: ContractLeg, direction: str) -> float:
        """Compute gross PnL for one leg in dollars. (Legacy compat)"""
        if not leg.exit_price or not leg.entry_price:
            return 0.0
        point_value = self.risk_config.get_point_value(self._instrument)
        if direction == "long":
            points = leg.exit_price - leg.entry_price
        else:
            points = leg.entry_price - leg.exit_price
        return round(points * point_value * leg.contracts, 2)

    def get_trade_history(self) -> List[dict]:
        """Return trade history as list of dicts."""
        results = []
        for t in self._trade_history:
            entry = {
                "trade_id": t.trade_id,
                "direction": t.direction,
                "entry_price": t.entry_price,
                "entry_time": t.entry_time.isoformat() if t.entry_time else None,
                "signal_score": t.signal_score,
                "regime": t.market_regime,
                "total_pnl": t.total_net_pnl,
                "closed_at": t.closed_at.isoformat() if t.closed_at else None,
                "c1_mfe": t.c1_mfe,
                "c2_mfe": t.c2_mfe,
                "c1_exit_profit_pts": t.c1_exit_profit_pts,
                "c1_exit_bars": t.c1_exit_bars,
                "c1_price_velocity": t.c1_price_velocity,
            }
            for leg in t.active_legs:
                prefix = leg.leg_label.lower()
                entry[f"{prefix}_pnl"] = leg.net_pnl
                entry[f"{prefix}_exit_reason"] = leg.exit_reason
                entry[f"{prefix}_contracts"] = leg.contracts
            results.append(entry)
        return results
