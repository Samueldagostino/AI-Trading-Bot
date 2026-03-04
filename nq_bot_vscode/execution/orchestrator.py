"""
IBKR Live Pipeline Orchestrator
==================================
Wires the complete vertical slice from market data to kill switch:

  IBKRClient -> CandleAggregator -> candle_to_bar() -> Bar
                                                      ↓
                                                process_bar()
                                                      ↓
                                          SignalBridge.translate()
                                                      ↓
                                    IBKROrderExecutor (safety rails)
                                                      ↓
                                    PositionManager (recon + P&L)
                                              ↓            ↓
                                        IBKRClient    kill switch
                                         (verify)     (if needed)

This module does NOT modify signal generation logic.  The feature
engine, signal aggregator, HTF engine, regime detector, risk engine,
and sweep detector all run unchanged.  Only the execution path is
adapted: instead of ScaleOutExecutor -> Tradovate, we route through
SignalBridge -> IBKROrderExecutor -> PositionManager.
"""

import asyncio
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

from features.engine import NQFeatureEngine, Bar
from features.htf_engine import HTFBiasEngine, HTFBiasResult
from signals.aggregator import SignalAggregator, SignalDirection
from signals.liquidity_sweep import LiquiditySweepDetector
from risk.engine import RiskEngine, RiskDecision
from risk.regime_detector import RegimeDetector

from Broker.ibkr_client_portal import IBKRClient, IBKRConfig, IBKRDataFeed
from Broker.order_executor import IBKROrderExecutor, ExecutorConfig
from Broker.position_manager import PositionManager
from execution.signal_bridge import SignalBridge, TradeDecision

