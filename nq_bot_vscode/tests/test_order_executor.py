"""
Tests for IBKROrderExecutor safety rails.

Every safety rail has dedicated tests that verify:
  1. The rail blocks when it should
  2. The rail allows when it should
  3. The rejection reason is logged
  4. There is no bypass path
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

from Broker.order_executor import (
    IBKROrderExecutor,
    OrderRequest,
    OrderRecord,
    OrderSide,
    IBKROrderType,
    OrderState,
    OpenPosition,
    ExecutorConfig,
    ExecutorState,
    MAX_CONTRACTS_PER_ORDER,
    MAX_OPEN_POSITIONS,
    DAILY_LOSS_LIMIT_DOLLARS,
    KILL_SWITCH_THRESHOLD_DOLLARS,
    MNQ_POINT_VALUE,
)
from Broker.ibkr_client_portal import IBKRClient, IBKRConfig, SessionType


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
    return IBKRClient(config)


@pytest.fixture
def executor(client):
    """Paper-mode executor with ETH allowed for most tests."""
    cfg = ExecutorConfig(allow_eth=True, paper_mode=True)
    ex = IBKROrderExecutor(client, cfg)
    # Provide a default price so paper fills work
    client._last_snapshot = MagicMock()
    client._last_snapshot.last_price = 21000.0
    client._last_snapshot.bid = 20999.75
    client._last_snapshot.ask = 21000.25
    return ex


@pytest.fixture
def rth_executor(client):
    """Paper-mode executor with ETH blocked (default)."""
    cfg = ExecutorConfig(allow_eth=False, paper_mode=True)
    ex = IBKROrderExecutor(client, cfg)
    client._last_snapshot = MagicMock()
    client._last_snapshot.last_price = 21000.0
    client._last_snapshot.bid = 20999.75
    client._last_snapshot.ask = 21000.25
    return ex


def _market_buy(contracts: int = 1, tag: str = "") -> OrderRequest:
    return OrderRequest(
        side=OrderSide.BUY,
        order_type=IBKROrderType.MARKET,
        contracts=contracts,
        tag=tag,
    )


def _limit_buy(price: float, contracts: int = 1, tag: str = "") -> OrderRequest:
    return OrderRequest(
        side=OrderSide.BUY,
        order_type=IBKROrderType.LIMIT,
        contracts=contracts,
        limit_price=price,
        tag=tag,
    )


# ═══════════════════════════════════════════════════════════════
# SAFETY RAIL 1: MAX CONTRACTS PER ORDER
# ═══════════════════════════════════════════════════════════════

class TestMaxContractsPerOrder:
    """Max 2 contracts per order — HARD BLOCK."""

    @pytest.mark.asyncio
    async def test_reject_3_contracts(self, executor):
        record = await executor.place_order(_market_buy(contracts=3))
        assert record.accepted is False
        assert "MAX_CONTRACTS_PER_ORDER" in record.rejection_reason
        assert record.state == OrderState.REJECTED

    @pytest.mark.asyncio
    async def test_reject_10_contracts(self, executor):
        record = await executor.place_order(_market_buy(contracts=10))
        assert record.accepted is False
        assert "MAX_CONTRACTS_PER_ORDER" in record.rejection_reason

    @pytest.mark.asyncio
    async def test_allow_2_contracts(self, executor):
        record = await executor.place_order(_market_buy(contracts=2))
        assert record.accepted is True

    @pytest.mark.asyncio
    async def test_allow_1_contract(self, executor):
        record = await executor.place_order(_market_buy(contracts=1))
        assert record.accepted is True

    def test_constant_value(self):
        assert MAX_CONTRACTS_PER_ORDER == 2


# ═══════════════════════════════════════════════════════════════
# SAFETY RAIL 2: MAX OPEN POSITIONS
# ═══════════════════════════════════════════════════════════════

class TestMaxOpenPositions:
    """Max 4 open positions at any time — HARD BLOCK."""

    @pytest.mark.asyncio
    async def test_reject_at_limit(self, executor):
        # Fill up to the limit
        for i in range(MAX_OPEN_POSITIONS):
            executor._state.open_positions.append(
                OpenPosition(
                    broker_order_id=f"POS-{i}",
                    side="BUY",
                    contracts=1,
                    entry_price=21000.0,
                )
            )

        record = await executor.place_order(_market_buy())
        assert record.accepted is False
        assert "MAX_OPEN_POSITIONS" in record.rejection_reason
        assert record.state == OrderState.REJECTED

    @pytest.mark.asyncio
    async def test_allow_below_limit(self, executor):
        # 3 positions open, 1 more should be allowed
        for i in range(MAX_OPEN_POSITIONS - 1):
            executor._state.open_positions.append(
                OpenPosition(
                    broker_order_id=f"POS-{i}",
                    side="BUY",
                    contracts=1,
                    entry_price=21000.0,
                )
            )

        record = await executor.place_order(_market_buy())
        assert record.accepted is True

    @pytest.mark.asyncio
    async def test_allow_after_closing_position(self, executor):
        # Fill to limit
        for i in range(MAX_OPEN_POSITIONS):
            executor._state.open_positions.append(
                OpenPosition(
                    broker_order_id=f"POS-{i}",
                    side="BUY",
                    contracts=1,
                    entry_price=21000.0,
                )
            )

        # Close one
        executor.close_position("POS-0")
        assert executor.open_position_count == MAX_OPEN_POSITIONS - 1

        record = await executor.place_order(_market_buy())
        assert record.accepted is True

    def test_constant_value(self):
        assert MAX_OPEN_POSITIONS == 4


# ═══════════════════════════════════════════════════════════════
# SAFETY RAIL 3: RTH / ETH SESSION CHECK
# ═══════════════════════════════════════════════════════════════

class TestSessionRestriction:
    """No orders outside RTH unless config allows ETH."""

    @pytest.mark.asyncio
    async def test_reject_eth_when_not_allowed(self, rth_executor):
        # Patch get_session_type to return ETH
        with patch(
            "Broker.order_executor.get_session_type",
            return_value=SessionType.ETH,
        ):
            record = await rth_executor.place_order(_market_buy())
        assert record.accepted is False
        assert "ETH_BLOCKED" in record.rejection_reason

    @pytest.mark.asyncio
    async def test_allow_rth_when_eth_not_allowed(self, rth_executor):
        with patch(
            "Broker.order_executor.get_session_type",
            return_value=SessionType.RTH,
        ):
            record = await rth_executor.place_order(_market_buy())
        assert record.accepted is True

    @pytest.mark.asyncio
    async def test_allow_eth_when_configured(self, executor):
        # executor fixture has allow_eth=True
        with patch(
            "Broker.order_executor.get_session_type",
            return_value=SessionType.ETH,
        ):
            record = await executor.place_order(_market_buy())
        assert record.accepted is True

    @pytest.mark.asyncio
    async def test_allow_rth_always(self, executor):
        with patch(
            "Broker.order_executor.get_session_type",
            return_value=SessionType.RTH,
        ):
            record = await executor.place_order(_market_buy())
        assert record.accepted is True


# ═══════════════════════════════════════════════════════════════
# SAFETY RAIL 4: DAILY LOSS LIMIT
# ═══════════════════════════════════════════════════════════════

class TestDailyLossLimit:
    """Daily loss limit ($500 default) blocks new orders."""

    @pytest.mark.asyncio
    async def test_reject_at_loss_limit(self, executor):
        executor._state.daily_pnl = -500.0
        record = await executor.place_order(_market_buy())
        assert record.accepted is False
        assert "DAILY_LOSS_LIMIT" in record.rejection_reason

    @pytest.mark.asyncio
    async def test_reject_beyond_loss_limit(self, executor):
        executor._state.daily_pnl = -750.0
        record = await executor.place_order(_market_buy())
        assert record.accepted is False
        assert "DAILY_LOSS_LIMIT" in record.rejection_reason

    @pytest.mark.asyncio
    async def test_allow_just_above_limit(self, executor):
        executor._state.daily_pnl = -499.99
        record = await executor.place_order(_market_buy())
        assert record.accepted is True

    @pytest.mark.asyncio
    async def test_allow_positive_pnl(self, executor):
        executor._state.daily_pnl = 250.0
        record = await executor.place_order(_market_buy())
        assert record.accepted is True

    @pytest.mark.asyncio
    async def test_allow_zero_pnl(self, executor):
        executor._state.daily_pnl = 0.0
        record = await executor.place_order(_market_buy())
        assert record.accepted is True

    def test_constant_value(self):
        assert DAILY_LOSS_LIMIT_DOLLARS == 500.0


# ═══════════════════════════════════════════════════════════════
# SAFETY RAIL 5: KILL SWITCH
# ═══════════════════════════════════════════════════════════════

class TestKillSwitch:
    """Kill switch at -$1000 halts all trading."""

    @pytest.mark.asyncio
    async def test_kill_switch_at_threshold(self, executor):
        executor._state.daily_pnl = -1000.0
        record = await executor.place_order(_market_buy())
        assert record.accepted is False
        assert executor._state.is_halted is True
        assert "KILL_SWITCH" in record.rejection_reason

    @pytest.mark.asyncio
    async def test_kill_switch_beyond_threshold(self, executor):
        executor._state.daily_pnl = -1500.0
        record = await executor.place_order(_market_buy())
        assert record.accepted is False
        assert executor._state.is_halted is True

    def test_record_trade_pnl_triggers_kill_switch(self, executor):
        executor.record_trade_pnl(-1000.0)
        assert executor._state.is_halted is True
        assert "KILL SWITCH" in executor._state.halt_reason

    def test_record_trade_pnl_cumulative_kill_switch(self, executor):
        executor.record_trade_pnl(-400.0)
        assert executor._state.is_halted is False
        executor.record_trade_pnl(-400.0)
        assert executor._state.is_halted is False
        executor.record_trade_pnl(-200.0)
        assert executor._state.is_halted is True

    @pytest.mark.asyncio
    async def test_halted_state_blocks_all_subsequent_orders(self, executor):
        executor._state.is_halted = True
        executor._state.halt_reason = "manual halt"

        record = await executor.place_order(_market_buy())
        assert record.accepted is False
        assert "HALTED" in record.rejection_reason

    @pytest.mark.asyncio
    async def test_emergency_flatten_halts_trading(self, executor):
        await executor.emergency_flatten("test reason")
        assert executor._state.is_halted is True
        assert "emergency" in executor._state.halt_reason

        record = await executor.place_order(_market_buy())
        assert record.accepted is False

    def test_constant_value(self):
        assert KILL_SWITCH_THRESHOLD_DOLLARS == 1000.0


# ═══════════════════════════════════════════════════════════════
# SAFETY RAIL INTERACTION: NO BYPASS PATH
# ═══════════════════════════════════════════════════════════════

class TestNoBypassPath:
    """Verify that all safety checks run on every order path."""

    @pytest.mark.asyncio
    async def test_scale_out_respects_max_contracts(self, executor):
        """Scale-out entry still checks each leg."""
        # Fill to position limit
        for i in range(MAX_OPEN_POSITIONS):
            executor._state.open_positions.append(
                OpenPosition(
                    broker_order_id=f"POS-{i}",
                    side="BUY",
                    contracts=1,
                    entry_price=21000.0,
                )
            )

        result = await executor.place_scale_out_entry("long")
        assert result["c1"].accepted is False
        assert result["c2"].accepted is False

    @pytest.mark.asyncio
    async def test_scale_out_c2_blocked_if_c1_rejected(self, executor):
        """If C1 is rejected, C2 should also be rejected."""
        executor._state.is_halted = True
        executor._state.halt_reason = "test halt"

        result = await executor.place_scale_out_entry("long")
        assert result["c1"].accepted is False
        assert result["c2"].accepted is False
        assert "C1 rejected" in result["c2"].rejection_reason

    @pytest.mark.asyncio
    async def test_multiple_rails_checked_in_order(self, executor):
        """Even with multiple violations, first one fires."""
        executor._state.is_halted = True
        executor._state.halt_reason = "killed"
        executor._state.daily_pnl = -2000.0

        record = await executor.place_order(_market_buy(contracts=5))
        # HALTED is check #1 — should be the rejection reason
        assert "HALTED" in record.rejection_reason

    @pytest.mark.asyncio
    async def test_all_orders_logged_even_when_rejected(self, executor):
        executor._state.is_halted = True
        executor._state.halt_reason = "test"

        await executor.place_order(_market_buy())
        await executor.place_order(_market_buy())

        assert len(executor._state.order_log) == 2
        assert all(not r.accepted for r in executor._state.order_log)

    @pytest.mark.asyncio
    async def test_blocked_counter_increments(self, executor):
        executor._state.is_halted = True
        executor._state.halt_reason = "test"

        await executor.place_order(_market_buy())
        await executor.place_order(_market_buy())

        assert executor._state.daily_blocked == 2


# ═══════════════════════════════════════════════════════════════
# ORDER LOGGING
# ═══════════════════════════════════════════════════════════════

class TestOrderLogging:
    """Every order attempt is logged with full details."""

    @pytest.mark.asyncio
    async def test_accepted_order_logged(self, executor):
        record = await executor.place_order(
            _limit_buy(price=20950.0, tag="C1")
        )
        assert len(executor._state.order_log) == 1
        log = executor._state.order_log[0]
        assert log.timestamp is not None
        assert log.side == "BUY"
        assert log.contracts == 1
        assert log.price == 20950.0
        assert log.tag == "C1"
        assert log.accepted is True
        assert log.broker_order_id != ""

    @pytest.mark.asyncio
    async def test_rejected_order_logged_with_reason(self, executor):
        record = await executor.place_order(_market_buy(contracts=5))
        assert len(executor._state.order_log) == 1
        log = executor._state.order_log[0]
        assert log.accepted is False
        assert log.rejection_reason != ""
        assert "MAX_CONTRACTS_PER_ORDER" in log.rejection_reason

    @pytest.mark.asyncio
    async def test_record_contains_timestamp(self, executor):
        before = datetime.now(timezone.utc)
        record = await executor.place_order(_market_buy())
        after = datetime.now(timezone.utc)

        assert before <= record.timestamp <= after

    @pytest.mark.asyncio
    async def test_record_direction_and_size(self, executor):
        sell_req = OrderRequest(
            side=OrderSide.SELL,
            order_type=IBKROrderType.MARKET,
            contracts=2,
            tag="exit",
        )
        record = await executor.place_order(sell_req)
        assert record.side == "SELL"
        assert record.contracts == 2
        assert record.tag == "exit"


# ═══════════════════════════════════════════════════════════════
# PAPER TRADING MODE
# ═══════════════════════════════════════════════════════════════

class TestPaperTrading:
    """Paper mode fills immediately and tracks positions."""

    @pytest.mark.asyncio
    async def test_paper_fill_market_order(self, executor):
        record = await executor.place_order(_market_buy())
        assert record.accepted is True
        assert record.state == OrderState.FILLED
        assert record.fill_price == 21000.0
        assert record.broker_order_id.startswith("PAPER-")

    @pytest.mark.asyncio
    async def test_paper_fill_limit_order(self, executor):
        record = await executor.place_order(
            _limit_buy(price=20950.0)
        )
        assert record.accepted is True
        assert record.fill_price == 20950.0

    @pytest.mark.asyncio
    async def test_paper_fill_adds_to_positions(self, executor):
        assert executor.open_position_count == 0
        await executor.place_order(_market_buy(tag="C1"))
        assert executor.open_position_count == 1

    @pytest.mark.asyncio
    async def test_paper_fill_increments_daily_trades(self, executor):
        assert executor._state.daily_trades == 0
        await executor.place_order(_market_buy())
        assert executor._state.daily_trades == 1

    @pytest.mark.asyncio
    async def test_on_fill_callback_fires(self, executor):
        fills = []
        executor.on_fill(lambda r: fills.append(r))

        await executor.place_order(_market_buy())
        assert len(fills) == 1
        assert fills[0].accepted is True

    @pytest.mark.asyncio
    async def test_on_fill_callback_not_called_on_rejection(self, executor):
        fills = []
        executor.on_fill(lambda r: fills.append(r))

        executor._state.is_halted = True
        executor._state.halt_reason = "test"
        await executor.place_order(_market_buy())

        assert len(fills) == 0


# ═══════════════════════════════════════════════════════════════
# LIVE EXECUTION GUARD
# ═══════════════════════════════════════════════════════════════

class TestLiveExecutionGuard:
    """Live execution raises NotImplementedError."""

    @pytest.mark.asyncio
    async def test_place_order_live_raises(self, client):
        cfg = ExecutorConfig(allow_eth=True, paper_mode=False)
        ex = IBKROrderExecutor(client, cfg)
        client._last_snapshot = MagicMock()
        client._last_snapshot.last_price = 21000.0

        with pytest.raises(NotImplementedError, match="LIVE EXECUTION"):
            await ex.place_order(_market_buy())

    @pytest.mark.asyncio
    async def test_modify_stop_live_raises(self, client):
        cfg = ExecutorConfig(paper_mode=False)
        ex = IBKROrderExecutor(client, cfg)
        with pytest.raises(NotImplementedError):
            await ex.modify_stop("ORDER-1", 20900.0)

    @pytest.mark.asyncio
    async def test_cancel_order_live_raises(self, client):
        cfg = ExecutorConfig(paper_mode=False)
        ex = IBKROrderExecutor(client, cfg)
        with pytest.raises(NotImplementedError):
            await ex.cancel_order("ORDER-1")

    @pytest.mark.asyncio
    async def test_cancel_all_live_raises(self, client):
        cfg = ExecutorConfig(paper_mode=False)
        ex = IBKROrderExecutor(client, cfg)
        with pytest.raises(NotImplementedError):
            await ex.cancel_all_open_orders()


# ═══════════════════════════════════════════════════════════════
# SCALE-OUT ENTRY (2-CONTRACT)
# ═══════════════════════════════════════════════════════════════

class TestScaleOutEntry:
    """2-contract scale-out: C1 (target) + C2 (runner)."""

    @pytest.mark.asyncio
    async def test_long_entry_creates_two_orders(self, executor):
        result = await executor.place_scale_out_entry(
            direction="long",
            stop_loss=20950.0,
            c1_take_profit=21050.0,
        )
        assert result["c1"].accepted is True
        assert result["c2"].accepted is True
        assert result["c1"].tag == "C1"
        assert result["c2"].tag == "C2"
        assert result["c1"].side == "BUY"
        assert result["c2"].side == "BUY"
        assert executor.open_position_count == 2

    @pytest.mark.asyncio
    async def test_short_entry_uses_sell(self, executor):
        result = await executor.place_scale_out_entry(direction="short")
        assert result["c1"].side == "SELL"
        assert result["c2"].side == "SELL"

    @pytest.mark.asyncio
    async def test_limit_entry(self, executor):
        result = await executor.place_scale_out_entry(
            direction="long",
            limit_price=20980.0,
        )
        assert result["c1"].order_type == "LMT"
        assert result["c1"].fill_price == 20980.0

    @pytest.mark.asyncio
    async def test_market_entry(self, executor):
        result = await executor.place_scale_out_entry(direction="long")
        assert result["c1"].order_type == "MKT"
        assert result["c1"].fill_price == 21000.0

    @pytest.mark.asyncio
    async def test_each_leg_is_1_contract(self, executor):
        result = await executor.place_scale_out_entry(direction="long")
        assert result["c1"].contracts == 1
        assert result["c2"].contracts == 1


# ═══════════════════════════════════════════════════════════════
# STATE MANAGEMENT
# ═══════════════════════════════════════════════════════════════

class TestStateManagement:
    """Executor state tracking and resets."""

    def test_reset_daily_clears_counters(self, executor):
        executor._state.daily_pnl = -300.0
        executor._state.daily_trades = 5
        executor._state.daily_blocked = 2
        executor._state.is_halted = True
        executor._state.halt_reason = "test"

        executor.reset_daily()

        assert executor._state.daily_pnl == 0.0
        assert executor._state.daily_trades == 0
        assert executor._state.daily_blocked == 0
        assert executor._state.is_halted is False
        assert executor._state.halt_reason == ""

    def test_close_position_removes_from_ledger(self, executor):
        executor._state.open_positions.append(
            OpenPosition(
                broker_order_id="POS-1",
                side="BUY",
                contracts=1,
                entry_price=21000.0,
            )
        )
        assert executor.open_position_count == 1
        executor.close_position("POS-1")
        assert executor.open_position_count == 0

    def test_close_position_only_removes_matching(self, executor):
        executor._state.open_positions.append(
            OpenPosition(broker_order_id="POS-1", side="BUY",
                         contracts=1, entry_price=21000.0)
        )
        executor._state.open_positions.append(
            OpenPosition(broker_order_id="POS-2", side="BUY",
                         contracts=1, entry_price=21010.0)
        )
        executor.close_position("POS-1")
        assert executor.open_position_count == 1
        assert executor._state.open_positions[0].broker_order_id == "POS-2"

    def test_get_status_snapshot(self, executor):
        status = executor.get_status()
        assert status["paper_mode"] is True
        assert status["is_halted"] is False
        assert status["daily_pnl"] == 0.0
        assert status["open_positions"] == 0

    @pytest.mark.asyncio
    async def test_cancel_all_clears_positions(self, executor):
        executor._state.open_positions.append(
            OpenPosition(broker_order_id="P1", side="BUY",
                         contracts=1, entry_price=21000.0)
        )
        executor._state.open_positions.append(
            OpenPosition(broker_order_id="P2", side="BUY",
                         contracts=1, entry_price=21010.0)
        )
        count = await executor.cancel_all_open_orders()
        assert count == 2
        assert executor.open_position_count == 0


# ═══════════════════════════════════════════════════════════════
# MNQ POINT VALUE
# ═══════════════════════════════════════════════════════════════

class TestConstants:
    """Verify safety constants match project-wide values."""

    def test_mnq_point_value(self):
        assert MNQ_POINT_VALUE == 2.0

    def test_max_contracts_per_order(self):
        assert MAX_CONTRACTS_PER_ORDER == 2

    def test_max_open_positions(self):
        assert MAX_OPEN_POSITIONS == 4

    def test_daily_loss_limit(self):
        assert DAILY_LOSS_LIMIT_DOLLARS == 500.0

    def test_kill_switch_threshold(self):
        assert KILL_SWITCH_THRESHOLD_DOLLARS == 1000.0


# ═══════════════════════════════════════════════════════════════
# MODIFY / CANCEL IN PAPER MODE
# ═══════════════════════════════════════════════════════════════

class TestPaperModifyCancel:
    """Paper-mode modify and cancel operations."""

    @pytest.mark.asyncio
    async def test_modify_stop_paper(self, executor):
        result = await executor.modify_stop("PAPER-123", 20900.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_cancel_order_paper(self, executor):
        result = await executor.cancel_order("PAPER-123")
        assert result is True
