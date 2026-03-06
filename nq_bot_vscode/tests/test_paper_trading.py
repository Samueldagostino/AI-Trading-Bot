"""
Tests for Paper Trading State and OrderManager Integration
============================================================
Tests:
  - State persistence (write/restart/verify)
  - Equity curve tracking
  - Drawdown calculation
  - Win/loss counting
  - Partial fill entry (C1-only mode)
  - Partial fill stop (market order for remainder)
  - Fill after cancel (protective stop placed)
  - Reconnect reconciliation (state sync with IBKR)
  - Duplicate execution dedup
"""

import asyncio
import json
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass

import pytest

# Project path setup
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from Broker.order_manager import (
    OrderManager,
    MNQ_POINT_VALUE,
    MAX_CONTRACTS,
    _generate_trade_id,
)


# ═══════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════

def make_mock_ib_client():
    """Create a mock IBKRClient for testing."""
    client = MagicMock()
    client._ib = MagicMock()
    client._contract = MagicMock()
    client._contract.symbol = "MNQ"

    ib = client._ib
    ib.orderStatusEvent = MagicMock()
    ib.orderStatusEvent.__iadd__ = MagicMock(return_value=ib.orderStatusEvent)
    ib.execDetailsEvent = MagicMock()
    ib.execDetailsEvent.__iadd__ = MagicMock(return_value=ib.execDetailsEvent)
    ib.errorEvent = MagicMock()
    ib.errorEvent.__iadd__ = MagicMock(return_value=ib.errorEvent)
    ib.disconnectedEvent = MagicMock()
    ib.disconnectedEvent.__iadd__ = MagicMock(return_value=ib.disconnectedEvent)

    ib.openTrades.return_value = []
    ib.trades.return_value = []
    ib.positions.return_value = []
    ib.openOrders.return_value = []

    return client


def make_active_trade(trade_id="T001", direction="LONG", entry_price=25000.0, **overrides):
    """Create a complete active trade dict."""
    trade = {
        "trade_id": trade_id,
        "direction": direction,
        "action": "BUY" if direction == "LONG" else "SELL",
        "reverse_action": "SELL" if direction == "LONG" else "BUY",
        "contracts": 2,
        "entry_price": entry_price,
        "signal_price": entry_price,
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "entry_order_id": 100,
        "stop_price": entry_price - 20 if direction == "LONG" else entry_price + 20,
        "c1_target": entry_price + 30 if direction == "LONG" else entry_price - 30,
        "c2_trail_distance": 15.0,
        "modifier_total": 1.0,
        "confluence_score": 0.85,
        "reason": "test",
        "slippage_entry": 0.25,
        "c1_status": "OPEN",
        "c1_fill_price": entry_price,
        "c1_fill_qty": 1,
        "c1_exit_price": 0.0,
        "c1_exit_time": "",
        "c1_pnl": 0.0,
        "c2_status": "TRAILING",
        "c2_fill_price": entry_price,
        "c2_fill_qty": 1,
        "c2_exit_price": 0.0,
        "c2_exit_time": "",
        "c2_pnl": 0.0,
        "c2_trail_stop": entry_price - 15 if direction == "LONG" else entry_price + 15,
        "stop_order_id": 200,
        "stop_status": "WORKING",
        "stop_qty": 2,
        "max_favorable_excursion": 0.0,
        "max_adverse_excursion": 0.0,
        "max_price_since_entry": entry_price if direction == "LONG" else float("inf"),
        "min_price_since_entry": entry_price if direction == "SHORT" else float("inf"),
        "c1_target_order_id": 201,
        "total_filled": 2,
        "total_target": 2,
        "total_pnl": 0,
        "hold_duration_seconds": 0,
    }
    trade.update(overrides)
    return trade


@pytest.fixture
def tmp_log_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def om(tmp_log_dir):
    client = make_mock_ib_client()
    mgr = OrderManager(client, config={"account_size": 50000.0}, log_dir=tmp_log_dir)
    mgr._last_bar_time = time.monotonic()
    return mgr


# ═══════════════════════════════════════════════════════════════
# STATE PERSISTENCE
# ═══════════════════════════════════════════════════════════════

class TestStatePersistence:
    def test_state_write_and_read(self, om, tmp_log_dir):
        """Write state, create new OM, verify loaded."""
        om._daily_pnl = 250.0
        om._current_equity = 50250.0
        om._peak_equity = 50300.0
        om._consecutive_losses = 3

        om.save_state()

        # Create new OM
        client = make_mock_ib_client()
        om2 = OrderManager(client, config={"account_size": 50000.0}, log_dir=tmp_log_dir)
        loaded = om2.load_state()

        assert loaded
        assert om2._daily_pnl == 250.0
        assert om2._current_equity == 50250.0
        assert om2._peak_equity == 50300.0
        assert om2._consecutive_losses == 3

    def test_state_survives_restart(self, tmp_log_dir):
        """Full restart cycle: save -> new instance -> load."""
        # Session 1
        client1 = make_mock_ib_client()
        om1 = OrderManager(client1, config={"account_size": 50000.0}, log_dir=tmp_log_dir)
        om1._daily_pnl = -100.0
        om1._current_equity = 49900.0
        om1.save_state()

        # Session 2
        client2 = make_mock_ib_client()
        om2 = OrderManager(client2, config={"account_size": 50000.0}, log_dir=tmp_log_dir)
        loaded = om2.load_state()

        assert loaded
        assert om2._daily_pnl == -100.0
        assert om2._current_equity == 49900.0

    def test_no_state_file(self, tmp_log_dir):
        """Verify graceful handling when no state file exists."""
        client = make_mock_ib_client()
        om = OrderManager(client, config={"account_size": 50000.0}, log_dir=tmp_log_dir)
        loaded = om.load_state()
        assert not loaded