from config.settings import BotConfig, CONFIG
from config.constants import (
    HIGH_CONVICTION_MIN_SCORE, HIGH_CONVICTION_MAX_STOP_PTS,
    SWEEP_MIN_SCORE, SWEEP_CONFLUENCE_BONUS, HTF_TIMEFRAMES,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# PIPELINE STATE
# ═══════════════════════════════════════════════════════════════

class PipelineState(Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    HALTED = "halted"
    ERROR = "error"


# ═══════════════════════════════════════════════════════════════
# IBKR LIVE PIPELINE
# ═══════════════════════════════════════════════════════════════

class IBKRLivePipeline:
    """
    Complete live trading pipeline for IBKR Client Portal Gateway.

    Owns all components and manages their lifecycle:
      - IBKRClient + IBKRDataFeed (market data)
      - Feature / signal / risk engines (unchanged signal logic)
      - SignalBridge (decision -> order translation)
      - IBKROrderExecutor (safety rails + order placement)
      - PositionManager (tracking + reconciliation + P&L)
    """

    def __init__(
        self,
        bot_config: BotConfig = CONFIG,
        ibkr_config: Optional[IBKRConfig] = None,
        executor_config: Optional[ExecutorConfig] = None,
    ):
        self._bot_config = bot_config

        # ── IBKR infrastructure ──
        self._ibkr_config = ibkr_config or IBKRConfig()
        self._client = IBKRClient(self._ibkr_config)
        self._executor_config = executor_config or ExecutorConfig()
        # ── Concurrency guard ──
        # Prevents overlapping bar processing and ensures reconciliation
        # cannot interleave with trading logic at await points.
        self._bar_lock = asyncio.Lock()

        self._executor = IBKROrderExecutor(
            self._client, self._executor_config
        )
        self._position_manager = PositionManager(
            self._client, self._executor, trade_lock=self._bar_lock
        )
        self._bridge = SignalBridge(bot_config.risk)
        self._data_feed = IBKRDataFeed(self._client)

        # ── Signal pipeline (unchanged from TradingOrchestrator) ──
        self._feature_engine = NQFeatureEngine(bot_config)
        self._signal_aggregator = SignalAggregator(bot_config)
        self._risk_engine = RiskEngine(bot_config)
        self._regime_detector = RegimeDetector(bot_config)
        self._htf_engine = HTFBiasEngine(
            config=bot_config,
            timeframes=list(HTF_TIMEFRAMES),
        )
        self._sweep_detector = LiquiditySweepDetector()

        # ── Pipeline state ──
        self._state = PipelineState.IDLE
        self._htf_bias: Optional[HTFBiasResult] = None
        self._current_regime = "unknown"
        self._bars_processed = 0
        self._last_bar: Optional[Bar] = None

        # ── Active trade tracking ──
        self._active_group_id: Optional[str] = None

    # ──────────────────────────────────────────────────────────
    # LIFECYCLE
    # ──────────────────────────────────────────────────────────

    async def start(self) -> bool:
        """
        Start the full pipeline:
          1. Connect to IBKR Gateway
          2. Resolve MNQ contract
          3. Start data feed (backfill + streaming)
          4. Start reconciliation loop
          5. Wire bar callback
        """
        if self._state == PipelineState.RUNNING:
            logger.warning("Pipeline already running")
            return True

        self._state = PipelineState.STARTING
        logger.info("=" * 60)
        logger.info("IBKR LIVE PIPELINE — STARTING")
        logger.info("=" * 60)

        try:
            # 1. Connect
            connected = await self._client.connect()
            if not connected:
                self._state = PipelineState.ERROR
                logger.error("Failed to connect to IBKR Gateway")
                return False

            # 2. Verify contract was resolved during connect()
            if not self._client.contract:
                self._state = PipelineState.ERROR
                logger.error("Failed to resolve MNQ contract")
                return False

            # 3. Wire bar callback and start data feed
            self._data_feed.on_bar(self._on_bar)
            feed_started = await self._data_feed.start()
            if not feed_started:
                self._state = PipelineState.ERROR
                logger.error("Failed to start data feed")
                return False

            # 4. Start reconciliation
            await self._position_manager.start_reconciliation_loop()

            # 5. Reset daily counters
            self._executor.reset_daily()
            self._position_manager.reset_daily()

            self._state = PipelineState.RUNNING
            logger.info("IBKR Live Pipeline RUNNING")
            return True

        except Exception as e:
            self._state = PipelineState.ERROR
            logger.critical("Pipeline start failed: %s", e, exc_info=True)
            return False

    async def stop(self) -> None:
        """
        Graceful shutdown:
          1. Stop data feed
          2. Stop reconciliation loop
          3. Disconnect from IBKR
        """
        if self._state not in (PipelineState.RUNNING, PipelineState.HALTED):
            return

        self._state = PipelineState.STOPPING
        logger.info("IBKR Live Pipeline — stopping")

        await self._position_manager.stop_reconciliation_loop()
        await self._data_feed.stop()
        await self._client.disconnect()

        self._state = PipelineState.IDLE
        logger.info("IBKR Live Pipeline — stopped")

    # ──────────────────────────────────────────────────────────
    # BAR PROCESSING — the core pipeline
    # ──────────────────────────────────────────────────────────

    def _on_bar(self, bar: Bar) -> None:
        """
        Callback from IBKRDataFeed on every completed 2-minute bar.

        Dispatches to the async handler via the running event loop.
        IBKRDataFeed calls this synchronously, so we schedule
        the async work.
        """
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(self._process_bar_guarded(bar))
        else:
            asyncio.run(self._process_bar_guarded(bar))

    async def _process_bar_guarded(self, bar: Bar) -> Optional[Dict[str, Any]]:
        """Serialize bar processing — prevents concurrent mutations of shared state."""
        async with self._bar_lock:
            return await self._process_bar(bar)

    async def _process_bar(self, bar: Bar) -> Optional[Dict[str, Any]]:
        """
        Process one bar through the complete pipeline.

        Signal logic is IDENTICAL to TradingOrchestrator.process_bar():
          features -> HTF bias -> regime -> sweeps -> signals -> HC filter -> risk

        Execution path is adapted for IBKR:
          TradeDecision -> SignalBridge -> IBKROrderExecutor -> PositionManager
        """
        if self._state != PipelineState.RUNNING:
            return None

        if self._executor.is_halted:
            return None

        # === 0. SESSION VALIDITY — halt if gateway session expired ===
        if hasattr(self, '_client') and self._client and not self._client.is_connected:
            logger.error(
                "Gateway session invalid — halting pipeline to prevent "
                "silent order failures"
            )
            self._state = PipelineState.HALTED
            self._executor._state.is_halted = True
            self._executor._state.halt_reason = "Gateway session expired"
            return None

        self._bars_processed += 1
        self._last_bar = bar

        # === 1. FEATURES ===
        features = self._feature_engine.update(bar)

        # === 2. HTF BIAS ===
        htf_bias = self._htf_bias

        # === 3. REGIME ===
        bars_list = self._feature_engine._bars
        avg_vol = (
            sum(b.volume for b in bars_list[-20:]) / 20
            if len(bars_list) >= 20 else bar.volume
        )
        self._current_regime = self._regime_detector.classify(
            current_atr=features.atr_14,
            current_vix=features.vix_level or 0,
            trend_direction=features.trend_direction,
            trend_strength=features.trend_strength,
            current_volume=bar.volume,
            avg_volume=avg_vol,
            is_overnight=self._risk_engine.state.is_overnight,
            near_news_event=self._risk_engine.state.upcoming_news_event,
        )
        regime_adj = self._regime_detector.get_regime_adjustments(
            self._current_regime
        )

        # === 3b. LIQUIDITY SWEEP DETECTOR ===
        sweep_signal = None
        et_time = bar.timestamp.astimezone(ZoneInfo("America/New_York"))
        h, m = et_time.hour, et_time.minute
        t = h + m / 60.0
        is_rth = 9.5 <= t < 16.0

        sweep_signal = self._sweep_detector.update_bar(
            bar=bar,
            vwap=features.session_vwap,
            htf_bias=htf_bias,
            is_rth=is_rth,
        )

        # === 4. SKIP IF POSITION ACTIVE ===
        if self._active_group_id is not None:
            return None

        # === 5. SIGNAL AGGREGATION ===
        signal = self._signal_aggregator.aggregate(
            feature_snapshot=features,
            ml_prediction=None,
            htf_bias=htf_bias,
            current_time=bar.timestamp,
        )

        # Determine entry source (identical to main.py logic)
        has_signal = signal and signal.should_trade
        has_sweep = (
            sweep_signal is not None
            and sweep_signal.score >= SWEEP_MIN_SCORE
        )

        entry_direction = None
        entry_score = 0.0
        entry_source = None

        if has_signal and has_sweep:
            direction_str = (
                "long" if signal.direction == SignalDirection.LONG
                else "short"
            )
            sweep_dir = (
                "long" if sweep_signal.direction == "LONG" else "short"
            )
            if direction_str == sweep_dir:
                entry_direction = direction_str
                entry_score = signal.combined_score + SWEEP_CONFLUENCE_BONUS
                entry_source = "confluence"
            else:
                entry_direction = direction_str
                entry_score = signal.combined_score
                entry_source = "signal"
        elif has_signal:
            entry_direction = (
                "long" if signal.direction == SignalDirection.LONG
                else "short"
            )
            entry_score = signal.combined_score
            entry_source = "signal"
        elif has_sweep:
            entry_direction = (
                "long" if sweep_signal.direction == "LONG" else "short"
            )
            entry_score = sweep_signal.score
            entry_source = "sweep"

        if entry_direction is None:
            return None

        # === NaN GUARD — NaN comparisons always return False, bypassing gates ===
        if not math.isfinite(entry_score):
            logger.error("HC REJECT: entry_score is NaN/Inf — blocking trade")
            return None

        # === 6. HIGH-CONVICTION GATE 1 — min score ===
        if entry_score < HIGH_CONVICTION_MIN_SCORE:
            return None

        # === 7. RISK CHECK ===
        risk_assessment = self._risk_engine.evaluate_trade(
            direction=entry_direction,
            entry_price=bar.close,
            atr=features.atr_14,
            vix=features.vix_level or 0,
            current_time=bar.timestamp,
        )

        raw_stop = risk_assessment.suggested_stop_distance

        if not math.isfinite(raw_stop):
            logger.error("HC REJECT: stop distance is NaN/Inf — blocking trade")
            return None

        # === 8. HIGH-CONVICTION GATE 2 — stop distance cap ===
        if raw_stop > HIGH_CONVICTION_MAX_STOP_PTS:
            return None

        # Regime gate
        if regime_adj["size_multiplier"] == 0:
            return None

        if risk_assessment.decision not in (
            RiskDecision.APPROVE, RiskDecision.REDUCE_SIZE
        ):
            return None

        # ═══════════════════════════════════════════════════
        # IBKR EXECUTION PATH (replaces ScaleOutExecutor)
        # ═══════════════════════════════════════════════════

        # Build TradeDecision from signal pipeline output
        decision = TradeDecision(
            direction=entry_direction,
            entry_price=bar.close,
            signal_score=entry_score,
            atr=features.atr_14,
            htf_bias=(
                htf_bias.consensus_direction if htf_bias else "neutral"
            ),
            htf_allows_long=(
                htf_bias.htf_allows_long if htf_bias else False
            ),
            htf_allows_short=(
                htf_bias.htf_allows_short if htf_bias else False
            ),
            entry_source=entry_source,
            market_regime=self._current_regime,
            timestamp=bar.timestamp,
        )

        # Translate decision -> order params
        bridge_result = self._bridge.translate(decision)
        if not bridge_result.approved:
            logger.info(
                "Bridge rejected: %s", bridge_result.rejection_reason
            )
            return None

        # Execute via IBKR order executor
        params = bridge_result.params
        records = await self._executor.place_scale_out_entry(
            direction=params.direction,
            limit_price=params.limit_price,
            stop_loss=params.stop_loss,
            c1_take_profit=params.c1_take_profit,
        )

        c1_record = records["c1"]
        c2_record = records["c2"]

        if not c1_record.accepted:
            logger.warning(
                "C1 rejected by executor: %s",
                c1_record.rejection_reason,
            )
            return None

        # Register with PositionManager
        group_id = str(uuid.uuid4())[:12]
        self._active_group_id = group_id

        self._position_manager.open_position(
            position_id=f"{group_id}-C1",
            broker_order_id=c1_record.broker_order_id,
            side=entry_direction,
            contracts=1,
            entry_price=c1_record.fill_price,
            tag="C1",
            group_id=group_id,
        )

        if c2_record.accepted:
            self._position_manager.open_position(
                position_id=f"{group_id}-C2",
                broker_order_id=c2_record.broker_order_id,
                side=entry_direction,
                contracts=1,
                entry_price=c2_record.fill_price,
                tag="C2",
                group_id=group_id,
            )
        else:
            logger.warning(
                "PARTIAL FILL: C1 filled but C2 rejected: %s",
                c2_record.rejection_reason,
            )
            self._position_manager.mark_partial_fill(group_id, "C2")

        action_result = {
            "action": "entry",
            "timestamp": bar.timestamp.isoformat(),
            "direction": entry_direction,
            "contracts": 2 if c2_record.accepted else 1,
            "c1_fill_price": c1_record.fill_price,
            "c2_fill_price": (
                c2_record.fill_price if c2_record.accepted else None
            ),
            "stop_loss": params.stop_loss,
            "c1_take_profit": params.c1_take_profit,
            "signal_score": entry_score,
            "entry_source": entry_source,
            "regime": self._current_regime,
            "group_id": group_id,
            "metadata": bridge_result.metadata,
        }

        logger.info(
            "ENTRY: %s %d×MNQ @ %.2f | stop=%.2f target=%.2f "
            "| score=%.3f source=%s group=%s",
            entry_direction.upper(),
            action_result["contracts"],
            c1_record.fill_price,
            params.stop_loss,
            params.c1_take_profit,
            entry_score,
            entry_source,
            group_id,
        )

        return action_result

    # ──────────────────────────────────────────────────────────
    # POSITION CLOSE — called externally on exit signals
    # ──────────────────────────────────────────────────────────

    def close_position(
        self,
        position_id: str,
        exit_price: float,
        exit_reason: str = "",
    ) -> None:
        """
        Close a tracked position immediately.

        Updates PositionManager -> feeds P&L to executor -> checks
        kill switch.  If both legs closed, clears active group.
        """
        self._position_manager.close_position(
            position_id, exit_price, exit_reason
        )

        # Check if the full group is now closed
        if self._active_group_id:
            group = self._position_manager.get_scale_out_group(
                self._active_group_id
            )
            if group and group.is_fully_closed:
                logger.info(
                    "Group %s fully closed — P&L: $%.2f",
                    self._active_group_id,
                    group.total_net_pnl,
                )
                self._active_group_id = None

    # ──────────────────────────────────────────────────────────
    # HTF BAR ROUTING
    # ──────────────────────────────────────────────────────────

    def process_htf_bar(self, timeframe: str, bar: Bar) -> None:
        """Route a higher-timeframe bar to the HTF Bias Engine."""
        from features.htf_engine import HTFBar
        htf_bar = HTFBar(
            timestamp=bar.timestamp,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        self._htf_engine.update_bar(timeframe, htf_bar)
        self._htf_bias = self._htf_engine.get_bias(bar.timestamp)

    # ──────────────────────────────────────────────────────────
    # STATUS
    # ──────────────────────────────────────────────────────────

    @property
    def state(self) -> PipelineState:
        return self._state

    @property
    def bars_processed(self) -> int:
        return self._bars_processed

    def get_status(self) -> Dict[str, Any]:
        """Full pipeline health snapshot."""
        htf = self._htf_bias
        return {
            "pipeline_state": self._state.value,
            "bars_processed": self._bars_processed,
            "current_regime": self._current_regime,
            "htf_consensus": (
                htf.consensus_direction if htf else "n/a"
            ),
            "htf_strength": htf.consensus_strength if htf else 0,
            "active_group_id": self._active_group_id,
            "executor": self._executor.get_status(),
            "positions": self._position_manager.get_status(),
            "bridge": {
                "translations": self._bridge.translations,
                "rejections": self._bridge.rejections,
            },
            "data_feed": self._data_feed.get_status(),
        }
