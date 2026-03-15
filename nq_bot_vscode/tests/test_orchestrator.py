"""
Tests for IBKRLivePipeline -- full vertical-slice integration.

Updated for unified execution engine refactor:
  - _executor = ScaleOutExecutor (trade management)
  - _ibkr_executor = IBKROrderExecutor (broker orders)
  - No more _active_group_id (replaced by _executor.has_active_trade)
  - close_position() delegates to _executor.emergency_flatten()

Covers:
  - Bar -> signal evaluation -> bridge -> executor -> position manager
  - No signal -> no orders
  - Bridge rejection -> no orders
  - Executor halt -> pipeline stops processing
  - Fill registration with PositionManager
  - Position close -> emergency flatten
  - Active trade blocks new entries
  - Pipeline lifecycle (state transitions)
  - Status reporting
"""

import pytest
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from execution.orchestrator import IBKRLivePipeline, PipelineState
from config.constants import (
    HIGH_CONVICTION_MIN_SCORE, HIGH_CONVICTION_MAX_STOP_PTS,
    SWEEP_MIN_SCORE, SWEEP_CONFLUENCE_BONUS,
)
from execution.signal_bridge import TradeDecision, BridgeResult, ScaleOutParams
from Broker.order_executor import (
    IBKROrderExecutor,
    ExecutorConfig,
    OrderRecord,
    OrderState,
    OrderSide,
    IBKROrderType,
)
from Broker.position_manager import PositionManager
from Broker.ibkr_client_portal import IBKRClient, IBKRConfig, ContractInfo
from features.engine import Bar
from signals.aggregator import SignalDirection
from risk.engine import RiskDecision
from execution.signal_bridge import SignalBridge
from config.settings import BotConfig, CONFIG, RiskConfig


# ═══════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def ibkr_config():
    return IBKRConfig(
        gateway_host="localhost",
        gateway_port=5000,
        account_type="paper",
        symbol="MNQ",
    )


@pytest.fixture
def client(ibkr_config):
    c = IBKRClient(ibkr_config)
    c._account_id = "DU123456"
    c._contract = ContractInfo(conid=553850, symbol="MNQ")
    c._last_snapshot = MagicMock()
    c._last_snapshot.last_price = 21000.0
    c._last_snapshot.bid = 20999.75
    c._last_snapshot.ask = 21000.25
    return c


@pytest.fixture
def executor_config():
    return ExecutorConfig(allow_eth=True, paper_mode=True)


@pytest.fixture
def ibkr_executor(client, executor_config):
    """The IBKR broker executor (renamed from 'executor' to match refactored naming)."""
    return IBKROrderExecutor(client, executor_config)


@pytest.fixture
def executor(client, executor_config):
    """Alias for backward compat -- returns IBKROrderExecutor."""
    return IBKROrderExecutor(client, executor_config)


@pytest.fixture
def position_manager(client, ibkr_executor):
    return PositionManager(client, ibkr_executor)


def _make_bar(
    close: float = 21000.0,
    **overrides,
) -> Bar:
    """Build a Bar with sensible defaults."""
    defaults = dict(
        timestamp=datetime(2026, 3, 1, 14, 30, tzinfo=timezone.utc),
        open=close - 1.0,
        high=close + 2.0,
        low=close - 3.0,
        close=close,
        volume=500,
    )
    defaults.update(overrides)
    return Bar(**defaults)


def _make_filled_record(tag: str = "C1", price: float = 21000.0) -> OrderRecord:
    """Build a filled OrderRecord."""
    return OrderRecord(
        timestamp=datetime.now(timezone.utc),
        side="BUY",
        order_type="MKT",
        contracts=1,
        price=0.0,
        tag=tag,
        accepted=True,
        broker_order_id=f"PAPER-{tag}-12345",
        state=OrderState.FILLED,
        fill_price=price,
    )


def _make_rejected_record(
    tag: str = "C2", reason: str = "test rejection"
) -> OrderRecord:
    """Build a rejected OrderRecord."""
    return OrderRecord(
        timestamp=datetime.now(timezone.utc),
        side="BUY",
        order_type="MKT",
        contracts=1,
        price=0.0,
        tag=tag,
        accepted=False,
        rejection_reason=reason,
        state=OrderState.REJECTED,
    )


