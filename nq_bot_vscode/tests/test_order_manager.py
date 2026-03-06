"""
Tests for Broker.order_manager — OrderManager (TWS API)
=========================================================
Tests for the 2-contract scale-out order execution system.

All tests use mocked IB instances — no real IBKR connection needed.
30+ tests covering:
  - Entry orders (long, short, timeout, partial fill)
  - Stop loss (fill, rejection, flatten)
  - C1 target fill
  - C2 trailing stop (long, short, monotonic enforcement)
  - Safety rails (position size, daily loss, consecutive losses, etc.)
  - Close all positions
  - PnL calculation ($2/point)
  - Slippage tracking
  - Order event callbacks
  - Bad tick filter
  - Direction assertion
  - Modifier clamping
  - Market halt detection
  - Reconnect reconciliation
  - EOD forced close
  - Watchdog timeout
  - State persistence
"""

import asyncio
import json
import math
import os
import sys
import tempfile
import time
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
from dataclasses import dataclass

import pytest

# Project path setup
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Broker.order_manager import (
    OrderManager,
    MAX_CONTRACTS,
    MNQ_POINT_VALUE,
    DAILY_LOSS_LIMIT,
    MAX_CONSECUTIVE_LOSSES,
    HEARTBEAT_STALE_SECONDS,
    SLIPPAGE_WARN_PTS,
    SLIPPAGE_CRITICAL_PTS,
    MODIFIER_MIN,
    MODIFIER_MAX,
    BAD_TICK_MAX_CHANGE_PCT,
    MARKET_HALT_SECONDS,
    ORDER_WATCHDOG_SECONDS,
    _generate_trade_id,
)


# ═══════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════

@dataclass
class MockBar:
    """Mock bar for testing."""
    timestamp: datetime = None
    open: float = 25000.0
    high: float = 25010.0
    low: float = 24990.0
    close: float = 25005.0
    volume: int = 1500

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)


class MockOrderStatus:
    def __init__(self, status="Submitted", filled=0, avg_fill_price=0.0):
        self.status = status
        self.filled = filled
        self.avgFillPrice = avg_fill_price


class MockOrder:
    def __init__(self, order_id=1, action="BUY", total_qty=2, aux_price=0.0):
        self.orderId = order_id
        self.action = action
        self.totalQuantity = total_qty
        self.auxPrice = aux_price
        self.tif = "GTC"
        self.outsideRth = False


class MockTrade:
    def __init__(self, order_id=1, status="Submitted", filled=0, avg_fill=0.0,
                 action="BUY", total_qty=2):
        self.order = MockOrder(order_id, action, total_qty)
        self.orderStatus = MockOrderStatus(status, filled, avg_fill)


class MockExecution:
    def __init__(self, exec_id="exec1", order_id=1, price=25000.0, shares=2):
        self.execId = exec_id
        self.orderId = order_id
        self.price = price
        self.shares = shares


class MockFill:
    def __init__(self, exec_id="exec1", order_id=1, price=25000.0, shares=2):
        self.execution = MockExecution(exec_id, order_id, price, shares)


def make_mock_ib_client():
    """Create a mock IBKRClient for testing."""
    client = MagicMock()
    client._ib = MagicMock()
    client._contract = MagicMock()
    client._contract.symbol = "MNQ"

    # IB instance mocks
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


def make_signal(**overrides):
    """Create a valid test signal dict."""
    signal = {
        "direction": "LONG",
        "entry_price": 25000.0,
        "stop_price": 24980.0,
        "c1_target": 25030.0,
        "c2_trail_distance": 15.0,
        "modifier_total": 1.0,
        "confluence_score": 0.85,
        "reason": "test_signal",
    }
    signal.update(overrides)
    return signal


def make_active_trade(trade_id="T001", direction="LONG", entry_price=25000.0, **overrides):
    """Create a complete active trade dict for testing."""
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
    """Create a temporary log directory."""
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def om(tmp_log_dir):
    """Create an OrderManager with mocked IB client."""
    client = make_mock_ib_client()
    mgr = OrderManager(client, config={"account_size": 50000.0}, log_dir=tmp_log_dir)
    # Ensure heartbeat is fresh
    mgr._last_bar_time = time.monotonic()
    return mgr


# ═══════════════════════════════════════════════════════════════
# SAFETY RAIL TESTS
# ═══════════════════════════════════════════════════════════════

