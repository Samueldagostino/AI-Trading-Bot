"""
IBKR Live Pipeline Orchestrator v1.3.1
=========================================
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
from datetime import datetime, timezone
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
from execution.scale_out_executor import ScaleOutExecutor

from config.settings import BotConfig, CONFIG
from config.constants import (
    HIGH_CONVICTION_MIN_SCORE, HIGH_CONVICTION_MAX_STOP_PTS,
    HIGH_CONVICTION_MIN_STOP_PTS,
    SWEEP_MIN_SCORE, SWEEP_CONFLUENCE_BONUS, HTF_TIMEFRAMES,
    CONTEXT_AGGREGATOR_BOOST, CONTEXT_OB_BOOST, CONTEXT_FVG_BOOST,
    RANGING_BLOCK_LONGS,
    RTH_ENTRY_CUTOFF_HOUR, RTH_ENTRY_CUTOFF_MINUTE,
    MAINTENANCE_FLATTEN_HOUR, MAINTENANCE_FLATTEN_MINUTE,
    EVENING_SESSION_OPEN_HOUR,
)
from data_feeds.market_context import MarketContext
from data_feeds.quantdata_client import QuantDataClient

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Phase 3 Additive: Post-Sweep FVG Tracking (data collection only)
# ICT model: Sweep → Displacement → FVG → Retracement
# Entries are IMMEDIATE (Phase 1 behavior). FVG detection runs in
# background for data collection -- no entry delay, no score changes.
# ═══════════════════════════════════════════════════════════════
SWEEP_FVG_TRACKING = True
SWEEP_FVG_DISPLACEMENT_WINDOW = 5       # Bars after sweep to find displacement candle
SWEEP_FVG_DISPLACEMENT_MIN_ATR = 0.8    # Displacement candle body >= 0.8× ATR
SWEEP_FVG_RETRACEMENT_WINDOW = 8        # Bars after FVG to wait for retracement
SWEEP_FVG_MIN_GAP_ATR = 0.3            # Minimum FVG size (gap_size >= 0.3 × ATR)


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


class TradePhase(Enum):
    """Current phase of the active trade -- mirrors ScaleOutPhase from backtest."""
    PHASE_1 = "phase_1"     # All contracts open, C1 5-bar timer running
    SCALING = "scaling"     # C1 exited, managing C2/C3 independently
    DONE = "done"           # Trade complete


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

        self._ibkr_executor = IBKROrderExecutor(
            self._client, self._executor_config
        )
        self._position_manager = PositionManager(
            self._client, self._ibkr_executor, trade_lock=self._bar_lock
        )
        self._bridge = SignalBridge(bot_config.risk)
        self._data_feed = IBKRDataFeed(self._client)

        # ── Scale-out executor for trade management ──
        # Handles all trade logic: Phase 1, C1 exit, scaling, trailing, breakeven
        self._executor = ScaleOutExecutor(bot_config)

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
        # Map from executor's internal trade ID to IBKR group_id for broker reconciliation
        self._executor_to_group_id: Dict[str, str] = {}
        # Legacy C3 delayed entry tracking (kept for broker order reconciliation)
        self._c3_delayed_entry = bot_config.scale_out.c3_delayed_entry_enabled

        # ── QuantData market context (LOG-ONLY) ──
        self._quantdata_client = QuantDataClient()
        self._market_context: Optional[MarketContext] = None
        self._last_context_refresh: Optional[datetime] = None
        self._context_refresh_minutes = 30

        # ── Phase 3 Additive: FVG Tracking (background data collection) ──
        self._sweep_event: Optional[Dict] = None   # Last sweep for FVG tracking
        self._sweep_fvg: Optional[Dict] = None      # FVG formed after sweep
        self._sweep_fvg_stats = {
            "sweeps_tracked": 0,
            "displacement_found": 0,
            "fvg_formed": 0,
            "fvg_retrace_confirmed": 0,
        }

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
        logger.info("IBKR LIVE PIPELINE -- STARTING")
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
            self._ibkr_executor.reset_daily()
            self._position_manager.reset_daily()

            # 6. Initial market context refresh (LOG-ONLY, non-blocking)
            try:
                self._market_context = await self._quantdata_client.get_market_context()
                self._last_context_refresh = datetime.now(timezone.utc)
                logger.info(
                    "Market context loaded: gamma=%s, flow=%s, source=%s",
                    self._market_context.gamma_regime,
                    self._market_context.flow_direction,
                    self._market_context.source,
                )
            except Exception as e:
                logger.warning("Initial market context refresh failed (non-fatal): %s", e)
                self._market_context = None

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
        logger.info("IBKR Live Pipeline -- stopping")

        await self._position_manager.stop_reconciliation_loop()
        await self._data_feed.stop()
        await self._client.disconnect()

        self._state = PipelineState.IDLE
        logger.info("IBKR Live Pipeline -- stopped")

    # ──────────────────────────────────────────────────────────
    # BAR PROCESSING -- the core pipeline
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
        """Serialize bar processing -- prevents concurrent mutations of shared state."""
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

        if self._ibkr_executor.is_halted:
            return None

        # === MAINTENANCE WINDOW CHECKS (must be FIRST -- Axiom 2) ===
        from datetime import time as dt_time
        bar_et = bar.timestamp.astimezone(ZoneInfo("America/New_York"))
        current_time_et = bar_et.time()

        # Hard flatten at 4:50 PM ET -- close ALL positions unconditionally
        if current_time_et >= dt_time(MAINTENANCE_FLATTEN_HOUR, MAINTENANCE_FLATTEN_MINUTE):
            if self._executor.has_active_trade:
                logger.warning(
                    "MAINTENANCE FLATTEN: Closing all positions -- "
                    "10 minutes to maintenance halt"
                )
                try:
                    await self._ibkr_executor.flatten_all(reason="MAINTENANCE_FLATTEN")
                except Exception as e:
                    logger.error("Maintenance flatten failed: %s", e)
            return None  # No further processing after 4:50 PM ET

        # Entry cutoff at 3:30 PM ET -- block new entries
        self._maintenance_entry_blocked = current_time_et >= dt_time(RTH_ENTRY_CUTOFF_HOUR, RTH_ENTRY_CUTOFF_MINUTE)

        # === 0. SESSION VALIDITY -- halt if gateway session expired ===
        if hasattr(self, '_client') and self._client and not self._client.is_connected:
            logger.error(
                "Gateway session invalid -- halting pipeline to prevent "
                "silent order failures"
            )
            self._state = PipelineState.HALTED
            self._ibkr_executor._state.is_halted = True
            self._ibkr_executor._state.halt_reason = "Gateway session expired"
            return None

        self._bars_processed += 1
        self._last_bar = bar

        # === 0b. MARKET CONTEXT REFRESH (every 30 min, LOG-ONLY) ===
        await self._refresh_market_context_if_needed()

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

        # === 3c. PHASE 3 ADDITIVE: FVG STATE MACHINE (data collection) ===
        self._update_sweep_fvg_state(bar)

        # === 4. MANAGE ACTIVE TRADE (if any) ===
        if self._executor.has_active_trade:
            try:
                bar_high = bar.high if math.isfinite(bar.high) else None
                bar_low = bar.low if math.isfinite(bar.low) else None
                result = await self._executor.update(
                    bar.close, bar.timestamp,
                    bar_high=bar_high, bar_low=bar_low,
                )
                if result:
                    # Executor returned an action (exit, etc.) -- handle it
                    return await self._handle_executor_result(result, bar)
                return None
            except Exception as e:
                logger.error("executor.update() failed: %s", e, exc_info=True)
                return None

        # === 4b. MAINTENANCE WINDOW ENTRY CUTOFF ===
        if getattr(self, '_maintenance_entry_blocked', False):
            logger.info(
                "BLOCKED: New entry rejected -- past 3:30 PM ET cutoff "
                "(maintenance window protection)"
            )
            return None

        # === 5. SIGNAL AGGREGATION ===
        signal = self._signal_aggregator.aggregate(
            feature_snapshot=features,
            ml_prediction=None,
            htf_bias=htf_bias,
            current_time=bar.timestamp,
        )

        # PATH C: Sweep-only trigger architecture (mirrors main.py)
        has_signal = signal and signal.should_trade
        has_sweep = (
            sweep_signal is not None
            and sweep_signal.score >= SWEEP_MIN_SCORE
        )

        entry_direction = None
        entry_score = 0.0
        entry_source = None

        if has_sweep:
            entry_direction = (
                "long" if sweep_signal.direction == "LONG" else "short"
            )
            entry_score = sweep_signal.score
            entry_source = "sweep"
            # Layer 2 context boost from aggregator alignment
            if has_signal:
                signal_dir = (
                    "long" if signal.direction == SignalDirection.LONG
                    else "short"
                )
                if signal_dir == entry_direction:
                    entry_score += CONTEXT_AGGREGATOR_BOOST
            # Layer 2 structural context boosts
            if features:
                if entry_direction == "long":
                    if getattr(features, 'near_bullish_ob', False):
                        entry_score += CONTEXT_OB_BOOST
                    if getattr(features, 'inside_bullish_fvg', False):
                        entry_score += CONTEXT_FVG_BOOST
                elif entry_direction == "short":
                    if getattr(features, 'near_bearish_ob', False):
                        entry_score += CONTEXT_OB_BOOST
                    if getattr(features, 'inside_bearish_fvg', False):
                        entry_score += CONTEXT_FVG_BOOST
        # Aggregator alone cannot trigger (PATH C)

        if entry_direction is None:
            return None

        # === HTF DIRECTIONAL GATE (softened: score penalty instead of block) ===
        # A sweep IS a reversal -- HTF disagreement is expected. Penalize -0.10.
        if htf_bias is not None:
            htf_disagrees = False
            if entry_direction == "long" and not htf_bias.htf_allows_long:
                htf_disagrees = True
            if entry_direction == "short" and not htf_bias.htf_allows_short:
                htf_disagrees = True
            if htf_disagrees:
                entry_score -= 0.10
                logger.info(
                    "HTF BIAS PENALTY: %s penalized -0.10 -- HTF %s (%.2f) "
                    "[source=%s, new_score=%.2f]",
                    entry_direction, htf_bias.consensus_direction,
                    htf_bias.consensus_strength, entry_source, entry_score,
                )
        else:
            # Fail-safe: no HTF data → block all trades
            logger.warning(
                "HTF GATE BLOCK: %s blocked -- no HTF data [source=%s]",
                entry_direction, entry_source,
            )
            return None

        # === NaN GUARD -- NaN comparisons always return False, bypassing gates ===
        if not math.isfinite(entry_score):
            logger.error("HC REJECT: entry_score is NaN/Inf -- blocking trade")
            return None

        # === 6. HIGH-CONVICTION GATE 1 -- min score ===
        if entry_score < HIGH_CONVICTION_MIN_SCORE:
            logger.info(
                "HC GATE REJECT: %s score=%.2f < %.2f [source=%s, levels=%s]",
                entry_direction, entry_score, HIGH_CONVICTION_MIN_SCORE,
                entry_source,
                getattr(sweep_signal, 'swept_levels', []) if sweep_signal else [],
            )
            return None

        # === 7. RISK CHECK ===
        # Pass sweep structural stop so risk engine uses min(structural, ATR)
        structural_stop = None
        if sweep_signal and sweep_signal.entry_price and sweep_signal.stop_price:
            structural_stop = abs(sweep_signal.entry_price - sweep_signal.stop_price)

        risk_assessment = self._risk_engine.evaluate_trade(
            direction=entry_direction,
            entry_price=bar.close,
            atr=features.atr_14,
            vix=features.vix_level or 0,
            current_time=bar.timestamp,
            structural_stop_distance=structural_stop,
        )

        raw_stop = risk_assessment.suggested_stop_distance

        if not math.isfinite(raw_stop):
            logger.error("HC REJECT: stop distance is NaN/Inf -- blocking trade")
            return None

        # === 8. HIGH-CONVICTION GATE 2 -- stop distance floor ===
        if raw_stop < HIGH_CONVICTION_MIN_STOP_PTS:
            logger.info(
                "STOP FLOOR REJECT: %s stop=%.1f < %.1f min [score=%.2f, source=%s, levels=%s]",
                entry_direction, raw_stop, HIGH_CONVICTION_MIN_STOP_PTS,
                entry_score, entry_source,
                getattr(sweep_signal, 'swept_levels', []) if sweep_signal else [],
            )
            return None

        # === 9. HIGH-CONVICTION GATE 3 -- stop distance cap ===
        if raw_stop > HIGH_CONVICTION_MAX_STOP_PTS:
            logger.info(
                "STOP CAP REJECT: %s stop=%.1f > %.1f max [score=%.2f, source=%s, levels=%s]",
                entry_direction, raw_stop, HIGH_CONVICTION_MAX_STOP_PTS,
                entry_score, entry_source,
                getattr(sweep_signal, 'swept_levels', []) if sweep_signal else [],
            )
            return None

        # Regime gate
        if regime_adj["size_multiplier"] == 0:
            logger.info(
                "REGIME REJECT: %s blocked -- regime size_multiplier=0 [score=%.2f]",
                entry_direction, entry_score,
            )
            return None

        # Ranging longs filter: ranging longs are toxic (28.6% WR), shorts are OK
        if RANGING_BLOCK_LONGS and self._current_regime == "ranging" and entry_direction == "long":
            logger.info(
                "RANGING LONG REJECT: longs blocked in ranging regime [score=%.2f]",
                entry_score,
            )
            return None

        if risk_assessment.decision not in (
            RiskDecision.APPROVE, RiskDecision.REDUCE_SIZE
        ):
            logger.info(
                "RISK REJECT: %s blocked -- risk decision=%s [score=%.2f]",
                entry_direction, risk_assessment.decision, entry_score,
            )
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

        # Delegate entry to ScaleOutExecutor (handles all trade logic)
        stop_distance = bridge_result.metadata.get("stop_distance_pts", 0.0)

        # Use executor to create and enter the trade
        trade = await self._executor.enter_trade(
            direction=entry_direction,
            entry_price=bar.close,
            stop_distance=stop_distance,
            atr=features.atr_14,
            timestamp=bar.timestamp,
            signal_score=entry_score,
            signal_source=entry_source,
            htf_bias=htf_bias.consensus_direction if htf_bias else "neutral",
            regime=self._current_regime,
        )

        if not trade:
            logger.warning("ScaleOutExecutor.enter_trade() returned None")
            return None

        if trade.c1.contracts == 0:
            logger.warning("C1 not allocated by executor")
            return None

        # Now route through IBKR order executor to place actual broker orders
        # This is the BROKER-SPECIFIC part that remains
        group_id = trade.trade_id
        self._executor_to_group_id[trade.trade_id] = group_id

        records = await self._ibkr_executor.place_scale_out_entry(
            direction=entry_direction,
            limit_price=bar.close,
            stop_loss=trade.initial_stop,
            c1_take_profit=0.0,  # Executor manages timing, not bridge
            c3_contracts=trade.c3.contracts if trade.c3.contracts > 0 else 3,
        )

        c1_record = records["c1"]
        c2_record = records["c2"]
        c3_record = records["c3"]

        if not c1_record.accepted:
            logger.warning("C1 rejected by IBKR executor: %s", c1_record.rejection_reason)
            return None

        # Register filled orders with PositionManager for reconciliation
        # The executor owns the trade logic; broker execution just fills it
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
                "PARTIAL FILL: C2 rejected: %s",
                c2_record.rejection_reason,
            )
            self._position_manager.mark_partial_fill(group_id, "C2")

        if c3_record.accepted:
            self._position_manager.open_position(
                position_id=f"{group_id}-C3",
                broker_order_id=c3_record.broker_order_id,
                side=entry_direction,
                contracts=c3_record.contracts if hasattr(c3_record, 'contracts') else 3,
                entry_price=c3_record.fill_price,
                tag="C3",
                group_id=group_id,
            )
        else:
            logger.warning(
                "PARTIAL FILL: C3 rejected: %s",
                c3_record.rejection_reason,
            )
            self._position_manager.mark_partial_fill(group_id, "C3")

        # Compute total contracts entered
        total_contracts = 1  # C1 always accepted at this point
        if c2_record.accepted:
            total_contracts += 1
        if c3_record.accepted:
            total_contracts += (c3_record.contracts if hasattr(c3_record, 'contracts') else 3)

        # Build market context snapshot for logging
        ctx = self._market_context
        action_result = {
            "action": "entry",
            "timestamp": bar.timestamp.isoformat(),
            "direction": entry_direction,
            "contracts": total_contracts,
            "c1_fill_price": c1_record.fill_price,
            "c2_fill_price": (
                c2_record.fill_price if c2_record.accepted else None
            ),
            "c3_fill_price": (
                c3_record.fill_price if c3_record.accepted else None
            ),
            "c3_contracts": (c3_record.contracts if hasattr(c3_record, 'contracts') else 3) if c3_record.accepted else 0,
            "stop_loss": trade.initial_stop,
            "signal_score": entry_score,
            "entry_source": entry_source,
            "regime": self._current_regime,
            "group_id": group_id,
            # QuantData market context (LOG-ONLY)
            "market_context": ctx.to_dict() if ctx else None,
            "gamma_regime_at_entry": ctx.gamma_regime if ctx else "unknown",
            "flow_aligned_with_trade": (
                ctx.aligns_with_direction(entry_direction) if ctx else None
            ),
            "favorable_for_momentum": (
                ctx.is_favorable_for_momentum() if ctx else None
            ),
        }

        logger.info(
            "ENTRY: %s %d×MNQ @ %.2f | stop=%.2f | score=%.3f source=%s group=%s [C3=%s]",
            entry_direction.upper(),
            total_contracts,
            c1_record.fill_price,
            trade.initial_stop,
            entry_score,
            entry_source,
            group_id,
            "active" if c3_record.accepted else "rejected",
        )

        # Phase 3 additive: start FVG tracking in background
        self._start_fvg_tracking(entry_direction, features.atr_14)

        return action_result

    # ──────────────────────────────────────────────────────────
    # TRADE RESULT HANDLER
    # Routes executor results to broker execution
    # ──────────────────────────────────────────────────────────

    async def _handle_executor_result(self, result: Dict[str, Any], bar: Bar) -> Dict[str, Any]:
        """
        Handle results from ScaleOutExecutor.update().

        The executor owns all trade logic (Phase 1, C1 exit, scaling, trailing, breakeven).
        This method translates executor actions into broker order closure/management.

        Executor returns action dict with:
          - action: "trade_closed", "c1_exit", "legs_closed", etc.
          - direction, entry_price, exit_price, total_pnl, etc.
        """
        if not result:
            return None

        action = result.get("action")

        if action == "trade_closed":
            # All legs closed -- finalize the trade
            trade_id = self._executor.active_trade.trade_id if self._executor.active_trade else None
            group_id = self._executor_to_group_id.get(trade_id) if trade_id else None

            if group_id and group_id in self._executor_to_group_id.values():
                # Close all remaining broker positions for this group
                for tag in ("C1", "C2", "C3"):
                    pos_id = f"{group_id}-{tag}"
                    if pos_id in self._position_manager.open_positions:
                        self._position_manager.close_position(
                            pos_id,
                            bar.close,
                            result.get("exit_reason", "executor_closed")
                        )
                logger.info(
                    "TRADE CLOSED: %s | entry=%.2f exit=%.2f | pnl=$%.2f | reason=%s",
                    group_id,
                    result.get("entry_price", 0.0),
                    result.get("exit_price", bar.close),
                    result.get("total_pnl", 0.0),
                    result.get("exit_reason", "unknown"),
                )
            return result

        elif action == "c1_exit":
            # C1 exited -- close C1 position in broker, others continue
            trade_id = self._executor.active_trade.trade_id if self._executor.active_trade else None
            group_id = self._executor_to_group_id.get(trade_id) if trade_id else None
            if group_id:
                c1_pos_id = f"{group_id}-C1"
                if c1_pos_id in self._position_manager.open_positions:
                    self._position_manager.close_position(
                        c1_pos_id,
                        bar.close,
                        result.get("exit_reason", "c1_exit")
                    )
                logger.info(
                    "C1 EXITED: %s | bars=%d | c1_pnl=$%.2f | c3_blocked=%s",
                    group_id,
                    result.get("c1_bars", 0),
                    result.get("c1_pnl", 0.0),
                    result.get("c3_blocked", False),
                )
            return result

        # Other actions (legs_closed, etc.) -- executor manages these internally,
        # broker positions remain open until executor signals full close
        return result

    def _close_broker_leg(self, position_id: str, exit_price: float, reason: str) -> None:
        """Close a broker position via PositionManager."""
        if position_id in self._position_manager.open_positions:
            self._position_manager.close_position(position_id, exit_price, reason)
        else:
            logger.debug(
                "Position %s already closed or not in manager",
                position_id,
            )

    # ──────────────────────────────────────────────────────────
    # PHASE 3 ADDITIVE: FVG BACKGROUND TRACKING (data only)
    # ──────────────────────────────────────────────────────────

    def _start_fvg_tracking(self, direction: str, atr: float) -> None:
        """Start FVG tracking after a sweep triggers an entry."""
        if not SWEEP_FVG_TRACKING:
            return
        self._sweep_event = {
            "direction": direction,
            "atr": atr,
            "sweep_bar_index": self._bars_processed,
            "displacement_found": False,
            "recent_bars": [],
        }
        self._sweep_fvg = None
        self._sweep_fvg_stats["sweeps_tracked"] += 1

    def _update_sweep_fvg_state(self, bar: Bar) -> None:
        """Phase 3 additive: background FVG tracking (data collection only).

        Tracks displacement → FVG → retracement after each sweep.
        Does NOT affect entries -- all entries happen immediately (Phase 1).
        Data is collected for future analysis and potential strategy refinement.
        """
        if self._sweep_event is None:
            return

        event = self._sweep_event
        bars_since_sweep = self._bars_processed - event["sweep_bar_index"]

        # Timeout: stop tracking after displacement + retracement windows
        max_window = SWEEP_FVG_DISPLACEMENT_WINDOW + SWEEP_FVG_RETRACEMENT_WINDOW
        if bars_since_sweep > max_window:
            self._sweep_event = None
            self._sweep_fvg = None
            return

        bar_data = {
            "high": bar.high, "low": bar.low,
            "open": bar.open, "close": bar.close,
        }

        # Stage 1: Watch for displacement candle
        if not event["displacement_found"] and bars_since_sweep <= SWEEP_FVG_DISPLACEMENT_WINDOW:
            event["recent_bars"].append(bar_data)

            atr = event["atr"] or 20.0
            body_size = abs(bar.close - bar.open)
            if body_size >= atr * SWEEP_FVG_DISPLACEMENT_MIN_ATR:
                if event["direction"] == "long" and bar.close > bar.open:
                    event["displacement_found"] = True
                    self._sweep_fvg_stats["displacement_found"] += 1
                elif event["direction"] == "short" and bar.close < bar.open:
                    event["displacement_found"] = True
                    self._sweep_fvg_stats["displacement_found"] += 1
        elif not event["displacement_found"]:
            event["recent_bars"].append(bar_data)

        # Stage 2: Check for FVG formation
        if event["displacement_found"] and self._sweep_fvg is None:
            if bars_since_sweep > 0 and (
                not event["recent_bars"] or event["recent_bars"][-1]["high"] != bar.high
            ):
                event["recent_bars"].append(bar_data)

            recent = event["recent_bars"]
            if len(recent) >= 3:
                bar_a = recent[-3]
                bar_c = recent[-1]
                atr = event["atr"] or 20.0

                if event["direction"] == "long" and bar_c["low"] > bar_a["high"]:
                    gap_size = bar_c["low"] - bar_a["high"]
                    if gap_size >= atr * SWEEP_FVG_MIN_GAP_ATR:
                        self._sweep_fvg = {
                            "high": bar_c["low"], "low": bar_a["high"],
                            "type": "bullish", "size": gap_size,
                            "formed_bar": self._bars_processed,
                        }
                        self._sweep_fvg_stats["fvg_formed"] += 1

                elif event["direction"] == "short" and bar_c["high"] < bar_a["low"]:
                    gap_size = bar_a["low"] - bar_c["high"]
                    if gap_size >= atr * SWEEP_FVG_MIN_GAP_ATR:
                        self._sweep_fvg = {
                            "high": bar_a["low"], "low": bar_c["high"],
                            "type": "bearish", "size": gap_size,
                            "formed_bar": self._bars_processed,
                        }
                        self._sweep_fvg_stats["fvg_formed"] += 1

        # Stage 3: Check for retracement into FVG
        if self._sweep_fvg is not None:
            fvg = self._sweep_fvg
            if event["direction"] == "long" and bar.low <= fvg["high"]:
                self._sweep_fvg_stats["fvg_retrace_confirmed"] += 1
                self._sweep_event = None
                self._sweep_fvg = None
            elif event["direction"] == "short" and bar.high >= fvg["low"]:
                self._sweep_fvg_stats["fvg_retrace_confirmed"] += 1
                self._sweep_event = None
                self._sweep_fvg = None

    # ──────────────────────────────────────────────────────────
    # POSITION CLOSE -- external API (for reconciliation/emergency)
    # ──────────────────────────────────────────────────────────

    def close_position(
        self,
        position_id: str,
        exit_price: float,
        exit_reason: str = "",
    ) -> None:
        """
        Close a tracked position immediately (external API).

        Called by reconciliation or emergency flatten -- NOT by the
        trade management state machine (which uses _close_leg directly).

        Updates PositionManager -> feeds P&L to executor -> checks
        kill switch. Also cleans up trade management state.
        """
        self._position_manager.close_position(
            position_id, exit_price, exit_reason
        )

        # If the executor has an active trade, flatten it through the
        # unified execution engine so all state (legs, PnL, logs) stays
        # consistent.  This is the emergency / reconciliation path.
        if self._executor.has_active_trade:
            logger.warning(
                "close_position(%s) — flattening active executor trade at %.2f",
                position_id, exit_price,
            )
            self._executor.emergency_flatten(exit_price)

    # ──────────────────────────────────────────────────────────
    # MARKET CONTEXT REFRESH (LOG-ONLY)
    # ──────────────────────────────────────────────────────────

    async def _refresh_market_context_if_needed(self) -> None:
        """Refresh market context every 30 minutes during RTH."""
        now = datetime.now(timezone.utc)
        if (
            self._last_context_refresh is None
            or (now - self._last_context_refresh).total_seconds()
            >= self._context_refresh_minutes * 60
        ):
            try:
                self._market_context = (
                    await self._quantdata_client.get_market_context()
                )
                self._last_context_refresh = now
            except Exception as e:
                logger.warning("Market context refresh failed (non-fatal): %s", e)

    @property
    def market_context(self) -> Optional[MarketContext]:
        """Current market context snapshot (may be None)."""
        return self._market_context

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
        # Feed HTF bars to sweep detector for HTF-first sweep detection
        self._sweep_detector.update_htf_bar(timeframe, htf_bar)

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
            "active_trade": self._executor.has_active_trade,
            "executor": self._executor.get_stats(),
            "positions": self._position_manager.get_status(),
            "bridge": {
                "translations": self._bridge.translations,
                "rejections": self._bridge.rejections,
            },
            "data_feed": self._data_feed.get_status(),
            "market_context": (
                self._market_context.to_dict() if self._market_context else None
            ),
        }
