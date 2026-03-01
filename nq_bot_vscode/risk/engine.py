"""
Risk Engine
============
INDEPENDENT risk management layer. This module has VETO POWER over
every trade signal and execution decision. It cannot be bypassed.

Design principles:
1. The risk engine runs in its own evaluation loop
2. It can ONLY reduce exposure, never increase it
3. Kill switch operates on a separate logical path
4. All limits are HARD — no exceptions for "high confidence" signals
"""

import logging
from datetime import datetime, timezone, timedelta, time
from dataclasses import dataclass, field
from typing import Optional, List, Tuple
from enum import Enum
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


class RiskDecision(Enum):
    APPROVE = "approve"
    REDUCE_SIZE = "reduce_size"
    REJECT = "reject"
    KILL_SWITCH = "kill_switch"


@dataclass
class RiskState:
    """Current risk state of the trading system."""
    # Equity tracking
    starting_equity: float = 0.0
    current_equity: float = 0.0
    peak_equity: float = 0.0
    
    # Daily tracking
    daily_starting_equity: float = 0.0
    daily_pnl: float = 0.0
    daily_trades: int = 0
    daily_wins: int = 0
    daily_losses: int = 0
    
    # Drawdown
    current_drawdown_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    
    # Streak tracking
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    
    # Position
    open_contracts: int = 0
    open_direction: str = "flat"   # 'long', 'short', 'flat'
    unrealized_pnl: float = 0.0
    
    # Flags
    kill_switch_active: bool = False
    kill_switch_reason: str = ""
    kill_switch_resume_at: Optional[datetime] = None
    daily_limit_hit: bool = False
    is_overnight: bool = False
    upcoming_news_event: bool = False
    
    # Current VIX
    current_vix: float = 0.0


@dataclass
class RiskAssessment:
    """Result of risk evaluation for a proposed trade."""
    decision: RiskDecision
    max_contracts: int
    reason: str
    suggested_stop_distance: float    # In NQ points
    suggested_target_distance: float  # In NQ points
    risk_per_contract: float          # Dollar risk per contract
    total_risk_dollars: float         # Total dollar risk for proposed size
    
    # Adjustments applied
    size_multiplier: float = 1.0     # 1.0 = full size, 0.5 = half, etc.
    adjustments: List[str] = field(default_factory=list)