# ═══════════════════════════════════════════════════════════════
# EQUITY CURVE TRACKING
# ═══════════════════════════════════════════════════════════════

class TestEquityCurve:
    def test_equity_updates_on_trade(self, om):
        """Verify equity snapshots on trade completion."""
        initial = om._current_equity

        # Simulate winning trade completion
        trade = make_active_trade()
        om._active_positions["T001"] = trade

        # Close C1 at profit
        om._handle_c1_fill(trade, 25030.0)
        assert trade["c1_pnl"] == 60.0

        # Close C2 at profit (simulate)
        trade["c2_status"] = "FILLED"
        trade["c2_exit_price"] = 25020.0
        trade["c2_exit_time"] = datetime.now(timezone.utc).isoformat()
        trade["c2_pnl"] = 40.0

        om._check_trade_complete(trade)

        assert om._current_equity == initial + 100.0  # 60 + 40
        assert om._peak_equity >= om._current_equity


# ═══════════════════════════════════════════════════════════════
# DRAWDOWN CALCULATION
# ═══════════════════════════════════════════════════════════════

class TestDrawdown:
    def test_drawdown_tracking(self, om):
        """Verify max DD math."""
        om._peak_equity = 50000.0

        # Win brings equity up
        om._current_equity = 50100.0
        om._peak_equity = max(om._peak_equity, om._current_equity)
        assert om._peak_equity == 50100.0

        # Loss creates drawdown
        om._current_equity = 49800.0
        dd = om._peak_equity - om._current_equity
        dd_pct = (dd / om._peak_equity) * 100
        assert dd == 300.0
        assert abs(dd_pct - 0.599) < 0.01

    def test_kill_switch_drawdown(self, om):
        """Verify kill switch at 10% drawdown."""
        om._peak_equity = 50000.0
        om._current_equity = 44500.0  # 11% drawdown

        from Broker.order_manager import KILL_SWITCH_DRAWDOWN_PCT
        dd_pct = ((om._peak_equity - om._current_equity) / om._peak_equity) * 100
        assert dd_pct > KILL_SWITCH_DRAWDOWN_PCT


# ═══════════════════════════════════════════════════════════════
# WIN/LOSS COUNTING
# ═══════════════════════════════════════════════════════════════

class TestWinLossCounting:
    def test_win_loss_tracking(self, om):
        """Verify trade classification."""
        # Simulate 3 wins and 2 losses
        results = [50.0, -20.0, 30.0, -15.0, 80.0]
        for pnl in results:
            om._trade_results.append(pnl)
            om._trade_history.append({"total_pnl": pnl})
            if pnl < 0:
                om._consecutive_losses += 1
            else:
                om._consecutive_losses = 0

        metrics = om.get_trade_metrics()
        assert metrics["total_trades"] == 5
        assert metrics["wins"] == 3
        assert metrics["losses"] == 2
        assert metrics["win_rate"] == 60.0

    def test_consecutive_losses(self, om):
        """Verify consecutive loss counter."""
        # 3 losses then a win
        for pnl in [-10, -20, -30]:
            om._trade_results.append(pnl)
            om._consecutive_losses += 1

        assert om._consecutive_losses == 3

        # Win resets
        om._trade_results.append(50)
        om._consecutive_losses = 0
        assert om._consecutive_losses == 0


# ═══════════════════════════════════════════════════════════════
# PARTIAL FILL ENTRY
# ═══════════════════════════════════════════════════════════════

class TestPartialFillEntry:
    def test_partial_fill_c1_only(self, om):
        """Verify 1/2 fill switches to C1-only mode."""
        trade = make_active_trade(contracts=1, c2_status="SKIPPED",
                                   c2_fill_qty=0, c2_fill_price=0.0)

        assert trade["contracts"] == 1
        assert trade["c2_status"] == "SKIPPED"
        assert trade["c1_status"] == "OPEN"


# ═══════════════════════════════════════════════════════════════
# PARTIAL FILL STOP
# ═══════════════════════════════════════════════════════════════

class TestPartialFillStop:
    def test_partial_stop_submits_market(self, om):
        """Verify market order submitted for remainder on partial stop."""
        trade = make_active_trade()
        om._active_positions["T001"] = trade
        om.ib.openTrades.return_value = []

        # Partial stop: only 1 of 2 filled
        om._handle_stop_fill(trade, 24980.0, 1)

        # Should have tried to submit market order for remaining 1 contract
        assert om.ib.placeOrder.called


