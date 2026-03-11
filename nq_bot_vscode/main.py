"""
Main Trading Orchestrator - Multi-Timeframe Edition
=====================================================
Coordinates: HTF Bias -> Data -> Features -> Signals -> Risk -> Scale-Out -> Monitoring

Architecture:
  HTF bars -> HTF Bias Engine -> directional gate
  Execution TF bar -> features -> signal (gated by HTF) -> HC filter -> risk -> trade

Two operating modes:
  BACKTEST: Multi-TF TradingView CSVs -> synchronized processing -> report
  LIVE:     Tradovate WebSocket -> real-time pipeline -> paper/live execution
"""

import asyncio
import logging
import math
import sys
import numpy as np
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List
from zoneinfo import ZoneInfo

from config.settings import BotConfig, CONFIG
from config.constants import (
    HIGH_CONVICTION_MIN_SCORE, HIGH_CONVICTION_MAX_STOP_PTS,
    SWEEP_MIN_SCORE, SWEEP_CONFLUENCE_BONUS,
    HTF_TIMEFRAMES, EXECUTION_TIMEFRAMES,
    HTF_STRENGTH_GATE,
    UCL_FVG_CONFLUENCE_BOOST,
    CONTEXT_AGGREGATOR_BOOST, CONTEXT_OB_BOOST, CONTEXT_FVG_BOOST,
)
from config.validator import validate_config
from database.connection import DatabaseManager
from features.engine import NQFeatureEngine, Bar
from features.htf_engine import HTFBiasEngine, HTFBar, HTFBiasResult
from features.session_volatility import SessionVolatilityScaler
from signals.aggregator import SignalAggregator, SignalDirection
from signals.liquidity_sweep import LiquiditySweepDetector, SweepSignal
from signals.fvg_detector import FVGDetector
from signals.watch_state import WatchStateManager, WatchState, ConfirmedSignal
from signals.institutional_modifiers import InstitutionalModifierEngine
from risk.engine import RiskEngine, RiskDecision
from risk.regime_detector import RegimeDetector
from execution.scale_out_executor import ScaleOutExecutor
from monitoring.engine import MonitoringEngine
from monitoring.trade_decision_logger import TradeDecisionLogger
from monitoring.alerting import AlertManager, set_alert_manager, get_alert_manager
from monitoring.alert_templates import AlertTemplates
from data_pipeline.pipeline import (
    DataPipeline, BarData, MultiTimeframeIterator,
    bardata_to_bar, bardata_to_htfbar, MINUTES_TO_LABEL,
)

logger = logging.getLogger(__name__)



# All HC constants imported from config.constants (single source of truth)
MIN_RR_RATIO = 1.5  # Minimum risk/reward ratio for entry

# ── CONFIG D GATE ASSERTION ───────────────────────────────────────────
# Both HTFBiasEngine.STRENGTH_GATE and HTF_STRENGTH_GATE now source from
# config/constants.py (single source of truth).  This assertion catches
# drift if someone redefines the class attribute directly.
# ──────────────────────────────────────────────────────────────────────
assert HTFBiasEngine.STRENGTH_GATE == HTF_STRENGTH_GATE, (
    f"HTF gate drift detected! "
    f"HTFBiasEngine.STRENGTH_GATE={HTFBiasEngine.STRENGTH_GATE}, "
    f"expected {HTF_STRENGTH_GATE} (Config D, from config/constants.py). "
    f"Do NOT change without full backtest validation."
)


