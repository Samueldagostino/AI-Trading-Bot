"""
Tests for Order Manager
========================
Tests:
- Bracket order creation (C1 + C2)
- Position size guard (max 2 contracts)
- Daily loss check before order
- Emergency cancel_all
- Trailing stop modification
"""

import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from Broker.order_manager import OrderManager, MAX_CONTRACTS


# ================================================================
# FIXTURES
# ================================================================

@pytest.fixture
def mock_client():
    """Mock IBKRClient for order manager."""
    client = MagicMock()
    client.place_order = AsyncMock()
    client.cancel_order = AsyncMock(return_value=True)
    return client


@pytest.fixture
def order_mgr(mock_client, tmp_path):
    """Order manager with mock client and temp log dir."""
    return OrderManager(
        ibkr_client=mock_client,
        max_daily_loss=500.0,
        log_dir=str(tmp_path),
    )


# ================================================================
# BRACKET ORDER TESTS
# ================================================================

class TestBracketOrders:
    @pytest.mark.asyncio
    async def test_c1_c2_bracket_entry(self, order_mgr, mock_client):
        """Full 2-contract bracket entry: C1 (TP + stop) + C2 (stop)."""
        # Mock sequential order IDs
        order_ids = iter([101, 102, 103, 104, 105])
        mock_client.place_order = AsyncMock(side_effect=lambda **kw: next(order_ids))

        result = await order_mgr.submit_entry(
            direction="LONG",
            size=2,
            stop_price=20950.0,
            entry_price=21000.0,
        )

        assert result is not None
        orders = result["orders"]

        # C1: entry + TP + stop = 3 orders
        assert "c1_entry" in orders
        assert "c1_tp" in orders
        assert "c1_stop" in orders
        # C2: entry + stop = 2 orders
        assert "c2_entry" in orders
        assert "c2_stop" in orders

        assert mock_client.place_order.await_count == 5

    @pytest.mark.asyncio
    async def test_c1_tp_price_long(self, order_mgr, mock_client):
        """C1 take-profit at 1.5x R:R for LONG."""
        calls = []

        async def capture_call(**kwargs):
            calls.append(kwargs)
            return len(calls)

        mock_client.place_order = AsyncMock(side_effect=capture_call)

        await order_mgr.submit_entry(
            direction="LONG",
            size=2,
            stop_price=20950.0,  # risk = 50 pts
            entry_price=21000.0,
        )

        # Find the LMT order (TP)
        lmt_calls = [c for c in calls if c.get("order_type") == "LMT"]
        assert len(lmt_calls) == 1
        # TP = entry + (risk * 1.5) = 21000 + 75 = 21075
        assert lmt_calls[0]["limit_price"] == 21075.0

    @pytest.mark.asyncio
    async def test_c1_tp_price_short(self, order_mgr, mock_client):
        """C1 take-profit at 1.5x R:R for SHORT."""
        calls = []

        async def capture_call(**kwargs):
            calls.append(kwargs)
            return len(calls)

        mock_client.place_order = AsyncMock(side_effect=capture_call)

        await order_mgr.submit_entry(
            direction="SHORT",
            size=2,
            stop_price=21050.0,  # risk = 50 pts
            entry_price=21000.0,
        )

        lmt_calls = [c for c in calls if c.get("order_type") == "LMT"]
        assert len(lmt_calls) == 1
        # TP = entry - (risk * 1.5) = 21000 - 75 = 20925
        assert lmt_calls[0]["limit_price"] == 20925.0

    @pytest.mark.asyncio
    async def test_single_contract_entry(self, order_mgr, mock_client):
        """Single contract entry without entry_price."""
        order_ids = iter([201, 202])
        mock_client.place_order = AsyncMock(side_effect=lambda **kw: next(order_ids))

        result = await order_mgr.submit_entry(
            direction="LONG",
            size=1,
            stop_price=20950.0,
        )

        assert result is not None
        orders = result["orders"]
        assert "entry" in orders
        assert "stop" in orders
        assert mock_client.place_order.await_count == 2


# ================================================================
# POSITION SIZE GUARD TESTS
# ================================================================

class TestPositionSizeGuard:
    @pytest.mark.asyncio
    async def test_max_2_contracts(self, order_mgr, mock_client):
        """Cannot exceed 2 contracts."""
        mock_client.place_order = AsyncMock(return_value=1)

        # First entry: 2 contracts
        result1 = await order_mgr.submit_entry("LONG", 2, 20950.0, 21000.0)
        assert result1 is not None

        # Second entry: should be blocked (already at 2)
        result2 = await order_mgr.submit_entry("LONG", 1, 20950.0, 21000.0)
        assert result2 is None

    @pytest.mark.asyncio
    async def test_max_contracts_constant(self):
        """MAX_CONTRACTS is 2."""
        assert MAX_CONTRACTS == 2

    @pytest.mark.asyncio
    async def test_position_size_tracks_entries(self, order_mgr, mock_client):
        """Position size increments on entry."""
        mock_client.place_order = AsyncMock(return_value=1)

        assert order_mgr.current_position_size == 0
        await order_mgr.submit_entry("LONG", 1, 20950.0)
        assert order_mgr.current_position_size == 1
        await order_mgr.submit_entry("LONG", 1, 20950.0)
        assert order_mgr.current_position_size == 2

    @pytest.mark.asyncio
    async def test_oversized_entry_blocked(self, order_mgr, mock_client):
        """Entry of 3 contracts blocked even from zero."""
        result = await order_mgr.submit_entry("LONG", 3, 20950.0, 21000.0)
        assert result is None