def _make_mock_scale_out_executor(has_active_trade: bool = False):
    """Create a mock ScaleOutExecutor with correct API surface."""
    mock = MagicMock()
    mock.has_active_trade = has_active_trade
    mock.active_trade = None
    mock.enter_trade = AsyncMock(return_value=None)
    mock.update = AsyncMock(return_value=None)
    mock.emergency_flatten = MagicMock()
    mock.get_stats = MagicMock(return_value={
        "total_trades": 0,
        "win_rate": 0.0,
        "net_pnl": 0.0,
    })
    return mock


def _make_mock_trade(trade_id: str = "T-001", direction: str = "long"):
    """Create a mock ScaleOutTrade returned by enter_trade()."""
    trade = MagicMock()
    trade.trade_id = trade_id
    trade.direction = direction
    trade.initial_stop = 20985.0
    # C1: 1 contract
    trade.c1 = MagicMock()
    trade.c1.contracts = 1
    # C2: 1 contract
    trade.c2 = MagicMock()
    trade.c2.contracts = 1
    # C3: 3 contracts
    trade.c3 = MagicMock()
    trade.c3.contracts = 3
    return trade


# ═══════════════════════════════════════════════════════════════
# PIPELINE WIRING -- bar to execution
# ═══════════════════════════════════════════════════════════════

class TestPipelineWiring:
    """Full path: signal fires -> bridge approves -> executor fills -> PM tracks."""

    @pytest.mark.asyncio
    async def test_approved_signal_places_order(
        self, client, ibkr_executor, position_manager
    ):
        """When signal + bridge + executor all approve, positions open."""
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._state = PipelineState.RUNNING
        pipeline._ibkr_executor = ibkr_executor
        pipeline._position_manager = position_manager
        pipeline._bars_processed = 0
        pipeline._last_bar = None
        pipeline._current_regime = "unknown"
        pipeline._executor_to_group_id = {}

        # ScaleOutExecutor mock -- no active trade, enter_trade returns mock trade
        mock_trade = _make_mock_trade()
        scale_exec = _make_mock_scale_out_executor(has_active_trade=False)
        scale_exec.enter_trade = AsyncMock(return_value=mock_trade)
        pipeline._executor = scale_exec

        # HTF bias must be present -- fail-safe blocks trades when None
        from features.htf_engine import HTFBiasResult
        pipeline._htf_bias = HTFBiasResult(
            consensus_direction="bullish",
            consensus_strength=0.6,
            htf_allows_long=True,
            htf_allows_short=False,
        )

        # Mock signal pipeline to produce a strong long signal
        mock_features = MagicMock()
        mock_features.atr_14 = 10.0
        mock_features.vix_level = 15.0
        mock_features.trend_direction = "up"
        mock_features.trend_strength = 0.7
        mock_features.session_vwap = 21000.0

        pipeline._feature_engine = MagicMock()
        pipeline._feature_engine.update.return_value = mock_features
        pipeline._feature_engine._bars = [_make_bar()] * 20

        # Signal aggregator returns a strong long signal
        mock_signal = MagicMock()
        mock_signal.should_trade = True
        mock_signal.direction = SignalDirection.LONG
        mock_signal.combined_score = 0.85
        pipeline._signal_aggregator = MagicMock()
        pipeline._signal_aggregator.aggregate.return_value = mock_signal

        # Risk engine approves
        mock_risk = MagicMock()
        mock_risk.decision = RiskDecision.APPROVE
        mock_risk.suggested_stop_distance = 15.0
        pipeline._risk_engine = MagicMock()
        pipeline._risk_engine.evaluate_trade.return_value = mock_risk
        pipeline._risk_engine.state = MagicMock()
        pipeline._risk_engine.state.is_overnight = False
        pipeline._risk_engine.state.upcoming_news_event = False

        # Regime allows trading
        pipeline._regime_detector = MagicMock()
        pipeline._regime_detector.classify.return_value = "trending_up"
        pipeline._regime_detector.get_regime_adjustments.return_value = {
            "size_multiplier": 1.0
        }

        # Sweep detector returns nothing
        pipeline._sweep_detector = MagicMock()
        pipeline._sweep_detector.update_bar.return_value = None

        # Bridge (real instance, will approve with score=0.85)
        pipeline._bridge = SignalBridge(RiskConfig())

        bar = _make_bar(close=21000.0)
        result = await pipeline._process_bar(bar)

        # Verify ScaleOutExecutor.enter_trade was called
        scale_exec.enter_trade.assert_awaited_once()

        # Verify IBKR orders were placed
        assert result is not None
        assert result["action"] == "entry"
        assert result["direction"] == "long"
        assert result["group_id"] is not None
        assert position_manager.open_position_count >= 1

    @pytest.mark.asyncio
    async def test_no_signal_no_order(
        self, client, ibkr_executor, position_manager
    ):
        """No signal -> nothing happens."""
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._state = PipelineState.RUNNING
        pipeline._ibkr_executor = ibkr_executor
        pipeline._executor = _make_mock_scale_out_executor(has_active_trade=False)
        pipeline._position_manager = position_manager
        pipeline._bars_processed = 0
        pipeline._last_bar = None
        pipeline._current_regime = "unknown"
        pipeline._htf_bias = None

        mock_features = MagicMock()
        mock_features.atr_14 = 10.0
        mock_features.vix_level = 15.0
        mock_features.trend_direction = "up"
        mock_features.trend_strength = 0.3
        mock_features.session_vwap = 21000.0

        pipeline._feature_engine = MagicMock()
        pipeline._feature_engine.update.return_value = mock_features
        pipeline._feature_engine._bars = [_make_bar()] * 20

        # No signal
        pipeline._signal_aggregator = MagicMock()
        pipeline._signal_aggregator.aggregate.return_value = None

        pipeline._risk_engine = MagicMock()
        pipeline._risk_engine.state = MagicMock()
        pipeline._risk_engine.state.is_overnight = False
        pipeline._risk_engine.state.upcoming_news_event = False

        pipeline._regime_detector = MagicMock()
        pipeline._regime_detector.classify.return_value = "ranging"
        pipeline._regime_detector.get_regime_adjustments.return_value = {
            "size_multiplier": 1.0
        }

        pipeline._sweep_detector = MagicMock()
        pipeline._sweep_detector.update_bar.return_value = None

        result = await pipeline._process_bar(_make_bar())
        assert result is None
        assert position_manager.open_position_count == 0


