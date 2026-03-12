"""
Paper Trading Monitor -- Real-Time Statistics Tracker
======================================================
Observes paper trades and maintains running statistics.
READ-ONLY -- it observes, never modifies trades.

Features:
  - Real-time running statistics (PnL, win rate, drawdown, Sharpe, etc.)
  - State persistence to logs/paper_trading_state.json every 5 minutes
  - Trade log to logs/paper_trades.json on every trade
  - Statistical validation thresholds (min 100 trades, min 20 trading days)
  - On-demand dashboard summary

Usage:
    from scripts.paper_trading_monitor import PaperTradingMonitor

    monitor = PaperTradingMonitor()
    monitor.record_trade(pnl=25.0, direction="long", entry_price=20100, ...)
    monitor.print_dashboard()
"""

import json
import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# STATISTICAL VALIDATION THRESHOLDS
# ═══════════════════════════════════════════════════════════════

MIN_TRADES_FOR_SIGNIFICANCE = 100    # Minimum trades before results are meaningful
MIN_TRADING_DAYS_FOR_SHARPE = 20     # Minimum trading days before Sharpe is stable


# ═══════════════════════════════════════════════════════════════
# TRADE RECORD
# ═══════════════════════════════════════════════════════════════

@dataclass
class PaperTradeRecord:
    """A single paper trade."""
    timestamp: str
    direction: str
    pnl: float
    entry_price: float = 0.0
    exit_price: float = 0.0
    stop_distance: float = 0.0
    signal_score: float = 0.0
    signal_source: str = ""
    regime: str = ""
    htf_bias: str = ""
    c1_pnl: float = 0.0
    c2_pnl: float = 0.0
    contracts: int = 2
    metadata: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp,
            "direction": self.direction,
            "pnl": round(self.pnl, 2),
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "stop_distance": self.stop_distance,
            "signal_score": round(self.signal_score, 4),
            "signal_source": self.signal_source,
            "regime": self.regime,
            "htf_bias": self.htf_bias,
            "c1_pnl": round(self.c1_pnl, 2),
            "c2_pnl": round(self.c2_pnl, 2),
            "contracts": self.contracts,
            "metadata": self.metadata,
        }


# ═══════════════════════════════════════════════════════════════
# PAPER TRADING MONITOR
# ═══════════════════════════════════════════════════════════════