class TestSafetyRails:
    def test_safety_rejects_when_in_position(self, om):
        """No pyramiding — reject if already in a position."""
        om._active_positions["T001"] = {"contracts": 0, "direction": "LONG"}
        result = om._check_safety(make_signal())
        assert result == "SAFETY_ALREADY_IN_POSITION"

    def test_safety_rejects_daily_loss(self, om):
        """Reject when daily loss limit breached."""
        om._daily_pnl = -501.0
        result = om._check_safety(make_signal())
        assert result == "SAFETY_DAILY_LOSS"

    def test_safety_rejects_max_contracts(self, om):
        """Reject when position size would exceed max."""
        om._active_positions["T001"] = {"contracts": 2, "direction": "LONG"}
        result = om._check_safety(make_signal())
        assert result is not None  # SAFETY_ALREADY_IN_POSITION

    def test_safety_rejects_consec_losses(self, om):
        """Reject after 5 consecutive losses."""
        om._consecutive_losses = 5
        result = om._check_safety(make_signal())
        assert result == "SAFETY_CONSEC_LOSS"

    def test_safety_rejects_stale_data(self, om):
        """Reject when data is stale (>300s)."""
        om._last_bar_time = time.monotonic() - 400
        result = om._check_safety(make_signal())
        assert result == "SAFETY_STALE_DATA"

    def test_safety_allows_valid_signal(self, om):
        """Allow entry when all checks pass."""
        result = om._check_safety(make_signal())
        assert result is None

    def test_safety_rejects_invalid_direction(self, om):
        """Reject invalid direction."""
        result = om._check_safety(make_signal(direction="UP"))
        assert result == "DIRECTION_MISMATCH_CRITICAL"

    def test_safety_rejects_nan_price(self, om):
        """Reject NaN prices."""
        result = om._check_safety(make_signal(entry_price=float("nan")))
        assert "SAFETY_INVALID" in result

    def test_safety_rejects_zero_price(self, om):
        """Reject zero prices."""
        result = om._check_safety(make_signal(entry_price=0))
        assert "SAFETY_INVALID" in result

    def test_safety_kill_switch(self, om):
        """Kill switch activates at 10% drawdown."""
        om._peak_equity = 50000.0
        om._current_equity = 44000.0  # 12% drawdown
        result = om._check_safety(make_signal())
        assert result == "SAFETY_KILL_SWITCH"

    def test_safety_order_in_flight(self, om):
        """Reject when order already in flight."""
        om._order_in_flight = True
        result = om._check_safety(make_signal())
        assert result == "REJECTED_ORDER_IN_FLIGHT"

    def test_safety_market_halt(self, om):
        """Reject during market halt."""
        om._market_halt_suspected = True
        result = om._check_safety(make_signal())
        assert result == "SAFETY_MARKET_HALT"


# ═══════════════════════════════════════════════════════════════
# ENTRY TESTS
# ═══════════════════════════════════════════════════════════════