class TestBridgeRejection:
    """Bridge rejects low-score signals -- no orders placed."""

    @pytest.mark.asyncio
    async def test_low_score_rejected(
        self, client, ibkr_executor, position_manager
    ):
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._state = PipelineState.RUNNING
        pipeline._ibkr_executor = ibkr_executor
        pipeline._executor = _make_mock_scale_out_executor(has_active_trade=False)
        pipeline._position_manager = position_manager
        pipeline._bars_processed = 0
        pipeline._last_bar = None
        pipeline._current_regime = "unknown"
        pipeline._htf_bias = None

        mock_features = MagicMock()
        mock_features.atr_14 = 10.0
        mock_features.vix_level = 15.0
        mock_features.trend_direction = "up"
        mock_features.trend_strength = 0.7
        mock_features.session_vwap = 21000.0

        pipeline._feature_engine = MagicMock()
        pipeline._feature_engine.update.return_value = mock_features
        pipeline._feature_engine._bars = [_make_bar()] * 20

        # Signal below HC threshold
        mock_signal = MagicMock()
        mock_signal.should_trade = True
        mock_signal.direction = SignalDirection.LONG
        mock_signal.combined_score = 0.60  # Below 0.75
        pipeline._signal_aggregator = MagicMock()
        pipeline._signal_aggregator.aggregate.return_value = mock_signal

        pipeline._risk_engine = MagicMock()
        pipeline._risk_engine.state = MagicMock()
        pipeline._risk_engine.state.is_overnight = False
        pipeline._risk_engine.state.upcoming_news_event = False

        pipeline._regime_detector = MagicMock()
        pipeline._regime_detector.classify.return_value = "trending_up"
        pipeline._regime_detector.get_regime_adjustments.return_value = {
            "size_multiplier": 1.0
        }

        pipeline._sweep_detector = MagicMock()
        pipeline._sweep_detector.update_bar.return_value = None

        result = await pipeline._process_bar(_make_bar())
        # Rejected at HC gate 1 (before bridge even sees it)
        assert result is None
        assert position_manager.open_position_count == 0


