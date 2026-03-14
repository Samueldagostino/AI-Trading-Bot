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
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple

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
    HIGH_CONVICTION_MIN_STOP_PTS,
    SWEEP_MIN_SCORE, SWEEP_CONFLUENCE_BONUS, HTF_TIMEFRAMES,
    CONTEXT_AGGREGATOR_BOOST, CONTEXT_OB_BOOST, CONTEXT_FVG_BOOST,
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
        self._c3_delayed_entry = bot_config.scale_out.c3_delayed_entry_enabled
        # Track C3 state per group for delayed entry logic
        # Maps group_id -> {"c3_position_id": str, "c3_broker_order_id": str}
        self._c3_tracking: Dict[str, Dict[str, str]] = {}

        # ── Trade management state (mirrors scale_out_executor.py) ──
        self._trade_phase: TradePhase = TradePhase.DONE
        self._trade_direction: Optional[str] = None  # "long" or "short"
        self._entry_price: float = 0.0
        self._initial_stop: float = 0.0
        self._stop_distance: float = 0.0
        self._atr_at_entry: float = 0.0
        self._entry_time: Optional[datetime] = None
        self._c2_target_price: float = 0.0

        # C1 bar counter (Phase 1)
        self._c1_bars_elapsed: int = 0

        # Per-leg state: {position_id: {best_price, mfe, stop, bars_since_active,
        #                                be_triggered, trailing_stop, exit_strategy}}
        self._leg_state: Dict[str, Dict[str, Any]] = {}

        # Scale-out config reference
        self._scale_config = bot_config.scale_out

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
            self._executor.reset_daily()
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

        if self._executor.is_halted:
            return None

        # === MAINTENANCE WINDOW CHECKS (must be FIRST -- Axiom 2) ===
        from datetime import time as dt_time
        bar_et = bar.timestamp.astimezone(ZoneInfo("America/New_York"))
        current_time_et = bar_et.time()

        # Hard flatten at 4:50 PM ET -- close ALL positions unconditionally
        if current_time_et >= dt_time(MAINTENANCE_FLATTEN_HOUR, MAINTENANCE_FLATTEN_MINUTE):
            if self._active_group_id is not None:
                logger.warning(
                    "MAINTENANCE FLATTEN: Closing all positions -- "
                    "10 minutes to maintenance halt"
                )
                try:
                    await self._executor.flatten_all(reason="MAINTENANCE_FLATTEN")
                except Exception as e:
                    logger.error("Maintenance flatten failed: %s", e)
                self._active_group_id = None
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
            self._executor._state.is_halted = True
            self._executor._state.halt_reason = "Gateway session expired"
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
        if self._active_group_id is not None:
            result = await self._manage_active_trade(bar)
            return result

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

        # Execute via IBKR order executor (5-contract: C1+C2+C3)
        params = bridge_result.params
        records = await self._executor.place_scale_out_entry(
            direction=params.direction,
            limit_price=params.limit_price,
            stop_loss=params.stop_loss,
            c1_take_profit=params.c1_take_profit,
            c3_contracts=getattr(self._scale_config, 'c3_contracts', 3),
        )

        c1_record = records["c1"]
        c2_record = records["c2"]
        c3_record = records["c3"]

        if not c1_record.accepted:
            logger.warning(
                "C1 rejected by executor: %s",
                c1_record.rejection_reason,
            )
            return None

        # Register with PositionManager
        group_id = str(uuid.uuid4())[:12]
        self._active_group_id = group_id

        # ── Initialize trade management state ──
        self._trade_phase = TradePhase.PHASE_1
        self._trade_direction = entry_direction
        self._entry_price = c1_record.fill_price
        self._initial_stop = params.stop_loss
        self._stop_distance = bridge_result.metadata.get("stop_distance_pts", 0.0)
        self._atr_at_entry = features.atr_14
        self._entry_time = bar.timestamp
        self._c1_bars_elapsed = 0
        self._leg_state.clear()

        # C2 target: use BACKTEST-IDENTICAL 2×R formula, NOT bridge's ATR target.
        # Backtest: structural_target=0 -> fallback = 2 × stop_distance from entry.
        # Validated in backtest at 2×R; bridge uses ATR×1.5 which differs.
        c2_fallback_dist = self._stop_distance * 2.0
        if entry_direction == "long":
            self._c2_target_price = round(
                c1_record.fill_price + c2_fallback_dist, 2
            )
        else:
            self._c2_target_price = round(
                c1_record.fill_price - c2_fallback_dist, 2
            )
        # Validate: C2 target must be at least 1×R away
        min_target_dist = self._stop_distance * 1.0
        if entry_direction == "long":
            if self._c2_target_price - c1_record.fill_price < min_target_dist:
                self._c2_target_price = round(
                    c1_record.fill_price + self._stop_distance * 2.0, 2
                )
        else:
            if c1_record.fill_price - self._c2_target_price < min_target_dist:
                self._c2_target_price = round(
                    c1_record.fill_price - self._stop_distance * 2.0, 2
                )

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

        if c3_record.accepted:
            self._position_manager.open_position(
                position_id=f"{group_id}-C3",
                broker_order_id=c3_record.broker_order_id,
                side=entry_direction,
                contracts=3,
                entry_price=c3_record.fill_price,
                tag="C3",
                group_id=group_id,
            )
            # Track C3 for delayed entry logic
            self._c3_tracking[group_id] = {
                "c3_position_id": f"{group_id}-C3",
                "c3_broker_order_id": c3_record.broker_order_id,
            }
        else:
            logger.warning(
                "PARTIAL FILL: C3 rejected: %s",
                c3_record.rejection_reason,
            )
            self._position_manager.mark_partial_fill(group_id, "C3")

        # ── Initialize per-leg state for trade management ──
        fill_price = c1_record.fill_price
        self._leg_state[f"{group_id}-C1"] = {
            "best_price": fill_price,
            "mfe": 0.0,
            "stop_price": params.stop_loss,
            "bars_since_active": 0,
            "be_triggered": False,
            "trailing_stop": 0.0,
            "exit_strategy": "time_5bar",
            "contracts": 1,
        }
        if c2_record.accepted:
            self._leg_state[f"{group_id}-C2"] = {
                "best_price": fill_price,
                "mfe": 0.0,
                "stop_price": params.stop_loss,
                "bars_since_active": 0,
                "be_triggered": False,
                "trailing_stop": 0.0,
                "exit_strategy": "structural_target",
                "target_price": self._c2_target_price,
                "contracts": 1,
            }
        if c3_record.accepted:
            self._leg_state[f"{group_id}-C3"] = {
                "best_price": fill_price,
                "mfe": 0.0,
                "stop_price": params.stop_loss,
                "bars_since_active": 0,
                "be_triggered": False,
                "trailing_stop": 0.0,
                "exit_strategy": "atr_trail",
                "contracts": 3,
            }

        # Compute total contracts entered
        total_contracts = 1  # C1 always accepted at this point
        if c2_record.accepted:
            total_contracts += 1
        if c3_record.accepted:
            total_contracts += 3

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
            "c3_contracts": getattr(self._scale_config, 'c3_contracts', 3) if c3_record.accepted else 0,
            "stop_loss": params.stop_loss,
            "c1_take_profit": params.c1_take_profit,
            "signal_score": entry_score,
            "entry_source": entry_source,
            "regime": self._current_regime,
            "group_id": group_id,
            "metadata": bridge_result.metadata,
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
            "ENTRY: %s %d×MNQ @ %.2f | stop=%.2f target=%.2f "
            "| score=%.3f source=%s group=%s [C3=%s]",
            entry_direction.upper(),
            total_contracts,
            c1_record.fill_price,
            params.stop_loss,
            params.c1_take_profit,
            entry_score,
            entry_source,
            group_id,
            "active" if c3_record.accepted else "rejected",
        )

        # Phase 3 additive: start FVG tracking in background
        self._start_fvg_tracking(entry_direction, features.atr_14)

        return action_result

    # ──────────────────────────────────────────────────────────
    # TRADE MANAGEMENT STATE MACHINE
    # Mirrors scale_out_executor.py exit logic exactly
    # ──────────────────────────────────────────────────────────

    async def _manage_active_trade(self, bar: Bar) -> Optional[Dict[str, Any]]:
        """
        Bar-by-bar trade management -- the core exit logic.

        Routes to Phase 1 or Scaling management based on current phase.
        This mirrors ScaleOutExecutor.update() from the backtest engine.
        """
        if self._trade_phase == TradePhase.DONE:
            return None

        # Safety net: if all legs were externally closed (reconciliation,
        # broker disconnect, etc.), finalize the trade gracefully.
        remaining = self._get_open_leg_ids()
        if not remaining:
            logger.warning(
                "All legs externally closed for group %s -- finalizing",
                self._active_group_id,
            )
            self._finalize_trade()
            return {"action": "trade_closed", "reason": "external_close"}

        price = bar.close
        current_time = bar.timestamp

        if self._trade_phase == TradePhase.PHASE_1:
            return await self._manage_phase_1(price, current_time, bar)

        elif self._trade_phase == TradePhase.SCALING:
            return await self._manage_scaling(price, current_time, bar)

        return None

    async def _manage_phase_1(
        self, price: float, current_time: datetime, bar: Bar
    ) -> Optional[Dict[str, Any]]:
        """
        Phase 1: All contracts share initial stop. C1 runs its 5-bar timer.

        Mirrors scale_out_executor._manage_phase_1() exactly:
        - Check initial stop (all legs)
        - Count C1 bars
        - C1 5-bar time exit (if unrealized >= 3.0 pts)
        - C1 12-bar fallback (if unrealized > 0)
        - C2 structural target check (fast move during Phase 1)
        """
        direction = self._trade_direction
        group_id = self._active_group_id

        # --- Check STOP (all contracts) ---
        stop_hit = False
        if direction == "long" and price <= self._initial_stop:
            stop_hit = True
        elif direction == "short" and price >= self._initial_stop:
            stop_hit = True

        if stop_hit:
            return await self._close_all_legs(price, current_time, "stop")

        # --- Count bars ---
        self._c1_bars_elapsed += 1

        # --- Compute unrealized profit ---
        if direction == "long":
            unrealized = price - self._entry_price
        else:
            unrealized = self._entry_price - price

        # --- Update MFE and best prices for all open legs ---
        self._update_leg_tracking(price, direction, unrealized)

        # --- C2 structural target check during Phase 1 (fast move) ---
        c2_pos_id = f"{group_id}-C2"
        if c2_pos_id in self._leg_state:
            c2_state = self._leg_state[c2_pos_id]
            target = c2_state.get("target_price", 0.0)
            if target > 0:
                target_hit = False
                if direction == "long" and price >= target:
                    target_hit = True
                elif direction == "short" and price <= target:
                    target_hit = True
                if target_hit:
                    self._close_leg(c2_pos_id, target, current_time,
                                    "structural_target")
                    logger.info(
                        "C2 STRUCTURAL TARGET HIT during Phase 1 @ %.2f",
                        target,
                    )

        # --- B:5 time-based exit: exit C1 after 5 bars if profitable ---
        c1_exit_bars = self._scale_config.c1_time_exit_bars
        if self._c1_bars_elapsed >= c1_exit_bars:
            c1_profit_threshold = getattr(self._scale_config, 'c1_profit_threshold_pts', 3.0)

            c1_in_profit = unrealized >= c1_profit_threshold
            if c1_in_profit:
                return await self._transition_c1_to_scaling(
                    round(price, 2), current_time,
                    f"time_{c1_exit_bars}bars",
                )

        # --- Fallback: max bars, exit if any profit ---
        max_bars = self._scale_config.c1_max_bars_fallback
        if self._c1_bars_elapsed >= max_bars:
            if unrealized > 0:
                return await self._transition_c1_to_scaling(
                    round(price, 2), current_time,
                    f"time_{max_bars}bars_fallback",
                )

        return None

    async def _transition_c1_to_scaling(
        self, exit_price: float, current_time: datetime, reason: str
    ) -> Dict[str, Any]:
        """
        Close C1 and transition remaining legs to independent management.

        Mirrors scale_out_executor._transition_c1_to_scaling() exactly:
        - Close C1
        - If C1 profitable: move C2/C3 stops to breakeven (entry + 2pt buffer)
        - If C1 lost: close C3 immediately (delayed block)
        - Transition to SCALING phase
        """
        direction = self._trade_direction
        group_id = self._active_group_id
        c1_pos_id = f"{group_id}-C1"

        # Close C1
        self._close_leg(c1_pos_id, exit_price, current_time, reason)

        # Determine C1 profitability
        group = self._position_manager.get_scale_out_group(group_id)
        c1_net_pnl = group.c1.net_pnl if group and group.c1 else 0.0

        if direction == "long":
            c1_profit_pts = round(exit_price - self._entry_price, 2)
        else:
            c1_profit_pts = round(self._entry_price - exit_price, 2)

        c1_was_profitable = c1_net_pnl > 0

        # ── BREAKEVEN on remaining legs after C1 profit ──
        # Variant B (delayed): skip immediate BE -- _apply_delayed_be() handles it
        # during SCALING once MFE >= 1.5× stop_distance.
        BE_BUFFER_PTS = self._scale_config.c2_breakeven_buffer_points
        be_variant = getattr(self._scale_config, "c2_be_variant", "B")
        c3_blocked = False

        if c1_was_profitable and be_variant != "B":
            # Variant D / A / C: immediate breakeven (original behavior)
            for pos_id, state in list(self._leg_state.items()):
                if pos_id == c1_pos_id:
                    continue
                if pos_id not in self._position_manager.open_positions:
                    continue
                if direction == "long":
                    be_stop = round(self._entry_price + BE_BUFFER_PTS, 2)
                    if be_stop > state["stop_price"]:
                        state["stop_price"] = be_stop
                        state["be_triggered"] = True
                else:
                    be_stop = round(self._entry_price - BE_BUFFER_PTS, 2)
                    if be_stop < state["stop_price"]:
                        state["stop_price"] = be_stop
                        state["be_triggered"] = True

            be_legs = [pid.split("-")[-1] for pid, s in self._leg_state.items()
                       if s.get("be_triggered") and pid != c1_pos_id]
            if be_legs:
                logger.info(
                    "BE TRIGGERED (immediate) on %s after C1 profit | New stop: %.2f",
                    be_legs,
                    self._entry_price + (BE_BUFFER_PTS if direction == "long"
                                         else -BE_BUFFER_PTS),
                )
        elif c1_was_profitable and be_variant == "B":
            # Variant B: delayed breakeven -- keep original stops
            threshold = round(self._stop_distance * self._scale_config.c2_be_delay_multiplier, 1)
            open_leg_labels = [pid.split("-")[-1] for pid, s in self._leg_state.items()
                               if pid != c1_pos_id and pid in self._position_manager.open_positions]
            logger.info(
                "BE DELAYED (Variant B) on %s | C1 profitable but BE requires MFE >= %.1fpts "
                "(%.1fx stop %.1fpts) | Keeping original stops",
                open_leg_labels, threshold,
                self._scale_config.c2_be_delay_multiplier, self._stop_distance,
            )
        else:
            # C1 lost → close C3 immediately (delayed block)
            c3_pos_id = f"{group_id}-C3"
            if (self._c3_delayed_entry
                    and c3_pos_id in self._leg_state
                    and c3_pos_id in self._position_manager.open_positions):
                c3_blocked = True
                self._close_leg(c3_pos_id, exit_price, current_time,
                                "c3_delayed_blocked")
                if group:
                    group.c3_blocked = True
                self._c3_tracking.pop(group_id, None)
                logger.info(
                    "C3 DELAYED BLOCKED at C1 exit: C1 net PnL $%.2f <= 0 | "
                    "C3 closed at market",
                    c1_net_pnl,
                )

        logger.info(
            "C1 EXIT (%s) @ bar %d | Price: %.2f (%.1fpts) | "
            "C1 PnL: $%.2f | BE applied: %s | C3 blocked: %s",
            reason, self._c1_bars_elapsed, exit_price, c1_profit_pts,
            c1_net_pnl, c1_was_profitable, c3_blocked,
        )

        # Check if all legs are now closed
        remaining = self._get_open_leg_ids()
        if not remaining:
            self._finalize_trade()
            return {
                "action": "trade_closed",
                "reason": f"c1_{reason}_all_closed",
                "c1_pnl": c1_net_pnl,
                "c3_blocked": c3_blocked,
            }

        # Transition to SCALING phase
        self._trade_phase = TradePhase.SCALING
        # Reset bars_since_active for remaining legs
        for pos_id in remaining:
            if pos_id in self._leg_state:
                self._leg_state[pos_id]["bars_since_active"] = 0

        return {
            "action": "c1_exit",
            "c1_pnl": c1_net_pnl,
            "c1_bars": self._c1_bars_elapsed,
            "remaining_legs": [pid.split("-")[-1] for pid in remaining],
            "c3_blocked": c3_blocked,
        }

    async def _manage_scaling(
        self, price: float, current_time: datetime, bar: Bar
    ) -> Optional[Dict[str, Any]]:
        """
        Manage remaining legs independently after C1 exits.

        Mirrors scale_out_executor._manage_scaling() exactly:
        - Per-leg stop check
        - C2: structural target + 20-bar time stop + delayed BE
        - C3: ATR trailing stop + 150pt max target + 2hr time stop + delayed BE
        """
        direction = self._trade_direction
        group_id = self._active_group_id
        closed_legs = []

        for pos_id in list(self._get_open_leg_ids()):
            state = self._leg_state.get(pos_id)
            if not state:
                continue

            # Update per-leg tracking
            state["bars_since_active"] += 1

            if direction == "long":
                state["best_price"] = max(state["best_price"], price)
                unrealized = price - self._entry_price
            else:
                state["best_price"] = min(state["best_price"], price)
                unrealized = self._entry_price - price

            if unrealized > 0:
                state["mfe"] = max(state["mfe"], unrealized)

            # ------ STOP CHECK (per-leg stop) ------
            stop_to_check = state["stop_price"]
            stop_hit = False
            if direction == "long" and price <= stop_to_check:
                stop_hit = True
            elif direction == "short" and price >= stop_to_check:
                stop_hit = True

            if stop_hit:
                exit_reason = ("trailing" if state["trailing_stop"] > 0
                               else ("breakeven" if state["be_triggered"]
                                     else "stop"))
                self._close_leg(pos_id, stop_to_check, current_time,
                                exit_reason)
                closed_legs.append(pos_id.split("-")[-1])
                continue

            # ------ PER-LEG EXIT STRATEGY ------
            strategy = state["exit_strategy"]

            if strategy == "structural_target":
                # C2: Exit at structural target (swing point)
                target = state.get("target_price", 0.0)
                if target > 0:
                    target_hit = False
                    if direction == "long" and price >= target:
                        target_hit = True
                    elif direction == "short" and price <= target:
                        target_hit = True
                    if target_hit:
                        self._close_leg(pos_id, target, current_time,
                                        "structural_target")
                        closed_legs.append(pos_id.split("-")[-1])
                        continue

                # C2: Time stop -- max bars (configurable, default 35 bars)
                c2_max_bars = getattr(self._scale_config, 'c2_time_stop_bars', 35)
                if state["bars_since_active"] >= c2_max_bars:
                    self._close_leg(pos_id, round(price, 2), current_time,
                                    f"time_{c2_max_bars}bars")
                    closed_legs.append(pos_id.split("-")[-1])
                    continue

                # C2: Delayed BE (Variant B)
                self._apply_delayed_be(state, direction)

            elif strategy == "atr_trail":
                # C3: Pure ATR trailing stop (runner)
                self._update_atr_trail(state, direction)

                # C3: Max target safety valve (150pts)
                points_from_entry = abs(price - self._entry_price)
                max_target_pts = getattr(self._scale_config, 'c3_max_target_points', 300.0)
                if points_from_entry >= max_target_pts:
                    self._close_leg(pos_id, round(price, 2), current_time,
                                    "c3_max_target")
                    closed_legs.append(pos_id.split("-")[-1])
                    continue

                # C3: Time stop (4 hours — full session runway)
                if self._entry_time:
                    elapsed_min = (
                        (current_time - self._entry_time).total_seconds() / 60
                    )
                    if elapsed_min >= getattr(self._scale_config, 'c3_time_stop_minutes', 240):
                        self._close_leg(pos_id, round(price, 2), current_time,
                                        "time_stop")
                        closed_legs.append(pos_id.split("-")[-1])
                        continue

                # C3: Delayed BE
                self._apply_delayed_be(state, direction)

        # Check if all legs closed
        remaining = self._get_open_leg_ids()
        if not remaining:
            self._finalize_trade()
            return {
                "action": "trade_closed",
                "reason": "all_legs_exited",
                "closed_legs": closed_legs,
            }

        if closed_legs:
            return {
                "action": "legs_closed",
                "closed": closed_legs,
                "remaining": [pid.split("-")[-1] for pid in remaining],
            }

        return None

    # ──────────────────────────────────────────────────────────
    # TRADE MANAGEMENT HELPERS
    # ──────────────────────────────────────────────────────────

    def _update_leg_tracking(
        self, price: float, direction: str, unrealized: float
    ) -> None:
        """Update best_price and MFE for all open legs."""
        for pos_id in self._get_open_leg_ids():
            state = self._leg_state.get(pos_id)
            if not state:
                continue
            if direction == "long":
                state["best_price"] = max(state["best_price"], price)
            else:
                state["best_price"] = min(state["best_price"], price)
            if unrealized > 0:
                state["mfe"] = max(state["mfe"], unrealized)

    def _apply_delayed_be(self, state: Dict[str, Any], direction: str) -> None:
        """
        Variant B: Move stop to breakeven once MFE >= stop_distance × 1.5.

        Mirrors scale_out_executor._apply_delayed_be() exactly.
        The breakeven trigger is DELAYED until the trade proves itself.
        """
        if state["be_triggered"]:
            return

        be_multiplier = self._scale_config.c2_be_delay_multiplier
        threshold = self._stop_distance * be_multiplier

        if state["mfe"] >= threshold:
            buf = self._scale_config.c2_breakeven_buffer_points
            if direction == "long":
                new_stop = round(self._entry_price + buf, 2)
                if new_stop > state["stop_price"]:
                    state["stop_price"] = new_stop
                    state["be_triggered"] = True
            else:
                new_stop = round(self._entry_price - buf, 2)
                if new_stop < state["stop_price"]:
                    state["stop_price"] = new_stop
                    state["be_triggered"] = True

            if state["be_triggered"]:
                logger.info(
                    "%s BREAKEVEN ACTIVATED: MFE %.1fpts reached threshold "
                    "%.1fpts -- stop moved to %.2f",
                    state.get("leg_label", "C2"), state["mfe"],
                    threshold, state["stop_price"],
                )
        else:
            pct = round(state["mfe"] / threshold * 100, 1) if threshold > 0 else 0
            logger.debug(
                "%s breakeven DELAYED: MFE %.1fpts < threshold %.1fpts "
                "(%s%% of target) -- keeping original stop at %.2f",
                state.get("leg_label", "C2"), state["mfe"],
                threshold, pct, state["stop_price"],
            )

    def _update_atr_trail(self, state: Dict[str, Any], direction: str) -> None:
        """
        Update ATR-based trailing stop for C3 runner: trail = best_price - (ATR × multiplier).

        Mirrors scale_out_executor._update_atr_trail() exactly.
        Only tightens (never widens the stop).
        """
        multiplier = getattr(self._scale_config, 'c3_trailing_atr_multiplier', 3.0)
        distance = self._atr_at_entry * multiplier

        if direction == "long":
            new_trail = state["best_price"] - distance
            if new_trail > state["stop_price"]:
                state["stop_price"] = round(new_trail, 2)
                state["trailing_stop"] = state["stop_price"]
        else:
            new_trail = state["best_price"] + distance
            if new_trail < state["stop_price"]:
                state["stop_price"] = round(new_trail, 2)
                state["trailing_stop"] = state["stop_price"]

    def _close_leg(
        self,
        position_id: str,
        exit_price: float,
        current_time: datetime,
        exit_reason: str,
    ) -> None:
        """
        Close a single leg via PositionManager.

        Handles the case where the position is already closed
        (non-fill, reconciliation, etc.) gracefully.
        """
        # Remove from leg state tracking
        self._leg_state.pop(position_id, None)

        # Close via PositionManager (computes P&L, feeds to executor)
        if position_id in self._position_manager.open_positions:
            self._position_manager.close_position(
                position_id, exit_price, exit_reason
            )
        else:
            logger.warning(
                "close_leg: position %s not in open_positions "
                "(already closed or non-fill)",
                position_id,
            )

    async def _close_all_legs(
        self, price: float, current_time: datetime, reason: str
    ) -> Dict[str, Any]:
        """
        Close all open legs at once (initial stop hit during Phase 1).

        Mirrors scale_out_executor._close_all() exactly:
        - When Phase 1 stop is hit, all legs close
        - C3 is marked as blocked (it never proved direction)
        """
        group_id = self._active_group_id
        direction = self._trade_direction

        # Determine if C3 should be blocked
        c3_pos_id = f"{group_id}-C3"
        c3_should_block = (
            self._c3_delayed_entry
            and self._trade_phase == TradePhase.PHASE_1
            and reason == "stop"
            and c3_pos_id in self._leg_state
        )

        for pos_id in list(self._get_open_leg_ids()):
            if c3_should_block and pos_id == c3_pos_id:
                self._close_leg(pos_id, price, current_time,
                                "c3_delayed_blocked")
            else:
                self._close_leg(pos_id, price, current_time, reason)

        # Mark C3 as blocked in the group
        if c3_should_block:
            group = self._position_manager.get_scale_out_group(group_id)
            if group:
                group.c3_blocked = True
            self._c3_tracking.pop(group_id, None)

        self._finalize_trade()

        return {
            "action": "trade_closed",
            "reason": f"phase1_{reason}",
            "c3_blocked": c3_should_block,
        }

    def _get_open_leg_ids(self) -> List[str]:
        """Get position IDs of legs that are still open."""
        group_id = self._active_group_id
        if not group_id:
            return []
        open_ids = []
        for tag in ("C1", "C2", "C3"):
            pos_id = f"{group_id}-{tag}"
            if (pos_id in self._leg_state
                    and pos_id in self._position_manager.open_positions):
                open_ids.append(pos_id)
        return open_ids

    def _finalize_trade(self) -> None:
        """
        Clean up trade state after all legs are closed.

        Logs final P&L, resets state, clears active group.
        """
        group_id = self._active_group_id
        if group_id:
            group = self._position_manager.get_scale_out_group(group_id)
            if group:
                logger.info(
                    "TRADE COMPLETE: group=%s total_pnl=$%.2f "
                    "c3_blocked=%s",
                    group_id, group.total_net_pnl, group.c3_blocked,
                )

        # Reset all trade state
        self._trade_phase = TradePhase.DONE
        self._trade_direction = None
        self._entry_price = 0.0
        self._initial_stop = 0.0
        self._stop_distance = 0.0
        self._atr_at_entry = 0.0
        self._entry_time = None
        self._c2_target_price = 0.0
        self._c1_bars_elapsed = 0
        self._leg_state.clear()
        self._c3_tracking.pop(group_id, None)
        self._active_group_id = None

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

        # Remove from leg state if tracked
        self._leg_state.pop(position_id, None)

        # Check if the full group is now closed
        if self._active_group_id:
            remaining = self._get_open_leg_ids()
            if not remaining:
                self._finalize_trade()

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
            "active_group_id": self._active_group_id,
            "executor": self._executor.get_status(),
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
