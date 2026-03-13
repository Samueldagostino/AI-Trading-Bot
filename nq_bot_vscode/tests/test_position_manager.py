"""
Tests for PositionManager.

Covers:
  - Reconciliation mismatch detection
  - Partial fill handling
  - P&L calculation accuracy
  - Halt trigger on position discrepancy
  - Immediate state update on position close
  - Scale-out group tracking
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from Broker.position_manager import (
    PositionManager,
    TrackedPosition,
    ScaleOutGroup,
    ReconciliationResult,
    BrokerPosition,
    FillState,
    PositionSide,
    RECONCILIATION_INTERVAL_SECONDS,
    COMMISSION_PER_CONTRACT,
    MNQ_POINT_VALUE,
)
from Broker.order_executor import (
    IBKROrderExecutor,
    ExecutorConfig,
    MNQ_POINT_VALUE as EXECUTOR_MNQ_POINT_VALUE,
)
from Broker.ibkr_client_portal import IBKRClient, IBKRConfig, ContractInfo


# ═══════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def config():
    return IBKRConfig(
        gateway_host="localhost",
        gateway_port=5000,
        account_type="paper",
        symbol="MNQ",
    )


@pytest.fixture
def client(config):
    c = IBKRClient(config)
    c._account_id = "DU123456"
    c._contract = ContractInfo(conid=553850, symbol="MNQ")
    c._last_snapshot = MagicMock()
    c._last_snapshot.last_price = 21000.0
    c._last_snapshot.bid = 20999.75
    c._last_snapshot.ask = 21000.25
    return c


@pytest.fixture
def executor(client):
    cfg = ExecutorConfig(allow_eth=True, paper_mode=True)
    return IBKROrderExecutor(client, cfg)


@pytest.fixture
def manager(client, executor):
    return PositionManager(client, executor)


# ═══════════════════════════════════════════════════════════════
# P&L CALCULATION
# ═══════════════════════════════════════════════════════════════

class TestPnLCalculation:
    """P&L calculation accuracy with MNQ $2.00/point."""

    def test_long_profit(self, manager):
        pnl = manager._compute_pnl(
            PositionSide.LONG, 21000.0, 21010.0, 1
        )
        # 10 points × $2.00 × 1 contract = $20.00
        assert pnl == 20.0

    def test_long_loss(self, manager):
        pnl = manager._compute_pnl(
            PositionSide.LONG, 21000.0, 20990.0, 1
        )
        # -10 points × $2.00 × 1 contract = -$20.00
        assert pnl == -20.0

    def test_short_profit(self, manager):
        pnl = manager._compute_pnl(
            PositionSide.SHORT, 21000.0, 20990.0, 1
        )
        assert pnl == 20.0

    def test_short_loss(self, manager):
        pnl = manager._compute_pnl(
            PositionSide.SHORT, 21000.0, 21010.0, 1
        )
        assert pnl == -20.0

    def test_two_contracts(self, manager):
        pnl = manager._compute_pnl(
            PositionSide.LONG, 21000.0, 21005.0, 2
        )
        # 5 points × $2.00 × 2 = $20.00
        assert pnl == 20.0

    def test_fractional_points(self, manager):
        pnl = manager._compute_pnl(
            PositionSide.LONG, 21000.0, 21000.25, 1
        )
        # 0.25 points × $2.00 = $0.50
        assert pnl == 0.5

    def test_zero_move(self, manager):
        pnl = manager._compute_pnl(
            PositionSide.LONG, 21000.0, 21000.0, 1
        )
        assert pnl == 0.0

    def test_net_pnl_includes_commission(self, manager):
        pos = manager.open_position(
            position_id="P1",
            broker_order_id="B1",
            side="long",
            contracts=1,
            entry_price=21000.0,
        )
        closed = manager.close_position("P1", 21010.0, "target")
        # Gross: $20.00, Commission: $1.50, Net: $18.50
        assert closed.gross_pnl == 20.0
        assert closed.commission == COMMISSION_PER_CONTRACT
        assert closed.net_pnl == round(20.0 - COMMISSION_PER_CONTRACT, 2)

    def test_point_value_matches_executor(self):
        """Ensure position_manager and order_executor agree on MNQ point value."""
        assert MNQ_POINT_VALUE == EXECUTOR_MNQ_POINT_VALUE
        assert MNQ_POINT_VALUE == 2.0

    def test_unrealized_pnl_across_positions(self, manager):
        manager.open_position("P1", "B1", "long", 1, 21000.0)
        manager.open_position("P2", "B2", "long", 1, 21005.0)
        # Current price 21010: P1 earns 10pts=$20, P2 earns 5pts=$10
        unrealized = manager.get_unrealized_pnl(21010.0)
        assert unrealized == 30.0


# ═══════════════════════════════════════════════════════════════
# POSITION TRACKING
# ═══════════════════════════════════════════════════════════════

class TestPositionTracking:
    """Open, close, and ledger management."""

    def test_open_position(self, manager):
        pos = manager.open_position(
            position_id="P1",
            broker_order_id="PAPER-1",
            side="long",
            contracts=1,
            entry_price=21000.0,
            tag="C1",
        )
        assert pos.position_id == "P1"
        assert pos.side == PositionSide.LONG
        assert pos.contracts == 1
        assert pos.entry_price == 21000.0
        assert pos.tag == "C1"
        assert pos.is_open is True
        assert manager.open_position_count == 1

    def test_close_position_immediate(self, manager):
        """Position close updates state immediately, not on recon cycle."""
        manager.open_position("P1", "B1", "long", 1, 21000.0)
        assert manager.open_position_count == 1

        closed = manager.close_position("P1", 21010.0, "target")
        assert manager.open_position_count == 0
        assert closed is not None
        assert closed.is_open is False
        assert closed.exit_price == 21010.0
        assert closed.exit_reason == "target"

    def test_close_unknown_position_returns_none(self, manager):
        result = manager.close_position("UNKNOWN", 21000.0)
        assert result is None

    def test_close_feeds_pnl_to_executor(self, manager, executor):
        manager.open_position("P1", "B1", "long", 1, 21000.0)
        manager.close_position("P1", 21010.0, "target")
        # Net P&L = $20.00 - $1.50 = $18.50
        expected_net = round(20.0 - COMMISSION_PER_CONTRACT, 2)
        assert executor.daily_pnl == expected_net

    def test_close_removes_from_executor_ledger(self, manager, executor):
        manager.open_position("P1", "B1", "long", 1, 21000.0)
        # Manually add to executor's ledger (simulating what place_order does)
        from Broker.order_executor import OpenPosition
        executor._state.open_positions.append(
            OpenPosition(broker_order_id="B1", side="BUY",
                         contracts=1, entry_price=21000.0)
        )
        assert executor.open_position_count == 1

        manager.close_position("P1", 21010.0, "target")
        assert executor.open_position_count == 0

    def test_daily_pnl_accumulates(self, manager):
        manager.open_position("P1", "B1", "long", 1, 21000.0)
        manager.close_position("P1", 21010.0, "target")

        manager.open_position("P2", "B2", "long", 1, 21010.0)
        manager.close_position("P2", 21020.0, "target")

        # Two wins: each $20 gross - $1.50 commission
        expected = round(2 * (20.0 - COMMISSION_PER_CONTRACT), 2)
        assert manager.daily_realized_pnl == expected
        assert manager.trade_count == 2

    def test_entry_price_rounded(self, manager):
        pos = manager.open_position(
            "P1", "B1", "long", 1, 21000.123456
        )
        assert pos.entry_price == 21000.12

    def test_short_position_tracking(self, manager):
        pos = manager.open_position("P1", "B1", "short", 1, 21000.0)
        assert pos.side == PositionSide.SHORT

        closed = manager.close_position("P1", 20990.0, "target")
        assert closed.gross_pnl == 20.0  # 10pts × $2


# ═══════════════════════════════════════════════════════════════
# PARTIAL FILL HANDLING
# ═══════════════════════════════════════════════════════════════

class TestPartialFills:
    """Handle when only one leg of a scale-out fills."""

    def test_c1_fills_c2_does_not(self, manager):
        manager.open_position(
            "P1", "B1", "long", 1, 21000.0,
            tag="C1", group_id="G1",
        )
        group = manager.get_scale_out_group("G1")
        assert group is not None
        assert group.c1 is not None
        assert group.c2 is None
        assert group.is_partial is True

    def test_both_legs_fill(self, manager):
        manager.open_position(
            "P1", "B1", "long", 1, 21000.0,
            tag="C1", group_id="G1",
        )
        manager.open_position(
            "P2", "B2", "long", 1, 21000.0,
            tag="C2", group_id="G1",
        )
        group = manager.get_scale_out_group("G1")
        assert group.c1 is not None
        assert group.c2 is not None
        assert group.is_partial is False

    def test_mark_partial_fill(self, manager):
        manager.open_position(
            "P1", "B1", "long", 1, 21000.0,
            tag="C1", group_id="G1",
        )
        manager.open_position(
            "P2", "B2", "long", 1, 21000.0,
            tag="C2", group_id="G1",
        )
        manager.mark_partial_fill("G1", "C2")
        group = manager.get_scale_out_group("G1")
        assert group.c2.fill_state == FillState.UNFILLED

    def test_mark_partial_fill_unknown_group(self, manager):
        # Should log warning but not crash
        manager.mark_partial_fill("UNKNOWN", "C1")

    def test_group_total_pnl(self, manager):
        manager.open_position(
            "P1", "B1", "long", 1, 21000.0,
            tag="C1", group_id="G1",
        )
        manager.open_position(
            "P2", "B2", "long", 1, 21000.0,
            tag="C2", group_id="G1",
        )
        manager.close_position("P1", 21010.0, "target")
        manager.close_position("P2", 21030.0, "trailing")

        group = manager.get_scale_out_group("G1")
        # C1: 10pts=$20 - $1.50 = $18.50
        # C2: 30pts=$60 - $1.50 = $58.50
        expected = round(18.50 + 58.50, 2)
        assert group.total_net_pnl == expected

    def test_group_fully_closed(self, manager):
        manager.open_position(
            "P1", "B1", "long", 1, 21000.0,
            tag="C1", group_id="G1",
        )
        manager.open_position(
            "P2", "B2", "long", 1, 21000.0,
            tag="C2", group_id="G1",
        )
        group = manager.get_scale_out_group("G1")
        assert group.is_fully_closed is False

        manager.close_position("P1", 21010.0, "target")
        assert group.is_fully_closed is False

        manager.close_position("P2", 21020.0, "trailing")
        assert group.is_fully_closed is True


# ═══════════════════════════════════════════════════════════════
# RECONCILIATION -- MISMATCH DETECTION
# ═══════════════════════════════════════════════════════════════

class TestReconciliationMismatch:
    """Mismatch triggers CRITICAL log + HALT."""

    @pytest.mark.asyncio
    async def test_ghost_position_triggers_halt(self, manager, executor):
        """Broker has a position we don't track -> ghost -> HALT."""
        # Internal: no positions
        assert manager.open_position_count == 0

        # Broker: 1 long position
        manager._client._get = AsyncMock(return_value=[
            {
                "conid": 553850,
                "contractDesc": "MNQ",
                "position": 1,
                "avgPrice": 21000.0,
                "unrealizedPnl": 50.0,
                "realizedPnl": 0.0,
            }
        ])

        result = await manager.reconcile()
        assert result.matched is False
        assert len(result.ghost_positions) > 0
        assert executor.is_halted is True

    @pytest.mark.asyncio
    async def test_missing_position_triggers_halt(self, manager, executor):
        """We track a position broker doesn't show -> missing -> HALT."""
        # Internal: 1 long position
        manager.open_position("P1", "B1", "long", 1, 21000.0)

        # Broker: no positions
        manager._client._get = AsyncMock(return_value=[])

        result = await manager.reconcile()
        assert result.matched is False
        assert len(result.missing_positions) > 0
        assert executor.is_halted is True

    @pytest.mark.asyncio
    async def test_quantity_mismatch_triggers_halt(self, manager, executor):
        """Internal says 1 contract, broker says 2 -> HALT."""
        manager.open_position("P1", "B1", "long", 1, 21000.0)

        manager._client._get = AsyncMock(return_value=[
            {
                "conid": 553850,
                "contractDesc": "MNQ",
                "position": 2,
                "avgPrice": 21000.0,
            }
        ])

        result = await manager.reconcile()
        assert result.matched is False
        assert executor.is_halted is True

    @pytest.mark.asyncio
    async def test_matching_positions_no_halt(self, manager, executor):
        """Internal and broker agree -> no halt."""
        manager.open_position("P1", "B1", "long", 1, 21000.0)

        manager._client._get = AsyncMock(return_value=[
            {
                "conid": 553850,
                "contractDesc": "MNQ",
                "position": 1,
                "avgPrice": 21000.0,
            }
        ])

        result = await manager.reconcile()
        assert result.matched is True
        assert executor.is_halted is False

    @pytest.mark.asyncio
    async def test_both_empty_matches(self, manager, executor):
        """No positions on either side -> match."""
        manager._client._get = AsyncMock(return_value=[])

        result = await manager.reconcile()
        assert result.matched is True
        assert executor.is_halted is False

    @pytest.mark.asyncio
    async def test_two_positions_match(self, manager, executor):
        """2 internal positions matching broker 2 contracts."""
        manager.open_position("P1", "B1", "long", 1, 21000.0)
        manager.open_position("P2", "B2", "long", 1, 21005.0)

        manager._client._get = AsyncMock(return_value=[
            {
                "conid": 553850,
                "contractDesc": "MNQ",
                "position": 2,
                "avgPrice": 21002.5,
            }
        ])

        result = await manager.reconcile()
        assert result.matched is True

    @pytest.mark.asyncio
    async def test_short_positions_match(self, manager, executor):
        """Short position: internal -1, broker -1."""
        manager.open_position("P1", "B1", "short", 1, 21000.0)

        manager._client._get = AsyncMock(return_value=[
            {
                "conid": 553850,
                "contractDesc": "MNQ",
                "position": -1,
                "avgPrice": 21000.0,
            }
        ])

        result = await manager.reconcile()
        assert result.matched is True

    @pytest.mark.asyncio
    async def test_no_auto_correction_on_mismatch(self, manager, executor):
        """Mismatch halts but does NOT modify internal positions."""
        manager.open_position("P1", "B1", "long", 1, 21000.0)
        initial_count = manager.open_position_count

        # Broker disagrees
        manager._client._get = AsyncMock(return_value=[
            {
                "conid": 553850,
                "contractDesc": "MNQ",
                "position": 2,
                "avgPrice": 21000.0,
            }
        ])

        await manager.reconcile()
        # Positions should NOT be auto-corrected
        # (cancel_all_open_orders clears executor ledger, not ours)
        # The position_manager's internal state is preserved for human review
        assert "P1" in manager._open_positions

    @pytest.mark.asyncio
    async def test_recon_result_stored_in_history(self, manager):
        manager._client._get = AsyncMock(return_value=[])

        await manager.reconcile()
        assert len(manager._recon_history) == 1
        assert manager.last_reconciliation is not None
        assert manager.last_reconciliation.matched is True

    @pytest.mark.asyncio
    async def test_ignores_other_contracts(self, manager, executor):
        """Positions for other conids are ignored."""
        manager._client._get = AsyncMock(return_value=[
            {
                "conid": 999999,  # different contract
                "contractDesc": "ES",
                "position": 5,
                "avgPrice": 5000.0,
            }
        ])

        result = await manager.reconcile()
        assert result.matched is True  # no MNQ positions on either side


