"""
Main Trading Orchestrator
==========================
Coordinates: Data → Features → Signals → Risk → Scale-Out Execution → Monitoring

Two operating modes:
  BACKTEST: Load TradingView CSV → run through pipeline → report
  LIVE:     Tradovate WebSocket → real-time pipeline → paper/live execution
"""

import asyncio
import logging
import sys
import numpy as np
from datetime import datetime, timezone, timedelta
from typing import Optional

from config.settings import BotConfig, CONFIG
from database.connection import DatabaseManager
from discord_ingestion.listener import DiscordBiasParser, DiscordListener
from features.engine import NQFeatureEngine, Bar
from signals.aggregator import SignalAggregator, SignalDirection
from risk.engine import RiskEngine, RiskDecision
from risk.regime_detector import RegimeDetector
from execution.scale_out_executor import ScaleOutExecutor
from monitoring.engine import MonitoringEngine
from data_pipeline.pipeline import DataPipeline

logger = logging.getLogger(__name__)


class TradingOrchestrator:
    """
    Main system orchestrator.
    
    Pipeline per bar:
    1. Compute features (OB, FVG, sweeps, VWAP, delta)
    2. Check Discord signals
    3. Detect market regime
    4. If in position: manage scale-out (C1 target, C2 trailing, stops)
    5. If flat: aggregate signals → risk check → enter if approved
    6. Log everything
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

        # Execution — scale-out is the primary executor
        self.executor = ScaleOutExecutor(config)

        # Discord
        self.discord_parser = DiscordBiasParser(config)
        self.discord_listener = DiscordListener(config, self.db, self.discord_parser)

        # Tradovate client (initialized on connect)
        self.broker_client = None

        # State
        self._running = False
        self._last_discord_signal = None
        self._bars_processed = 0
        self._current_regime = "unknown"

    # ================================================================
    # INITIALIZATION
    # ================================================================
    async def initialize(self, skip_db: bool = False) -> None:
        """Initialize all components."""
        logger.info("=" * 60)
        logger.info("NQ TRADING BOT — INITIALIZING")
        logger.info(f"  Environment:  {self.config.environment}")
        logger.info(f"  Broker:       Tradovate ({self.config.tradovate.environment})")
        logger.info(f"  Symbol:       {self.config.tradovate.symbol}")
        logger.info(f"  Strategy:     2-contract scale-out")
        logger.info(f"  C1 Target:    {self.config.scale_out.c1_target_min_points}-{self.config.scale_out.c1_target_max_points} pts")
        logger.info(f"  Account:      ${self.config.risk.account_size:,.2f}")
        logger.info(f"  Max Risk:     {self.config.risk.max_risk_per_trade_pct}% per trade")
        logger.info(f"  Daily Limit:  {self.config.risk.max_daily_loss_pct}%")
        logger.info(f"  Kill Switch:  {self.config.risk.max_total_drawdown_pct}% drawdown")
        logger.info(f"  Discord:      {self.config.discord.server_name} #{self.config.discord.channel_name}")
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
        """Graceful shutdown — flatten positions first."""
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
    # MAIN BAR PROCESSING
    # ================================================================
    async def process_bar(self, bar: Bar) -> Optional[dict]:
        """
        Process one 1-minute bar through the full pipeline.
        This is THE core method — called on every bar.
        """
        if not self._running:
            return None

        self._bars_processed += 1
        self._last_bar = bar
        action_result = None

        # === 1. FEATURES ===
        features = self.feature_engine.update(bar)

        # === 2. DISCORD SIGNALS ===
        discord_signal = await self.discord_listener.get_latest_signal(timeout=0.01)
        if discord_signal:
            self._last_discord_signal = discord_signal

        # Expire stale Discord signals (>5 min old)
        if (self._last_discord_signal and
            (bar.timestamp - self._last_discord_signal.timestamp).total_seconds() > 300):
            self._last_discord_signal = None

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
                    # Trade complete — record in risk engine
                    total_pnl = result["total_pnl"]
                    self.risk_engine.record_trade_result(total_pnl, result["direction"])
                    self.monitoring.record_trade({
                        "action": "exit",
                        "pnl": total_pnl,
                        "direction": result["direction"],
                    })
                    
                    # Update Discord author accuracy
                    if self._last_discord_signal:
                        was_correct = (
                            (self._last_discord_signal.bias == "bullish" and total_pnl > 0 and result["direction"] == "long") or
                            (self._last_discord_signal.bias == "bearish" and total_pnl > 0 and result["direction"] == "short")
                        )
                        self.discord_parser.update_author_accuracy(
                            self._last_discord_signal.author_id, was_correct
                        )

                    action_result = result

                elif result.get("action") == "c1_target_hit":
                    # C1 target hit — informational
                    action_result = result

            return action_result

        # === 5. SIGNAL AGGREGATION (only if flat) ===
        signal = self.signal_aggregator.aggregate(
            discord_signal=self._last_discord_signal,
            feature_snapshot=features,
            ml_prediction=None,
            current_time=bar.timestamp,
        )

        if signal and signal.should_trade:
            direction = "long" if signal.direction == SignalDirection.LONG else "short"

            # === 6. RISK CHECK ===
            risk_assessment = self.risk_engine.evaluate_trade(
                direction=direction,
                entry_price=bar.close,
                atr=features.atr_14,
                vix=features.vix_level or 0,
                current_time=bar.timestamp,
            )

            # Regime gate — don't trade in certain regimes
            if regime_adj["size_multiplier"] == 0:
                logger.debug(f"Regime {self._current_regime} blocks new trades")
                return None

            if risk_assessment.decision in (RiskDecision.APPROVE, RiskDecision.REDUCE_SIZE):
                # === 7. ENTER SCALE-OUT TRADE ===
                trade = await self.executor.enter_trade(
                    direction=direction,
                    entry_price=bar.close,
                    stop_distance=risk_assessment.suggested_stop_distance,
                    atr=features.atr_14,
                    signal_score=signal.combined_score,
                    regime=self._current_regime,
                )

                if trade:
                    action_result = {
                        "action": "entry",
                        "direction": direction,
                        "contracts": 2,
                        "entry_price": trade.entry_price,
                        "stop": trade.initial_stop,
                        "c1_target": trade.c1.target_price,
                        "signal_score": signal.combined_score,
                        "regime": self._current_regime,
                    }
            else:
                logger.debug(f"Risk rejected: {risk_assessment.reason}")

        return action_result

    def _get_last_price(self) -> float:
        """Get the last known price."""
        if hasattr(self, '_last_bar') and self._last_bar:
            return self._last_bar.close
        return 0.0

    # ================================================================
    # BACKTEST
    # ================================================================
    async def run_backtest(self, bars: list) -> dict:
        """
        Full backtest over historical bars.
        Bars can be Bar objects or BarData objects (auto-converted).
        """
        logger.info(f"Starting backtest: {len(bars)} bars")

        # Convert BarData to Bar if needed
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

            # Progress logging
            if (i + 1) % 500 == 0:
                logger.info(f"  Processed {i+1}/{len(bars)} bars...")

        # Stats
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
        logger.info("  BACKTEST RESULTS — 2-CONTRACT SCALE-OUT")
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
        """Connect to Tradovate for live/paper trading."""
        from broker.tradovate_client import TradovateClient

        self.broker_client = TradovateClient(self.config.tradovate)
        connected = await self.broker_client.connect()

        if connected:
            # Wire up the scale-out executor to use broker
            self.executor.broker = self.broker_client

            # Register callbacks
            self.broker_client.on_bar(self._on_live_bar)
            self.broker_client.on_fill(self._on_live_fill)

            # Subscribe to market data
            await self.broker_client.subscribe_market_data()

            self.monitoring.update_health("execution", "healthy")
            logger.info("Broker connected and ready")
        else:
            self.monitoring.update_health("execution", "error", "Connection failed")

        return connected

    async def _on_live_bar(self, data: dict) -> None:
        """Callback for live bar completion from Tradovate."""
        try:
            # Parse Tradovate bar format into our Bar object
            bar = Bar(
                timestamp=datetime.now(timezone.utc),
                open=data.get("open", 0),
                high=data.get("high", 0),
                low=data.get("low", 0),
                close=data.get("close", 0),
                volume=data.get("volume", 0),
                bid_volume=data.get("bidVolume", 0),
                ask_volume=data.get("askVolume", 0),
                delta=data.get("askVolume", 0) - data.get("bidVolume", 0),
            )
            await self.process_bar(bar)
        except Exception as e:
            logger.error(f"Error processing live bar: {e}", exc_info=True)

    async def _on_live_fill(self, fill) -> None:
        """Callback for order fills from Tradovate."""
        logger.info(f"Live fill received: {fill.action} {fill.filled_qty}x @ {fill.fill_price}")

    async def _load_economic_calendar(self) -> None:
        """Load economic events from database."""
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
            pass  # Non-critical if DB unavailable

    # ================================================================
    # STATUS
    # ================================================================
    def get_system_status(self) -> dict:
        """Full system status for dashboard."""
        trade = self.executor.active_trade
        return {
            "running": self._running,
            "bars_processed": self._bars_processed,
            "current_regime": self._current_regime,
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
            "discord_connected": self.discord_listener.is_running,
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
        logger.info("  1. Run backtest:   python scripts/run_backtest.py --sample")
        logger.info("  2. Start dashboard: uvicorn dashboard.server:app --port 8080")
        logger.info("  3. Connect broker: requires Tradovate credentials in .env")
        logger.info("")

    except KeyboardInterrupt:
        logger.info("Interrupted")
    except Exception as e:
        logger.critical(f"Fatal: {e}", exc_info=True)
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