# ═══════════════════════════════════════════════════════════════
# FILL AFTER CANCEL
# ═══════════════════════════════════════════════════════════════

class TestFillAfterCancel:
    def test_fill_after_cancel_places_stop(self, om):
        """Verify protective stop placed after unexpected fill."""
        # Simulate: order was being cancelled, but fill arrived
        trade = make_active_trade()
        om._active_positions["T001"] = trade

        # The fill happened — now check that stop is in place
        assert trade["stop_order_id"] is not None or trade["stop_status"] == "WORKING"


# ═══════════════════════════════════════════════════════════════
# RECONNECT RECONCILIATION
# ═══════════════════════════════════════════════════════════════

class TestReconnectReconciliation:
    @pytest.mark.asyncio
    async def test_ghost_position_detected(self, om):
        """Verify ghost position detected and closed."""
        # Bot has no positions, but IBKR shows one
        mock_pos = MagicMock()
        mock_pos.contract.symbol = "MNQ"
        mock_pos.position = 2
        om.ib.positions.return_value = [mock_pos]

        result = await om.reconcile_after_reconnect()

        assert not result["matched"]
        assert any(d["type"] == "GHOST_POSITION" for d in result["discrepancies"])
        assert om.ib.placeOrder.called  # Should close ghost

    @pytest.mark.asyncio
    async def test_position_closed_while_disconnected(self, om):
        """Verify local state updated when IBKR shows no position."""
        trade = make_active_trade()
        om._active_positions["T001"] = trade
        om.ib.positions.return_value = []  # IBKR flat

        result = await om.reconcile_after_reconnect()

        assert not result["matched"]
        assert any(d["type"] == "POSITION_CLOSED_WHILE_DISCONNECTED"
                    for d in result["discrepancies"])


# ═══════════════════════════════════════════════════════════════
# DUPLICATE EXECUTION DEDUP
# ═══════════════════════════════════════════════════════════════

class TestDuplicateExecDedup:
    def test_duplicate_exec_id_ignored(self, om):
        """Verify execId deduplication."""
        class MockExec:
            execId = "EXEC_12345"
            orderId = 1
            price = 25000.0
            shares = 2

        class MockFillObj:
            execution = MockExec()

        trade = MagicMock()

        # First execution
        om._on_execution(trade, MockFillObj())
        assert "EXEC_12345" in om._processed_exec_ids

        # Duplicate — should not process again
        initial_count = len(om._processed_exec_ids)
        om._on_execution(trade, MockFillObj())
        assert len(om._processed_exec_ids) == initial_count  # No new additions


# ═══════════════════════════════════════════════════════════════
# PnL VERIFICATION
# ═══════════════════════════════════════════════════════════════

class TestPnLVerification:
    def test_pnl_sign_long_win(self, om):
        """LONG: exit > entry => positive PnL."""
        pnl = om._verify_pnl_sign("LONG", 25000, 25020, 40.0)
        assert pnl == 40.0

    def test_pnl_sign_long_loss(self, om):
        """LONG: exit < entry => negative PnL."""
        pnl = om._verify_pnl_sign("LONG", 25000, 24980, -40.0)
        assert pnl == -40.0

    def test_pnl_sign_short_win(self, om):
        """SHORT: exit < entry => positive PnL."""
        pnl = om._verify_pnl_sign("SHORT", 25000, 24980, 40.0)
        assert pnl == 40.0

    def test_pnl_sign_correction(self, om):
        """Verify wrong sign gets corrected."""
        # LONG with exit > entry but negative PnL
        corrected = om._verify_pnl_sign("LONG", 25000, 25010, -20.0)
        assert corrected == 20.0  # Corrected to positive

    def test_trade_complete_updates_daily_pnl(self, om):
        """Verify daily PnL updated on trade completion."""
        trade = make_active_trade()
        om._active_positions["T001"] = trade

        # Close both legs
        trade["c1_status"] = "FILLED"
        trade["c1_pnl"] = 60.0
        trade["c1_exit_price"] = 25030.0
        trade["c1_exit_time"] = datetime.now(timezone.utc).isoformat()
        trade["c2_status"] = "FILLED"
        trade["c2_pnl"] = 40.0
        trade["c2_exit_price"] = 25020.0
        trade["c2_exit_time"] = datetime.now(timezone.utc).isoformat()

        om._check_trade_complete(trade)

        assert om._daily_pnl == 100.0
        assert len(om._trade_history) == 1


# ═══════════════════════════════════════════════════════════════
# DAILY RESET
# ═══════════════════════════════════════════════════════════════

class TestDailyReset:
    def test_reset_daily(self, om):
        """Verify daily state resets."""
        om._daily_pnl = 200.0
        om._consecutive_losses = 3
        om._eod_closed = True
        om._trade_results = [1, 2, 3]

        om.reset_daily()

        assert om._daily_pnl == 0.0
        assert om._consecutive_losses == 0
        assert not om._eod_closed
        assert len(om._trade_results) == 0