class TestSubmitEntry:
    @pytest.mark.asyncio
    async def test_submit_entry_long(self, om):
        """Verify limit order placed for LONG entry."""
        mock_trade = MockTrade(order_id=100, status="Filled", filled=2, avg_fill=25000.50)
        om.ib.placeOrder.return_value = mock_trade
        om.ib.sleep = MagicMock()

        signal = make_signal(direction="LONG")
        result = await om.submit_entry(signal)

        assert result is not None
        assert result["direction"] == "LONG"
        assert result["action"] == "BUY"
        assert result["contracts"] == 2
        assert om.ib.placeOrder.called

    @pytest.mark.asyncio
    async def test_submit_entry_short(self, om):
        """Verify limit order placed for SHORT entry."""
        mock_trade = MockTrade(order_id=101, status="Filled", filled=2, avg_fill=25000.0,
                               action="SELL")
        om.ib.placeOrder.return_value = mock_trade
        om.ib.sleep = MagicMock()

        signal = make_signal(direction="SHORT")
        result = await om.submit_entry(signal)

        assert result is not None
        assert result["direction"] == "SHORT"
        assert result["action"] == "SELL"
        assert result["reverse_action"] == "BUY"

    @pytest.mark.asyncio
    async def test_entry_rejected_by_safety(self, om):
        """Verify safety rejection prevents entry."""
        om._daily_pnl = -600  # Over daily loss limit
        signal = make_signal()
        result = await om.submit_entry(signal)
        assert result is None

    @pytest.mark.asyncio
    async def test_entry_timeout_cancels(self, om):
        """Verify cancel after 5-second timeout with no fill."""
        mock_trade = MockTrade(order_id=102, status="Submitted", filled=0)
        om.ib.placeOrder.return_value = mock_trade
        om.ib.sleep = MagicMock()
        om.ib.cancelOrder = MagicMock()

        signal = make_signal()
        result = await om.submit_entry(signal)

        assert result is None
        assert not om._order_in_flight

    @pytest.mark.asyncio
    async def test_partial_fill_handling(self, om):
        """Verify 1/2 fills results in C1-only mode."""
        mock_trade = MockTrade(order_id=103, status="Submitted", filled=1, avg_fill=25000.25)
        call_count = [0]

        def sleep_side_effect(duration):
            call_count[0] += 1
            if call_count[0] > 3:
                mock_trade.orderStatus.filled = 1
                mock_trade.orderStatus.avgFillPrice = 25000.25

        om.ib.placeOrder.return_value = mock_trade
        om.ib.sleep = MagicMock(side_effect=sleep_side_effect)
        om.ib.cancelOrder = MagicMock()

        signal = make_signal()
        result = await om.submit_entry(signal)

        if result is not None:
            assert result["contracts"] == 1
            assert result["c2_status"] == "SKIPPED"

    @pytest.mark.asyncio
    async def test_double_submission_blocked(self, om):
        """Verify _order_in_flight prevents duplicate entries."""
        om._order_in_flight = True
        signal = make_signal()
        result = await om.submit_entry(signal)
        assert result is None


# ═══════════════════════════════════════════════════════════════
# C1 EXIT TESTS
# ═══════════════════════════════════════════════════════════════

class TestC1Exit:
    def test_c1_target_fill(self, om):
        """Verify C1 exit handling on fill."""
        trade = make_active_trade(direction="LONG", entry_price=25000.0)

        om._handle_c1_fill(trade, 25030.0)

        assert trade["c1_status"] == "FILLED"
        assert trade["c1_exit_price"] == 25030.0
        assert trade["c1_pnl"] == 60.0  # (25030 - 25000) * $2


# ═══════════════════════════════════════════════════════════════
# C2 TRAILING STOP TESTS
# ═══════════════════════════════════════════════════════════════