class RiskEngine:
    """
    Independent risk management engine.
    
    CRITICAL: This engine has absolute authority over position sizing
    and trade approval. No signal, regardless of confidence, can
    override risk limits.
    """

    def __init__(self, config):
        self.config = config.risk
        self.state = RiskState(
            starting_equity=self.config.account_size,
            current_equity=self.config.account_size,
            peak_equity=self.config.account_size,
            daily_starting_equity=self.config.account_size,
        )
        self._economic_events: List[dict] = []

    # ================================================================
    # Primary Risk Assessment
    # ================================================================
    def evaluate_trade(
        self,
        direction: str,
        entry_price: float,
        atr: float,
        vix: float = 0.0,
        current_time: Optional[datetime] = None,
    ) -> RiskAssessment:
        """
        Evaluate whether a proposed trade is allowed and compute
        position size. Returns a RiskAssessment with decision.
        """
        if current_time is None:
            current_time = datetime.now(timezone.utc)

        # Update state
        self.state.current_vix = vix
        self.state.is_overnight = self._is_overnight(current_time)

        # === KILL SWITCH CHECK (highest priority) ===
        if self.state.kill_switch_active:
            if self._can_resume(current_time):
                self._deactivate_kill_switch()
                logger.info("Kill switch deactivated — resuming trading")
            else:
                return RiskAssessment(
                    decision=RiskDecision.KILL_SWITCH,
                    max_contracts=0,
                    reason=f"Kill switch active: {self.state.kill_switch_reason}",
                    suggested_stop_distance=0,
                    suggested_target_distance=0,
                    risk_per_contract=0,
                    total_risk_dollars=0,
                )

        # === DAILY LOSS LIMIT CHECK ===
        if self.state.daily_limit_hit:
            return RiskAssessment(
                decision=RiskDecision.REJECT,
                max_contracts=0,
                reason="Daily loss limit reached — no more trades today",
                suggested_stop_distance=0,
                suggested_target_distance=0,
                risk_per_contract=0,
                total_risk_dollars=0,
            )

        daily_loss_pct = abs(self.state.daily_pnl / self.state.daily_starting_equity * 100)
        if self.state.daily_pnl < 0 and daily_loss_pct >= self.config.max_daily_loss_pct:
            self.state.daily_limit_hit = True
            logger.warning(f"DAILY LOSS LIMIT HIT: {daily_loss_pct:.2f}%")
            return RiskAssessment(
                decision=RiskDecision.REJECT,
                max_contracts=0,
                reason=f"Daily loss limit hit: {daily_loss_pct:.2f}%",
                suggested_stop_distance=0,
                suggested_target_distance=0,
                risk_per_contract=0,
                total_risk_dollars=0,
            )

        # === MAX DRAWDOWN CHECK ===
        self._update_drawdown()
        if self.state.current_drawdown_pct >= self.config.max_total_drawdown_pct:
            self._activate_kill_switch(
                f"Max drawdown breached: {self.state.current_drawdown_pct:.2f}%",
                current_time,
            )
            return RiskAssessment(
                decision=RiskDecision.KILL_SWITCH,
                max_contracts=0,
                reason=f"Max drawdown: {self.state.current_drawdown_pct:.2f}%",
                suggested_stop_distance=0,
                suggested_target_distance=0,
                risk_per_contract=0,
                total_risk_dollars=0,
            )

        # === CONSECUTIVE LOSS CHECK ===
        if self.state.consecutive_losses >= self.config.kill_switch_max_consecutive_losses:
            self._activate_kill_switch(
                f"Consecutive losses: {self.state.consecutive_losses}",
                current_time,
            )
            return RiskAssessment(
                decision=RiskDecision.KILL_SWITCH,
                max_contracts=0,
                reason=f"Consecutive losses: {self.state.consecutive_losses}",
                suggested_stop_distance=0,
                suggested_target_distance=0,
                risk_per_contract=0,
                total_risk_dollars=0,
            )

        # === NEWS EVENT CHECK ===
        self.state.upcoming_news_event = self._is_near_news_event(current_time)
        if self.state.upcoming_news_event:
            return RiskAssessment(
                decision=RiskDecision.REJECT,
                max_contracts=0,
                reason="Near scheduled news event — no new trades",
                suggested_stop_distance=0,
                suggested_target_distance=0,
                risk_per_contract=0,
                total_risk_dollars=0,
            )

        # === VIX CHECK ===
        if vix >= self.config.max_vix_for_trading:
            return RiskAssessment(
                decision=RiskDecision.REJECT,
                max_contracts=0,
                reason=f"VIX too high: {vix:.1f} (limit: {self.config.max_vix_for_trading})",
                suggested_stop_distance=0,
                suggested_target_distance=0,
                risk_per_contract=0,
                total_risk_dollars=0,
            )

        # === COMPUTE POSITION SIZE ===
        stop_distance, target_distance = self._compute_stop_target(atr)
        size_multiplier = self._compute_size_multiplier(vix, current_time)
        max_contracts = self._compute_position_size(
            stop_distance, entry_price, size_multiplier
        )

        # Dollar risk calculation
        point_value = (self.config.nq_point_value_micro if self.config.use_micro 
                      else self.config.nq_point_value_mini)
        risk_per_contract = stop_distance * point_value + self.config.commission_per_contract
        total_risk = risk_per_contract * max_contracts

        adjustments = []
        decision = RiskDecision.APPROVE

        if size_multiplier < 1.0:
            decision = RiskDecision.REDUCE_SIZE
            if self.state.is_overnight:
                adjustments.append(f"Overnight session: size * {size_multiplier:.1f}")
            if vix > self.config.max_vix_for_full_size:
                adjustments.append(f"Elevated VIX ({vix:.1f}): size reduced")
            if self.state.consecutive_losses >= 3:
                adjustments.append(f"Loss streak ({self.state.consecutive_losses}): size reduced")

        if max_contracts == 0:
            decision = RiskDecision.REJECT
            adjustments.append("Computed size is 0 contracts — risk too high for account")

        return RiskAssessment(
            decision=decision,
            max_contracts=max_contracts,
            reason="Approved" if decision == RiskDecision.APPROVE else "; ".join(adjustments),
            suggested_stop_distance=round(stop_distance, 2),
            suggested_target_distance=round(target_distance, 2),
            risk_per_contract=round(risk_per_contract, 2),
            total_risk_dollars=round(total_risk, 2),
            size_multiplier=size_multiplier,
            adjustments=adjustments,
        )

    # ================================================================
    # Position Sizing
    # ================================================================
    def _compute_position_size(
        self, stop_distance: float, entry_price: float, size_multiplier: float
    ) -> int:
        """
        Volatility-adjusted position sizing.
        Risk per trade = account_size * max_risk_per_trade_pct / 100
        Contracts = risk_budget / (stop_distance * point_value + commission)
        """
        risk_budget = self.state.current_equity * (self.config.max_risk_per_trade_pct / 100)
        
        point_value = (self.config.nq_point_value_micro if self.config.use_micro 
                      else self.config.nq_point_value_mini)

        # Include slippage in risk calculation
        slippage_cost = self.config.max_slippage_ticks * 0.25 * point_value  # ticks -> points -> dollars
        cost_per_contract = (stop_distance * point_value 
                           + self.config.commission_per_contract 
                           + slippage_cost)

        if cost_per_contract <= 0:
            return 0

        raw_contracts = risk_budget / cost_per_contract
        adjusted_contracts = int(raw_contracts * size_multiplier)

        # Apply hard max
        max_allowed = (self.config.max_contracts_micro if self.config.use_micro 
                      else self.config.max_contracts_mini)
        
        final_contracts = min(adjusted_contracts, max_allowed)
        final_contracts = max(final_contracts, 0)  # Floor at 0

        return final_contracts

    def _compute_stop_target(self, atr: float) -> Tuple[float, float]:
        """Compute stop loss and take profit distances from ATR."""
        stop_distance = atr * self.config.atr_multiplier_stop
        target_distance = atr * self.config.atr_multiplier_target

        # Enforce minimum R:R
        if target_distance / stop_distance < self.config.min_rr_ratio:
            target_distance = stop_distance * self.config.min_rr_ratio

        return stop_distance, target_distance

    def _compute_size_multiplier(self, vix: float, current_time: datetime) -> float:
        """
        Compute a multiplicative factor to reduce position size
        based on current conditions. Always <= 1.0.
        """
        multiplier = 1.0

        # Overnight reduction
        if self.state.is_overnight and self.config.reduce_size_overnight:
            multiplier *= 0.5

        # VIX-based reduction
        if vix > self.config.max_vix_for_full_size:
            vix_factor = max(0.3, 1.0 - (vix - self.config.max_vix_for_full_size) / 30)
            multiplier *= vix_factor

        # Loss streak reduction (gradual)
        if self.state.consecutive_losses >= 3:
            streak_factor = max(0.25, 1.0 - (self.state.consecutive_losses - 2) * 0.15)
            multiplier *= streak_factor

        # Drawdown reduction
        if self.state.current_drawdown_pct >= 5.0:
            dd_factor = max(0.25, 1.0 - (self.state.current_drawdown_pct - 5.0) / 10)
            multiplier *= dd_factor

        return round(min(multiplier, 1.0), 2)

    # ================================================================
    # Trade Outcome Recording
    # ================================================================
    def record_trade_result(self, net_pnl: float, direction: str) -> None:
        """Update risk state after a trade closes."""
        self.state.daily_pnl += net_pnl
        self.state.current_equity += net_pnl
        self.state.daily_trades += 1

        if net_pnl >= 0:
            self.state.daily_wins += 1
            self.state.consecutive_wins += 1
            self.state.consecutive_losses = 0
        else:
            self.state.daily_losses += 1
            self.state.consecutive_losses += 1
            self.state.consecutive_wins = 0

        # Update peak equity
        if self.state.current_equity > self.state.peak_equity:
            self.state.peak_equity = self.state.current_equity

        self._update_drawdown()

        logger.info(
            f"Trade result: PnL=${net_pnl:.2f} | Daily=${self.state.daily_pnl:.2f} | "
            f"Equity=${self.state.current_equity:.2f} | DD={self.state.current_drawdown_pct:.2f}%"
        )

    def reset_daily_state(self) -> None:
        """Call at the start of each trading day."""
        self.state.daily_starting_equity = self.state.current_equity
        self.state.daily_pnl = 0.0
        self.state.daily_trades = 0
        self.state.daily_wins = 0
        self.state.daily_losses = 0
        self.state.daily_limit_hit = False
        logger.info(f"Daily reset. Starting equity: ${self.state.current_equity:.2f}")

    # ================================================================
    # Kill Switch
    # ================================================================
    def _activate_kill_switch(self, reason: str, current_time: datetime) -> None:
        """Activate kill switch — stops all trading."""
        self.state.kill_switch_active = True
        self.state.kill_switch_reason = reason
        self.state.kill_switch_resume_at = (
            current_time + timedelta(minutes=self.config.kill_switch_cooldown_minutes)
        )
        logger.critical(f"KILL SWITCH ACTIVATED: {reason}")
        logger.critical(f"Resume at: {self.state.kill_switch_resume_at}")

    def _deactivate_kill_switch(self) -> None:
        """Deactivate kill switch after cooldown."""
        self.state.kill_switch_active = False
        self.state.kill_switch_reason = ""
        self.state.kill_switch_resume_at = None
        # Reset consecutive losses to prevent immediate re-trigger
        self.state.consecutive_losses = 0

    def _can_resume(self, current_time: datetime) -> bool:
        """Check if kill switch cooldown has elapsed."""
        if self.state.kill_switch_resume_at is None:
            return True
        return current_time >= self.state.kill_switch_resume_at

    def force_kill_switch(self, reason: str = "Manual activation") -> None:
        """Manual kill switch activation (e.g., from monitoring dashboard)."""
        self._activate_kill_switch(reason, datetime.now(timezone.utc))

    # ================================================================
    # Helpers
    # ================================================================
    def _update_drawdown(self) -> None:
        """Update current and max drawdown percentages."""
        if self.state.peak_equity > 0:
            dd = (self.state.peak_equity - self.state.current_equity) / self.state.peak_equity * 100
            self.state.current_drawdown_pct = round(max(dd, 0), 2)
            self.state.max_drawdown_pct = max(self.state.max_drawdown_pct, 
                                               self.state.current_drawdown_pct)

    def _is_overnight(self, current_time: datetime) -> bool:
        """Check if current time is in overnight/thin liquidity session."""
        et_time = current_time.astimezone(ZoneInfo("America/New_York"))
        et_hour = et_time.hour
        return (et_hour >= self.config.overnight_start_hour or
                et_hour < self.config.overnight_end_hour)

    def _is_near_news_event(self, current_time: datetime) -> bool:
        """Check if any high-impact economic event is within the guard window."""
        before_window = timedelta(minutes=self.config.no_trade_minutes_before_news)
        after_window = timedelta(minutes=self.config.no_trade_minutes_after_news)

        for event in self._economic_events:
            event_time = event.get("event_time")
            if event_time is None:
                continue
            if event.get("impact_level") not in ("high", "critical"):
                continue
            if (current_time >= event_time - before_window and 
                current_time <= event_time + after_window):
                return True
        return False

    def load_economic_calendar(self, events: List[dict]) -> None:
        """Load upcoming economic events for news-guard logic."""
        self._economic_events = events
        logger.info(f"Loaded {len(events)} economic events into risk engine")

    def get_state_snapshot(self) -> dict:
        """Return current risk state as a dictionary for monitoring."""
        return {
            "equity": self.state.current_equity,
            "daily_pnl": self.state.daily_pnl,
            "drawdown_pct": self.state.current_drawdown_pct,
            "max_drawdown_pct": self.state.max_drawdown_pct,
            "consecutive_losses": self.state.consecutive_losses,
            "kill_switch_active": self.state.kill_switch_active,
            "daily_limit_hit": self.state.daily_limit_hit,
            "is_overnight": self.state.is_overnight,
            "vix": self.state.current_vix,
            "daily_trades": self.state.daily_trades,
            "daily_win_rate": (self.state.daily_wins / self.state.daily_trades * 100
                              if self.state.daily_trades > 0 else 0),
        }