# ================================================================
# DAILY LOSS CHECK TESTS
# ================================================================

class TestDailyLossCheck:
    @pytest.mark.asyncio
    async def test_loss_limit_blocks_entry(self, order_mgr, mock_client):
        """Entry blocked when daily loss limit breached."""
        order_mgr.record_pnl(-500.0)  # Hit the limit

        result = await order_mgr.submit_entry("LONG", 1, 20950.0, 21000.0)
        assert result is None

    @pytest.mark.asyncio
    async def test_under_limit_allows_entry(self, order_mgr, mock_client):
        """Entry allowed when under daily loss limit."""
        mock_client.place_order = AsyncMock(return_value=1)
        order_mgr.record_pnl(-400.0)  # Under the $500 limit

        result = await order_mgr.submit_entry("LONG", 1, 20950.0)
        assert result is not None

    def test_pnl_tracking(self, order_mgr):
        """Daily PnL is tracked correctly."""
        order_mgr.record_pnl(-100.0)
        order_mgr.record_pnl(-200.0)
        assert order_mgr.daily_pnl == -300.0


# ================================================================
# CIRCUIT BREAKER TESTS
# ================================================================

class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_entry(self, order_mgr, mock_client):
        """Circuit breaker blocks all entries."""
        order_mgr.trip_circuit_breaker()
        assert order_mgr.circuit_breaker_active is True

        result = await order_mgr.submit_entry("LONG", 1, 20950.0, 21000.0)
        assert result is None

    @pytest.mark.asyncio
    async def test_circuit_breaker_reset(self, order_mgr, mock_client):
        """Circuit breaker can be reset."""
        mock_client.place_order = AsyncMock(return_value=1)

        order_mgr.trip_circuit_breaker()
        order_mgr.reset_circuit_breaker()
        assert order_mgr.circuit_breaker_active is False

        result = await order_mgr.submit_entry("LONG", 1, 20950.0)
        assert result is not None


# ================================================================
# EMERGENCY CANCEL ALL TESTS
# ================================================================

class TestEmergencyCancelAll:
    @pytest.mark.asyncio
    async def test_cancel_all_empty(self, order_mgr, mock_client):
        """Cancel all with no open orders."""
        count = await order_mgr.cancel_all()
        assert count == 0

    @pytest.mark.asyncio
    async def test_cancel_all_with_orders(self, order_mgr, mock_client):
        """Cancel all with open orders."""
        mock_client.place_order = AsyncMock(return_value=1)
        await order_mgr.submit_entry("LONG", 1, 20950.0)

        mock_client.cancel_order = AsyncMock(return_value=True)
        count = await order_mgr.cancel_all()
        assert count >= 1
        assert order_mgr.current_position_size == 0
        assert len(order_mgr.get_open_orders()) == 0


# ================================================================
# TRAILING STOP MODIFICATION TESTS
# ================================================================

class TestTrailingStopModification:
    @pytest.mark.asyncio
    async def test_modify_stop(self, order_mgr, mock_client):
        """Modify stop cancels old and places new."""
        # Setup: place an entry to get tracked orders
        mock_client.place_order = AsyncMock(side_effect=[301, 302])
        await order_mgr.submit_entry("LONG", 1, 20950.0)

        # Find the stop order ID
        orders = order_mgr.get_open_orders()
        stop_order = [o for o in orders if "stop" in o["label"]]
        assert len(stop_order) >= 1

        stop_id = stop_order[0]["order_id"]

        # Modify it
        mock_client.cancel_order = AsyncMock(return_value=True)
        mock_client.place_order = AsyncMock(return_value=400)

        success = await order_mgr.modify_stop(stop_id, 20980.0)
        assert success is True

        # Old order should be gone, new one tracked
        current_orders = order_mgr.get_open_orders()
        order_ids = [o["order_id"] for o in current_orders]
        assert stop_id not in order_ids
        assert 400 in order_ids

    @pytest.mark.asyncio
    async def test_modify_nonexistent_stop(self, order_mgr, mock_client):
        """Modify stop fails for unknown order ID."""
        success = await order_mgr.modify_stop(9999, 20980.0)
        assert success is False


# ================================================================
# ORDER LOG TESTS
# ================================================================

class TestOrderLog:
    @pytest.mark.asyncio
    async def test_entry_logged(self, order_mgr, mock_client, tmp_path):
        """Entry actions are logged to order_log.json (JSONL format)."""
        mock_client.place_order = AsyncMock(return_value=1)
        await order_mgr.submit_entry("LONG", 1, 20950.0)

        log_path = tmp_path / "order_log.json"
        assert log_path.exists()
        lines = [json.loads(l) for l in log_path.read_text().strip().splitlines() if l.strip()]
        assert len(lines) >= 1
        assert lines[-1]["action"] == "ENTRY"

    @pytest.mark.asyncio
    async def test_blocked_logged(self, order_mgr, mock_client, tmp_path):
        """Blocked orders are logged (JSONL format)."""
        order_mgr.trip_circuit_breaker()
        await order_mgr.submit_entry("LONG", 1, 20950.0, 21000.0)

        log_path = tmp_path / "order_log.json"
        assert log_path.exists()
        lines = [json.loads(l) for l in log_path.read_text().strip().splitlines() if l.strip()]
        assert lines[-1]["action"] == "BLOCKED"