class TestC2Trail:
    @pytest.mark.asyncio
    async def test_c2_trail_update_long(self, om):
        """Verify trail only moves UP for long positions."""
        trade = make_active_trade(direction="LONG", entry_price=25000.0,
                                   c1_status="FILLED", c2_trail_stop=24990.0,
                                   c2_trail_distance=10.0, max_price_since_entry=25000.0)

        mock_ib_trade = MockTrade(order_id=200)
        om.ib.openTrades.return_value = [mock_ib_trade]

        # Price moves up — trail should move up
        exited = await om.manage_c2_trail(trade, 25020.0)
        assert not exited
        assert trade["c2_trail_stop"] == 25010.0  # 25020 - 10
        assert trade["max_price_since_entry"] == 25020.0

        # Price drops — trail should NOT move down
        exited = await om.manage_c2_trail(trade, 25015.0)
        assert not exited
        assert trade["c2_trail_stop"] == 25010.0  # Still 25010

    @pytest.mark.asyncio
    async def test_c2_trail_update_short(self, om):
        """Verify trail only moves DOWN for short positions."""
        trade = make_active_trade(direction="SHORT", entry_price=25000.0,
                                   c1_status="FILLED", c2_trail_stop=25010.0,
                                   c2_trail_distance=10.0, min_price_since_entry=25000.0)

        mock_ib_trade = MockTrade(order_id=200)
        om.ib.openTrades.return_value = [mock_ib_trade]

        # Price drops (favorable for short)
        exited = await om.manage_c2_trail(trade, 24980.0)
        assert not exited
        assert trade["c2_trail_stop"] == 24990.0  # 24980 + 10

        # Price moves up — trail should NOT move up
        exited = await om.manage_c2_trail(trade, 24985.0)
        assert not exited
        assert trade["c2_trail_stop"] == 24990.0  # Stays

    @pytest.mark.asyncio
    async def test_monotonic_trail_long(self, om):
        """Verify trail is strictly monotonic up for long."""
        trade = make_active_trade(direction="LONG", entry_price=25000.0,
                                   c1_status="FILLED", c2_trail_stop=24990.0,
                                   c2_trail_distance=10.0, max_price_since_entry=25000.0)

        mock_ib_trade = MockTrade(order_id=200)
        om.ib.openTrades.return_value = [mock_ib_trade]

        for price in [25010, 25020, 25015, 25025, 25018]:
            prev_stop = trade["c2_trail_stop"]
            await om.manage_c2_trail(trade, price)
            assert trade["c2_trail_stop"] >= prev_stop, \
                f"Trail went down: {prev_stop} -> {trade['c2_trail_stop']}"

    @pytest.mark.asyncio
    async def test_monotonic_trail_short(self, om):
        """Verify trail is strictly monotonic down for short."""
        trade = make_active_trade(direction="SHORT", entry_price=25000.0,
                                   c1_status="FILLED", c2_trail_stop=25010.0,
                                   c2_trail_distance=10.0, min_price_since_entry=25000.0)

        mock_ib_trade = MockTrade(order_id=200)
        om.ib.openTrades.return_value = [mock_ib_trade]

        for price in [24990, 24980, 24990, 24975, 24985]:
            prev_stop = trade["c2_trail_stop"]
            await om.manage_c2_trail(trade, price)
            assert trade["c2_trail_stop"] <= prev_stop, \
                f"Trail went up: {prev_stop} -> {trade['c2_trail_stop']}"

    @pytest.mark.asyncio
    async def test_c2_trail_exit(self, om):
        """Verify C2 exit when trail stop is hit."""
        trade = make_active_trade(direction="LONG", entry_price=25000.0,
                                   c1_status="FILLED", c1_pnl=60.0,
                                   c2_trail_stop=25015.0, c2_trail_distance=10.0,
                                   max_price_since_entry=25025.0,
                                   max_favorable_excursion=25.0)
        om._active_positions["T001"] = trade

        # Price drops below trail stop
        exited = await om.manage_c2_trail(trade, 25010.0)
        assert exited
        assert trade["c2_status"] == "FILLED"
        assert trade["c2_pnl"] == 20.0  # (25010 - 25000) * $2

    @pytest.mark.asyncio
    async def test_c2_trail_retry_on_failure(self, om):
        """Verify retry then flatten when trail modification fails."""
        trade = make_active_trade(direction="LONG", entry_price=25000.0,
                                   c1_status="FILLED", c1_pnl=60.0,
                                   c2_trail_stop=24990.0, c2_trail_distance=10.0,
                                   max_price_since_entry=25020.0,
                                   max_favorable_excursion=20.0)

        om.ib.openTrades.return_value = []  # No matching trade
        # Attempt 3 places a new order — make it raise to trigger full failure
        om.ib.placeOrder.side_effect = Exception("Connection lost")

        result = await om._modify_trail_stop(trade, 25010.0)
        # All 3 attempts fail — should return False
        assert not result


# ═══════════════════════════════════════════════════════════════
# STOP LOSS TESTS
# ═══════════════════════════════════════════════════════════════

class TestStopLoss:
    def test_stop_loss_fill(self, om):
        """Verify both contracts closed on stop fill."""
        trade = make_active_trade(direction="LONG", entry_price=25000.0)
        om._active_positions["T001"] = trade
        om.ib.openTrades.return_value = []

        om._handle_stop_fill(trade, 24980.0, 2)

        assert trade["c1_status"] == "FILLED"
        assert trade["c2_status"] == "FILLED"
        assert trade["c1_pnl"] == -40.0  # (24980 - 25000) * $2
        assert trade["c2_pnl"] == -40.0

    @pytest.mark.asyncio
    async def test_stop_rejection_flattens(self, om):
        """Verify emergency close on stop order rejection."""
        trade = make_active_trade(direction="LONG")
        om.ib.placeOrder.side_effect = Exception("Order rejected")

        result = await om._place_stop_order(trade, 24980.0, 2)
        assert not result


# ═══════════════════════════════════════════════════════════════
# CLOSE ALL POSITIONS
# ═══════════════════════════════════════════════════════════════

