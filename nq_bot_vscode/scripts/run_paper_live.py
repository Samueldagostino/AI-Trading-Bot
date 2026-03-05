"""
IBKR Paper Trading Runner with Safety Rails
==============================================
Main entry point for paper trading via TWS API (ib_insync).

Pipeline:
  IBKRClient (TWS socket) -> on_bar_update callback -> tws_adapter -> Bar
    -> process_bar() (HTF gate + HC filter + institutional modifiers)
      -> Safety Rails check -> Paper order execution
        -> PaperTradingMonitor (statistics, persistence)

Startup sequence:
  1. Connect to TWS / IB Gateway via socket
  2. Subscribe to MNQ real-time bars
  3. Initialize HTF engine, confluence scorer, modifier engine
  4. Initialize safety rails
  5. Initialize paper trading monitor
  6. Start main loop

Shutdown (Ctrl+C / SIGINT / SIGTERM):
  Save state, close connections, print final summary

Usage:
    python scripts/run_paper_live.py
    python scripts/run_paper_live.py --dry-run
    python scripts/run_paper_live.py --max-daily-loss 300
    python scripts/run_paper_live.py --port 4002
    python scripts/run_paper_live.py --log-level DEBUG

Port reference:
  7497 = TWS paper trading (default)
  7496 = TWS live trading
  4002 = IB Gateway paper trading
  4001 = IB Gateway live trading

Requires TWS / IB Gateway running with API access enabled.
"""

import nest_asyncio
nest_asyncio.apply()

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import random
import signal
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional
from zoneinfo import ZoneInfo

# ── Project path setup ──
script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))
sys.path.insert(0, str(script_dir))

# ── Load .env before any project imports ──
_env_path = project_dir / ".env"
if _env_path.exists():
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                os.environ.setdefault(key.strip(), val)

from config.settings import CONFIG
from features.engine import Bar
from features.htf_engine import HTFBar
from main import TradingOrchestrator, HTF_TIMEFRAMES
from execution.safety_rails import SafetyRails, SafetyRailsConfig
from paper_trading_monitor import PaperTradingMonitor
from live_dashboard import atomic_write_json
from signals.gex_monitor import GEXMonitor

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# HISTORICAL BACKFILL CONFIGURATION
# ═══════════════════════════════════════════════════════════════
HISTORICAL_TF_CONFIG = {
    "1m":  {"durationStr": "3600 S",  "barSizeSetting": "1 min"},
    "2m":  {"durationStr": "7200 S",  "barSizeSetting": "2 mins"},
    "5m":  {"durationStr": "1 D",     "barSizeSetting": "5 mins"},
    "15m": {"durationStr": "2 D",     "barSizeSetting": "15 mins"},
    "30m": {"durationStr": "5 D",     "barSizeSetting": "30 mins"},
    "1H":  {"durationStr": "10 D",    "barSizeSetting": "1 hour"},
    "4H":  {"durationStr": "30 D",    "barSizeSetting": "4 hours"},
    "1D":  {"durationStr": "180 D",   "barSizeSetting": "1 day"},
}
HISTORICAL_BARS_COUNT = 200

# ═══════════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════════
LOGS_DIR = project_dir / "logs"
ET_TZ = ZoneInfo("America/New_York")


# ═══════════════════════════════════════════════════════════════
# DRY-RUN DATA GENERATOR
# ═══════════════════════════════════════════════════════════════

class DryRunDataGenerator:
    """
    Generates synthetic MNQ-like 2-minute bars for --dry-run mode.
    No IBKR connection required.
    """

    def __init__(self, base_price: float = 24500.0):
        self._price = base_price
        self._bar_count = 0

    def generate_bar(self) -> Bar:
        """Generate a single synthetic 2-minute bar."""
        # Random walk with slight mean reversion
        move = random.gauss(0, 8.0)  # ~8pt std dev per 2m bar (MNQ-like)
        mean_revert = (24500.0 - self._price) * 0.001
        self._price += move + mean_revert

        open_price = self._price
        high = open_price + abs(random.gauss(0, 5.0))
        low = open_price - abs(random.gauss(0, 5.0))
        close = open_price + random.gauss(0, 3.0)
        volume = max(100, int(random.gauss(1500, 500)))

        self._bar_count += 1

        return Bar(
            timestamp=datetime.now(timezone.utc),
            open=round(open_price, 2),
            high=round(max(high, open_price, close), 2),
            low=round(min(low, open_price, close), 2),
            close=round(close, 2),
            volume=volume,
        )


# ═══════════════════════════════════════════════════════════════
# PAPER TRADING RUNNER
# ═══════════════════════════════════════════════════════════════