# ═══════════════════════════════════════════════════════════════
# EXECUTOR HALT -> PIPELINE STOPS
# ═══════════════════════════════════════════════════════════════

class TestHaltPropagation:
    """IBKR executor halt stops all bar processing."""

    @pytest.mark.asyncio
    async def test_halted_executor_skips_bar(
        self, client, ibkr_executor, position_manager
    ):
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._state = PipelineState.RUNNING
        pipeline._ibkr_executor = ibkr_executor
        pipeline._executor = _make_mock_scale_out_executor()
        pipeline._bars_processed = 0
        pipeline._last_bar = None

        # Halt the IBKR executor (line 311 checks _ibkr_executor.is_halted)
        ibkr_executor._state.is_halted = True
        ibkr_executor._state.halt_reason = "test halt"

        result = await pipeline._process_bar(_make_bar())
        assert result is None

    @pytest.mark.asyncio
    async def test_idle_pipeline_skips_bar(self, ibkr_executor):
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._state = PipelineState.IDLE
        pipeline._ibkr_executor = ibkr_executor
        pipeline._executor = _make_mock_scale_out_executor()

        result = await pipeline._process_bar(_make_bar())
        assert result is None


# ═══════════════════════════════════════════════════════════════
# POSITION CLOSE -> EMERGENCY FLATTEN
# ═══════════════════════════════════════════════════════════════

class TestPositionClose:
    """Position close feeds P&L to position manager and flattens via executor."""

    def test_close_position_updates_pm_immediately(
        self, client, ibkr_executor, position_manager
    ):
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._position_manager = position_manager
        # Mock ScaleOutExecutor with active trade
        pipeline._executor = _make_mock_scale_out_executor(has_active_trade=True)

        position_manager.open_position(
            "G1-C1", "B1", "long", 1, 21000.0, tag="C1", group_id="G1"
        )
        assert position_manager.open_position_count == 1

        pipeline.close_position("G1-C1", 21010.0, "target")
        assert position_manager.open_position_count == 0

    def test_close_calls_emergency_flatten(
        self, client, ibkr_executor, position_manager
    ):
        """When executor has active trade, close_position calls emergency_flatten."""
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._position_manager = position_manager
        pipeline._executor = _make_mock_scale_out_executor(has_active_trade=True)

        position_manager.open_position(
            "G1-C1", "B1", "long", 1, 21000.0, tag="C1", group_id="G1"
        )

        pipeline.close_position("G1-C1", 21010.0, "emergency")
        pipeline._executor.emergency_flatten.assert_called_once_with(21010.0)

    def test_close_no_flatten_when_no_active_trade(
        self, client, ibkr_executor, position_manager
    ):
        """When executor has no active trade, emergency_flatten is NOT called."""
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._position_manager = position_manager
        pipeline._executor = _make_mock_scale_out_executor(has_active_trade=False)

        position_manager.open_position(
            "G1-C1", "B1", "long", 1, 21000.0, tag="C1", group_id="G1"
        )

        pipeline.close_position("G1-C1", 21010.0, "reconciliation")
        pipeline._executor.emergency_flatten.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# ACTIVE TRADE BLOCKS NEW ENTRIES
# ═══════════════════════════════════════════════════════════════

