"""
Tests for terminal monitoring dashboard in scripts/run_ibkr.py.

Covers:
  - Session boundary computation (RTH/ETH transitions)
  - Dashboard rendering (no crashes, correct fields)
  - Dashboard timer integration
"""

import pytest
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("IBKR_GATEWAY_HOST", "localhost")
os.environ.setdefault("IBKR_GATEWAY_PORT", "5000")
os.environ.setdefault("IBKR_ACCOUNT_TYPE", "paper")

from scripts.run_ibkr import IBKRLiveRunner
from Broker.ibkr_client_portal import IBKRConfig, SessionType
from Broker.ibkr_client_portal import ET_TZ as ET_OFFSET
from Broker.order_executor import ExecutorConfig


# ═══════════════════════════════════════════════════════════════
# SESSION BOUNDARY CALCULATION
# ═══════════════════════════════════════════════════════════════

class TestSessionBoundary:
    """_next_session_boundary() returns correct label and countdown."""

    def _et(self, hour: int, minute: int = 0) -> datetime:
        """Build a datetime in ET timezone."""
        return datetime(2026, 3, 1, hour, minute, 0, tzinfo=ET_OFFSET)

    def test_during_rth_points_to_close(self):
        et = self._et(10, 30)  # 10:30 ET, RTH
        label, delta = IBKRLiveRunner._next_session_boundary(et)
        assert label == "RTH close"
        assert "5h 30m" == delta

    def test_rth_open_boundary(self):
        et = self._et(9, 30)  # Exactly 09:30 ET = RTH open
        label, delta = IBKRLiveRunner._next_session_boundary(et)
        assert label == "RTH close"
        assert "6h 30m" == delta

    def test_just_before_rth_open(self):
        et = self._et(9, 0)  # 09:00 ET, before RTH
        label, delta = IBKRLiveRunner._next_session_boundary(et)
        assert label == "RTH open"
        assert "30m" == delta

    def test_premarket_morning(self):
        et = self._et(7, 0)  # 07:00 ET
        label, delta = IBKRLiveRunner._next_session_boundary(et)
        assert label == "RTH open"
        assert "2h 30m" == delta

    def test_after_rth_close(self):
        et = self._et(16, 30)  # 16:30 ET, after RTH
        label, delta = IBKRLiveRunner._next_session_boundary(et)
        assert label == "RTH open"
        # Next day 09:30 - today 16:30 = 17h
        assert "17h 00m" == delta

    def test_evening_eth(self):
        et = self._et(20, 0)  # 20:00 ET
        label, delta = IBKRLiveRunner._next_session_boundary(et)
        assert label == "RTH open"
        # Next day 09:30 - 20:00 = 13h 30m
        assert "13h 30m" == delta

    def test_just_before_close(self):
        et = self._et(15, 58)  # 15:58 ET, 2 min to close
        label, delta = IBKRLiveRunner._next_session_boundary(et)
        assert label == "RTH close"
        assert "2m" == delta

    def test_midnight(self):
        et = self._et(0, 0)  # Midnight ET
        label, delta = IBKRLiveRunner._next_session_boundary(et)
        assert label == "RTH open"
        assert "9h 30m" == delta


# ═══════════════════════════════════════════════════════════════
# DASHBOARD RENDERING
# ═══════════════════════════════════════════════════════════════

def _make_mock_pipeline(
    has_position: bool = False,
    daily_pnl: float = 0.0,
    trade_count: int = 0,
    halted: bool = False,
):
    """Build a mock pipeline with all the status APIs the dashboard reads."""
    pipeline = MagicMock()

    # Executor status
    pipeline._executor.get_status.return_value = {
        "paper_mode": True,
        "is_halted": halted,
        "halt_reason": "test halt" if halted else "",
        "daily_pnl": daily_pnl,
        "daily_trades": trade_count,
        "daily_blocked": 3,
        "open_positions": 2 if has_position else 0,
        "allow_eth": False,
    }

    # Position manager status
    pipeline._position_manager.get_status.return_value = {
        "open_positions": 2 if has_position else 0,
        "daily_realized_pnl": daily_pnl,
        "trade_count": trade_count,
        "closed_positions": trade_count,
        "scale_out_groups": 1 if has_position else 0,
        "last_recon_matched": True,
        "last_recon_time": "2026-03-01T14:30:00+00:00",
        "recon_loop_active": True,
    }

    # Open positions dict
    if has_position:
        pos1 = MagicMock()
        pos1.side.value = "LONG"
        pos1.contracts = 1
        pos1.entry_price = 21000.0
        pos1.tag = "C1"
        pos2 = MagicMock()
        pos2.side.value = "LONG"
        pos2.contracts = 1
        pos2.entry_price = 21000.0
        pos2.tag = "C2"
        pipeline._position_manager.open_positions = {
            "G1-C1": pos1,
            "G1-C2": pos2,
        }
    else:
        pipeline._position_manager.open_positions = {}

    # Unrealized PnL
    pipeline._position_manager.get_unrealized_pnl.return_value = 12.50

    # Closed positions (for win/loss counting)
    closed_win = MagicMock()
    closed_win.net_pnl = 25.0
    closed_loss = MagicMock()
    closed_loss.net_pnl = -15.0
    pipeline._position_manager._closed_positions = [
        closed_win, closed_loss
    ] if trade_count >= 2 else []

    # Signal bridge
    pipeline._bridge.rejections = 7
    pipeline._bridge.translations = 3

    # Signal aggregator
    pipeline._signal_aggregator.get_signal_stats.return_value = {
        "total_signals_evaluated": 100,
        "trade_signals_generated": 10,
        "htf_blocked_signals": 22,
        "htf_block_rate": 22.0,
    }

    # Client status
    pipeline._client.get_status.return_value = {
        "connected": True,
        "session_valid": True,
    }

    # Data feed status
    pipeline._data_feed.get_status.return_value = {
        "running": True,
        "data_mode": "websocket",
    }

    # Last bar
    last_bar = MagicMock()
    last_bar.close = 21050.0
    pipeline._last_bar = last_bar

    return pipeline


