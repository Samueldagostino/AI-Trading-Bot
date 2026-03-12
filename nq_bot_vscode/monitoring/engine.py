"""
Monitoring Layer
=================
Real-time system observability and alerting.
Tracks PnL, drawdown, fill quality, regime drift, and model decay.

Can be extended to push to:
- Console logging (default)
- Web dashboard (FastAPI + WebSocket)
- Slack/Discord alerts
- Prometheus metrics
"""

import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PerformanceMetrics:
    """Rolling performance metrics."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    avg_trade_duration_minutes: float = 0.0
    
    @property
    def win_rate(self) -> float:
        return (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0.0
    
    @property
    def profit_factor(self) -> float:
        return abs(self.gross_profit / self.gross_loss) if self.gross_loss != 0 else float('inf')
    
    @property
    def avg_winner(self) -> float:
        return self.gross_profit / self.winning_trades if self.winning_trades > 0 else 0.0
    
    @property
    def avg_loser(self) -> float:
        return self.gross_loss / self.losing_trades if self.losing_trades > 0 else 0.0
    
    @property
    def expectancy(self) -> float:
        """Average expected PnL per trade."""
        if self.total_trades == 0:
            return 0.0
        return self.total_pnl / self.total_trades


class MonitoringEngine:
    """
    System monitoring and alerting engine.
    Runs independently, polling system state periodically.
    """

    def __init__(self, config, db_manager=None):
        self.config = config
        self.db = db_manager
        self.metrics = PerformanceMetrics()
        self._alerts: List[dict] = []
        self._health_status: Dict[str, str] = {
            "data": "unknown",
            "features": "unknown",
            "signals": "unknown",
            "risk": "unknown",
            "execution": "unknown",
            "discord": "unknown",
        }

    def record_trade(self, trade_result: dict) -> None:
        """Record a completed trade for metrics."""
        if trade_result.get("action") != "exit":
            return

        pnl = trade_result.get("pnl", 0.0)
        self.metrics.total_trades += 1
        self.metrics.total_pnl += pnl

        if pnl >= 0:
            self.metrics.winning_trades += 1
            self.metrics.gross_profit += pnl
            self.metrics.largest_win = max(self.metrics.largest_win, pnl)
        else:
            self.metrics.losing_trades += 1
            self.metrics.gross_loss += pnl
            self.metrics.largest_loss = min(self.metrics.largest_loss, pnl)

        # Check for alert conditions
        self._check_alerts(trade_result)

    def _check_alerts(self, trade_result: dict) -> None:
        """Check if any alert conditions are met."""
        pnl = trade_result.get("pnl", 0.0)
        
        # Large loss alert
        if pnl < -500:
            self._emit_alert(
                level="warning",
                message=f"Large loss: ${pnl:.2f} on {trade_result.get('direction')} trade",
                data=trade_result,
            )

        # Win streak / loss streak detection
        if self.metrics.losing_trades >= 3 and self.metrics.total_trades >= 5:
            recent_loss_rate = self.metrics.losing_trades / self.metrics.total_trades
            if recent_loss_rate > 0.7:
                self._emit_alert(
                    level="critical",
                    message=f"High loss rate: {recent_loss_rate:.0%} over {self.metrics.total_trades} trades",
                    data={"loss_rate": recent_loss_rate},
                )

    def _emit_alert(self, level: str, message: str, data: dict = None) -> None:
        """Emit an alert."""
        alert = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "message": message,
            "data": data or {},
        }
        self._alerts.append(alert)
        
        if level == "critical":
            logger.critical(f"ALERT: {message}")
        elif level == "warning":
            logger.warning(f"ALERT: {message}")
        else:
            logger.info(f"ALERT: {message}")

    def update_health(self, component: str, status: str, message: str = "") -> None:
        """Update health status for a component."""
        self._health_status[component] = status
        if status in ("error", "offline"):
            self._emit_alert(
                level="critical" if status == "offline" else "warning",
                message=f"Component {component}: {status} - {message}",
            )

    def get_dashboard_data(self, risk_state: dict = None) -> dict:
        """
        Compile all monitoring data for dashboard display.
        Call this periodically to refresh the monitoring view.
        """
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "performance": {
                "total_trades": self.metrics.total_trades,
                "win_rate": round(self.metrics.win_rate, 1),
                "total_pnl": round(self.metrics.total_pnl, 2),
                "profit_factor": round(self.metrics.profit_factor, 2),
                "avg_winner": round(self.metrics.avg_winner, 2),
                "avg_loser": round(self.metrics.avg_loser, 2),
                "expectancy": round(self.metrics.expectancy, 2),
                "largest_win": round(self.metrics.largest_win, 2),
                "largest_loss": round(self.metrics.largest_loss, 2),
            },
            "risk": risk_state or {},
            "health": self._health_status.copy(),
            "recent_alerts": self._alerts[-10:],  # Last 10 alerts
        }

    def print_status(self, risk_state: dict = None) -> None:
        """Print formatted status to console."""
        data = self.get_dashboard_data(risk_state)
        
        print("\n" + "=" * 60)
        print("  NQ TRADING BOT -- STATUS DASHBOARD")
        print("=" * 60)
        
        perf = data["performance"]
        print(f"\n  PERFORMANCE")
        print(f"  Trades: {perf['total_trades']} | Win Rate: {perf['win_rate']}%")
        print(f"  Total PnL: ${perf['total_pnl']:,.2f}")
        print(f"  Profit Factor: {perf['profit_factor']}")
        print(f"  Avg Win: ${perf['avg_winner']:,.2f} | Avg Loss: ${perf['avg_loser']:,.2f}")
        print(f"  Expectancy: ${perf['expectancy']:,.2f}/trade")
        
        if risk_state:
            print(f"\n  RISK")
            print(f"  Equity: ${risk_state.get('equity', 0):,.2f}")
            print(f"  Daily PnL: ${risk_state.get('daily_pnl', 0):,.2f}")
            print(f"  Drawdown: {risk_state.get('drawdown_pct', 0):.2f}%")
            print(f"  Kill Switch: {'ACTIVE' if risk_state.get('kill_switch_active') else 'Off'}")
        
        print(f"\n  SYSTEM HEALTH")
        for component, status in data["health"].items():
            icon = "✓" if status == "healthy" else "⚠" if status == "degraded" else "✗"
            print(f"  {icon} {component}: {status}")
        
        if data["recent_alerts"]:
            print(f"\n  RECENT ALERTS")
            for alert in data["recent_alerts"][-3:]:
                print(f"  [{alert['level']}] {alert['message']}")
        
        print("=" * 60 + "\n")