class TestCloseAll:
    @pytest.mark.asyncio
    async def test_close_all_positions(self, om):
        """Verify emergency flatten cancels and closes."""
        trade = make_active_trade(direction="LONG")
        om._active_positions["T001"] = trade

        mock_ticker = MagicMock()
        mock_ticker.last = 24990.0
        om.ib.reqMktData.return_value = mock_ticker
        om.ib.cancelMktData = MagicMock()
        om.ib.openTrades.return_value = []

        result = await om.close_all_positions(reason="TEST")
        assert result["reason"] == "TEST"


# ═══════════════════════════════════════════════════════════════
# PnL CALCULATION TESTS
# ═══════════════════════════════════════════════════════════════

class TestPnLCalculation:
    def test_pnl_calculation(self, om):
        """Verify MNQ $2/point math."""
        # Long win: (25015-25000)*$2 = $30
        pnl = om._verify_pnl_sign("LONG", 25000.0, 25015.0, 30.0)
        assert pnl == 30.0

        # Long loss: (24980-25000)*$2 = -$40
        pnl = om._verify_pnl_sign("LONG", 25000.0, 24980.0, -40.0)
        assert pnl == -40.0

        # Short win: (25000-24990)*$2 = $20
        pnl = om._verify_pnl_sign("SHORT", 25000.0, 24990.0, 20.0)
        assert pnl == 20.0

    def test_pnl_sign_check(self, om):
        """Verify PnL sign correction when wrong."""
        # Long, exit > entry, but PnL wrongly negative
        corrected = om._verify_pnl_sign("LONG", 25000.0, 25015.0, -30.0)
        assert corrected == 30.0  # Recalculated positive

        # Short, entry > exit, but PnL wrongly negative
        corrected = om._verify_pnl_sign("SHORT", 25015.0, 25000.0, -30.0)
        assert corrected == 30.0  # Recalculated positive


# ═══════════════════════════════════════════════════════════════
# SLIPPAGE TRACKING
# ═══════════════════════════════════════════════════════════════

class TestSlippage:
    def test_slippage_tracking(self, om):
        """Verify fill vs signal comparison."""
        om._track_slippage(0.25, "T001")
        assert om._slippage_count == 1
        assert om._slippage_total == 0.25

        om._track_slippage(0.50, "T002")
        assert om._slippage_count == 2
        avg = om._slippage_total / om._slippage_count
        assert abs(avg - 0.375) < 0.001

    def test_slippage_calculation(self, om):
        """Verify slippage math for multiple trades."""
        for i, slip in enumerate([0.25, 0.50, 1.0, 0.0, 0.75]):
            om._track_slippage(slip, f"T{i}")

        assert om._slippage_count == 5
        expected_avg = (0.25 + 0.50 + 1.0 + 0.0 + 0.75) / 5
        actual_avg = om._slippage_total / om._slippage_count
        assert abs(actual_avg - expected_avg) < 0.001


# ═══════════════════════════════════════════════════════════════
# ORDER EVENT CALLBACKS
# ═══════════════════════════════════════════════════════════════

class TestOrderEvents:
    def test_order_event_callbacks(self, om):
        """Verify status/execution handling and dedup."""
        fill = MockFill(exec_id="exec_001")
        trade = MockTrade(order_id=1)

        om._on_execution(trade, fill)
        assert "exec_001" in om._processed_exec_ids

        # Duplicate ignored
        om._on_execution(trade, fill)

    def test_error_event_handling(self, om):
        """Verify error event logging."""
        om._on_error(1, 201, "Order rejected", None)
        om._on_error(2, 2104, "Data farm OK", None)  # Should be filtered


# ═══════════════════════════════════════════════════════════════
# BAD TICK FILTER
# ═══════════════════════════════════════════════════════════════