# ═══════════════════════════════════════════════════════════════
# RECONCILIATION -- FETCH BROKER POSITIONS
# ═══════════════════════════════════════════════════════════════

class TestFetchBrokerPositions:
    """Parsing of IBKR portfolio endpoint responses."""

    @pytest.mark.asyncio
    async def test_parse_position_response(self, manager):
        manager._client._get = AsyncMock(return_value=[
            {
                "conid": 553850,
                "contractDesc": "MNQ DEC 2026",
                "position": 2,
                "avgPrice": 21050.25,
                "unrealizedPnl": 100.50,
                "realizedPnl": 0.0,
            }
        ])

        positions = await manager._fetch_broker_positions()
        assert len(positions) == 1
        assert positions[0].conid == 553850
        assert positions[0].quantity == 2
        assert positions[0].avg_price == 21050.25
        assert positions[0].unrealized_pnl == 100.50

    @pytest.mark.asyncio
    async def test_api_failure_returns_empty(self, manager):
        manager._client._get = AsyncMock(return_value=None)

        positions = await manager._fetch_broker_positions()
        assert positions == []

    @pytest.mark.asyncio
    async def test_no_account_id_returns_empty(self, manager):
        manager._client._account_id = ""

        positions = await manager._fetch_broker_positions()
        assert positions == []

    @pytest.mark.asyncio
    async def test_dict_response_wrapped(self, manager):
        """Single position returned as dict instead of list."""
        manager._client._get = AsyncMock(return_value={
            "conid": 553850,
            "contractDesc": "MNQ",
            "position": 1,
            "avgPrice": 21000.0,
        })

        positions = await manager._fetch_broker_positions()
        assert len(positions) == 1


