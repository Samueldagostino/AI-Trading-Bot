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
import sys
import numpy as np
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List

from config.settings import BotConfig, CONFIG
from database.connection import DatabaseManager
from features.engine import NQFeatureEngine, Bar
from features.htf_engine import HTFBiasEngine, HTFBar, HTFBiasResult
from signals.aggregator import SignalAggregator, SignalDirection
from risk.engine import RiskEngine, RiskDecision
from risk.regime_detector import RegimeDetector
from execution.scale_out_executor import ScaleOutExecutor
from monitoring.engine import MonitoringEngine
from data_pipeline.pipeline import (
    DataPipeline, BarData, MultiTimeframeIterator,
    bardata_to_bar, bardata_to_htfbar, MINUTES_TO_LABEL,
)

logger = logging.getLogger(__name__)


# Which timeframes are "higher" (feed the HTF bias engine)
# vs "execution" (feed the feature engine for entries)
HTF_TIMEFRAMES = {"1D", "4H", "1H", "30m", "15m", "5m"}
EXECUTION_TIMEFRAMES = {"2m", "3m", "1m"}

# ── HIGH-CONVICTION FILTER ──────────────────────────────────────────
# Derived from backtest forensics (167 trades -> 62 trades, Jan-Feb 2026).
# Only the intersection of tight stops + strong signals showed
# durable edge (PF 2.35, worst loss $135, max DD 1.03%).
#
#   Rule 1 – Min signal score >= 0.75   (eliminates low-conviction noise)
#   Rule 2 – Max stop distance <= 30 pts (caps tail risk per trade)
#   Rule 3 – C1 target = stop_dist x 1.5 (R:R enforced, not fixed pts)
#
# These are HARD gates. If a setup doesn't meet all three, we skip it
# and wait. The bot's job is survival, not activity.
# ─────────────────────────────────────────────────────────────────────
HIGH_CONVICTION_MIN_SCORE = 0.75
HIGH_CONVICTION_MAX_STOP_PTS = 30.0
HIGH_CONVICTION_TP1_RR_RATIO = 1.5    # TP1 = stop_distance x this


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
        self.db = DatabaseManager(config.db.dsn)
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

        # Execution - scale-out is the primary executor
        self.executor = ScaleOutExecutor(config)

        # Tradovate client (initialized on connect)
        self.broker_client = None

        # State
        self._running = False
        self._bars_processed = 0
        self._htf_bars_processed = 0
        self._current_regime = "unknown"
        self._execution_tf = "2m"     # Default execution timeframe

    # ================================================================
    # INITIALIZATION
    # ================================================================
    async def initialize(self, skip_db: bool = False) -> None:
        """Initialize all components."""
        logger.info("=" * 60)
        logger.info("NQ TRADING BOT - INITIALIZING (MULTI-TIMEFRAME)")
        logger.info(f"  Environment:  {self.config.environment}")
        logger.info(f"  Broker:       Tradovate ({self.config.tradovate.environment})")
        logger.info(f"  Symbol:       {self.config.tradovate.symbol}")
        logger.info(f"  Strategy:     2-contract scale-out (HC filtered)")
        logger.info(f"  C1 Target:    {self.config.scale_out.c1_target_min_points}-{self.config.scale_out.c1_target_max_points} pts (config default)")
        logger.info(f"  HC Override:  TP1 = stop x {HIGH_CONVICTION_TP1_RR_RATIO} R:R")
        logger.info(f"  HC Min Score: {HIGH_CONVICTION_MIN_SCORE}")
        logger.info(f"  HC Max Stop:  {HIGH_CONVICTION_MAX_STOP_PTS} pts")
        logger.info(f"  Account:      ${self.config.risk.account_size:,.2f}")
        logger.info(f"  Max Risk:     {self.config.risk.max_risk_per_trade_pct}% per trade")
        logger.info(f"  Daily Limit:  {self.config.risk.max_daily_loss_pct}%")
        logger.info(f"  Kill Switch:  {self.config.risk.max_total_drawdown_pct}% drawdown")
        logger.info(f"  HTF Engine:   {', '.join(sorted(HTF_TIMEFRAMES))}")
        logger.info(f"  Exec TF:      {self._execution_tf}")
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
        logger.info("Orchestrator initialized")

    async def shutdown(self) -> None:
        """Graceful shutdown - flatten positions first."""
        logger.info("Initiating shutdown...")
        self._running = False

        if self.executor.has_active_trade:
            logger.warning("Flattening active position on shutdown")
            last_price = self._get_last_price()
            await self.executor.emergency_flatten(last_price)

        if self.broker_client:
            await self.broker_client.disconnect()

        await self.db.close()
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
        action_result = None

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

        # === 4. MANAGE ACTIVE POSITION ===
        if self.executor.has_active_trade:
            result = await self.executor.update(bar.close, bar.timestamp)
            if result:
                if result.get("action") == "trade_closed":
                    result["close_timestamp"] = bar.timestamp.isoformat()
                    total_pnl = result["total_pnl"]
                    self.risk_engine.record_trade_result(total_pnl, result["direction"])
                    self.monitoring.record_trade({
                        "action": "exit",
                        "pnl": total_pnl,
                        "direction": result["direction"],
                    })

                    action_result = result

                elif result.get("action") == "c1_target_hit":
                    action_result = result

            return action_result

        # === 5. SIGNAL AGGREGATION (only if flat, HTF-gated) ===
        signal = self.signal_aggregator.aggregate(
            feature_snapshot=features,
            ml_prediction=None,
            htf_bias=htf_bias,
            current_time=bar.timestamp,
        )

        if signal and signal.should_trade:
            direction = "long" if signal.direction == SignalDirection.LONG else "short"

            # -- HIGH-CONVICTION GATE 1: Signal Score --
            if signal.combined_score < HIGH_CONVICTION_MIN_SCORE:
                logger.debug(
                    f"HC REJECT: score {signal.combined_score:.3f} "
                    f"< {HIGH_CONVICTION_MIN_SCORE} (need higher conviction)"
                )
                return None

            # === 6. RISK CHECK ===
            risk_assessment = self.risk_engine.evaluate_trade(
                direction=direction,
                entry_price=bar.close,
                atr=features.atr_14,
                vix=features.vix_level or 0,
                current_time=bar.timestamp,
            )

            # -- HIGH-CONVICTION GATE 2: Stop Distance Cap --
            raw_stop = risk_assessment.suggested_stop_distance
            if raw_stop > HIGH_CONVICTION_MAX_STOP_PTS:
                logger.debug(
                    f"HC REJECT: stop {raw_stop:.1f} pts "
                    f"> {HIGH_CONVICTION_MAX_STOP_PTS} (too wide, wait for tighter entry)"
                )
                return None

            # Regime gate
            if regime_adj["size_multiplier"] == 0:
                logger.debug(f"Regime {self._current_regime} blocks new trades")
                return None

            if risk_assessment.decision in (RiskDecision.APPROVE, RiskDecision.REDUCE_SIZE):
                # -- HIGH-CONVICTION GATE 3: TP1 = Stop x R:R Ratio --
                # Override config-based C1 target with R:R-derived target.
                # TP1 is a function of how tight the entry is, not a fixed value.
                hc_c1_target_pts = raw_stop * HIGH_CONVICTION_TP1_RR_RATIO

                # === 7. ENTER SCALE-OUT TRADE ===
                trade = await self.executor.enter_trade(
                    direction=direction,
                    entry_price=bar.close,
                    stop_distance=raw_stop,
                    atr=features.atr_14,
                    signal_score=signal.combined_score,
                    regime=self._current_regime,
                    c1_target_override=hc_c1_target_pts,
                )

                if trade:
                    htf_dir = htf_bias.consensus_direction if htf_bias else "n/a"
                    htf_str = htf_bias.consensus_strength if htf_bias else 0.0
                    action_result = {
                        "action": "entry",
                        "timestamp": bar.timestamp.isoformat(),
                        "direction": direction,
                        "contracts": 2,
                        "entry_price": trade.entry_price,
                        "stop": trade.initial_stop,
                        "c1_target": trade.c1.target_price,
                        "signal_score": signal.combined_score,
                        "regime": self._current_regime,
                        "htf_bias": htf_dir,
                        "htf_strength": round(htf_str, 3),
                    }
            else:
                logger.debug(f"Risk rejected: {risk_assessment.reason}")

        return action_result

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
        except Exception:
            pass

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
            "htf_allows_long": htf.htf_allows_long if htf else True,
            "htf_allows_short": htf.htf_allows_short if htf else True,
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