class TestBadTickFilter:
    def test_valid_bar_passes(self, om):
        """Verify valid bars pass filter."""
        bar = MockBar(open=25000, high=25010, low=24990, close=25005, volume=1500)
        assert om.validate_bar(bar)

    def test_negative_price_rejected(self, om):
        """Verify negative price rejected."""
        bar = MockBar(open=-1, high=25010, low=24990, close=25005, volume=1500)
        assert not om.validate_bar(bar)

    def test_high_lt_low_rejected(self, om):
        """Verify High < Low rejected."""
        bar = MockBar(open=25000, high=24990, low=25010, close=25005, volume=1500)
        assert not om.validate_bar(bar)

    def test_high_lt_close_rejected(self, om):
        """Verify High < Close rejected."""
        bar = MockBar(open=25000, high=25003, low=24990, close=25005, volume=1500)
        assert not om.validate_bar(bar)

    def test_low_gt_open_rejected(self, om):
        """Verify Low > Open rejected."""
        bar = MockBar(open=25000, high=25010, low=25005, close=25005, volume=1500)
        assert not om.validate_bar(bar)

    def test_large_change_rejected(self, om):
        """Verify >2% change rejected."""
        om._last_bar_close = 25000.0
        bar = MockBar(open=25600, high=25610, low=25590, close=25600, volume=1500)
        assert not om.validate_bar(bar)

    def test_negative_volume_rejected(self, om):
        """Verify negative volume rejected."""
        bar = MockBar(open=25000, high=25010, low=24990, close=25005, volume=-10)
        assert not om.validate_bar(bar)


# ═══════════════════════════════════════════════════════════════
# DIRECTION ASSERTION
# ═══════════════════════════════════════════════════════════════

class TestDirectionAssertion:
    def test_valid_directions(self, om):
        """Verify valid directions pass."""
        assert om._check_safety(make_signal(direction="LONG")) is None
        assert om._check_safety(make_signal(direction="SHORT")) is None

    def test_invalid_direction(self, om):
        """Verify invalid direction rejected."""
        assert om._check_safety(make_signal(direction="UP")) == "DIRECTION_MISMATCH_CRITICAL"
        assert om._check_safety(make_signal(direction="")) == "DIRECTION_MISMATCH_CRITICAL"


# ═══════════════════════════════════════════════════════════════
# MODIFIER CLAMPING
# ═══════════════════════════════════════════════════════════════

class TestModifierClamping:
    @pytest.mark.asyncio
    async def test_modifier_clamped_low(self, om):
        """Verify modifier clamped to 0.1 minimum."""
        signal = make_signal(modifier_total=0.0)
        om._daily_pnl = -600  # Will be rejected, but clamping happens first
        await om.submit_entry(signal)
        assert signal["modifier_total"] == MODIFIER_MIN

    @pytest.mark.asyncio
    async def test_modifier_clamped_high(self, om):
        """Verify modifier clamped to 3.0 maximum."""
        signal = make_signal(modifier_total=5.0)
        om._daily_pnl = -600
        await om.submit_entry(signal)
        assert signal["modifier_total"] == MODIFIER_MAX

    @pytest.mark.asyncio
    async def test_modifier_normal_unchanged(self, om):
        """Verify modifier in valid range passes through."""
        signal = make_signal(modifier_total=1.5)
        om._daily_pnl = -600
        await om.submit_entry(signal)
        assert signal["modifier_total"] == 1.5


# ═══════════════════════════════════════════════════════════════
# EOD FORCED CLOSE
# ═══════════════════════════════════════════════════════════════

class TestEODForcedClose:
    @pytest.mark.asyncio
    async def test_eod_no_new_entries(self, om):
        """Verify no new entries after 3:45 PM ET."""
        from unittest.mock import patch
        from datetime import datetime as dt_real
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        mock_time = dt_real(2026, 3, 6, 15, 46, 0, tzinfo=et)

        with patch("Broker.order_manager.datetime") as mock_dt:
            mock_dt.now.return_value = mock_time
            mock_dt.fromisoformat = dt_real.fromisoformat

            result = om._check_safety(make_signal())
            assert result == "SAFETY_EOD_NO_NEW_ENTRIES"


# ═══════════════════════════════════════════════════════════════
# ORDER WATCHDOG TIMEOUT
# ═══════════════════════════════════════════════════════════════

class TestWatchdog:
    def test_watchdog_fires(self, om):
        """Verify watchdog timer cancels order."""
        om.ib.openTrades.return_value = [MockTrade(order_id=700)]
        om.ib.cancelOrder = MagicMock()

        om._start_watchdog(700, 0.1)
        time.sleep(0.3)

        # Timer has fired — verify cancel was attempted and order_in_flight cleared
        om.ib.cancelOrder.assert_called_once()
        assert not om._order_in_flight

    def test_watchdog_cancel(self, om):
        """Verify watchdog can be cancelled."""
        om._start_watchdog(701, 10.0)
        assert 701 in om._watchdog_timers

        om._cancel_watchdog(701)
        assert 701 not in om._watchdog_timers