class PaperTradingMonitor:
    """
    Tracks all paper trades in real-time and maintains running statistics.

    READ-ONLY -- observes trades, never modifies them.

    Statistics maintained:
      trade_count, wins, losses, total_pnl, max_drawdown,
      current_drawdown, profit_factor, win_rate, sharpe_estimate

    Persistence:
      - State to logs/paper_trading_state.json every 5 minutes
      - Trade log to logs/paper_trades.json on every trade
    """

    STATE_SAVE_INTERVAL = 300  # 5 minutes in seconds

    def __init__(self, log_dir: Optional[str] = None, account_size: float = 50000.0):
        if log_dir is None:
            log_dir = str(Path(__file__).resolve().parent.parent / "logs")

        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self._log_dir / "paper_trading_state.json"
        self._trades_path = self._log_dir / "paper_trades.json"

        self._account_size = account_size
        self._trades: List[PaperTradeRecord] = []
        self._daily_pnls: Dict[str, float] = {}  # date -> daily PnL
        self._trading_days: set = set()

        # Running state
        self._peak_equity = account_size
        self._current_equity = account_size
        self._max_drawdown = 0.0
        self._consecutive_losses = 0
        self._max_consecutive_losses = 0

        # State save timer
        self._last_state_save = time.monotonic()

        # Load existing state if available
        self._load_state()

    # ════════════════════════════════════════════════════════════
    # CORE API
    # ════════════════════════════════════════════════════════════

    def record_trade(
        self,
        pnl: float,
        direction: str,
        entry_price: float = 0.0,
        exit_price: float = 0.0,
        stop_distance: float = 0.0,
        signal_score: float = 0.0,
        signal_source: str = "",
        regime: str = "",
        htf_bias: str = "",
        c1_pnl: float = 0.0,
        c2_pnl: float = 0.0,
        contracts: int = 2,
        metadata: Optional[Dict] = None,
    ) -> None:
        """Record a completed paper trade."""
        now = datetime.now(timezone.utc)
        trade = PaperTradeRecord(
            timestamp=now.isoformat(),
            direction=direction,
            pnl=pnl,
            entry_price=entry_price,
            exit_price=exit_price,
            stop_distance=stop_distance,
            signal_score=signal_score,
            signal_source=signal_source,
            regime=regime,
            htf_bias=htf_bias,
            c1_pnl=c1_pnl,
            c2_pnl=c2_pnl,
            contracts=contracts,
            metadata=metadata or {},
        )
        self._trades.append(trade)

        # Update running state
        self._current_equity += pnl
        self._peak_equity = max(self._peak_equity, self._current_equity)
        dd = self._peak_equity - self._current_equity
        self._max_drawdown = max(self._max_drawdown, dd)

        # Track consecutive losses
        if pnl < 0:
            self._consecutive_losses += 1
            self._max_consecutive_losses = max(
                self._max_consecutive_losses, self._consecutive_losses
            )
        else:
            self._consecutive_losses = 0

        # Track trading days
        date_str = now.strftime("%Y-%m-%d")
        self._trading_days.add(date_str)
        self._daily_pnls[date_str] = self._daily_pnls.get(date_str, 0.0) + pnl

        # Log trade immediately
        self._log_trade(trade)

        # Periodic state save
        self._maybe_save_state()

    def update(self) -> None:
        """Periodic update -- call from main loop to trigger state saves."""
        self._maybe_save_state()

    # ════════════════════════════════════════════════════════════
    # STATISTICS
    # ════════════════════════════════════════════════════════════

    @property
    def trade_count(self) -> int:
        return len(self._trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self._trades if t.pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self._trades if t.pnl < 0)

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self._trades)

    @property
    def win_rate(self) -> float:
        if self.trade_count == 0:
            return 0.0
        return (self.wins / self.trade_count) * 100

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl for t in self._trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self._trades if t.pnl < 0))
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def max_drawdown(self) -> float:
        """Max drawdown in dollars."""
        return self._max_drawdown

    @property
    def current_drawdown(self) -> float:
        """Current drawdown in dollars."""
        return self._peak_equity - self._current_equity

    @property
    def max_drawdown_pct(self) -> float:
        """Max drawdown as percentage of account."""
        if self._account_size <= 0:
            return 0.0
        return (self._max_drawdown / self._account_size) * 100

    @property
    def current_drawdown_pct(self) -> float:
        if self._account_size <= 0:
            return 0.0
        return (self.current_drawdown / self._account_size) * 100

    @property
    def sharpe_estimate(self) -> float:
        """
        Annualized Sharpe ratio estimate from daily returns.

        Only stable after MIN_TRADING_DAYS_FOR_SHARPE trading days.
        Uses daily PnL as returns (not percentage).
        Annualized with sqrt(252).
        """
        if len(self._daily_pnls) < 2:
            return 0.0

        daily_returns = list(self._daily_pnls.values())
        mean_return = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_return) ** 2 for r in daily_returns) / len(daily_returns)
        std_dev = math.sqrt(variance)

        if std_dev == 0:
            return 0.0

        daily_sharpe = mean_return / std_dev
        return daily_sharpe * math.sqrt(252)

    @property
    def trading_days_count(self) -> int:
        return len(self._trading_days)

    @property
    def consecutive_losses_current(self) -> int:
        return self._consecutive_losses

    @property
    def max_consecutive_losses(self) -> int:
        return self._max_consecutive_losses

    # ════════════════════════════════════════════════════════════
    # STATISTICAL VALIDATION
    # ════════════════════════════════════════════════════════════

    @property
    def results_are_meaningful(self) -> bool:
        """True if enough trades for statistical significance."""
        return self.trade_count >= MIN_TRADES_FOR_SIGNIFICANCE

    @property
    def sharpe_is_stable(self) -> bool:
        """True if enough trading days for stable Sharpe estimate."""
        return self.trading_days_count >= MIN_TRADING_DAYS_FOR_SHARPE

    # ════════════════════════════════════════════════════════════
    # PERSISTENCE
    # ════════════════════════════════════════════════════════════

    def _log_trade(self, trade: PaperTradeRecord) -> None:
        """Append trade to paper_trades.json (JSONL)."""
        try:
            with open(self._trades_path, "a") as f:
                f.write(json.dumps(trade.to_dict()) + "\n")
        except OSError as e:
            logger.warning("Failed to write paper trade log: %s", e)

    def _maybe_save_state(self) -> None:
        """Save state if STATE_SAVE_INTERVAL has passed."""
        now = time.monotonic()
        if now - self._last_state_save >= self.STATE_SAVE_INTERVAL:
            self.save_state()
            self._last_state_save = now

    def save_state(self) -> None:
        """Write full state to paper_trading_state.json (atomic write)."""
        state = {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "account_size": self._account_size,
            "current_equity": round(self._current_equity, 2),
            "peak_equity": round(self._peak_equity, 2),
            "trade_count": self.trade_count,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": round(self.total_pnl, 2),
            "win_rate": round(self.win_rate, 2),
            "profit_factor": round(self.profit_factor, 4) if self.profit_factor < 1000 else None,
            "max_drawdown": round(self.max_drawdown, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "current_drawdown": round(self.current_drawdown, 2),
            "current_drawdown_pct": round(self.current_drawdown_pct, 2),
            "sharpe_estimate": round(self.sharpe_estimate, 4),
            "trading_days": self.trading_days_count,
            "max_consecutive_losses": self.max_consecutive_losses,
            "consecutive_losses_current": self.consecutive_losses_current,
            "results_meaningful": self.results_are_meaningful,
            "sharpe_stable": self.sharpe_is_stable,
            "daily_pnls": {k: round(v, 2) for k, v in self._daily_pnls.items()},
        }
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=str(self._log_dir),
                suffix=".tmp",
                delete=False,
            ) as tmp:
                json.dump(state, tmp, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = tmp.name
            # Windows PermissionError retry -- another process may hold the file briefly
            for attempt in range(3):
                try:
                    os.replace(tmp_path, str(self._state_path))
                    break
                except PermissionError:
                    if attempt < 2:
                        time.sleep(0.1)
                    else:
                        raise
        except OSError as e:
            logger.warning("Failed to write paper trading state: %s", e)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _load_state(self) -> None:
        """Load state from disk if available."""
        if not self._state_path.exists():
            return
        try:
            with open(self._state_path) as f:
                state = json.load(f)
            self._current_equity = state.get("current_equity", self._account_size)
            self._peak_equity = state.get("peak_equity", self._account_size)
            self._max_drawdown = state.get("max_drawdown", 0.0)
            self._max_consecutive_losses = state.get("max_consecutive_losses", 0)
            # Restore daily PnLs
            self._daily_pnls = state.get("daily_pnls", {})
            self._trading_days = set(self._daily_pnls.keys())
            logger.info(
                "Loaded paper trading state: %d trades, $%.2f equity",
                state.get("trade_count", 0),
                self._current_equity,
            )
        except (json.JSONDecodeError, IOError, KeyError) as e:
            logger.warning("Failed to load paper trading state: %s", e)

    # ════════════════════════════════════════════════════════════
    # DASHBOARD
    # ════════════════════════════════════════════════════════════

    def get_stats(self) -> Dict:
        """Get all statistics as a dictionary."""
        pf = self.profit_factor
        return {
            "trade_count": self.trade_count,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": round(self.total_pnl, 2),
            "win_rate": round(self.win_rate, 2),
            "profit_factor": round(pf, 4) if pf < 1000 else None,
            "max_drawdown": round(self.max_drawdown, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "current_drawdown": round(self.current_drawdown, 2),
            "current_drawdown_pct": round(self.current_drawdown_pct, 2),
            "sharpe_estimate": round(self.sharpe_estimate, 4),
            "trading_days": self.trading_days_count,
            "max_consecutive_losses": self.max_consecutive_losses,
            "consecutive_losses_current": self.consecutive_losses_current,
            "results_meaningful": self.results_are_meaningful,
            "sharpe_stable": self.sharpe_is_stable,
            "current_equity": round(self._current_equity, 2),
        }

    def print_dashboard(self) -> None:
        """Print a formatted dashboard summary to stdout."""
        stats = self.get_stats()
        pf = stats["profit_factor"]
        pf_str = f"{pf:.2f}" if pf is not None else "inf"

        W = 62
        bar = "=" * W
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        # Validation status
        valid_label = "YES" if stats["results_meaningful"] else f"NO (need {MIN_TRADES_FOR_SIGNIFICANCE} trades)"
        sharpe_label = "YES" if stats["sharpe_stable"] else f"NO (need {MIN_TRADING_DAYS_FOR_SHARPE} days)"

        lines = [
            "",
            bar,
            f"  PAPER TRADING MONITOR  {now_str}",
            bar,
            "",
            "  RUNNING STATISTICS",
            "  " + "-" * 58,
            f"  Trades:          {stats['trade_count']}  ({stats['wins']}W / {stats['losses']}L)",
            f"  Total PnL:       ${stats['total_pnl']:+.2f}",
            f"  Win Rate:        {stats['win_rate']:.1f}%",
            f"  Profit Factor:   {pf_str}",
            f"  Sharpe Estimate: {stats['sharpe_estimate']:.2f}",
            f"  Equity:          ${stats['current_equity']:,.2f}",
            "",
            "  DRAWDOWN",
            "  " + "-" * 58,
            f"  Current:         ${stats['current_drawdown']:.2f}  ({stats['current_drawdown_pct']:.2f}%)",
            f"  Maximum:         ${stats['max_drawdown']:.2f}  ({stats['max_drawdown_pct']:.2f}%)",
            f"  Max Consec Loss: {stats['max_consecutive_losses']}  (current: {stats['consecutive_losses_current']})",
            "",
            "  STATISTICAL VALIDATION",
            "  " + "-" * 58,
            f"  Results meaningful:  {valid_label}",
            f"  Sharpe stable:       {sharpe_label}",
            f"  Trading days:        {stats['trading_days']}",
            "",
            bar,
        ]
        print("\n".join(lines))
