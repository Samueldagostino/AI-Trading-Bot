"""
Tests for PaperTradingMonitor
================================
Covers:
  - Running statistics calculation (PnL, win rate, Sharpe, drawdown, etc.)
  - State persistence to JSON
  - Statistical validation thresholds (100 trades, 20 trading days)
  - Consecutive loss tracking
  - Dashboard rendering
"""

import json
import os
import tempfile
import pytest
from datetime import datetime, timezone

from scripts.paper_trading_monitor import (
    PaperTradingMonitor,
    PaperTradeRecord,
    MIN_TRADES_FOR_SIGNIFICANCE,
    MIN_TRADING_DAYS_FOR_SHARPE,
)


# =====================================================================
#  BASIC STATISTICS
# =====================================================================
class TestRunningStatistics:

    def test_empty_monitor(self):
        """Fresh monitor has zero everything."""
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        assert monitor.trade_count == 0
        assert monitor.wins == 0
        assert monitor.losses == 0
        assert monitor.total_pnl == 0.0
        assert monitor.win_rate == 0.0
        assert monitor.profit_factor == 0.0
        assert monitor.max_drawdown == 0.0
        assert monitor.current_drawdown == 0.0
        assert monitor.sharpe_estimate == 0.0

    def test_single_winning_trade(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp(), account_size=50000.0)
        monitor.record_trade(pnl=100.0, direction="long")
        assert monitor.trade_count == 1
        assert monitor.wins == 1
        assert monitor.losses == 0
        assert monitor.total_pnl == 100.0
        assert monitor.win_rate == 100.0
        assert monitor.max_drawdown == 0.0

    def test_single_losing_trade(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp(), account_size=50000.0)
        monitor.record_trade(pnl=-50.0, direction="short")
        assert monitor.trade_count == 1
        assert monitor.wins == 0
        assert monitor.losses == 1
        assert monitor.total_pnl == -50.0
        assert monitor.win_rate == 0.0
        assert monitor.max_drawdown == 50.0

    def test_mixed_trades(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp(), account_size=50000.0)
        monitor.record_trade(pnl=100.0, direction="long")
        monitor.record_trade(pnl=-50.0, direction="short")
        monitor.record_trade(pnl=75.0, direction="long")
        monitor.record_trade(pnl=-25.0, direction="short")

        assert monitor.trade_count == 4
        assert monitor.wins == 2
        assert monitor.losses == 2
        assert monitor.total_pnl == 100.0  # 100-50+75-25
        assert monitor.win_rate == 50.0

    def test_profit_factor(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        monitor.record_trade(pnl=200.0, direction="long")   # gross profit: 200
        monitor.record_trade(pnl=-100.0, direction="short")  # gross loss: 100
        assert monitor.profit_factor == 2.0  # 200/100

    def test_profit_factor_no_losses(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        monitor.record_trade(pnl=100.0, direction="long")
        assert monitor.profit_factor == float("inf")

    def test_profit_factor_no_wins(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        monitor.record_trade(pnl=-100.0, direction="short")
        assert monitor.profit_factor == 0.0


# =====================================================================
#  DRAWDOWN
# =====================================================================
class TestDrawdown:

    def test_drawdown_from_peak(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp(), account_size=50000.0)
        monitor.record_trade(pnl=200.0, direction="long")   # equity: 50200
        monitor.record_trade(pnl=-150.0, direction="short")  # equity: 50050
        # Peak was 50200, current 50050, drawdown = 150
        assert monitor.current_drawdown == 150.0
        assert monitor.max_drawdown == 150.0

    def test_drawdown_recovers(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp(), account_size=50000.0)
        monitor.record_trade(pnl=200.0, direction="long")   # equity: 50200 (peak)
        monitor.record_trade(pnl=-150.0, direction="short")  # equity: 50050
        monitor.record_trade(pnl=200.0, direction="long")   # equity: 50250 (new peak)
        assert monitor.current_drawdown == 0.0  # Recovered
        assert monitor.max_drawdown == 150.0    # Historic max remains

    def test_drawdown_percentage(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp(), account_size=10000.0)
        monitor.record_trade(pnl=-500.0, direction="short")
        # Drawdown of 500 on 10000 account = 5%
        assert monitor.max_drawdown_pct == 5.0

    def test_deepening_drawdown(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp(), account_size=50000.0)
        monitor.record_trade(pnl=-100.0, direction="short")
        assert monitor.max_drawdown == 100.0
        monitor.record_trade(pnl=-200.0, direction="short")
        assert monitor.max_drawdown == 300.0  # 100+200 total drawdown


# =====================================================================
#  CONSECUTIVE LOSSES
# =====================================================================
class TestConsecutiveLosses:

    def test_consecutive_loss_tracking(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        monitor.record_trade(pnl=-50.0, direction="short")
        monitor.record_trade(pnl=-50.0, direction="short")
        monitor.record_trade(pnl=-50.0, direction="short")
        assert monitor.consecutive_losses_current == 3
        assert monitor.max_consecutive_losses == 3

    def test_win_resets_consecutive(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        monitor.record_trade(pnl=-50.0, direction="short")
        monitor.record_trade(pnl=-50.0, direction="short")
        monitor.record_trade(pnl=100.0, direction="long")
        assert monitor.consecutive_losses_current == 0
        assert monitor.max_consecutive_losses == 2

    def test_max_consecutive_tracked_across_streaks(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        # First streak: 3 losses
        for _ in range(3):
            monitor.record_trade(pnl=-50.0, direction="short")
        monitor.record_trade(pnl=100.0, direction="long")  # Reset
        # Second streak: 5 losses
        for _ in range(5):
            monitor.record_trade(pnl=-50.0, direction="short")
        monitor.record_trade(pnl=100.0, direction="long")  # Reset
        # Third streak: 2 losses
        for _ in range(2):
            monitor.record_trade(pnl=-50.0, direction="short")

        assert monitor.consecutive_losses_current == 2
        assert monitor.max_consecutive_losses == 5


# =====================================================================
#  SHARPE ESTIMATE
# =====================================================================
class TestSharpeEstimate:

    def test_sharpe_with_no_trades(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        assert monitor.sharpe_estimate == 0.0

    def test_sharpe_with_one_day(self):
        """Single day means only 1 daily return — can't compute std."""
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        monitor.record_trade(pnl=100.0, direction="long")
        # Only 1 daily PnL entry, need at least 2
        assert monitor.sharpe_estimate == 0.0

    def test_sharpe_positive_with_consistent_wins(self):
        """Mostly positive days with variance should give positive Sharpe."""
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        # Simulate 5 trading days with varying positive PnL (need variance)
        daily_pnls = [80.0, 120.0, 90.0, 110.0, 100.0]
        for day, pnl in enumerate(daily_pnls, 1):
            date_str = f"2026-03-{day:02d}"
            monitor._daily_pnls[date_str] = pnl
            monitor._trading_days.add(date_str)
        assert monitor.sharpe_estimate > 0


# =====================================================================
#  STATISTICAL VALIDATION THRESHOLDS
# =====================================================================
class TestStatisticalValidation:

    def test_not_meaningful_under_100_trades(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        for _ in range(99):
            monitor.record_trade(pnl=10.0, direction="long")
        assert not monitor.results_are_meaningful

    def test_meaningful_at_100_trades(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        for _ in range(100):
            monitor.record_trade(pnl=10.0, direction="long")
        assert monitor.results_are_meaningful

    def test_sharpe_not_stable_under_20_days(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        for day in range(1, 20):
            date_str = f"2026-03-{day:02d}"
            monitor._trading_days.add(date_str)
        assert not monitor.sharpe_is_stable

    def test_sharpe_stable_at_20_days(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        for day in range(1, 21):
            date_str = f"2026-03-{day:02d}"
            monitor._trading_days.add(date_str)
        assert monitor.sharpe_is_stable

    def test_min_trades_constant(self):
        assert MIN_TRADES_FOR_SIGNIFICANCE == 100

    def test_min_days_constant(self):
        assert MIN_TRADING_DAYS_FOR_SHARPE == 20


# =====================================================================
#  STATE PERSISTENCE
# =====================================================================
class TestStatePersistence:

    def test_state_written_to_json(self):
        tmpdir = tempfile.mkdtemp()
        monitor = PaperTradingMonitor(log_dir=tmpdir, account_size=50000.0)
        monitor.record_trade(pnl=100.0, direction="long")
        monitor.record_trade(pnl=-50.0, direction="short")
        monitor.save_state()

        state_path = os.path.join(tmpdir, "paper_trading_state.json")
        assert os.path.exists(state_path)

        with open(state_path) as f:
            state = json.load(f)

        assert state["trade_count"] == 2
        assert state["wins"] == 1
        assert state["losses"] == 1
        assert state["total_pnl"] == 50.0
        assert state["win_rate"] == 50.0
        assert state["current_equity"] == 50050.0
        assert "last_updated" in state
        assert "daily_pnls" in state

    def test_trade_log_written(self):
        tmpdir = tempfile.mkdtemp()
        monitor = PaperTradingMonitor(log_dir=tmpdir)
        monitor.record_trade(pnl=100.0, direction="long", entry_price=20000.0)

        trades_path = os.path.join(tmpdir, "paper_trades.json")
        assert os.path.exists(trades_path)

        with open(trades_path) as f:
            line = f.readline()
            trade = json.loads(line)

        assert trade["pnl"] == 100.0
        assert trade["direction"] == "long"
        assert trade["entry_price"] == 20000.0

    def test_state_loaded_on_init(self):
        """State is loaded from disk when monitor is re-initialized."""
        tmpdir = tempfile.mkdtemp()

        # First monitor writes state
        m1 = PaperTradingMonitor(log_dir=tmpdir, account_size=50000.0)
        m1.record_trade(pnl=200.0, direction="long")
        m1.record_trade(pnl=-50.0, direction="short")
        m1.save_state()

        # Second monitor loads state
        m2 = PaperTradingMonitor(log_dir=tmpdir, account_size=50000.0)
        assert m2._current_equity == 50150.0  # 50000 + 200 - 50
        assert m2._peak_equity == 50200.0
        assert m2._max_drawdown == 50.0

    def test_state_handles_missing_file(self):
        """No crash when state file doesn't exist."""
        tmpdir = tempfile.mkdtemp()
        monitor = PaperTradingMonitor(log_dir=tmpdir)
        assert monitor.trade_count == 0  # Starts fresh

    def test_state_handles_corrupt_json(self):
        """Gracefully handles corrupt state file."""
        tmpdir = tempfile.mkdtemp()
        state_path = os.path.join(tmpdir, "paper_trading_state.json")
        with open(state_path, "w") as f:
            f.write("not valid json {{{")

        monitor = PaperTradingMonitor(log_dir=tmpdir)
        assert monitor.trade_count == 0  # Falls back to defaults


# =====================================================================
#  GET_STATS AND DASHBOARD
# =====================================================================
class TestStatsAndDashboard:

    def test_get_stats_returns_all_fields(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        monitor.record_trade(pnl=100.0, direction="long")

        stats = monitor.get_stats()
        expected_keys = [
            "trade_count", "wins", "losses", "total_pnl", "win_rate",
            "profit_factor", "max_drawdown", "max_drawdown_pct",
            "current_drawdown", "current_drawdown_pct", "sharpe_estimate",
            "trading_days", "max_consecutive_losses",
            "consecutive_losses_current", "results_meaningful",
            "sharpe_stable", "current_equity",
        ]
        for key in expected_keys:
            assert key in stats, f"Missing key: {key}"

    def test_print_dashboard_no_crash(self, capsys):
        """Dashboard prints without errors."""
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        monitor.record_trade(pnl=100.0, direction="long")
        monitor.record_trade(pnl=-50.0, direction="short")
        monitor.print_dashboard()

        captured = capsys.readouterr()
        assert "PAPER TRADING MONITOR" in captured.out
        assert "RUNNING STATISTICS" in captured.out
        assert "DRAWDOWN" in captured.out
        assert "STATISTICAL VALIDATION" in captured.out

    def test_dashboard_shows_validation_status(self, capsys):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        monitor.print_dashboard()

        captured = capsys.readouterr()
        assert f"need {MIN_TRADES_FOR_SIGNIFICANCE} trades" in captured.out


# =====================================================================
#  TRADE RECORD
# =====================================================================
class TestPaperTradeRecord:

    def test_to_dict(self):
        record = PaperTradeRecord(
            timestamp="2026-03-04T10:00:00Z",
            direction="long",
            pnl=100.0,
            entry_price=20000.0,
            exit_price=20050.0,
            signal_score=0.85,
            regime="normal",
        )
        d = record.to_dict()
        assert d["pnl"] == 100.0
        assert d["direction"] == "long"
        assert d["entry_price"] == 20000.0
        assert d["signal_score"] == 0.85

    def test_default_values(self):
        record = PaperTradeRecord(
            timestamp="2026-03-04T10:00:00Z",
            direction="short",
            pnl=-50.0,
        )
        assert record.contracts == 2
        assert record.metadata == {}
        assert record.c1_pnl == 0.0
        assert record.c2_pnl == 0.0


# =====================================================================
#  TRADING DAYS TRACKING
# =====================================================================
class TestTradingDays:

    def test_trading_days_counted(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        monitor.record_trade(pnl=100.0, direction="long")
        assert monitor.trading_days_count >= 1

    def test_daily_pnls_accumulated(self):
        monitor = PaperTradingMonitor(log_dir=tempfile.mkdtemp())
        monitor.record_trade(pnl=100.0, direction="long")
        monitor.record_trade(pnl=-50.0, direction="short")
        # Both recorded today
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert today in monitor._daily_pnls
        assert monitor._daily_pnls[today] == 50.0  # 100 - 50


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