# ═══════════════════════════════════════════════════════════════
# HALT TRIGGER ON DISCREPANCY
# ═══════════════════════════════════════════════════════════════

class TestHaltOnDiscrepancy:
    """Reconciliation mismatch triggers executor halt."""

    @pytest.mark.asyncio
    async def test_halt_sets_executor_halted(self, manager, executor):
        manager._client._get = AsyncMock(return_value=[
            {
                "conid": 553850,
                "position": 1,
                "avgPrice": 21000.0,
            }
        ])
        # No internal positions -> ghost -> halt
        await manager.reconcile()
        assert executor.is_halted is True
        assert "reconciliation" in executor.state.halt_reason.lower()

    @pytest.mark.asyncio
    async def test_halt_blocks_subsequent_orders(self, manager, executor):
        """After recon halt, no more orders can be placed."""
        from Broker.order_executor import OrderRequest, OrderSide, IBKROrderType

        manager._client._get = AsyncMock(return_value=[
            {
                "conid": 553850,
                "position": 1,
                "avgPrice": 21000.0,
            }
        ])
        await manager.reconcile()

        record = await executor.place_order(OrderRequest(
            side=OrderSide.BUY,
            order_type=IBKROrderType.MARKET,
            contracts=1,
        ))
        assert record.accepted is False
        assert "HALTED" in record.rejection_reason