class PaperLiveRunner:
    """
    Main paper trading runner with TWS connectivity and safety rails.

    Coordinates:
      - TWS real-time bar feed (or dry-run synthetic data)
      - TradingOrchestrator.process_bar()
      - SafetyRails (circuit breakers)
      - PaperTradingMonitor (statistics)
    """

    # Dashboard refresh interval
    DASHBOARD_INTERVAL_SECONDS = 120

    def __init__(
        self,
        dry_run: bool = False,
        max_daily_loss: float = 500.0,
        log_level: str = "INFO",
        port: int = 7497,
    ):
        self._dry_run = dry_run
        self._max_daily_loss = max_daily_loss
        self._log_level = log_level
        self._tws_port = port

        # Core components (initialized in start())
        self._bot: Optional[TradingOrchestrator] = None
        self._ibkr_client = None

        # Safety rails
        self._safety_rails = SafetyRails(SafetyRailsConfig(
            max_daily_loss=max_daily_loss,
            max_consecutive_losses=5,
            max_position_size=2,       # ABSOLUTE — no exceptions
            heartbeat_alert_seconds=60.0,
            heartbeat_halt_seconds=300.0,
            log_dir=str(LOGS_DIR),
        ))

        # Paper trading monitor
        self._monitor = PaperTradingMonitor(
            log_dir=str(LOGS_DIR),
            account_size=CONFIG.risk.account_size,
        )

        # Dry-run data generator
        self._dry_gen = DryRunDataGenerator() if dry_run else None

        # State
        self._shutdown_event = asyncio.Event()
        self._bars_processed = 0
        self._trades_executed = 0
        self._last_dashboard_time: float = 0.0

        # Dashboard state: circular candle buffer (last 200 bars)
        self._candle_buffer: list = []
        self._candle_buffer_max = 200

        # Historical bars loaded flag (request ONCE on startup)
        self._historical_loaded = False

        # Track last confluence score for GEX refresh urgency
        self._last_confluence = 0.0

    # ──────────────────────────────────────────────────────────
    # STARTUP
    # ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Full startup sequence."""
        self._print_banner()
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        # a. Initialize TradingOrchestrator
        self._bot = TradingOrchestrator(CONFIG)
        await self._bot.initialize(skip_db=True)
        logger.info("TradingOrchestrator initialized (skip_db=True)")

        # a2. Initialize GEX Monitor and inject into modifier engine
        try:
            self._gex_monitor = GEXMonitor()
            if hasattr(self._bot, '_institutional_engine'):
                self._bot._institutional_engine.gex_monitor = self._gex_monitor
            # Attempt first GEX fetch (will use mock if no token)
            gex_result = self._gex_monitor.update()
            if self._gex_monitor.enabled:
                logger.info("GEX Monitor: LIVE (Quant Data API)")
            else:
                logger.info("GEX Monitor: MOCK (no token configured)")
            if gex_result:
                for ticker, data in gex_result.items():
                    logger.info("  %s: net_gex=%s regime=%s",
                                ticker.upper(),
                                data.get("net_gex_display", "N/A"),
                                data.get("regime", "UNKNOWN"))
        except Exception as e:
            logger.warning("GEX Monitor initialization failed: %s (continuing without GEX)", e)
            self._gex_monitor = None

        # b. Connect TWS (or skip for dry-run)
        if not self._dry_run:
            connected = await self._connect_tws()
            if not connected:
                logger.critical("TWS connection failed — exiting")
                sys.exit(1)
        else:
            logger.info("DRY-RUN mode — using synthetic data (no TWS)")

        # c. Safety rails already initialized in __init__
        logger.info("Safety rails initialized:")
        logger.info("  Max daily loss:       $%.2f", self._max_daily_loss)
        logger.info("  Max consecutive loss: %d", self._safety_rails.consecutive_losses.max_consecutive)
        logger.info("  Max position size:    %d contracts (ABSOLUTE)", self._safety_rails.position_size.max_contracts)
        logger.info("  Heartbeat alert:      %.0fs", self._safety_rails.heartbeat.alert_seconds)
        logger.info("  Heartbeat halt:       %.0fs", self._safety_rails.heartbeat.halt_seconds)

        # d. Monitor already initialized in __init__
        logger.info("Paper trading monitor initialized")

        # e. Historical data backfill (ONCE on startup)
        logger.info("Loading historical bars for dashboard...")
        await self._backfill_historical_bars()

        # f. Start main loop
        logger.info("=" * 60)
        logger.info("  PAPER TRADING ACTIVE")
        logger.info("=" * 60)

        await self._run_loop()

    async def _connect_tws(self) -> bool:
        """Initialize TWS connection and subscribe to market data."""
        try:
            from Broker.ibkr_client import IBKRClient

            # Use pre-injected client if available (from ibkr_startup)
            if self._ibkr_client is None:
                tws_host = os.environ.get("IBKR_TWS_HOST", "127.0.0.1")
                client_id = int(os.environ.get("IBKR_CLIENT_ID", "1"))

                self._ibkr_client = IBKRClient(
                    host=tws_host,
                    port=self._tws_port,
                    client_id=client_id,
                )
                connected = await self._ibkr_client.connect()
                if not connected:
                    return False

            # Register bar callback
            self._ibkr_client.on_bar_update(self._on_tws_bar)

            # Subscribe to MNQ market data
            subscribed = await self._ibkr_client.subscribe_market_data("MNQ", "CME")
            if not subscribed:
                logger.error("Failed to subscribe to MNQ market data")
                return False

            logger.info("TWS connected: port %d", self._tws_port)
            return True

        except ImportError:
            logger.error("ib_insync not installed. Required for TWS. Use --dry-run instead.")
            return False
        except Exception as e:
            logger.error("TWS connection failed: %s", e)
            return False

    def _print_banner(self) -> None:
        logger.info("=" * 60)
        logger.info("  IBKR PAPER TRADING RUNNER (TWS API + Safety Rails)")
        logger.info("  Mode:           %s", "DRY-RUN (synthetic data)" if self._dry_run else "LIVE DATA")
        logger.info("  TWS Port:       %d", self._tws_port)
        logger.info("  Max daily loss: $%.2f", self._max_daily_loss)
        logger.info("  Max pos size:   2 contracts (ABSOLUTE)")
        logger.info("  HC filter:      score>=0.75, stop<=30pts")
        logger.info("  HTF gate:       strength>=0.3")
        logger.info("  Log level:      %s", self._log_level)
        logger.info("=" * 60)

    # ──────────────────────────────────────────────────────────
    # BAR PROCESSING
    # ──────────────────────────────────────────────────────────

    def _on_tws_bar(self, bar: Bar) -> None:
        """Callback from TWS real-time bars — schedule async processing."""
        asyncio.get_event_loop().create_task(self._process_bar_safe(bar))

    async def _process_bar_safe(self, bar: Bar) -> None:
        """Process a bar with full error handling."""
        try:
            await self._process_bar(bar)
        except Exception as e:
            logger.error("Error processing bar: %s", e, exc_info=True)

    async def _process_bar(self, bar: Bar) -> None:
        """Process a single bar through the full pipeline with safety rails."""
        self._bars_processed += 1

        # Update heartbeat
        self._safety_rails.on_bar_received()

        # Check market hours for heartbeat
        et_now = bar.timestamp.astimezone(ET_TZ)
        t = et_now.hour + et_now.minute / 60.0
        is_market_hours = 9.5 <= t < 16.0
        self._safety_rails.set_market_hours(is_market_hours)

        # Check safety rails BEFORE processing
        if not self._safety_rails.check_all():
            status = self._safety_rails.get_status()
            if self._bars_processed % 60 == 1:  # Log every ~2 min (60 bars at 2s)
                logger.warning("Safety rails HALTED trading: %s", status)
            return

        # Refresh GEX data with urgency based on trading state
        if self._gex_monitor is not None:
            has_position = (
                self._bot and self._bot.executor.has_active_trade
            )
            if has_position:
                gex_urgency = "active"
            elif self._last_confluence > 0.65:
                gex_urgency = "preflight"
            else:
                gex_urgency = "idle"
            self._gex_monitor.update(urgency=gex_urgency)

        # Route through process_bar()
        result = await self._bot.process_bar(bar)

        if result:
            action = result.get("action", "")

            # Track confluence score for GEX urgency
            score = result.get("signal_score", 0)
            if score:
                self._last_confluence = score

            if action == "entry":
                # Clamp position size through safety guard
                requested = result.get("contracts", 2)
                allowed = self._safety_rails.clamp_position_size(requested)
                result["contracts"] = allowed

                self._trades_executed += 1
                logger.info(
                    "ENTRY: %s @ %.2f | Stop: %.2f | Score: %.3f | Source: %s",
                    result.get("direction", "?").upper(),
                    result.get("entry_price", 0),
                    result.get("stop", 0),
                    result.get("signal_score", 0),
                    result.get("signal_source", "?"),
                )

            elif action == "trade_closed":
                pnl = result.get("total_pnl", 0)

                # Record through safety rails
                tripped = self._safety_rails.record_trade(pnl)

                # Record in monitor
                self._monitor.record_trade(
                    pnl=pnl,
                    direction=result.get("direction", ""),
                    entry_price=result.get("entry_price", 0),
                    exit_price=result.get("exit_price", 0),
                    signal_score=result.get("signal_score", 0),
                    signal_source=result.get("signal_source", ""),
                    regime=result.get("regime", ""),
                    htf_bias=result.get("htf_bias", ""),
                    c1_pnl=result.get("c1_pnl", 0),
                    c2_pnl=result.get("c2_pnl", 0),
                    contracts=result.get("contracts", 2),
                    metadata={
                        "inst_position_mult": result.get("inst_position_mult"),
                        "inst_stop_mult": result.get("inst_stop_mult"),
                    },
                )

                logger.info(
                    "TRADE CLOSED: PnL $%.2f | C1: $%.2f | C2: $%.2f | Daily: $%.2f",
                    pnl,
                    result.get("c1_pnl", 0),
                    result.get("c2_pnl", 0),
                    self._safety_rails.daily_loss.daily_pnl,
                )

                if tripped:
                    logger.critical("SAFETY RAIL TRIPPED — trading halted")

        # Periodic monitor update
        self._monitor.update()

        # Append candle to buffer for dashboard
        self._append_candle(bar)

        # Append to historical 2m file (so dashboard has seamless data)
        self._append_live_bar_to_historical(bar)

        # Write dashboard state files (lightweight, atomic)
        self._write_dashboard_state(bar)

    # ──────────────────────────────────────────────────────────
    # DASHBOARD STATE FILES
    # ──────────────────────────────────────────────────────────

    def _append_candle(self, bar: Bar) -> None:
        """Append bar to circular candle buffer."""
        candle = {
            "time": bar.timestamp.isoformat(),
            "o": round(bar.open, 2),
            "h": round(bar.high, 2),
            "l": round(bar.low, 2),
            "c": round(bar.close, 2),
            "vol": bar.volume,
        }
        self._candle_buffer.append(candle)
        if len(self._candle_buffer) > self._candle_buffer_max:
            self._candle_buffer = self._candle_buffer[-self._candle_buffer_max:]

    def _write_dashboard_state(self, bar: Bar) -> None:
        """Write state files for the live dashboard (atomic writes)."""
        try:
            # Candle buffer
            atomic_write_json(
                LOGS_DIR / "candle_buffer.json",
                self._candle_buffer,
            )

            # Active trades
            active = []
            if self._bot and self._bot.executor.has_active_trade:
                exc = self._bot.executor
                trade_state = exc.get_state() if hasattr(exc, 'get_state') else {}
                active.append({
                    "id": trade_state.get("trade_id", "T1"),
                    "dir": trade_state.get("direction", ""),
                    "ep": trade_state.get("entry_price", 0),
                    "contracts": trade_state.get("contracts", 2),
                    "entry_time": trade_state.get("entry_time", ""),
                    "unrealized_pnl": trade_state.get("unrealized_pnl",
                        self._calc_unrealized_pnl(trade_state, bar.close)),
                    "modifier": trade_state.get("modifier", 1.0),
                })
            atomic_write_json(LOGS_DIR / "active_trades.json", active)

            # Modifier state
            mod_state = self._get_modifier_state()
            atomic_write_json(LOGS_DIR / "modifier_state.json", mod_state)

            # Safety state
            safety_state = self._get_safety_state()
            atomic_write_json(LOGS_DIR / "safety_state.json", safety_state)

        except Exception as e:
            logger.debug("Dashboard state write error: %s", e)

    def _calc_unrealized_pnl(self, trade_state: dict, current_price: float) -> float:
        """Calculate unrealized PnL for an active trade."""
        ep = trade_state.get("entry_price", 0)
        direction = trade_state.get("direction", "")
        contracts = trade_state.get("contracts", 2)
        if not ep or not direction:
            return 0.0
        # MNQ = $5 per point per contract
        pts = (current_price - ep) if direction == "long" else (ep - current_price)
        return round(pts * 5.0 * contracts, 2)

    def _get_modifier_state(self) -> dict:
        """Get current modifier values from the institutional engine."""
        default = {
            "overnight": {"value": 1.0, "reason": "No data"},
            "fomc": {"value": 1.0, "reason": "No data"},
            "gamma": {"value": 1.0, "reason": "No data"},
            "har_rv": {"value": 1.0, "reason": "No data"},
            "total": 1.0,
        }
        if not self._bot or not hasattr(self._bot, '_institutional_engine'):
            return default

        engine = self._bot._institutional_engine
        if not hasattr(engine, '_last_result') or engine._last_result is None:
            return default

        result = engine._last_result
        details = result.details if hasattr(result, 'details') else {}
        overnight = details.get("overnight", {})
        fomc = details.get("fomc", {})
        gamma = details.get("gamma", {})
        vol = details.get("volatility", {})

        return {
            "overnight": {
                "value": overnight.get("position_multiplier", 1.0),
                "reason": overnight.get("classification", "neutral"),
            },
            "fomc": {
                "value": fomc.get("position_multiplier", 1.0),
                "reason": fomc.get("window", "none"),
            },
            "gamma": {
                "value": gamma.get("position_multiplier", gamma.get("position", 1.0)),
                "reason": gamma.get("reason", gamma.get("regime", "unknown")),
            },
            "har_rv": {
                "value": vol.get("position_multiplier", vol.get("position", 1.0)),
                "reason": vol.get("reason", vol.get("forecast_label", "normal")),
            },
            "total": result.position_multiplier if hasattr(result, 'position_multiplier') else 1.0,
        }

    def _get_safety_state(self) -> dict:
        """Get current safety rail state for dashboard."""
        sr = self._safety_rails
        return {
            "daily_pnl": sr.daily_loss.daily_pnl,
            "daily_limit": sr.daily_loss.max_daily_loss,
            "consec_losses": sr.consecutive_losses._consecutive_losses,
            "max_consec": sr.consecutive_losses.max_consecutive,
            "position_size": sr.position_size.max_contracts if (
                self._bot and self._bot.executor.has_active_trade) else 0,
            "max_position": sr.position_size.max_contracts,
            "heartbeat_age_sec": round(sr.heartbeat.seconds_since_last, 1),
            "all_ok": sr.check_all(),
        }

    # ──────────────────────────────────────────────────────────
    # HISTORICAL DATA BACKFILL
    # ──────────────────────────────────────────────────────────

    async def _backfill_historical_bars(self) -> None:
        """
        Request historical bars from IBKR for each timeframe.
        Called ONCE on startup. Stores results in logs/historical_bars_{tf}.json.
        Also feeds bars through the HTF engine to seed multi-timeframe bias,
        initializes HAR-RV from daily bars, and seeds overnight modifier.
        """
        if self._historical_loaded:
            return

        if self._dry_run:
            logger.info("Dry-run mode: generating synthetic historical bars")
            self._generate_synthetic_historical()
            self._historical_loaded = True
            self._log_htf_biases()
            return

        if not self._ibkr_client:
            logger.warning("No IBKR client — skipping historical backfill")
            self._historical_loaded = True
            return

        try:
            ib = self._ibkr_client._ib  # Access underlying ib_insync.IB instance
            from ib_insync import Future

            contract = Future("MNQ", exchange="CME")
            ib.qualifyContracts(contract)

            for tf_name, tf_cfg in HISTORICAL_TF_CONFIG.items():
                try:
                    logger.info("Requesting historical bars: %s (%s, %s)",
                                tf_name, tf_cfg["durationStr"], tf_cfg["barSizeSetting"])

                    bars = ib.reqHistoricalData(
                        contract,
                        endDateTime="",
                        durationStr=tf_cfg["durationStr"],
                        barSizeSetting=tf_cfg["barSizeSetting"],
                        whatToShow="TRADES",
                        useRTH=False,
                        formatDate=1,
                    )

                    candle_list = []
                    htf_bars_fed = 0
                    for b in bars[-HISTORICAL_BARS_COUNT:]:
                        bar_time = b.date if hasattr(b, 'date') else b.time
                        candle_list.append({
                            "time": str(bar_time),
                            "o": round(float(b.open), 2),
                            "h": round(float(b.high), 2),
                            "l": round(float(b.low), 2),
                            "c": round(float(b.close), 2),
                            "vol": int(b.volume) if b.volume >= 0 else 0,
                        })

                        # Feed through HTF engine if this is an HTF timeframe
                        self._feed_htf_bar(tf_name, bar_time,
                                           float(b.open), float(b.high),
                                           float(b.low), float(b.close),
                                           int(b.volume) if b.volume >= 0 else 0)
                        htf_bars_fed += 1

                    filepath = LOGS_DIR / f"historical_bars_{tf_name}.json"
                    atomic_write_json(filepath, candle_list)
                    logger.info("  %s: %d bars saved, %d fed to HTF engine",
                                tf_name, len(candle_list), htf_bars_fed)

                    # Initialize HAR-RV from daily bars
                    if tf_name == "1D" and self._bot and candle_list:
                        self._init_har_rv_from_daily(candle_list)

                    # Initialize overnight modifier from daily bars
                    if tf_name == "1D" and self._bot and candle_list:
                        self._init_overnight_from_daily(candle_list)

                    # Brief pause to avoid IBKR pacing violations
                    await asyncio.sleep(1.0)

                except Exception as e:
                    logger.warning("Failed to fetch historical %s: %s", tf_name, e)

        except ImportError:
            logger.warning("ib_insync not available — skipping historical backfill")
        except Exception as e:
            logger.warning("Historical backfill error: %s", e)

        self._historical_loaded = True
        self._log_htf_biases()

    def _feed_htf_bar(self, tf_name: str, bar_time, o: float, h: float,
                      l: float, c: float, vol: int) -> None:
        """Feed a single historical bar into the HTF engine."""
        if not self._bot:
            return

        # Convert timestamp
        if isinstance(bar_time, datetime):
            ts = bar_time if bar_time.tzinfo else bar_time.replace(tzinfo=timezone.utc)
        elif isinstance(bar_time, str):
            try:
                ts = datetime.fromisoformat(bar_time)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                ts = datetime.now(timezone.utc)
        else:
            ts = datetime.now(timezone.utc)

        htf_bar = HTFBar(
            timestamp=ts,
            open=round(o, 2),
            high=round(max(h, o, c), 2),
            low=round(min(l, o, c), 2),
            close=round(c, 2),
            volume=max(vol, 1),
        )
        try:
            self._bot.htf_engine.update_bar(tf_name, htf_bar)
        except Exception as e:
            logger.debug("HTF feed error for %s: %s", tf_name, e)

    def _init_har_rv_from_daily(self, daily_candles: list) -> None:
        """Initialize the HAR-RV forecaster from historical daily bars."""
        try:
            forecaster = self._bot._institutional_engine.vol_forecaster
            import math
            for i in range(1, len(daily_candles)):
                prev_c = daily_candles[i - 1].get("c", 0)
                curr_c = daily_candles[i].get("c", 0)
                if prev_c > 0 and curr_c > 0:
                    log_return = math.log(curr_c / prev_c)
                    daily_rv = log_return ** 2
                    forecaster.update(daily_rv)
            if forecaster.has_enough_data:
                forecast = forecaster.forecast()
                logger.info("HAR-RV initialized: %d days of data, forecast=%.6f",
                            len(forecaster._daily_history), forecast)
            else:
                logger.info("HAR-RV seeded with %d days (need %d for forecasting)",
                            len(forecaster._daily_history), forecaster.MIN_HISTORY)
        except Exception as e:
            logger.warning("HAR-RV initialization failed: %s", e)

    def _init_overnight_from_daily(self, daily_candles: list) -> None:
        """Seed the overnight modifier with the most recent previous close."""
        try:
            if len(daily_candles) >= 2:
                overnight = self._bot._institutional_engine.overnight
                prev_close = daily_candles[-2].get("c", 0)
                if prev_close > 0:
                    overnight._prev_day_close = prev_close
                    # Use yesterday's date
                    try:
                        ts_str = daily_candles[-2].get("time", "")
                        prev_date = datetime.fromisoformat(str(ts_str)).date()
                    except (ValueError, TypeError):
                        prev_date = (datetime.now(ET_TZ) - timedelta(days=1)).date()
                    overnight._prev_close_date = prev_date
                    logger.info("Overnight modifier seeded: prev_close=%.2f", prev_close)
        except Exception as e:
            logger.warning("Overnight modifier initialization failed: %s", e)

    def _log_htf_biases(self) -> None:
        """Log HTF biases after backfill."""
        if not self._bot:
            return
        try:
            bias = self._bot.htf_engine.get_bias(datetime.now(timezone.utc))
            bias_parts = []
            for tf in ["1D", "4H", "1H", "30m", "15m", "5m"]:
                b = bias.tf_biases.get(tf, "N/A")
                bias_parts.append(f"{tf}={b.upper() if b != 'N/A' else 'N/A'}")
            logger.info("HTF backfill complete: %s", ", ".join(bias_parts))
            logger.info("HTF consensus: %s (strength=%.2f)",
                         bias.consensus_direction.upper(), bias.consensus_strength)
            # Cache the bias on the bot
            self._bot._htf_bias = bias
        except Exception as e:
            logger.warning("HTF bias summary failed: %s", e)

    def _generate_synthetic_historical(self) -> None:
        """Generate synthetic historical data for dry-run mode."""
        import math

        base_price = 24500.0
        # Timeframe-specific intervals in minutes for timestamp generation
        tf_minutes = {
            "1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30,
            "1H": 60, "4H": 240, "1D": 1440,
        }

        for tf_name in HISTORICAL_TF_CONFIG:
            candle_list = []
            price = base_price
            interval = tf_minutes.get(tf_name, 2)
            for i in range(HISTORICAL_BARS_COUNT):
                move = random.gauss(0, 6.0) + math.sin(i * 0.05) * 2.0
                price += move
                o = round(price, 2)
                h = round(o + abs(random.gauss(0, 4.0)), 2)
                l = round(o - abs(random.gauss(0, 4.0)), 2)
                c = round(o + random.gauss(0, 3.0), 2)
                h = max(h, o, c)
                l = min(l, o, c)
                vol = max(100, int(random.gauss(1500, 500)))
                # Create plausible timestamps going backwards
                ts = datetime.now(timezone.utc) - timedelta(
                    minutes=(HISTORICAL_BARS_COUNT - i) * interval)
                candle_list.append({
                    "time": ts.isoformat(),
                    "o": o, "h": h, "l": l, "c": c, "vol": vol,
                })

                # Feed through HTF engine
                self._feed_htf_bar(tf_name, ts, o, h, l, c, vol)

            filepath = LOGS_DIR / f"historical_bars_{tf_name}.json"
            atomic_write_json(filepath, candle_list)
            logger.info("  %s: %d synthetic bars saved + fed to HTF engine", tf_name, len(candle_list))

            # Initialize HAR-RV from daily bars
            if tf_name == "1D" and self._bot and candle_list:
                self._init_har_rv_from_daily(candle_list)

            # Initialize overnight modifier from daily bars
            if tf_name == "1D" and self._bot and candle_list:
                self._init_overnight_from_daily(candle_list)

    def _append_live_bar_to_historical(self, bar: Bar) -> None:
        """Append a new live 2m bar to the historical_bars_2m.json file."""
        filepath = LOGS_DIR / "historical_bars_2m.json"
        try:
            if filepath.exists():
                text = filepath.read_text(encoding="utf-8").strip()
                data = json.loads(text) if text else []
            else:
                data = []

            candle = {
                "time": bar.timestamp.isoformat(),
                "o": round(bar.open, 2),
                "h": round(bar.high, 2),
                "l": round(bar.low, 2),
                "c": round(bar.close, 2),
                "vol": bar.volume,
            }
            data.append(candle)

            # Keep only last 500 bars to prevent unbounded growth
            if len(data) > 500:
                data = data[-500:]

            atomic_write_json(filepath, data)
        except Exception as e:
            logger.debug("Error appending live bar to historical: %s", e)

    # ──────────────────────────────────────────────────────────
    # MAIN LOOP
    # ──────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        """Main event loop."""
        try:
            while not self._shutdown_event.is_set():
                if self._dry_run:
                    # Generate synthetic bar every 2 seconds
                    bar = self._dry_gen.generate_bar()
                    await self._process_bar(bar)
                    await asyncio.sleep(2.0)
                else:
                    # Bars arrive via TWS callback, just monitor
                    await asyncio.sleep(1.0)

                    # Check heartbeat
                    if not self._safety_rails.heartbeat.check():
                        logger.critical("Heartbeat HALT — no data received")
                        break

                # Dashboard refresh
                now = time.monotonic()
                if now - self._last_dashboard_time >= self.DASHBOARD_INTERVAL_SECONDS:
                    self._print_dashboard()
                    self._last_dashboard_time = now

                # Check if any breaker tripped
                if not self._safety_rails.check_all():
                    status = self._safety_rails.get_status()
                    logger.critical("Safety rails HALTED — require manual reset")
                    logger.info("Status: %s", json.dumps(status, indent=2))
                    # Continue loop but don't trade — wait for shutdown
                    await asyncio.sleep(5.0)

        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    # ──────────────────────────────────────────────────────────
    # DASHBOARD
    # ──────────────────────────────────────────────────────────

    def _print_dashboard(self) -> None:
        """Print status dashboard."""
        if not self._bot:
            return

        stats = self._monitor.get_stats()
        rail_status = self._safety_rails.get_status()
        pf = stats.get("profit_factor")
        pf_str = f"{pf:.2f}" if pf is not None else "inf"

        et_now = datetime.now(ET_TZ)
        et_str = et_now.strftime("%H:%M:%S ET")

        W = 62
        bar = "=" * W

        lines = [
            "",
            bar,
            f"  PAPER TRADING  {et_str}",
            bar,
            "",
            f"  BARS         {self._bars_processed}",
            f"  TRADES       {stats['trade_count']}  ({stats['wins']}W / {stats['losses']}L)",
            f"  PnL          ${stats['total_pnl']:+.2f}",
            f"  Win Rate     {stats['win_rate']:.1f}%",
            f"  Profit Fac   {pf_str}",
            f"  Sharpe       {stats['sharpe_estimate']:.2f}",
            f"  Drawdown     ${stats['current_drawdown']:.2f} (max: ${stats['max_drawdown']:.2f})",
            "",
            f"  SAFETY       {'OK' if rail_status['trading_allowed'] else 'HALTED'}",
            f"  Daily PnL    ${self._safety_rails.daily_loss.daily_pnl:+.2f}  (limit: -${self._max_daily_loss:.0f})",
            f"  Consec Loss  {self._safety_rails.consecutive_losses.consecutive_losses} / {self._safety_rails.consecutive_losses.max_consecutive}",
            "",
            bar,
        ]
        print("\n".join(lines))

    # ──────────────────────────────────────────────────────────
    # SHUTDOWN
    # ──────────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Graceful shutdown: save state, close connections, print summary."""
        logger.info("=" * 60)
        logger.info("  SHUTDOWN INITIATED")
        logger.info("=" * 60)

        # Flatten any open positions
        if self._bot and self._bot.executor.has_active_trade:
            logger.warning("Flattening open position before shutdown")
            price = self._bot._get_last_price()
            if price > 0:
                result = await self._bot.executor.emergency_flatten(price)
                if result:
                    pnl = result.get("total_pnl", 0)
                    self._safety_rails.record_trade(pnl)
                    self._monitor.record_trade(
                        pnl=pnl,
                        direction=result.get("direction", ""),
                        metadata={"exit_reason": "shutdown_flatten"},
                    )

        # Save monitor state
        self._monitor.save_state()

        # Print final summary
        self._monitor.print_dashboard()
        self._print_safety_summary()

        # Close TWS connection
        if self._ibkr_client:
            try:
                self._ibkr_client.disconnect()
            except Exception as e:
                logger.warning("Error disconnecting TWS: %s", e)

        # Close orchestrator
        if self._bot:
            try:
                await self._bot.shutdown()
            except Exception as e:
                logger.warning("Error shutting down bot: %s", e)

        logger.info("Shutdown complete — all state saved")

    def _print_safety_summary(self) -> None:
        """Print safety rail status summary."""
        status = self._safety_rails.get_status()
        print(f"\n  SAFETY RAILS: {'ALL OK' if status['trading_allowed'] else 'HALTED'}")
        print(f"  Daily loss breaker:     {'TRIPPED' if status['daily_loss']['tripped'] else 'OK'}")
        print(f"  Consecutive losses:     {'TRIPPED' if status['consecutive_losses']['tripped'] else 'OK'}")
        print(f"  Heartbeat:              {'TRIPPED' if status['heartbeat']['tripped'] else 'OK'}")
        print(f"  Position size clamps:   {status['position_size']['clamp_count']}")

    def request_shutdown(self) -> None:
        """Called from signal handler."""
        self._shutdown_event.set()