class TradingOrchestrator:
    """
    Main system orchestrator - multi-timeframe, HTF-gated, high-conviction.

    Pipeline per execution-TF bar:
    1. Route HTF bars to HTF Bias Engine (already done by timestamp)
    2. Compute execution-TF features (OB, FVG, sweeps, VWAP, delta)
    3. Get HTF bias consensus
    4. Detect market regime
    5. If in position: manage scale-out (C1 target, C2 trailing, stops)
    6. If flat: aggregate signals (gated by HTF) -> HC filter -> risk -> enter
    7. Log everything
    """

    def __init__(self, config: BotConfig = CONFIG):
        self.config = config

        # Core layers
        self.db = DatabaseManager(config.db.connection_params)
        self.feature_engine = NQFeatureEngine(config)
        self.signal_aggregator = SignalAggregator(config)
        self.risk_engine = RiskEngine(config)
        self.regime_detector = RegimeDetector(config)
        self.monitoring = MonitoringEngine(config)
        self.data_pipeline = DataPipeline(config, self.db)

        # HTF Bias Engine — multi-timeframe directional consensus
        self.htf_engine = HTFBiasEngine(
            config=config,
            timeframes=list(HTF_TIMEFRAMES),
        )
        self._htf_bias: Optional[HTFBiasResult] = None

        # Liquidity Sweep Detector — additive signal source
        self.sweep_detector = LiquiditySweepDetector()
        self._sweep_enabled = True  # Can be toggled for A/B testing

        # UCL — Universal Confirmation Layer (Phase 1)
        self._fvg_detector = FVGDetector()
        self._watch_manager = WatchStateManager()
        self._ucl_enabled = True  # Can be toggled for A/B testing

        # Session Volatility Scaler — U-shaped intraday ATR adjustment
        # Enabled via SESSION_VOLATILITY_SCALING env var (default: OFF)
        self._session_scaler = SessionVolatilityScaler()

        # Institutional Modifier Layer — Phase 1
        self._institutional_engine = InstitutionalModifierEngine()
        self._modifiers_enabled = True  # Can be toggled for A/B testing

        # Trade Decision Logger — records every approval and rejection
        self.decision_logger = TradeDecisionLogger(
            str(Path(__file__).resolve().parent / "logs")
        )

        # Execution - scale-out is the primary executor
        self.executor = ScaleOutExecutor(config)

        # Alerting — real-time notifications via console/Discord/Telegram
        self._alert_manager = AlertManager(
            config.alerting,
            rate_limit_seconds=config.alerting.rate_limit_seconds,
        )
        set_alert_manager(self._alert_manager)

        # Tradovate client (initialized on connect)
        self.broker_client = None

        # State
        self._running = False
        self._bars_processed = 0
        self._htf_bars_processed = 0
        self._current_regime = "unknown"
        self._execution_tf = "2m"     # Default execution timeframe
        self._last_trading_date = None  # Track session boundaries for VWAP reset

        # Shadow-trade rejection capture (set by ReplaySimulator)
        self._last_rejection = None   # Populated at each rejection point

        # Consecutive executor failure counter — escalates to emergency flatten
        self._executor_fail_count = 0
        self._EXECUTOR_FAIL_LIMIT = 5

        # Maintenance window entry cutoff flag
        self._maintenance_entry_blocked = False

    # ================================================================
    # INITIALIZATION
    # ================================================================
    async def initialize(self, skip_db: bool = False) -> None:
        """Initialize all components."""
        # ── Config validation — refuse to start with bad config ──
        config_errors = validate_config(self.config)
        if config_errors:
            raise SystemExit(
                f"Configuration validation failed with {len(config_errors)} errors. "
                "See log output above. Fix and restart."
            )
        logger.info("Config validation: PASSED")
        logger.info("=" * 60)
        logger.info("NQ TRADING BOT - INITIALIZING (MULTI-TIMEFRAME)")
        logger.info(f"  Environment:  {self.config.environment}")
        logger.info(f"  Broker:       Tradovate ({self.config.tradovate.environment})")
        logger.info(f"  Symbol:       {self.config.tradovate.symbol}")
        logger.info(f"  Strategy:     2-contract scale-out (HC filtered)")
        logger.info(f"  C1 Exit:      Trail from +{self.config.scale_out.c1_profit_threshold_pts}pts "
                     f"(trail {self.config.scale_out.c1_trail_distance_pts}pts, "
                     f"fallback {self.config.scale_out.c1_max_bars_fallback} bars)")
        logger.info(f"  HC Min Score: {HIGH_CONVICTION_MIN_SCORE}")
        logger.info(f"  HC Max Stop:  {HIGH_CONVICTION_MAX_STOP_PTS} pts")
        logger.info(f"  Account:      ${self.config.risk.account_size:,.2f}")
        logger.info(f"  Max Risk:     {self.config.risk.max_risk_per_trade_pct}% per trade")
        logger.info(f"  Daily Limit:  {self.config.risk.max_daily_loss_pct}%")
        logger.info(f"  Kill Switch:  {self.config.risk.max_total_drawdown_pct}% drawdown")
        logger.info(f"  HTF Engine:   {', '.join(sorted(HTF_TIMEFRAMES))}")
        logger.info(f"  HTF Gate:     {HTF_STRENGTH_GATE} (Config D)")
        logger.info(f"  Exec TF:      {self._execution_tf}")
        logger.info(f"  Sweep Det:    {'ENABLED' if self._sweep_enabled else 'DISABLED'} "
                     f"(min score {SWEEP_MIN_SCORE}, confluence +{SWEEP_CONFLUENCE_BONUS})")
        logger.info("=" * 60)

        if not skip_db:
            try:
                await self.db.initialize()
                if await self.db.health_check():
                    logger.info("Database: CONNECTED")
                    self.monitoring.update_health("data", "healthy")
            except Exception as e:
                logger.warning(f"Database not available: {e}. Running without persistence.")
                self.monitoring.update_health("data", "degraded", str(e))

        await self._load_economic_calendar()
        self._running = True
        self.monitoring.update_health("features", "healthy")
        self.monitoring.update_health("signals", "healthy")
        self.monitoring.update_health("risk", "healthy")
        self.monitoring.update_health("execution", "healthy")

        # Start alert manager background worker
        await self._alert_manager.start()
        self._alert_manager.enqueue(AlertTemplates.startup_complete(
            environment=self.config.environment,
            broker=self.config.tradovate.environment,
        ))

        logger.info("Orchestrator initialized")

    async def shutdown(self) -> None:
        """Graceful shutdown - flatten positions first."""
        logger.info("Initiating shutdown...")
        self._alert_manager.enqueue(AlertTemplates.shutdown_initiated("orchestrator_shutdown"))
        self._running = False

        if self.executor.has_active_trade:
            logger.warning("Flattening active position on shutdown")
            last_price = self._get_last_price()
            await self.executor.emergency_flatten(last_price)

        if self.broker_client:
            await self.broker_client.disconnect()

        await self.db.close()
        await self._alert_manager.stop()
        logger.info("Shutdown complete")

    # ================================================================
    # HTF BAR PROCESSING
    # ================================================================
    def process_htf_bar(self, timeframe: str, bar: BarData) -> None:
        """
        Route a higher-timeframe bar to the HTF Bias Engine.
        Called before execution-TF bars for the same timestamp window.
        """
        htf_bar = bardata_to_htfbar(bar)
        self.htf_engine.update_bar(timeframe, htf_bar)
        self._htf_bars_processed += 1
        # Update the cached bias
        self._htf_bias = self.htf_engine.get_bias(bar.timestamp)
        # Feed HTF bars to sweep detector for HTF-first sweep detection
        self.sweep_detector.update_htf_bar(timeframe, htf_bar)

    # ================================================================
    # MAIN EXECUTION-TF BAR PROCESSING
    # ================================================================
    async def process_bar(self, bar: Bar) -> Optional[dict]:
        """
        Process one execution-timeframe bar through the full pipeline.
        This is THE core method - called on every execution bar.
        """
        if not self._running:
            return None

        self._bars_processed += 1
        self._last_bar = bar
        self._last_rejection = None  # Clear previous rejection
        action_result = None

        # === MAINTENANCE WINDOW CHECKS (must be FIRST — Axiom 2: Survival Precedes Profit) ===
        from datetime import time as dt_time
        bar_et = bar.timestamp.astimezone(ZoneInfo("America/New_York"))
        current_time_et = bar_et.time()

        # Hard flatten at 4:50 PM ET — close ALL positions unconditionally
        if current_time_et >= dt_time(16, 50):
            if self.executor.has_active_trade:
                result = await self.executor.maintenance_flatten(
                    bar.close, bar.timestamp
                )
                if result:
                    total_pnl = result.get("total_pnl", 0.0)
                    if not math.isfinite(total_pnl):
                        total_pnl = 0.0
                    self.risk_engine.record_trade_result(total_pnl, result["direction"])
                    self.monitoring.record_trade({
                        "action": "exit",
                        "pnl": total_pnl,
                        "direction": result["direction"],
                    })
                    self.decision_logger.log_exit(
                        direction=result.get("direction", "UNKNOWN"),
                        entry_price=result.get("entry_price", 0.0),
                        exit_price=bar.close,
                        total_pnl=total_pnl,
                        exit_reason="EXIT_MAINTENANCE_FLATTEN",
                    )
                    action_result = result
            return action_result  # No further processing after 4:50 PM ET

        # Entry cutoff at 4:30 PM ET — block new entries, continue position management
        self._maintenance_entry_blocked = current_time_et >= dt_time(16, 30)

        # === SESSION BOUNDARY DETECTION — reset VWAP at new trading day ===
        bar_date = bar_et.date()
        if self._last_trading_date is not None and bar_date != self._last_trading_date:
            self.feature_engine.reset_session()
            logger.info("Session boundary: VWAP/delta reset for new day %s", bar_date)
        self._last_trading_date = bar_date

        # === 0. INSTITUTIONAL MODIFIER STATE UPDATE (every bar) ===
        if self._modifiers_enabled:
            try:
                self._institutional_engine.update_bar(bar)
            except Exception as e:
                logger.warning("Modifier engine update_bar failed (degraded): %s", e)

        # === 1. FEATURES (execution TF) ===
        features = self.feature_engine.update(bar)

        # === 2. HTF BIAS (already computed via process_htf_bar) ===
        htf_bias = self._htf_bias  # May be None if no HTF data yet

        # === 3. REGIME DETECTION ===
        bars_list = self.feature_engine._bars
        avg_vol = np.mean([b.volume for b in bars_list[-20:]]) if len(bars_list) >= 20 else bar.volume

        self._current_regime = self.regime_detector.classify(
            current_atr=features.atr_14,
            current_vix=features.vix_level or 0,
            trend_direction=features.trend_direction,
            trend_strength=features.trend_strength,
            current_volume=bar.volume,
            avg_volume=avg_vol,
            is_overnight=self.risk_engine.state.is_overnight,
            near_news_event=self.risk_engine.state.upcoming_news_event,
        )

        regime_adj = self.regime_detector.get_regime_adjustments(self._current_regime)

        # === 3b. LIQUIDITY SWEEP DETECTOR (additive, always runs) ===
        sweep_signal = None
        if self._sweep_enabled:
            # Determine if we're in RTH
            et_time = bar.timestamp.astimezone(ZoneInfo("America/New_York"))
            h, m = et_time.hour, et_time.minute
            t = h + m / 60.0
            is_rth = 9.5 <= t < 16.0

            sweep_signal = self.sweep_detector.update_bar(
                bar=bar,
                vwap=features.session_vwap,
                htf_bias=htf_bias,
                is_rth=is_rth,
            )

        # === 3c. FVG DETECTOR (UCL — always runs) ===
        if self._ucl_enabled:
            self._fvg_detector.update(
                bar=bar,
                bar_index=self._bars_processed,
                trend_direction=features.trend_direction,
            )

        # === 4. MANAGE ACTIVE POSITION ===
        if self.executor.has_active_trade:
            try:
                result = await self.executor.update(bar.close, bar.timestamp)
                self._executor_fail_count = 0  # Reset on success
            except Exception as e:
                self._executor_fail_count += 1
                logger.error(
                    "executor.update() raised (%d/%d): %s",
                    self._executor_fail_count, self._EXECUTOR_FAIL_LIMIT,
                    e, exc_info=True,
                )
                if self._executor_fail_count >= self._EXECUTOR_FAIL_LIMIT:
                    logger.critical(
                        "executor.update() failed %d times — emergency flatten",
                        self._executor_fail_count,
                    )
                    last_price = self._get_last_price()
                    await self.executor.emergency_flatten(last_price)
                    self._executor_fail_count = 0
                return action_result
            if result:
                if result.get("action") == "trade_closed":
                    result["close_timestamp"] = bar.timestamp.isoformat()
                    total_pnl = result["total_pnl"]
                    # NaN guard — prevent corrupted PnL from poisoning risk engine
                    if not math.isfinite(total_pnl):
                        logger.critical(
                            "NaN/Inf total_pnl from trade close — blocking risk update"
                        )
                        total_pnl = 0.0
                        result["total_pnl"] = 0.0
                        result["pnl_nan_guarded"] = True
                    self.risk_engine.record_trade_result(total_pnl, result["direction"])
                    self.monitoring.record_trade({
                        "action": "exit",
                        "pnl": total_pnl,
                        "direction": result["direction"],
                    })
                    # Log exit to decision logger
                    self.decision_logger.log_exit(
                        direction=result.get("direction", "UNKNOWN"),
                        entry_price=result.get("entry_price", 0.0),
                        exit_price=result.get("exit_price", bar.close),
                        total_pnl=total_pnl,
                        exit_reason=result.get("exit_type", "trade_closed"),
                    )

                    action_result = result

                elif result.get("action") == "c1_time_exit":
                    action_result = result

            return action_result

        # === 4b. MAINTENANCE WINDOW ENTRY CUTOFF ===
        if getattr(self, '_maintenance_entry_blocked', False):
            logger.info(
                "BLOCKED: New entry rejected — past 4:30 PM ET cutoff "
                "(maintenance window protection)"
            )
            return action_result

        # === 5. SIGNAL AGGREGATION (only if flat, HTF-gated) ===
        signal = self.signal_aggregator.aggregate(
            feature_snapshot=features,
            ml_prediction=None,
            htf_bias=htf_bias,
            current_time=bar.timestamp,
        )

        # === PATH C: 4-LAYER DECISION ENGINE ===
        # Layer 1: HTF Gate (hard filter — enforced in aggregator)
        # Layer 2: Structural context (aggregator, OB, FVG — boost sweep score)
        # Layer 3: Entry trigger (SWEEP ONLY — only sweeps generate trades)
        # Layer 4: Risk calibration (downstream HC gates + risk engine)
        #
        # Non-sweep signal sources are demoted to contextual score modifiers.
        # The aggregator alone CANNOT trigger trades.
        has_signal = signal and signal.should_trade
        has_sweep = (sweep_signal is not None and
                     sweep_signal.score >= SWEEP_MIN_SCORE)

        entry_direction = None
        entry_score = 0.0
        entry_source = None  # "sweep" or "ucl_confirmed_*" only
        sweep_stop_override = None

        if has_sweep:
            # Layer 3: Sweep is the ONLY entry trigger
            entry_direction = "long" if sweep_signal.direction == "LONG" else "short"
            entry_score = sweep_signal.score
            entry_source = "sweep"
            # Use sweep's stop price for tighter risk — validate first
            if sweep_signal.stop_price and sweep_signal.stop_price > 0:
                sweep_stop_override = abs(bar.close - sweep_signal.stop_price)
            else:
                sweep_stop_override = None  # Fall back to ATR-based stop

            # Layer 2: Contextual boosts from aggregator alignment
            if has_signal:
                signal_dir = "long" if signal.direction == SignalDirection.LONG else "short"
                if signal_dir == entry_direction:
                    entry_score += CONTEXT_AGGREGATOR_BOOST
                    logger.info(
                        f"CONTEXT BOOST: aggregator {signal_dir} agrees | "
                        f"+{CONTEXT_AGGREGATOR_BOOST} -> {entry_score:.3f}"
                    )

            # Layer 2: Structural context boosts from feature snapshot
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

            stop_str = f"{sweep_stop_override:.1f}pts" if sweep_stop_override else "ATR-based"
            logger.info(
                f"SWEEP SIGNAL: {entry_direction} | "
                f"Score: {entry_score:.3f} (base {sweep_signal.score:.2f}) | "
                f"Levels: {', '.join(sweep_signal.swept_levels)} | "
                f"Stop: {stop_str}"
            )

        elif has_signal:
            # PATH C: Aggregator alone CANNOT trigger trades — context only
            agg_dir = "long" if signal.direction == SignalDirection.LONG else "short"
            logger.debug(
                f"CONTEXT ONLY (no sweep): aggregator {agg_dir} "
                f"score={signal.combined_score:.3f} — no trade (sweep required)"
            )

        # ── Shadow rejection helper ──────────────────────────────────
        def _set_rejection(direction, score, stop_dist, atr, reason, gate):
            self._last_rejection = {
                "direction": direction.upper() if direction else "UNKNOWN",
                "score": score,
                "stop_distance": stop_dist,
                "atr": atr,
                "rejection_reason": reason,
                "gate": gate,
            }
            # Log to decision logger
            _dir = direction.upper() if direction else "UNKNOWN"
            _htf_biases = {}
            if htf_bias and hasattr(htf_bias, 'tf_biases'):
                _htf_biases = {
                    tf: b.upper()[:7] if b else "N/A"
                    for tf, b in htf_bias.tf_biases.items()
                }
            _conflicting = []
            if htf_bias and hasattr(htf_bias, 'tf_biases'):
                expected = "bullish" if _dir == "LONG" else "bearish"
                _conflicting = [
                    tf for tf, b in htf_bias.tf_biases.items()
                    if b != expected and b != "neutral"
                ]
            # Map gate numbers to stage names
            _stage_map = {
                1: "HTF_GATE", 2: "NAN_GUARD", 3: "CONFLUENCE",
                4: "NAN_GUARD", 5: "HC_STOP", 6: "MIN_RR",
                7: "REGIME", 8: "RISK_REJECT", 9: "MODIFIER_STANDSIDE",
            }
            _stage = _stage_map.get(gate, reason)
            self.decision_logger.log_rejection(
                price_at_signal=bar.close,
                signal_direction=_dir,
                rejection_stage=_stage,
                rejection_details={
                    "htf_biases": _htf_biases,
                    "conflicting_timeframes": _conflicting,
                    "confluence_score": score if score else None,
                    "confluence_threshold": HIGH_CONVICTION_MIN_SCORE,
                    "stand_aside_reason": reason if gate == 9 else None,
                    "safety_rail_triggered": reason if gate == 8 else None,
                },
            )

        # === 5b. UCL — EVALUATE ACTIVE WATCH STATES (runs every bar) ===
        ucl_confirmed: List[ConfirmedSignal] = []
        if self._ucl_enabled:
            ucl_confirmed = self._watch_manager.update(
                bar=bar,
                fvg_detector=self._fvg_detector,
                htf_bias=htf_bias,
            )

        if entry_direction is None:
            # Gate 1: HTF-blocked signal
            if (signal is not None and not signal.should_trade
                    and "HTF" in (signal.rejection_reason or "")):
                dir_str = "LONG" if signal.direction == SignalDirection.LONG else "SHORT"
                _set_rejection(dir_str, signal.combined_score, None,
                               features.atr_14, "HTF gate block", 1)
            # Even with no new signal, a confirmed watch may fire
            if not ucl_confirmed:
                return action_result
            # Fall through to process confirmed signal below

        if entry_direction is not None and not math.isfinite(entry_score):
            # Gate 2: NaN score guard
            logger.error("HC REJECT: entry_score is NaN/Inf — blocking trade")
            _set_rejection(entry_direction, entry_score, None,
                           features.atr_14, "NaN score guard", 2)
            return None

        # === 5c. UCL v2 — FVG CONFLUENCE SCORE BOOST ===
        # Before the HC gate, boost score if entry is near an active FVG
        if entry_direction is not None and self._ucl_enabled:
            fvg_direction = "bullish" if entry_direction == "long" else "bearish"
            active_fvgs = self._fvg_detector.get_active_fvgs(fvg_direction)
            fvg_confluence = False
            for fvg in active_fvgs:
                if fvg.status in ("UNFILLED", "PARTIALLY_FILLED"):
                    # Check if current price is inside this FVG zone
                    if fvg.fvg_low <= bar.close <= fvg.fvg_high:
                        fvg_confluence = True
                        break
                    # Also check if entry would be within 10pts of FVG
                    distance_to_fvg = min(
                        abs(bar.close - fvg.fvg_low),
                        abs(bar.close - fvg.fvg_high))
                    if distance_to_fvg <= 10.0:
                        fvg_confluence = True
                        break
            if fvg_confluence:
                old_score = entry_score
                entry_score += UCL_FVG_CONFLUENCE_BOOST
                logger.info(
                    f"FVG confluence: +{UCL_FVG_CONFLUENCE_BOOST} boost, "
                    f"score {old_score:.3f} → {entry_score:.3f}"
                )

        # === 5d. UCL v2 — PROCESS CONFIRMED SIGNALS ===
        # A confirmed wide-stop watch re-enters the pipeline with boosted score + tight stop
        if ucl_confirmed and entry_direction is None:
            cs = ucl_confirmed[0]  # Process first confirmed signal
            entry_direction = "long" if cs.direction == "LONG" else "short"
            entry_score = cs.boosted_score
            entry_source = f"ucl_confirmed_{cs.setup_type}"
            # Use tight confirmed stop from watch metadata
            if cs.stop_distance > 0:
                sweep_stop_override = cs.stop_distance
            elif cs.metadata.get("confirmed_stop_distance"):
                sweep_stop_override = cs.metadata["confirmed_stop_distance"]
            elif cs.metadata.get("sweep_low"):
                sweep_stop_override = abs(bar.close - cs.metadata["sweep_low"])
            logger.info(
                f"UCL wide-stop conversion: {entry_direction} via {cs.setup_type} | "
                f"original stop {cs.metadata.get('original_stop', '?')}pt → "
                f"confirmed stop {sweep_stop_override or '?'}pt | "
                f"boosted={cs.boosted_score:.3f} | bars={cs.bars_to_confirm}"
            )

        # -- HTF DIRECTIONAL GATE (softened: score penalty instead of hard block) --
        # A sweep IS a reversal signal — HTF bias disagreement is expected
        # at the moment of reversal. Penalize score by 0.10 instead of blocking.
        if entry_direction is not None and htf_bias is not None:
            htf_disagrees = False
            if entry_direction == "long" and not htf_bias.htf_allows_long:
                htf_disagrees = True
            if entry_direction == "short" and not htf_bias.htf_allows_short:
                htf_disagrees = True
            if htf_disagrees:
                entry_score -= 0.10
                logger.info(
                    "HTF BIAS PENALTY: %s entry penalized -0.10 — HTF %s (strength %.2f) "
                    "disagrees [source=%s, new_score=%.2f]",
                    entry_direction, htf_bias.consensus_direction,
                    htf_bias.consensus_strength, entry_source, entry_score,
                )
        elif entry_direction is not None and htf_bias is None:
            # Fail-safe: no HTF data → block all trades
            logger.warning(
                "HTF GATE BLOCK: %s entry blocked — no HTF data available "
                "[source=%s]", entry_direction, entry_source,
            )
            _set_rejection(entry_direction, entry_score, None,
                           features.atr_14, "No HTF data — fail-safe block", 1)
            return None

        # -- HIGH-CONVICTION GATE 1: Signal Score --
        if entry_direction is not None and entry_score < HIGH_CONVICTION_MIN_SCORE:
            logger.debug(
                f"HC REJECT: score {entry_score:.3f} "
                f"< {HIGH_CONVICTION_MIN_SCORE} (need higher conviction)"
            )
            _set_rejection(entry_direction, entry_score, None,
                           features.atr_14, "HC score below 0.75", 3)
            return None

        if entry_direction is None:
            return action_result

        # === 6. RISK CHECK ===
        # Compute structural stop distance from signal chain
        structural_stop_dist = None
        if signal and hasattr(signal, 'structural_stop_price') and signal.structural_stop_price is not None:
            structural_stop_dist = abs(bar.close - signal.structural_stop_price)
            if structural_stop_dist <= 0:
                structural_stop_dist = None

        # Session-aware ATR scaling for stop/target calculation.
        # C2 trail uses raw ATR (adapts in real-time, not locked to entry session).
        scaled_atr = self._session_scaler.scale_atr(features.atr_14, bar.timestamp)

        risk_assessment = self.risk_engine.evaluate_trade(
            direction=entry_direction,
            entry_price=bar.close,
            atr=scaled_atr,
            vix=features.vix_level or 0,
            current_time=bar.timestamp,
            structural_stop_distance=structural_stop_dist,
        )

        # For sweep/UCL entries, use the override stop if tighter
        raw_stop = risk_assessment.suggested_stop_distance
        if sweep_stop_override is not None and sweep_stop_override < raw_stop:
            raw_stop = sweep_stop_override
        # For UCL confirmed entries, always use the confirmed stop
        if (entry_source and entry_source.startswith("ucl_confirmed_")
                and sweep_stop_override is not None):
            raw_stop = sweep_stop_override

        if not math.isfinite(raw_stop):
            # Gate 4: NaN stop distance
            logger.error("HC REJECT: stop distance is NaN/Inf — blocking trade")
            _set_rejection(entry_direction, entry_score, None,
                           features.atr_14, "NaN stop distance", 4)
            return None

        # -- HIGH-CONVICTION GATE 2: Stop Distance Cap --
        if raw_stop > HIGH_CONVICTION_MAX_STOP_PTS:
            # UCL v2: route wide-stop sweeps to watch state for post-sweep confirmation
            if (self._ucl_enabled and entry_source == "sweep"
                    and entry_direction is not None):
                self._create_wide_stop_watch(
                    direction="LONG" if entry_direction == "long" else "SHORT",
                    score=entry_score,
                    sweep_low=bar.low,
                    sweep_high=bar.high,
                    original_stop=raw_stop,
                    bar_index=self._bars_processed,
                    current_bar=bar,
                )
                _set_rejection(entry_direction, entry_score, raw_stop,
                               features.atr_14,
                               "Max stop exceeded — routed to UCL watch", 5)
                return None

            logger.debug(
                f"HC REJECT: stop {raw_stop:.1f} pts "
                f"> {HIGH_CONVICTION_MAX_STOP_PTS} (too wide, wait for tighter entry)"
            )
            _set_rejection(entry_direction, entry_score, raw_stop,
                           features.atr_14, "Max stop exceeded", 5)
            return None

        # -- Min R:R Check --
        # Uses session-scaled ATR (consistent with stop calculation)
        target_distance = scaled_atr * self.config.risk.atr_multiplier_target
        if raw_stop > 0 and target_distance / raw_stop < MIN_RR_RATIO:
            logger.debug(
                f"HC REJECT: R:R {target_distance / raw_stop:.2f} "
                f"< {MIN_RR_RATIO} (unfavorable risk/reward)"
            )
            _set_rejection(entry_direction, entry_score, raw_stop,
                           features.atr_14, "Min R:R failed", 6)
            return None

        # Regime gate
        if regime_adj["size_multiplier"] == 0:
            logger.debug(f"Regime {self._current_regime} blocks new trades")
            _set_rejection(entry_direction, entry_score, raw_stop,
                           features.atr_14, "Regime gate block", 7)
            return None

        if risk_assessment.decision in (RiskDecision.APPROVE, RiskDecision.REDUCE_SIZE):
            # === 6b. INSTITUTIONAL MODIFIERS ===
            # Applied AFTER all gates pass, BEFORE trade execution.
            # Adjusts position size, stop width, C2 runner trail.
            modifier_result = None
            # C2 trail uses RAW ATR (not session-scaled) — the trail needs to
            # adapt to real-time conditions, not be locked to entry session vol.
            atr_for_entry = features.atr_14
            if self._modifiers_enabled:
                htf_dir = htf_bias.consensus_direction if htf_bias else None
                try:
                    modifier_result = self._institutional_engine.calculate(
                        current_time=bar.timestamp,
                        htf_bias_direction=htf_dir,
                    )
                except Exception as e:
                    # GRACEFUL DEGRADATION: modifier failure → use 1.0x multipliers
                    logger.warning(
                        "Modifier engine calculate() failed — using 1.0x multipliers: %s", e
                    )
                    modifier_result = None
                if modifier_result and modifier_result.stand_aside:
                    logger.info(
                        f"INSTITUTIONAL STAND-ASIDE: {modifier_result.stand_aside_reason}"
                    )
                    _set_rejection(entry_direction, entry_score, raw_stop,
                                   features.atr_14, "Institutional stand-aside", 9)
                    return None

                # Apply multipliers (skip if modifier_result is None from graceful degradation)
                if modifier_result:
                    raw_stop *= modifier_result.stop_multiplier
                    atr_for_entry = features.atr_14 * modifier_result.runner_multiplier

                # Re-check HC max stop after modifier widening
                if raw_stop > HIGH_CONVICTION_MAX_STOP_PTS:
                    logger.info(
                        "HC REJECT post-modifier: stop %.1f pts > %.1f (modifier widened)",
                        raw_stop, HIGH_CONVICTION_MAX_STOP_PTS,
                    )
                    _set_rejection(entry_direction, entry_score, raw_stop,
                                   features.atr_14, "Max stop exceeded after modifier", 5)
                    return None

                if (modifier_result
                        and (modifier_result.position_multiplier != 1.0
                        or modifier_result.stop_multiplier != 1.0
                        or modifier_result.runner_multiplier != 1.0)):
                    logger.info(
                        f"INSTITUTIONAL MODIFIERS: "
                        f"pos={modifier_result.position_multiplier:.2f}x "
                        f"stop={modifier_result.stop_multiplier:.2f}x "
                        f"runner={modifier_result.runner_multiplier:.2f}x | "
                        f"overnight={modifier_result.details.get('overnight', {}).get('classification', 'n/a')} "
                        f"fomc={modifier_result.details.get('fomc', {}).get('window', 'n/a')}"
                    )

            # === 7. ENTER SCALE-OUT TRADE ===
            # C1 exits via trail-from-profit (Variant C).
            # No fixed TP1 target — managed by ScaleOutExecutor.
            trade = await self.executor.enter_trade(
                direction=entry_direction,
                entry_price=bar.close,
                stop_distance=raw_stop,
                atr=atr_for_entry,
                signal_score=entry_score,
                regime=self._current_regime,
                timestamp=bar.timestamp,
            )

            if trade:
                htf_dir = htf_bias.consensus_direction if htf_bias else "n/a"
                htf_str = htf_bias.consensus_strength if htf_bias else 0.0
                action_result = {
                    "action": "entry",
                    "timestamp": bar.timestamp.isoformat(),
                    "direction": entry_direction,
                    "contracts": 2,
                    "entry_price": trade.entry_price,
                    "stop": trade.initial_stop,
                    "c1_exit_rule": f"trail_from_+{self.config.scale_out.c1_profit_threshold_pts}pts",
                    "signal_score": entry_score,
                    "signal_source": entry_source,
                    "regime": self._current_regime,
                    "htf_bias": htf_dir,
                    "htf_strength": round(htf_str, 3),
                }
                # Attach institutional modifier metadata
                if modifier_result is not None:
                    action_result["inst_position_mult"] = modifier_result.position_multiplier
                    action_result["inst_stop_mult"] = modifier_result.stop_multiplier
                    action_result["inst_runner_mult"] = modifier_result.runner_multiplier
                    action_result["inst_overnight"] = modifier_result.details.get(
                        "overnight", {}).get("classification", "n/a")
                    action_result["inst_fomc_window"] = modifier_result.details.get(
                        "fomc", {}).get("window", "n/a")
                # Attach sweep metadata if applicable
                if entry_source == "sweep" and sweep_signal:
                    action_result["sweep_levels"] = sweep_signal.swept_levels
                    action_result["sweep_score"] = sweep_signal.score
                    action_result["sweep_depth_pts"] = sweep_signal.sweep_depth_pts

                # Log approved trade to decision logger
                _mod_vals = {"overnight": 1.0, "fomc": 1.0, "gamma": 1.0,
                             "volatility": 1.0, "total": 1.0}
                if modifier_result is not None:
                    _mod_vals = {
                        "overnight": modifier_result.details.get(
                            "overnight", {}).get("position", 1.0),
                        "fomc": modifier_result.details.get(
                            "fomc", {}).get("position", 1.0),
                        "gamma": modifier_result.details.get(
                            "gamma", {}).get("position", 1.0),
                        "volatility": modifier_result.details.get(
                            "volatility", {}).get("position", 1.0),
                        "total": modifier_result.position_multiplier,
                    }
                self.decision_logger.log_approval(
                    price_at_signal=bar.close,
                    signal_direction=entry_direction.upper(),
                    confluence_score=entry_score,
                    modifier_values=_mod_vals,
                    position_size=2.0,
                    stop_width=raw_stop,
                    runner_trail_width=atr_for_entry,
                    entry_price=trade.entry_price,
                    c1_target=trade.entry_price + (
                        self.config.scale_out.c1_profit_threshold_pts
                        if entry_direction == "long" else
                        -self.config.scale_out.c1_profit_threshold_pts
                    ),
                    c2_trail_start=trade.entry_price + (
                        atr_for_entry if entry_direction == "long"
                        else -atr_for_entry
                    ),
                )
        else:
            logger.debug(f"Risk rejected: {risk_assessment.reason}")
            _set_rejection(entry_direction, entry_score, raw_stop,
                           features.atr_14, "Risk decision rejected", 8)

        return action_result

    def _create_wide_stop_watch(self, direction, score, sweep_low,
                               sweep_high, original_stop, bar_index, current_bar):
        """Create UCL watch for wide-stop sweep that needs confirmation.

        Wide-stop sweeps (score >= 0.75, stop > 30pt) are routed here instead
        of being blocked.  Post-sweep confirmation produces a tighter stop
        from the confirmation level.
        """
        if direction == "LONG":
            key_level = sweep_low       # swept level
            invalidation = sweep_low - 15.0  # wider invalidation for HTF setups
            stop_on_confirm = sweep_low - 5.0  # tight stop below sweep low
        else:
            key_level = sweep_high
            invalidation = sweep_high + 15.0
            stop_on_confirm = sweep_high + 5.0

        watch = WatchState(
            setup_type="wide_stop_sweep",
            direction=direction,
            trigger_bar=bar_index,
            trigger_price=current_bar.close,
            key_level=key_level,
            invalidation_price=invalidation,
            expiry_bars=90,  # wider window for HTF setups
            confirmation_conditions=["RECLAIM", "FVG_FORM", "FVG_TAP"],
            metadata={
                "original_score": score,
                "original_stop": original_stop,
                "confirmed_stop_distance": abs(current_bar.close - stop_on_confirm),
                "sweep_low": sweep_low,
                "sweep_high": sweep_high,
            },
            base_score=score,
            created_at=current_bar.timestamp,
        )
        self._watch_manager.add_watch(watch)
        logger.info(
            f"UCL wide-stop watch created: {direction} | "
            f"score={score:.3f} | original_stop={original_stop:.1f}pt | "
            f"confirmed_stop={abs(current_bar.close - stop_on_confirm):.1f}pt | "
            f"key_level={key_level:.2f}"
        )

    def _get_last_price(self) -> float:
        if hasattr(self, '_last_bar') and self._last_bar:
            return self._last_bar.close
        return 0.0

    # ================================================================
    # MULTI-TIMEFRAME BACKTEST
    # ================================================================
    async def run_backtest_mtf(
        self,
        mtf_iterator: MultiTimeframeIterator,
        execution_tf: str = "2m",
    ) -> dict:
        """
        Full multi-timeframe backtest.

        Processes all bars in chronological order:
        - HTF bars -> HTF Bias Engine (updates directional filter)
        - Execution-TF bars -> Feature Engine -> Signal -> Trade
        """
        self._execution_tf = execution_tf
        logger.info(f"Starting MTF backtest: {len(mtf_iterator)} total bars, exec_tf={execution_tf}")

        trades = []
        equity_curve = [self.config.risk.account_size]
        htf_log_interval = 1000
        exec_bars_count = 0
        htf_bars_count = 0
        htf_blocked_count = 0

        # Data collection for visualization
        exec_bars_log = []
        trade_log = []
        htf_bias_log = []
        equity_timestamps = []

        for i, (timeframe, bar_data) in enumerate(mtf_iterator):
            if timeframe in HTF_TIMEFRAMES:
                self.process_htf_bar(timeframe, bar_data)
                htf_bars_count += 1
            elif timeframe == execution_tf:
                exec_bar = bardata_to_bar(bar_data)
                result = await self.process_bar(exec_bar)
                exec_bars_count += 1

                exec_bars_log.append({
                    "time": bar_data.timestamp.isoformat(),
                    "open": bar_data.open,
                    "high": bar_data.high,
                    "low": bar_data.low,
                    "close": bar_data.close,
                    "volume": bar_data.volume,
                })

                if exec_bars_count % 50 == 0 and self._htf_bias:
                    bias = self._htf_bias
                    htf_bias_log.append({
                        "time": bar_data.timestamp.isoformat(),
                        "direction": bias.consensus_direction,
                        "strength": round(bias.consensus_strength, 3),
                        "allows_long": bias.htf_allows_long,
                        "allows_short": bias.htf_allows_short,
                    })

                if result:
                    trade_log.append(result)
                    trades.append(result)
                    if result.get("action") == "trade_closed":
                        equity_curve.append(self.risk_engine.state.current_equity)
                        equity_timestamps.append(bar_data.timestamp.isoformat())

            if (i + 1) % 5000 == 0:
                bias = self._htf_bias
                bias_str = f"{bias.consensus_direction}({bias.consensus_strength:.2f})" if bias else "n/a"
                logger.info(f"  Processed {i+1}/{len(mtf_iterator)} bars | "
                           f"HTF={htf_bars_count} Exec={exec_bars_count} | "
                           f"Bias={bias_str}")

        scale_out_stats = self.executor.get_stats()
        risk_state = self.risk_engine.get_state_snapshot()
        signal_stats = self.signal_aggregator.get_signal_stats()

        results = {
            "bars_processed_total": htf_bars_count + exec_bars_count,
            "htf_bars_processed": htf_bars_count,
            "exec_bars_processed": exec_bars_count,
            "execution_timeframe": execution_tf,
            "total_trades": scale_out_stats.get("total_trades", 0),
            "total_pnl": scale_out_stats.get("total_pnl", 0),
            "win_rate": scale_out_stats.get("win_rate", 0),
            "profit_factor": scale_out_stats.get("profit_factor", 0),
            "avg_winner": scale_out_stats.get("avg_winner", 0),
            "avg_loser": scale_out_stats.get("avg_loser", 0),
            "largest_win": scale_out_stats.get("largest_win", 0),
            "largest_loss": scale_out_stats.get("largest_loss", 0),
            "c1_total_pnl": scale_out_stats.get("c1_total_pnl", 0),
            "c2_total_pnl": scale_out_stats.get("c2_total_pnl", 0),
            "c2_outperformed_c1_pct": scale_out_stats.get("c2_outperformed_c1_pct", 0),
            "max_drawdown_pct": risk_state.get("max_drawdown_pct", 0),
            "final_equity": risk_state.get("equity", self.config.risk.account_size),
            "htf_blocked_signals": signal_stats.get("htf_blocked_signals", 0),
            "htf_block_rate": signal_stats.get("htf_block_rate", 0),
            "equity_curve": equity_curve,
            "exec_bars_log": exec_bars_log,
            "trade_log": trade_log,
            "htf_bias_log": htf_bias_log,
            "equity_timestamps": equity_timestamps,
        }

        logger.info("=" * 60)
        logger.info("  BACKTEST RESULTS - MTF 2-CONTRACT SCALE-OUT (HC FILTERED)")
        logger.info("=" * 60)
        for k, v in results.items():
            if k not in ("equity_curve", "exec_bars_log", "trade_log", "htf_bias_log", "equity_timestamps"):
                logger.info(f"  {k:.<40} {v}")
        logger.info("=" * 60)

        logger.info("\n" + self.htf_engine.get_summary())

        return results

    # Legacy single-TF backtest (kept for backward compatibility)
    async def run_backtest(self, bars: list) -> dict:
        """Single-TF backtest (legacy). Use run_backtest_mtf for multi-TF."""
        logger.info(f"Starting backtest: {len(bars)} bars")

        if bars and hasattr(bars[0], 'source'):
            bars = self.data_pipeline.convert_to_feature_bars(bars)

        trades = []
        equity_curve = [self.config.risk.account_size]

        for i, bar in enumerate(bars):
            result = await self.process_bar(bar)
            if result:
                trades.append(result)
                if result.get("action") == "trade_closed":
                    equity_curve.append(self.risk_engine.state.current_equity)
            if (i + 1) % 500 == 0:
                logger.info(f"  Processed {i+1}/{len(bars)} bars...")

        scale_out_stats = self.executor.get_stats()
        risk_state = self.risk_engine.get_state_snapshot()

        results = {
            "bars_processed": len(bars),
            "total_trades": scale_out_stats.get("total_trades", 0),
            "total_pnl": scale_out_stats.get("total_pnl", 0),
            "win_rate": scale_out_stats.get("win_rate", 0),
            "profit_factor": scale_out_stats.get("profit_factor", 0),
            "avg_winner": scale_out_stats.get("avg_winner", 0),
            "avg_loser": scale_out_stats.get("avg_loser", 0),
            "largest_win": scale_out_stats.get("largest_win", 0),
            "largest_loss": scale_out_stats.get("largest_loss", 0),
            "c1_total_pnl": scale_out_stats.get("c1_total_pnl", 0),
            "c2_total_pnl": scale_out_stats.get("c2_total_pnl", 0),
            "c2_outperformed_c1_pct": scale_out_stats.get("c2_outperformed_c1_pct", 0),
            "max_drawdown_pct": risk_state.get("max_drawdown_pct", 0),
            "final_equity": risk_state.get("equity", self.config.risk.account_size),
            "equity_curve": equity_curve,
        }

        logger.info("=" * 60)
        logger.info("  BACKTEST RESULTS - 2-CONTRACT SCALE-OUT")
        logger.info("=" * 60)
        for k, v in results.items():
            if k != "equity_curve":
                logger.info(f"  {k:.<35} {v}")
        logger.info("=" * 60)

        return results

    # ================================================================
    # LIVE MODE (Tradovate)
    # ================================================================
    async def connect_broker(self) -> bool:
        from broker.tradovate_client import TradovateClient
        self.broker_client = TradovateClient(self.config.tradovate)
        connected = await self.broker_client.connect()
        if connected:
            self.executor.broker = self.broker_client
            self.broker_client.on_bar(self._on_live_bar)
            self.broker_client.on_fill(self._on_live_fill)
            await self.broker_client.subscribe_market_data()
            self.monitoring.update_health("execution", "healthy")
            logger.info("Broker connected and ready")
        else:
            self.monitoring.update_health("execution", "error", "Connection failed")
        return connected

    async def _on_live_bar(self, data: dict) -> None:
        try:
            bar = Bar(
                timestamp=datetime.now(timezone.utc),
                open=data.get("open", 0), high=data.get("high", 0),
                low=data.get("low", 0), close=data.get("close", 0),
                volume=data.get("volume", 0),
                bid_volume=data.get("bidVolume", 0),
                ask_volume=data.get("askVolume", 0),
                delta=data.get("askVolume", 0) - data.get("bidVolume", 0),
            )
            await self.process_bar(bar)
        except Exception as e:
            logger.error(f"Error processing live bar: {e}", exc_info=True)

    async def _on_live_fill(self, fill) -> None:
        logger.info(f"Live fill: {fill.action} {fill.filled_qty}x @ {fill.fill_price}")

    async def _load_economic_calendar(self) -> None:
        try:
            rows = await self.db.fetch(
                "SELECT event_name, event_time_utc, impact_level FROM economic_events "
                "WHERE event_time_utc > NOW() - INTERVAL '1 day' ORDER BY event_time_utc"
            )
            events = [
                {"event_name": r["event_name"], "event_time": r["event_time_utc"],
                 "impact_level": r["impact_level"]}
                for r in rows
            ]
            self.risk_engine.load_economic_calendar(events)
        except Exception as e:
            logger.warning(
                "Economic calendar load failed: %s — "
                "risk engine will proceed without event awareness", e
            )

    # ================================================================
    # STATUS
    # ================================================================
    def get_system_status(self) -> dict:
        trade = self.executor.active_trade
        htf = self._htf_bias
        return {
            "running": self._running,
            "bars_processed": self._bars_processed,
            "htf_bars_processed": self._htf_bars_processed,
            "current_regime": self._current_regime,
            "htf_consensus": htf.consensus_direction if htf else "n/a",
            "htf_strength": htf.consensus_strength if htf else 0,
            "htf_allows_long": htf.htf_allows_long if htf else False,
            "htf_allows_short": htf.htf_allows_short if htf else False,
            "risk_state": self.risk_engine.get_state_snapshot(),
            "signal_stats": self.signal_aggregator.get_signal_stats(),
            "scale_out_stats": self.executor.get_stats(),
            "has_active_trade": self.executor.has_active_trade,
            "active_trade": {
                "direction": trade.direction if trade else None,
                "entry_price": trade.entry_price if trade else None,
                "phase": trade.phase.value if trade else None,
                "c1_open": trade.c1.is_open if trade else False,
                "c2_open": trade.c2.is_open if trade else False,
                "c2_trailing_stop": trade.c2_trailing_stop if trade else 0,
            } if trade else None,
            "broker_connected": self.broker_client.is_connected if self.broker_client else False,
            "sweep_detector": self.sweep_detector.get_stats() if self._sweep_enabled else None,
            "ucl_watch_state": self._watch_manager.get_stats() if self._ucl_enabled else None,
            "ucl_fvg_detector": self._fvg_detector.get_stats() if self._ucl_enabled else None,
            "institutional_modifiers": self._modifiers_enabled,
        }


# ================================================================
# ENTRYPOINT
# ================================================================
async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    bot = TradingOrchestrator()

    try:
        await bot.initialize()
        logger.info("")
        logger.info("Bot ready. Available commands:")
        logger.info("  1. Run MTF backtest: python scripts/run_backtest.py --tv")
        logger.info("  2. Run sample test:  python scripts/run_backtest.py --sample")
        logger.info("  3. Start dashboard:  uvicorn dashboard.server:app --port 8080")
        logger.info("")
    except KeyboardInterrupt:
        logger.info("Interrupted")
    except Exception as e:
        logger.critical(f"Fatal: {e}", exc_info=True)
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