# ═══════════════════════════════════════════════════════════════
# RECONCILIATION LOOP LIFECYCLE
# ═══════════════════════════════════════════════════════════════

class TestReconciliationLoop:
    """Start/stop the background loop."""

    @pytest.mark.asyncio
    async def test_start_creates_task(self, manager):
        await manager.start_reconciliation_loop()
        assert manager._recon_task is not None
        assert not manager._recon_task.done()
        await manager.stop_reconciliation_loop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self, manager):
        await manager.start_reconciliation_loop()
        await manager.stop_reconciliation_loop()
        assert manager._recon_task is None

    @pytest.mark.asyncio
    async def test_double_start_is_safe(self, manager):
        await manager.start_reconciliation_loop()
        first_task = manager._recon_task
        await manager.start_reconciliation_loop()
        assert manager._recon_task is first_task  # same task, not duplicated
        await manager.stop_reconciliation_loop()

    def test_interval_constant(self):
        assert RECONCILIATION_INTERVAL_SECONDS == 30


# ═══════════════════════════════════════════════════════════════
# STATE MANAGEMENT
# ═══════════════════════════════════════════════════════════════

class TestStateManagement:
    """Daily resets and status snapshots."""

    def test_reset_daily(self, manager):
        manager.open_position("P1", "B1", "long", 1, 21000.0)
        manager.close_position("P1", 21010.0, "target")
        assert manager.daily_realized_pnl != 0.0

        manager.reset_daily()
        assert manager.daily_realized_pnl == 0.0
        assert manager.trade_count == 0

    def test_get_status(self, manager):
        status = manager.get_status()
        assert "open_positions" in status
        assert "daily_realized_pnl" in status
        assert "trade_count" in status
        assert "last_recon_matched" in status
        assert status["open_positions"] == 0
        assert status["daily_realized_pnl"] == 0.0

    def test_open_positions_property_returns_copy(self, manager):
        manager.open_position("P1", "B1", "long", 1, 21000.0)
        positions = manager.open_positions
        positions["P99"] = None  # mutate the copy
        assert "P99" not in manager._open_positions  # original unchanged


# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════

class TestConstants:
    """Verify constants match project-wide values."""

    def test_commission(self):
        assert COMMISSION_PER_CONTRACT == 1.50

    def test_point_value(self):
        assert MNQ_POINT_VALUE == 2.0

    def test_recon_interval(self):
        assert RECONCILIATION_INTERVAL_SECONDS == 30