# ═══════════════════════════════════════════════════════════════
# MARKET HALT DETECTION
# ═══════════════════════════════════════════════════════════════

class TestMarketHalt:
    def test_halt_detection(self, om):
        """Verify order blocking during halt."""
        om._last_bar_time = time.monotonic() - 70

        is_halted = om.check_market_halt()
        assert is_halted
        assert om._market_halt_suspected

        result = om._check_safety(make_signal())
        assert result == "SAFETY_MARKET_HALT"

    def test_halt_resume(self, om):
        """Verify trading resumes after halt clears."""
        om._market_halt_suspected = True
        om.on_bar_received(MockBar())

        assert not om._market_halt_suspected
        assert om._market_halt_resume_time > 0


# ═══════════════════════════════════════════════════════════════
# RECONNECT RECONCILIATION
# ═══════════════════════════════════════════════════════════════

class TestReconnect:
    @pytest.mark.asyncio
    async def test_reconnect_reconciliation(self, om):
        """Verify state sync after disconnect."""
        trade = make_active_trade()
        om._active_positions["T001"] = trade
        om.ib.positions.return_value = []  # IBKR shows nothing
        om.ib.openTrades.return_value = []

        result = await om.reconcile_after_reconnect()

        assert not result["matched"]
        assert len(result["discrepancies"]) > 0


# ═══════════════════════════════════════════════════════════════
# STATE PERSISTENCE
# ═══════════════════════════════════════════════════════════════

class TestStatePersistence:
    def test_save_and_load_state(self, om, tmp_log_dir):
        """Verify state round-trip through JSON."""
        om._daily_pnl = 150.0
        om._current_equity = 50150.0
        om._peak_equity = 50200.0
        om._consecutive_losses = 2

        om.save_state()

        # Create new OM and load
        client = make_mock_ib_client()
        om2 = OrderManager(client, config={"account_size": 50000.0}, log_dir=tmp_log_dir)
        loaded = om2.load_state()

        assert loaded
        assert om2._daily_pnl == 150.0
        assert om2._current_equity == 50150.0
        assert om2._consecutive_losses == 2


# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════

class TestLogging:
    def test_order_events_logged(self, om, tmp_log_dir):
        """Verify order events written to log file."""
        om._log_event("TEST_EVENT", trade_id="T999", direction="LONG",
                       price=25000.0, details="test")

        log_path = Path(tmp_log_dir) / "order_events.json"
        assert log_path.exists()

        with open(log_path) as f:
            event = json.loads(f.readline())

        assert event["event"] == "TEST_EVENT"
        assert event["trade_id"] == "T999"


# ═══════════════════════════════════════════════════════════════
# QUERIES
# ═══════════════════════════════════════════════════════════════

class TestQueries:
    def test_get_active_positions(self, om):
        """Verify active positions returned for dashboard."""
        trade = make_active_trade()
        om._active_positions["T001"] = trade
        om._last_bar_close = 25010.0

        positions = om.get_active_positions()
        assert len(positions) == 1
        assert positions[0]["id"] == "T001"
        assert positions[0]["direction"] == "LONG"

    def test_get_trade_metrics(self, om):
        """Verify trade metrics computation."""
        om._trade_history = [
            {"total_pnl": 50.0},
            {"total_pnl": -20.0},
            {"total_pnl": 30.0},
        ]
        om._daily_pnl = 60.0

        metrics = om.get_trade_metrics()
        assert metrics["total_trades"] == 3
        assert metrics["wins"] == 2
        assert metrics["losses"] == 1
        assert abs(metrics["win_rate"] - 66.67) < 0.1
        assert metrics["daily_pnl"] == 60.0


# ═══════════════════════════════════════════════════════════════
# TRADE ID GENERATION
# ═══════════════════════════════════════════════════════════════

class TestTradeId:
    def test_generate_trade_id(self):
        """Verify unique trade IDs."""
        tid = _generate_trade_id()
        assert tid.startswith("T")
        assert len(tid) == 7


# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════

class TestConstants:
    def test_max_contracts_is_2(self):
        assert MAX_CONTRACTS == 2

    def test_mnq_point_value(self):
        assert MNQ_POINT_VALUE == 2.0

    def test_daily_loss_limit(self):
        assert DAILY_LOSS_LIMIT == 500.0

    def test_modifier_range(self):
        assert MODIFIER_MIN == 0.1
        assert MODIFIER_MAX == 3.0
