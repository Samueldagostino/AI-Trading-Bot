"""
Tests for Paper Trading Infrastructure
========================================
7 tests covering preflight, journal, and statistical monitor.
"""

import json
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add project paths
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR.parent / "scripts"))

# Import after path setup
from scripts.preflight_check import PreflightCheck  # noqa: E402


class TestPreflightEnvironmentChecks:
    """Test preflight environment validation."""

    def test_preflight_environment_checks(self):
        """Mock environment, verify pass/fail for basic checks."""
        checker = PreflightCheck()
        checker.check_python_version()
        checker.check_packages()

        # Python version should pass (we're running 3.10+)
        python_results = [r for r in checker.results if "Python" in r[2]]
        assert len(python_results) == 1
        assert python_results[0][1] == "PASS"

    def test_preflight_gateway_unreachable(self):
        """Mock timeout, verify BLOCKED verdict when gateway is unreachable."""
        checker = PreflightCheck()

        # Point to a port that's definitely not running IBKR gateway
        with patch.dict(os.environ, {"IBKR_GATEWAY_HOST": "127.0.0.1",
                                      "IBKR_GATEWAY_PORT": "59999"}):
            result = checker.check_gateway_reachable()

        assert result is False
        gateway_results = [r for r in checker.results if r[0] == "IBKR GATEWAY"]
        assert any(r[1] == "FAIL" for r in gateway_results)

    def test_preflight_contract_in_roll_window(self):
        """Verify WARNING when contract is close to expiry."""
        checker = PreflightCheck()

        # Simulate a contract expiring in 3 days
        today = datetime.now(timezone.utc).date()
        from datetime import timedelta
        near_expiry = (today + timedelta(days=3)).strftime("%Y%m%d")

        checker._check_rollover(near_expiry)

        warnings = [r for r in checker.results if r[1] == "WARN"]
        assert len(warnings) >= 1
        assert any("ROLL" in r[2].upper() or "expire" in r[2].lower() for r in warnings)


class TestPaperTradingJournal:
    """Test the paper trading journal."""

    def test_journal_trade_capture(self):
        """Mock trade, verify all fields written."""
        # Import here to avoid issues with path
        from scripts.paper_trading_journal import PaperTradingJournal, TradeRecord

        with tempfile.TemporaryDirectory() as tmpdir:
            journal = PaperTradingJournal(logs_dir=Path(tmpdir))

            trade = TradeRecord(
                trade_id=1,
                entry_timestamp="2026-03-06T10:00:00",
                entry_price=20100.50,
                direction="long",
                contracts=2,
                signal_source="sweep",
                hc_score=0.82,
                htf_bias="bullish",
                atr_at_entry=12.5,
                session="opening",
                stop_distance=18.0,
                c1_pnl=6.50,
                c2_pnl=24.00,
                total_pnl=30.50,
                duration_bars=8,
                mfe_pts=15.2,
                mae_pts=3.1,
                entry_slippage_pts=0.5,
            )
            journal.record_trade(trade)

            # Verify file was written
            trades = journal.get_all_trades()
            assert len(trades) == 1
            t = trades[0]
            assert t["trade_id"] == 1
            assert t["entry_price"] == 20100.50
            assert t["direction"] == "long"
            assert t["signal_source"] == "sweep"
            assert t["hc_score"] == 0.82
            assert t["session"] == "opening"
            assert t["total_pnl"] == 30.50
            assert t["mfe_pts"] == 15.2

    def test_journal_daily_summary(self):
        """Verify CSV row generation from journal data."""
        from scripts.paper_trading_journal import PaperTradingJournal, TradeRecord

        with tempfile.TemporaryDirectory() as tmpdir:
            journal = PaperTradingJournal(logs_dir=Path(tmpdir))

            # Add some trades
            for i, pnl in enumerate([10.0, -5.0, 15.0, -8.0, 20.0]):
                trade = TradeRecord(
                    trade_id=i + 1,
                    total_pnl=pnl,
                    session="opening" if i < 2 else "midday",
                    entry_slippage_pts=0.5,
                )
                journal.record_trade(trade)

            summary = journal.generate_daily_summary()
            assert summary is not None
            assert summary["trades"] == 5
            assert summary["wins"] == 3
            assert summary["losses"] == 2
            assert summary["pnl"] == 32.0  # 10 - 5 + 15 - 8 + 20
            assert summary["avg_slippage"] == 0.5

            # Test CSV write
            journal.append_summary_csv(summary)
            csv_path = journal._summary_path
            assert csv_path.exists()
            content = csv_path.read_text()
            assert "date" in content  # header
            assert "32.0" in content  # pnl value


class TestPaperVsBacktestMonitor:
    """Test the statistical comparison monitor."""

    def test_monitor_within_tolerance(self):
        """Mock 50 trades at WR=55%, verify CONTINUE verdict."""
        from scripts.paper_vs_backtest_monitor import (
            Baseline, compute_metrics, evaluate_status
        )

        # Create 50 mock trades: 27 wins, 23 losses (54% WR)
        trades = []
        for i in range(50):
            pnl = 15.0 if i < 27 else -12.0
            trades.append({"total_pnl": pnl, "entry_slippage_pts": 0.5})

        baseline = Baseline(
            profit_factor=1.53,
            win_rate_pct=58.3,
            avg_pnl_per_trade=11.25,
        )

        metrics = compute_metrics(trades)
        status = evaluate_status(metrics, baseline)

        assert metrics["total_trades"] == 50
        assert metrics["wins"] == 27
        assert 53 < metrics["win_rate"] < 55  # ~54%

        # WR=54% is within 8% tolerance of 58.3% baseline at n<100
        assert status["details"]["win_rate"] == "WITHIN TOLERANCE"
        assert status["verdict"] == "CONTINUE PAPER TRADING"

    def test_monitor_halt_signal(self):
        """Mock 100 trades at PF=0.7, verify HALT verdict."""
        from scripts.paper_vs_backtest_monitor import (
            Baseline, compute_metrics, evaluate_status
        )

        # Create 100 trades with PF ~0.7: 40 wins avg $8, 60 losses avg $7.6
        # gross_win = 40 * 8 = 320, gross_loss = 60 * 7.6 = 456, PF = 320/456 = 0.70
        trades = []
        for i in range(100):
            if i < 40:
                pnl = 8.0
            else:
                pnl = -7.6
            trades.append({"total_pnl": pnl, "entry_slippage_pts": 0.8})

        baseline = Baseline(
            profit_factor=1.53,
            win_rate_pct=58.3,
            avg_pnl_per_trade=11.25,
        )

        metrics = compute_metrics(trades)
        status = evaluate_status(metrics, baseline)

        assert metrics["total_trades"] == 100
        assert 0.69 < metrics["profit_factor"] < 0.72

        # PF < 0.8 at n >= 50 → HALT AND DIAGNOSE
        assert "HALT" in status["details"]["profit_factor"]
        assert status["verdict"] == "HALT AND DIAGNOSE"
