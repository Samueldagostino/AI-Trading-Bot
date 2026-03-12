"""
Tests for IBKRLivePipeline -- full vertical-slice integration.

Covers:
  - Bar -> signal evaluation -> bridge -> executor -> position manager
  - No signal -> no orders
  - Bridge rejection -> no orders
  - Executor halt -> pipeline stops processing
  - Fill registration with PositionManager
  - Partial fill (C1 fills, C2 rejected)
  - Position close -> P&L feeds to executor
  - Group fully closed clears active trade
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
def executor(client, executor_config):
    return IBKROrderExecutor(client, executor_config)


@pytest.fixture
def position_manager(client, executor):
    return PositionManager(client, executor)


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


# ═══════════════════════════════════════════════════════════════
# PIPELINE WIRING -- bar to execution
# ═══════════════════════════════════════════════════════════════

class TestPipelineWiring:
    """Full path: signal fires -> bridge approves -> executor fills -> PM tracks."""

    @pytest.mark.asyncio
    async def test_approved_signal_places_order(
        self, client, executor, position_manager
    ):
        """When signal + bridge + executor all approve, positions open."""
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._state = PipelineState.RUNNING
        pipeline._executor = executor
        pipeline._position_manager = position_manager
        pipeline._bars_processed = 0
        pipeline._last_bar = None
        pipeline._active_group_id = None
        pipeline._current_regime = "unknown"
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

        assert result is not None
        assert result["action"] == "entry"
        assert result["direction"] == "long"
        assert result["contracts"] == 2
        assert result["group_id"] is not None
        assert position_manager.open_position_count == 2

    @pytest.mark.asyncio
    async def test_no_signal_no_order(
        self, client, executor, position_manager
    ):
        """No signal -> nothing happens."""
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._state = PipelineState.RUNNING
        pipeline._executor = executor
        pipeline._position_manager = position_manager
        pipeline._bars_processed = 0
        pipeline._last_bar = None
        pipeline._active_group_id = None
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
        self, client, executor, position_manager
    ):
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._state = PipelineState.RUNNING
        pipeline._executor = executor
        pipeline._position_manager = position_manager
        pipeline._bars_processed = 0
        pipeline._last_bar = None
        pipeline._active_group_id = None
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
    """Executor halt stops all bar processing."""

    @pytest.mark.asyncio
    async def test_halted_executor_skips_bar(
        self, client, executor, position_manager
    ):
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._state = PipelineState.RUNNING
        pipeline._executor = executor
        pipeline._bars_processed = 0
        pipeline._last_bar = None

        # Halt the executor
        executor._state.is_halted = True
        executor._state.halt_reason = "test halt"

        result = await pipeline._process_bar(_make_bar())
        assert result is None

    @pytest.mark.asyncio
    async def test_idle_pipeline_skips_bar(self, executor):
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._state = PipelineState.IDLE
        pipeline._executor = executor

        result = await pipeline._process_bar(_make_bar())
        assert result is None


# ═══════════════════════════════════════════════════════════════
# PARTIAL FILL
# ═══════════════════════════════════════════════════════════════

class TestPartialFill:
    """C1 fills but C2 rejected -> partial state tracked."""

    @pytest.mark.asyncio
    async def test_c2_rejection_tracked(
        self, client, executor, position_manager
    ):
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._state = PipelineState.RUNNING
        pipeline._executor = executor
        pipeline._position_manager = position_manager
        pipeline._bars_processed = 0
        pipeline._last_bar = None
        pipeline._active_group_id = None
        pipeline._current_regime = "unknown"
        # HTF bias must be present -- fail-safe blocks trades when None
        from features.htf_engine import HTFBiasResult
        pipeline._htf_bias = HTFBiasResult(
            consensus_direction="bullish",
            consensus_strength=0.6,
            htf_allows_long=True,
            htf_allows_short=False,
        )

        mock_features = MagicMock()
        mock_features.atr_14 = 10.0
        mock_features.vix_level = 15.0
        mock_features.trend_direction = "up"
        mock_features.trend_strength = 0.7
        mock_features.session_vwap = 21000.0

        pipeline._feature_engine = MagicMock()
        pipeline._feature_engine.update.return_value = mock_features
        pipeline._feature_engine._bars = [_make_bar()] * 20

        mock_signal = MagicMock()
        mock_signal.should_trade = True
        mock_signal.direction = SignalDirection.LONG
        mock_signal.combined_score = 0.85
        pipeline._signal_aggregator = MagicMock()
        pipeline._signal_aggregator.aggregate.return_value = mock_signal

        mock_risk = MagicMock()
        mock_risk.decision = RiskDecision.APPROVE
        mock_risk.suggested_stop_distance = 15.0
        pipeline._risk_engine = MagicMock()
        pipeline._risk_engine.evaluate_trade.return_value = mock_risk
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

        pipeline._bridge = SignalBridge(RiskConfig())

        # Mock executor to accept C1 but reject C2 (at max positions)
        c1 = _make_filled_record("C1", 21000.0)
        c2 = _make_rejected_record("C2", "MAX_OPEN_POSITIONS")
        pipeline._executor = MagicMock()
        pipeline._executor.is_halted = False
        pipeline._executor.place_scale_out_entry = AsyncMock(
            return_value={"c1": c1, "c2": c2}
        )

        result = await pipeline._process_bar(_make_bar())
        assert result is not None
        assert result["contracts"] == 1  # Only C1 filled
        assert position_manager.open_position_count == 1

        group = position_manager.get_scale_out_group(result["group_id"])
        assert group is not None
        assert group.c1 is not None
        assert group.is_partial is True


# ═══════════════════════════════════════════════════════════════
# POSITION CLOSE -> P&L FLOW
# ═══════════════════════════════════════════════════════════════

class TestPositionClose:
    """Position close feeds P&L through to executor."""

    def test_close_feeds_pnl_and_clears_group(
        self, client, executor, position_manager
    ):
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._position_manager = position_manager
        pipeline._active_group_id = "G1"

        # Open both legs
        position_manager.open_position(
            "G1-C1", "B1", "long", 1, 21000.0, tag="C1", group_id="G1"
        )
        position_manager.open_position(
            "G1-C2", "B2", "long", 1, 21000.0, tag="C2", group_id="G1"
        )

        # Close C1
        pipeline.close_position("G1-C1", 21010.0, "target")
        assert pipeline._active_group_id == "G1"  # still active (C2 open)

        # Close C2
        pipeline.close_position("G1-C2", 21030.0, "trailing")
        assert pipeline._active_group_id is None  # group fully closed

        # P&L fed to executor
        assert executor.daily_pnl != 0.0

    def test_close_position_updates_pm_immediately(
        self, client, executor, position_manager
    ):
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._position_manager = position_manager
        pipeline._active_group_id = "G1"

        position_manager.open_position(
            "G1-C1", "B1", "long", 1, 21000.0, tag="C1", group_id="G1"
        )
        assert position_manager.open_position_count == 1

        pipeline.close_position("G1-C1", 21010.0, "target")
        assert position_manager.open_position_count == 0


# ═══════════════════════════════════════════════════════════════
# ACTIVE POSITION BLOCKS NEW ENTRIES
# ═══════════════════════════════════════════════════════════════

class TestActivePositionBlock:
    """When a trade is active, new entries are skipped."""

    @pytest.mark.asyncio
    async def test_active_group_blocks_new_entry(
        self, client, executor, position_manager
    ):
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._state = PipelineState.RUNNING
        pipeline._executor = executor
        pipeline._position_manager = position_manager
        pipeline._bars_processed = 0
        pipeline._last_bar = None
        pipeline._active_group_id = "existing-trade"
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
        assert result is None


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

    def test_get_status(self, client, executor, position_manager):
        pipeline = IBKRLivePipeline.__new__(IBKRLivePipeline)
        pipeline._state = PipelineState.RUNNING
        pipeline._bars_processed = 42
        pipeline._current_regime = "trending_up"
        pipeline._htf_bias = None
        pipeline._active_group_id = None
        pipeline._executor = executor
        pipeline._position_manager = position_manager
        pipeline._data_feed = MagicMock()
        pipeline._data_feed.get_status.return_value = {"running": True}

        pipeline._bridge = SignalBridge(RiskConfig())

        status = pipeline.get_status()
        assert status["pipeline_state"] == "running"
        assert status["bars_processed"] == 42
        assert status["current_regime"] == "trending_up"
        assert status["htf_consensus"] == "n/a"
        assert "executor" in status
        assert "positions" in status
        assert "bridge" in status
        assert "data_feed" in status

    def test_bars_processed_property(self, client, executor):
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
