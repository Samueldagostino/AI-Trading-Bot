"""
Alert Message Templates
=======================
Pre-formatted message templates for all event types.
Ensures consistent, professional messaging across channels.
"""

from datetime import datetime, timezone
from typing import Dict, Optional
from monitoring.alerting import Alert, AlertSeverity


class AlertTemplates:
    """Factory for creating alerts with standard messages."""

    # ────────────────────────────────────────────────────────────────
    # CRITICAL EVENTS
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def kill_switch_triggered(reason: str, stats: Optional[Dict] = None) -> Alert:
        """Kill switch activated."""
        message = f"Trading halted: {reason}"

        data = stats or {}
        data["triggered_at"] = datetime.now(timezone.utc).isoformat()

        return Alert(
            event_type="kill_switch_triggered",
            severity=AlertSeverity.EMERGENCY,
            title="🚨 KILL SWITCH TRIGGERED",
            message=message,
            data=data,
        )

    @staticmethod
    def connection_loss(component: str, error: str = "") -> Alert:
        """Broker or WebSocket connection lost."""
        message = f"{component} connection lost"
        if error:
            message += f": {error}"

        return Alert(
            event_type="connection_loss",
            severity=AlertSeverity.CRITICAL,
            title="🔌 Connection Lost",
            message=message,
            data={
                "component": component,
                "error": error,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @staticmethod
    def system_error(component: str, error: str) -> Alert:
        """System or module error."""
        return Alert(
            event_type="system_error",
            severity=AlertSeverity.CRITICAL,
            title="⚠️ System Error",
            message=f"{component}: {error}",
            data={
                "component": component,
                "error": error,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    # ────────────────────────────────────────────────────────────────
    # RISK WARNINGS
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def drawdown_warning(
        current_drawdown_pct: float,
        max_drawdown_pct: float,
        daily_pnl: float,
    ) -> Alert:
        """Daily drawdown approaching limit."""
        progress = (current_drawdown_pct / max_drawdown_pct * 100)
        message = (
            f"Daily drawdown: {current_drawdown_pct:.2f}% "
            f"({progress:.0f}% of {max_drawdown_pct:.2f}% limit)\n"
            f"Daily PnL: ${daily_pnl:,.2f}"
        )

        return Alert(
            event_type="drawdown_warning",
            severity=AlertSeverity.WARNING,
            title="📊 Drawdown Warning",
            message=message,
            data={
                "current_drawdown_pct": round(current_drawdown_pct, 2),
                "max_drawdown_pct": max_drawdown_pct,
                "daily_pnl": round(daily_pnl, 2),
                "progress_pct": round(progress, 1),
            },
        )

    @staticmethod
    def consecutive_loss_streak(
        consecutive_losses: int,
        avg_loss: float,
        total_loss: float,
    ) -> Alert:
        """Consecutive losses detected."""
        message = (
            f"{consecutive_losses} consecutive losses\n"
            f"Average loss: ${abs(avg_loss):,.2f}\n"
            f"Total loss: ${abs(total_loss):,.2f}"
        )

        return Alert(
            event_type="consecutive_losses",
            severity=AlertSeverity.WARNING,
            title="📉 Consecutive Loss Streak",
            message=message,
            data={
                "consecutive_losses": consecutive_losses,
                "avg_loss": round(avg_loss, 2),
                "total_loss": round(total_loss, 2),
            },
        )

    @staticmethod
    def high_vix_alert(vix_level: float, max_vix: float) -> Alert:
        """VIX above trading threshold."""
        message = (
            f"Current VIX: {vix_level:.1f}\n"
            f"Max trading VIX: {max_vix:.1f}\n"
            f"Consider reducing position size or standing aside"
        )

        return Alert(
            event_type="high_vix",
            severity=AlertSeverity.WARNING,
            title="📈 High VIX Alert",
            message=message,
            data={
                "current_vix": round(vix_level, 1),
                "max_trading_vix": max_vix,
            },
        )

    # ────────────────────────────────────────────────────────────────
    # TRADE EVENTS
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def trade_entry(
        direction: str,
        contracts: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        signal_confidence: float,
    ) -> Alert:
        """Trade entry executed."""
        direction_emoji = "📈" if direction.upper() == "LONG" else "📉"
        message = (
            f"{direction.upper()} {contracts} contract(s) at ${entry_price:.2f}\n"
            f"Stop: ${stop_loss:.2f} | Target: ${take_profit:.2f}\n"
            f"Signal Confidence: {signal_confidence:.0%}"
        )

        risk = abs(entry_price - stop_loss)
        reward = abs(take_profit - entry_price)
        rr_ratio = reward / risk if risk > 0 else 0

        return Alert(
            event_type="trade_entry",
            severity=AlertSeverity.INFO,
            title=f"{direction_emoji} Trade Entry",
            message=message,
            data={
                "direction": direction,
                "contracts": contracts,
                "entry_price": round(entry_price, 2),
                "stop_loss": round(stop_loss, 2),
                "take_profit": round(take_profit, 2),
                "risk_points": round(risk, 2),
                "reward_points": round(reward, 2),
                "rr_ratio": round(rr_ratio, 2),
                "signal_confidence": round(signal_confidence, 2),
            },
        )

    @staticmethod
    def trade_exit(
        direction: str,
        contracts: int,
        exit_price: float,
        entry_price: float,
        pnl: float,
        exit_reason: str,
    ) -> Alert:
        """Trade exit executed."""
        direction_emoji = "📈" if direction.upper() == "LONG" else "📉"
        pnl_emoji = "✅" if pnl >= 0 else "❌"

        _range = contracts * abs(exit_price - entry_price) * 2
        _pct = (pnl / _range * 100) if _range > 0 else 0.0
        message = (
            f"{direction.upper()} {contracts} contract(s)\n"
            f"Entry: ${entry_price:.2f} → Exit: ${exit_price:.2f}\n"
            f"PnL: ${pnl:+,.2f} ({_pct:+.1f}%)\n"
            f"Reason: {exit_reason}"
        )

        return Alert(
            event_type="trade_exit",
            severity=AlertSeverity.INFO,
            title=f"{pnl_emoji} {direction_emoji} Trade Exit",
            message=message,
            data={
                "direction": direction,
                "contracts": contracts,
                "entry_price": round(entry_price, 2),
                "exit_price": round(exit_price, 2),
                "pnl": round(pnl, 2),
                "exit_reason": exit_reason,
            },
        )

    @staticmethod
    def partial_exit(
        contracts_exited: int,
        remaining_contracts: int,
        exit_price: float,
        pnl: float,
    ) -> Alert:
        """Partial position exit (scale-out)."""
        message = (
            f"Exited {contracts_exited} contract(s) at ${exit_price:.2f}\n"
            f"Remaining: {remaining_contracts} contract(s)\n"
            f"PnL on exit: ${pnl:+,.2f}"
        )

        return Alert(
            event_type="partial_exit",
            severity=AlertSeverity.INFO,
            title="🎯 Partial Exit",
            message=message,
            data={
                "contracts_exited": contracts_exited,
                "remaining_contracts": remaining_contracts,
                "exit_price": round(exit_price, 2),
                "pnl": round(pnl, 2),
            },
        )

    # ────────────────────────────────────────────────────────────────
    # DAILY SUMMARY
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def daily_summary(
        total_trades: int,
        winning_trades: int,
        losing_trades: int,
        daily_pnl: float,
        win_rate: float,
        profit_factor: float,
        largest_win: float,
        largest_loss: float,
    ) -> Alert:
        """End-of-day performance summary."""
        emoji = "✅" if daily_pnl >= 0 else "❌"

        message = (
            f"Trades: {total_trades} (W: {winning_trades}, L: {losing_trades})\n"
            f"Win Rate: {win_rate:.1f}% | Profit Factor: {profit_factor:.2f}\n"
            f"Daily PnL: ${daily_pnl:+,.2f}\n"
            f"Best: ${largest_win:,.2f} | Worst: ${largest_loss:,.2f}"
        )

        return Alert(
            event_type="daily_summary",
            severity=AlertSeverity.INFO,
            title=f"{emoji} Daily Summary",
            message=message,
            data={
                "total_trades": total_trades,
                "winning_trades": winning_trades,
                "losing_trades": losing_trades,
                "daily_pnl": round(daily_pnl, 2),
                "win_rate": round(win_rate, 1),
                "profit_factor": round(profit_factor, 2),
                "largest_win": round(largest_win, 2),
                "largest_loss": round(largest_loss, 2),
            },
        )

    # ────────────────────────────────────────────────────────────────
    # SYSTEM INFO
    # ────────────────────────────────────────────────────────────────

    @staticmethod
    def startup_complete(environment: str, broker: str) -> Alert:
        """System startup completed."""
        message = (
            f"Environment: {environment.upper()}\n"
            f"Broker: {broker}\n"
            f"Ready to trade"
        )

        return Alert(
            event_type="startup_complete",
            severity=AlertSeverity.INFO,
            title="✅ System Ready",
            message=message,
            data={
                "environment": environment,
                "broker": broker,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @staticmethod
    def shutdown_initiated(reason: str) -> Alert:
        """System shutdown initiated."""
        return Alert(
            event_type="shutdown_initiated",
            severity=AlertSeverity.WARNING,
            title="⏹️ Shutdown",
            message=f"Reason: {reason}",
            data={
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

    @staticmethod
    def custom_alert(
        event_type: str,
        title: str,
        message: str,
        severity: AlertSeverity = AlertSeverity.INFO,
        data: Optional[Dict] = None,
    ) -> Alert:
        """Create a custom alert."""
        return Alert(
            event_type=event_type,
            severity=severity,
            title=title,
            message=message,
            data=data or {},
        )