# ═══════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="IBKR Paper Trading Runner — MNQ with safety rails (TWS API)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python scripts/run_paper_live.py                     # Live TWS paper trading
  python scripts/run_paper_live.py --dry-run            # Synthetic data, no IBKR
  python scripts/run_paper_live.py --max-daily-loss 300  # Override daily loss limit
  python scripts/run_paper_live.py --port 4002          # Use IB Gateway paper port
  python scripts/run_paper_live.py --log-level DEBUG    # Verbose logging

Port reference:
  7497 = TWS paper trading (default)
  7496 = TWS live trading
  4002 = IB Gateway paper trading
  4001 = IB Gateway live trading

Safety Rails:
  - Max daily loss:       $500 (configurable via --max-daily-loss)
  - Max consecutive loss: 5 trades -> HALT
  - Max position size:    2 contracts (ABSOLUTE, not configurable)
  - Heartbeat timeout:    60s alert, 300s halt

All circuit breakers require manual restart after tripping.
""",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate without IBKR connection (uses random synthetic data)",
    )
    parser.add_argument(
        "--max-daily-loss", type=float, default=500.0,
        help="Maximum daily loss before halting (default: $500)",
    )
    parser.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING"],
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--port", type=int, default=7497,
        help="TWS/Gateway port (default: 7497 for TWS paper)",
    )
    args = parser.parse_args()

    # Create log directory
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Configure logging
    log_level = getattr(logging, args.log_level)
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    root_logger.addHandler(console_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        str(LOGS_DIR / "paper_live.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root_logger.addHandler(file_handler)

    # Use ibkr_startup flow when not in dry-run mode for automated startup
    if not args.dry_run:
        try:
            from ibkr_startup import IBKRStartupRunner
            startup_runner = IBKRStartupRunner(
                dry_run=False,
                max_daily_loss=args.max_daily_loss,
                log_level=args.log_level,
                port=args.port,
            )
            loop = asyncio.new_event_loop()

            def _startup_signal_handler():
                logger.info("Shutdown signal received (SIGINT/SIGTERM)")
                startup_runner.request_shutdown()

            if sys.platform != "win32":
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, _startup_signal_handler)

            try:
                loop.run_until_complete(startup_runner.run())
            except KeyboardInterrupt:
                pass
            except Exception:
                logger.critical("UNHANDLED EXCEPTION:\n%s", traceback.format_exc())
                sys.exit(1)
            finally:
                loop.close()
            return
        except ImportError:
            logger.info("ibkr_startup not available, falling back to direct runner")

    runner = PaperLiveRunner(
        dry_run=args.dry_run,
        max_daily_loss=args.max_daily_loss,
        log_level=args.log_level,
        port=args.port,
    )

    # Event loop with signal handlers
    loop = asyncio.new_event_loop()

    def _signal_handler():
        logger.info("Shutdown signal received (SIGINT/SIGTERM)")
        runner.request_shutdown()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

    SHUTDOWN_TIMEOUT_SECONDS = 30

    try:
        loop.run_until_complete(runner.start())
    except KeyboardInterrupt:
        try:
            loop.run_until_complete(
                asyncio.wait_for(runner.shutdown(), timeout=SHUTDOWN_TIMEOUT_SECONDS)
            )
        except asyncio.TimeoutError:
            logger.critical(
                "Shutdown timed out after %ds — MANUAL CHECK REQUIRED",
                SHUTDOWN_TIMEOUT_SECONDS,
            )
    except Exception:
        logger.critical(
            "UNHANDLED EXCEPTION — shutting down\n%s",
            traceback.format_exc(),
        )
        try:
            loop.run_until_complete(
                asyncio.wait_for(runner.shutdown(), timeout=SHUTDOWN_TIMEOUT_SECONDS)
            )
        except asyncio.TimeoutError:
            logger.critical(
                "Shutdown timed out after %ds — MANUAL CHECK REQUIRED",
                SHUTDOWN_TIMEOUT_SECONDS,
            )
        sys.exit(1)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