class TestDashboardRender:
    """Dashboard prints without crashing and shows correct data."""

    def _make_runner(self) -> IBKRLiveRunner:
        config = IBKRConfig()
        exec_cfg = ExecutorConfig()
        return IBKRLiveRunner(config, exec_cfg, dry_run=False)

    def test_dashboard_flat_no_crash(self, capsys):
        runner = self._make_runner()
        runner._pipeline = _make_mock_pipeline(has_position=False)
        runner._warmup_complete = True

        runner._print_dashboard()
        out = capsys.readouterr().out

        assert "IBKR DASHBOARD" in out
        assert "FLAT" in out
        assert "waiting for signal" in out

    def test_dashboard_with_position(self, capsys):
        runner = self._make_runner()
        runner._pipeline = _make_mock_pipeline(
            has_position=True, daily_pnl=42.0, trade_count=2
        )

        runner._print_dashboard()
        out = capsys.readouterr().out

        assert "LONG 2x" in out
        assert "21000.00" in out
        assert "C1, C2" in out

    def test_dashboard_shows_pnl(self, capsys):
        runner = self._make_runner()
        runner._pipeline = _make_mock_pipeline(daily_pnl=42.0)

        runner._print_dashboard()
        out = capsys.readouterr().out

        assert "$+42.00" in out
        assert "Unrealized" in out
        assert "Net:" in out

    def test_dashboard_shows_trades(self, capsys):
        runner = self._make_runner()
        runner._pipeline = _make_mock_pipeline(trade_count=2)

        runner._print_dashboard()
        out = capsys.readouterr().out

        assert "2 today" in out
        assert "1W / 1L" in out

    def test_dashboard_shows_blocks(self, capsys):
        runner = self._make_runner()
        runner._pipeline = _make_mock_pipeline()

        runner._print_dashboard()
        out = capsys.readouterr().out

        assert "7 HC" in out
        assert "22 HTF" in out
        assert "3 executor" in out

    def test_dashboard_shows_connection(self, capsys):
        runner = self._make_runner()
        runner._pipeline = _make_mock_pipeline()

        runner._print_dashboard()
        out = capsys.readouterr().out

        assert "Gateway: OK" in out
        assert "Feed: websocket" in out
        assert "Recon: OK" in out
        assert "Halted: No" in out

    def test_dashboard_shows_halted(self, capsys):
        runner = self._make_runner()
        runner._pipeline = _make_mock_pipeline(halted=True)

        runner._print_dashboard()
        out = capsys.readouterr().out

        assert "Halted: YES" in out
        assert "test halt" in out

    def test_dashboard_shows_session_boundary(self, capsys):
        runner = self._make_runner()
        runner._pipeline = _make_mock_pipeline()

        runner._print_dashboard()
        out = capsys.readouterr().out

        # Should show either "RTH close in" or "RTH open in"
        assert "RTH" in out
        assert " in " in out

    def test_dashboard_shows_session_type(self, capsys):
        runner = self._make_runner()
        runner._pipeline = _make_mock_pipeline()

        runner._print_dashboard()
        out = capsys.readouterr().out

        # Must show RTH or ETH
        assert "Session:" in out
        assert ("RTH" in out or "ETH" in out)

    def test_dashboard_gateway_down(self, capsys):
        runner = self._make_runner()
        runner._pipeline = _make_mock_pipeline()
        runner._pipeline._client.get_status.return_value = {
            "connected": False,
            "session_valid": False,
        }

        runner._print_dashboard()
        out = capsys.readouterr().out

        assert "Gateway: DOWN" in out

    def test_no_pipeline_no_crash(self):
        runner = self._make_runner()
        runner._pipeline = None
        # Should return silently
        runner._print_dashboard()


# ═══════════════════════════════════════════════════════════════
# DASHBOARD TIMER
# ═══════════════════════════════════════════════════════════════

class TestDashboardTimer:
    """Dashboard fires every DASHBOARD_INTERVAL_SECONDS."""

    def test_interval_is_120_seconds(self):
        assert IBKRLiveRunner.DASHBOARD_INTERVAL_SECONDS == 120

    def test_initial_timer_is_zero(self):
        config = IBKRConfig()
        exec_cfg = ExecutorConfig()
        runner = IBKRLiveRunner(config, exec_cfg)
        assert runner._last_dashboard_time == 0.0