class TestActivePositionBlock:
    """When executor has an active trade, new entries are skipped (update path taken)."""

    @pytest.mark.asyncio
    async def test_active_trade_blocks_new_entry(
        self, client, ibkr_executor, position_manager
    ):
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._state = PipelineState.RUNNING
        pipeline._ibkr_executor = ibkr_executor
        pipeline._position_manager = position_manager
        pipeline._bars_processed = 0
        pipeline._last_bar = None
        pipeline._current_regime = "unknown"
        pipeline._htf_bias = None

        # ScaleOutExecutor with ACTIVE trade -- blocks new entries
        pipeline._executor = _make_mock_scale_out_executor(has_active_trade=True)
        pipeline._executor.update = AsyncMock(return_value=None)  # No exit this bar

        mock_features = MagicMock()
        mock_features.atr_14 = 10.0
        mock_features.vix_level = 15.0
        mock_features.trend_direction = "up"
        mock_features.trend_strength = 0.7
        mock_features.session_vwap = 21000.0

        pipeline._feature_engine = MagicMock()
        pipeline._feature_engine.update.return_value = mock_features
        pipeline._feature_engine._bars = [_make_bar()] * 20

        pipeline._risk_engine = MagicMock()
        pipeline._risk_engine.state = MagicMock()
        pipeline._risk_engine.state.is_overnight = False
        pipeline._risk_engine.state.upcoming_news_event = False

        pipeline._regime_detector = MagicMock()
        pipeline._regime_detector.classify.return_value = "trending_up"
        pipeline._regime_detector.get_regime_adjustments.return_value = {
            "size_multiplier": 1.0
        }

        pipeline._sweep_detector = MagicMock()
        pipeline._sweep_detector.update_bar.return_value = None

        result = await pipeline._process_bar(_make_bar())
        # Active trade -> went to update path -> no exit -> None
        assert result is None
        # Verify update() was called (position management path)
        pipeline._executor.update.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════
# PIPELINE STATE
# ═══════════════════════════════════════════════════════════════

class TestPipelineState:
    """State transitions and status reporting."""

    def test_initial_state_is_idle(self):
        # Can't construct full pipeline without real config,
        # so test the enum directly
        assert PipelineState.IDLE.value == "idle"
        assert PipelineState.RUNNING.value == "running"
        assert PipelineState.HALTED.value == "halted"

    def test_get_status(self, client, ibkr_executor, position_manager):
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._state = PipelineState.RUNNING
        pipeline._bars_processed = 42
        pipeline._current_regime = "trending_up"
        pipeline._htf_bias = None
        # ScaleOutExecutor mock for get_stats()
        pipeline._executor = _make_mock_scale_out_executor()
        pipeline._position_manager = position_manager
        pipeline._data_feed = MagicMock()
        pipeline._data_feed.get_status.return_value = {"running": True}
        pipeline._market_context = None

        pipeline._bridge = SignalBridge(RiskConfig())

        status = pipeline.get_status()
        assert status["pipeline_state"] == "running"
        assert status["bars_processed"] == 42
        assert status["current_regime"] == "trending_up"
        assert status["htf_consensus"] == "n/a"
        assert status["active_trade"] is False  # No active trade
        assert "executor" in status
        assert "positions" in status
        assert "bridge" in status
        assert "data_feed" in status

    def test_bars_processed_property(self, client, ibkr_executor):
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._bars_processed = 99
        assert pipeline.bars_processed == 99


# ═══════════════════════════════════════════════════════════════
# CONSTANTS MATCH MAIN.PY
# ═══════════════════════════════════════════════════════════════

class TestConstants:
    """Orchestrator constants must match main.py exactly."""

    def test_hc_min_score(self):
        assert HIGH_CONVICTION_MIN_SCORE == 0.75

    def test_hc_max_stop(self):
        assert HIGH_CONVICTION_MAX_STOP_PTS == 30.0

    def test_sweep_min_score(self):
        assert SWEEP_MIN_SCORE == 0.70

    def test_sweep_confluence_bonus(self):
        assert SWEEP_CONFLUENCE_BONUS == 0.05


# ═══════════════════════════════════════════════════════════════
# SIGNAL PIPELINE IMPORT CHAIN
# ═══════════════════════════════════════════════════════════════

class TestImportChain:
    """Verify all components in the vertical slice can be imported."""

    def test_ibkr_client_import(self):
        from Broker.ibkr_client_portal import IBKRClient, IBKRDataFeed
        assert IBKRClient is not None

    def test_order_executor_import(self):
        from Broker.order_executor import IBKROrderExecutor
        assert IBKROrderExecutor is not None

    def test_position_manager_import(self):
        from Broker.position_manager import PositionManager
        assert PositionManager is not None

    def test_signal_bridge_import(self):
        from execution.signal_bridge import SignalBridge
        assert SignalBridge is not None

    def test_orchestrator_import(self):
        from execution.orchestrator import IBKRLivePipeline
        assert IBKRLivePipeline is not None

    def test_scale_out_executor_import(self):
        from execution.scale_out_executor import ScaleOutExecutor
        assert ScaleOutExecutor is not None
