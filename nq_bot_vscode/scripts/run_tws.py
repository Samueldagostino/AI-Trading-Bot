"""
IBKR TWS Direct Runner
=======================
Connects to TWS/IB Gateway via socket API (ib_insync) on port 7497.
No Client Portal Gateway required — no browser login, no session timeouts.

Pipeline:
  TWS (5-sec real-time bars) -> 2-min candle aggregator -> Bar
    -> Feature Engine -> Signal Aggregator -> HC Filter -> Risk Engine
      -> SignalBridge -> IBKROrderExecutor (paper) -> PositionManager

Usage:
    python scripts/run_tws.py
    python scripts/run_tws.py --dry-run
    python scripts/run_tws.py --port 7497

Requires .env with:
    IBKR_TWS_HOST   (default: 127.0.0.1)
    IBKR_TWS_PORT   (default: 7497)
    IBKR_CLIENT_ID  (default: 1)
"""

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── Project path setup ──
script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))

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
from Broker.order_executor import ExecutorConfig
from monitoring.alerting import AlertManager, set_alert_manager
from monitoring.json_logger import JSONLineLogger

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# PATHS
# ═══════════════════════════════════════════════════════════════
LOGS_DIR = project_dir / "logs"

# ═══════════════════════════════════════════════════════════════
# 2-MINUTE CANDLE AGGREGATOR
# ═══════════════════════════════════════════════════════════════

class TwoCandleAggregator:
    """
    Aggregates TWS 5-second real-time bars into 2-minute candles.

    TWS reqRealTimeBars delivers 5-sec bars. We accumulate 24 of them
    (24 × 5 = 120 seconds = 2 minutes) and emit a completed Bar.
    """

    BARS_PER_CANDLE = 24  # 24 × 5-sec = 2 min

    def __init__(self, on_candle):
        self._on_candle = on_candle
        self._buffer = []
        self._candle_count = 0

    def on_bar(self, bar: Bar) -> None:
        """Feed a 5-second bar. Emits a 2-minute candle when complete."""
        self._buffer.append(bar)

        if len(self._buffer) >= self.BARS_PER_CANDLE:
            candle = Bar(
                timestamp=self._buffer[-1].timestamp,
                open=self._buffer[0].open,
                high=max(b.high for b in self._buffer),
                low=min(b.low for b in self._buffer),
                close=self._buffer[-1].close,
                volume=sum(b.volume for b in self._buffer),
            )
            self._candle_count += 1
            self._buffer.clear()
            self._on_candle(candle)


# ═══════════════════════════════════════════════════════════════
# HTF CANDLE AGGREGATOR  (2-min → 5-min, 15-min)
# ═══════════════════════════════════════════════════════════════

class HTFCandleAggregator:
    """
    Aggregates 2-minute execution bars into higher-timeframe candles
    (5-min, 15-min) and routes them to the pipeline's HTF engine.

    Uses clock-aligned boundaries:
      - 5-min:  boundaries at :00, :05, :10, …, :55
      - 15-min: boundaries at :00, :15, :30, :45

    When a 2-min bar's timestamp crosses into the next boundary, the
    accumulated OHLCV candle is emitted and a new one starts.
    """

    def __init__(self, on_htf_candle):
        """
        Args:
            on_htf_candle: callback(timeframe: str, bar: Bar) called
                           when a 5m or 15m candle completes.
        """
        self._on_htf_candle = on_htf_candle
        # Separate accumulators for each timeframe
        self._accum = {}          # tf -> {"o","h","l","c","vol","ts"}
        self._tf_minutes = {"5m": 5, "15m": 15}
        self._htf_candles_emitted = 0

    @staticmethod
    def _boundary(ts: datetime, minutes: int) -> datetime:
        """Return the clock-aligned boundary start for *ts*."""
        return ts.replace(
            minute=(ts.minute // minutes) * minutes,
            second=0, microsecond=0,
        )

    def on_bar(self, bar: Bar) -> None:
        """Feed a 2-minute bar. Emits HTF candles when a boundary is crossed."""
        for tf, mins in self._tf_minutes.items():
            boundary = self._boundary(bar.timestamp, mins)

            if tf not in self._accum:
                # First bar ever for this TF — just start accumulating
                self._accum[tf] = {
                    "o": bar.open, "h": bar.high, "l": bar.low,
                    "c": bar.close, "vol": bar.volume,
                    "ts": bar.timestamp, "boundary": boundary,
                }
                continue

            acc = self._accum[tf]

            if boundary != acc["boundary"]:
                # We crossed into a new period → emit the completed candle
                completed = Bar(
                    timestamp=acc["ts"],  # use last bar's timestamp
                    open=acc["o"],
                    high=acc["h"],
                    low=acc["l"],
                    close=acc["c"],
                    volume=acc["vol"],
                )
                self._on_htf_candle(tf, completed)
                self._htf_candles_emitted += 1

                # Start new accumulator with current bar
                self._accum[tf] = {
                    "o": bar.open, "h": bar.high, "l": bar.low,
                    "c": bar.close, "vol": bar.volume,
                    "ts": bar.timestamp, "boundary": boundary,
                }
            else:
                # Same period — extend OHLCV
                acc["h"] = max(acc["h"], bar.high)
                acc["l"] = min(acc["l"], bar.low)
                acc["c"] = bar.close
                acc["vol"] += bar.volume
                acc["ts"] = bar.timestamp


# ═══════════════════════════════════════════════════════════════
# STUB CLIENT (satisfies orchestrator's client interface)
# ═══════════════════════════════════════════════════════════════

class TWSClientStub:
    """
    Minimal stub that satisfies the orchestrator's expectations for
    self._client without needing Client Portal Gateway.

    The orchestrator checks self._client.is_connected and
    self._client.contract in _process_bar. This stub keeps those happy.
    Also provides _last_heartbeat for TWSHealthMonitor bar-flow checks.
    """

    def __init__(self, ib=None, runner=None):
        self._connected = True
        self._ib = ib  # real ib_insync.IB instance for health checks
        self._runner = runner  # reference to TWSLiveRunner for last_price
        self._last_heartbeat: float = 0.0  # updated on each bar arrival
        self._last_price: float = 0.0  # fallback if no runner
        self._contract = type("Contract", (), {
            "conid": 0, "symbol": "MNQ", "expiry": "", "exchange": "CME",
            "description": "MNQ via TWS"
        })()

    @property
    def is_connected(self) -> bool:
        # Check real TWS connection if available
        if self._ib is not None:
            return self._ib.isConnected()
        return self._connected

    def is_connected_method(self) -> bool:
        """Callable version for health monitor (calls is_connected())."""
        return self.is_connected

    @property
    def contract(self):
        return self._contract

    def get_status(self) -> dict:
        return {"connected": self.is_connected}

    def get_current_price(self) -> dict:
        """Return last known price for paper order fills.

        The executor calls this when limit_price=0 (market orders).
        We use the runner's _last_price which is updated on every
        2-min candle and during backfill.
        """
        price = self._last_price
        if self._runner:
            price = self._runner._last_price or price
        return {"last": price}

    @property
    def account_id(self) -> str:
        """Stub for position manager reconciliation (not used in TWS mode)."""
        return ""

    async def _get(self, endpoint: str):
        """Stub for HTTP-based Client Portal calls (not used in TWS mode)."""
        return None

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        self._connected = False
        if self._ib and self._ib.isConnected():
            self._ib.disconnect()


# ═══════════════════════════════════════════════════════════════
# STUB DATA FEED (satisfies orchestrator's data_feed interface)
# ═══════════════════════════════════════════════════════════════

class TWSHealthAdapter:
    """
    Thin adapter that wraps TWSClientStub for the health monitor.
    The health monitor calls client.is_connected() as a METHOD,
    but TWSClientStub exposes it as a property. This adapter bridges that.
    """

    def __init__(self, stub: TWSClientStub):
        self._stub = stub

    def is_connected(self) -> bool:
        return self._stub.is_connected

    @property
    def _last_heartbeat(self) -> float:
        return self._stub._last_heartbeat


class TWSDataFeedStub:
    """Stub that satisfies orchestrator's IBKRDataFeed interface."""

    def __init__(self):
        self._callbacks = []
        self._running = True

    def on_bar(self, callback):
        self._callbacks.append(callback)

    async def start(self) -> bool:
        return True

    async def stop(self) -> None:
        self._running = False

    def get_status(self) -> dict:
        return {"data_mode": "tws_socket", "running": self._running}

    @property
    def is_connected(self) -> bool:
        return self._running


# ═══════════════════════════════════════════════════════════════
# TWS LIVE RUNNER
# ═══════════════════════════════════════════════════════════════

class TWSLiveRunner:
    """
    Top-level runner for IBKR TWS paper trading via socket API.

    Replaces the Client Portal Gateway approach:
      - Connects directly to TWS on port 7497 via ib_insync
      - Aggregates 5-sec bars into 2-min candles
      - Feeds candles into the orchestrator's processing pipeline
    """

    WARMUP_BARS = 30

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 1,
        dry_run: bool = False,
    ):
        self._host = host
        self._port = port
        self._client_id = client_id
        self._dry_run = dry_run

        self._ib = None  # ib_insync.IB instance
        self._shutdown_event = asyncio.Event()
        self._warmup_complete = False
        self._warmup_count = 0
        self._pipeline = None
        self._aggregator = None
        self._last_dashboard_time: float = 0.0
        self._last_state_write: float = 0.0
        self._bars_processed = 0
        self._candle_buffer: list = []  # last 200 candles for website
        self._candle_buffer_max = 200
        self._last_price: float = 0.0
        self._htf_aggregator = None  # initialized after pipeline

        # Structured loggers
        self._decision_log = JSONLineLogger(
            directory=str(LOGS_DIR), prefix="ibkr_decisions", buffer_size=10,
        )
        self._trade_log = JSONLineLogger(
            directory=str(LOGS_DIR), prefix="ibkr_trades", buffer_size=10,
        )

    async def start(self) -> None:
        """Full startup: connect TWS -> subscribe bars -> run loop."""
        self._print_banner()

        # ── Import ib_insync ──
        try:
            import nest_asyncio
            nest_asyncio.apply()
            from ib_insync import IB, Future
        except ImportError:
            raise RuntimeError(
                "ib_insync not installed. Run: pip install ib_insync --break-system-packages"
            )

        # ── Connect to TWS (with retries for slow IBC startups) ──
        self._ib = IB()
        max_connect_attempts = 5
        for attempt in range(1, max_connect_attempts + 1):
            try:
                await self._ib.connectAsync(
                    self._host, self._port, clientId=self._client_id,
                    timeout=20,
                )
                logger.info("Connected to TWS at %s:%d (clientId=%d)",
                            self._host, self._port, self._client_id)
                break
            except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
                if attempt < max_connect_attempts:
                    wait = 10 * attempt
                    logger.warning(
                        "TWS connect attempt %d/%d failed: %s — retrying in %ds...",
                        attempt, max_connect_attempts, e, wait,
                    )
                    await asyncio.sleep(wait)
                    self._ib = IB()  # fresh instance for retry
                else:
                    raise RuntimeError(
                        f"Cannot connect to TWS at {self._host}:{self._port} "
                        f"after {max_connect_attempts} attempts: {e}"
                    )

        # ── Resolve MNQ front-month contract ──
        generic = Future("MNQ", exchange="CME")
        details_list = await self._ib.reqContractDetailsAsync(generic)
        if not details_list:
            raise RuntimeError("Could not find MNQ contract details")

        details_list.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
        front = details_list[0].contract
        qualified = await self._ib.qualifyContractsAsync(front)
        if not qualified:
            raise RuntimeError("Could not qualify front-month MNQ contract")

        contract = qualified[0]
        logger.info(
            "Contract: %s expiry=%s conId=%d exchange=%s",
            contract.symbol,
            contract.lastTradeDateOrContractMonth,
            contract.conId,
            contract.exchange,
        )

        # ── Build orchestrator pipeline (with stubs for client/data_feed) ──
        from execution.orchestrator import IBKRLivePipeline, PipelineState

        self._pipeline = IBKRLivePipeline(
            bot_config=CONFIG,
            executor_config=ExecutorConfig(paper_mode=True),
        )

        # Replace the Client Portal client/data_feed with our stubs
        self._pipeline._client = TWSClientStub(ib=self._ib, runner=self)
        self._pipeline._data_feed = TWSDataFeedStub()
        self._pipeline._state = PipelineState.RUNNING

        # Reset daily counters
        self._pipeline._executor.reset_daily()
        self._pipeline._position_manager.reset_daily()

        # ── HTF candle aggregator (2-min → 5-min, 15-min → HTF engine) ──
        self._htf_aggregator = HTFCandleAggregator(
            on_htf_candle=self._on_htf_candle,
        )

        logger.info("Pipeline initialized (paper mode, TWS socket)")
        logger.info("  HTF aggregator: 2m → 5m, 15m → HTF Bias Engine")

        # ── Historical backfill — prime ALL indicators + session levels ──
        await self._backfill_historical(contract)

        # ── Set up 2-min candle aggregator ──
        self._aggregator = TwoCandleAggregator(
            on_candle=self._on_2min_candle,
        )

        # ── Subscribe to real-time 5-sec bars ──
        bars = self._ib.reqRealTimeBars(
            contract, barSize=5, whatToShow="TRADES", useRTH=False,
        )
        bars.updateEvent += self._on_realtime_bar
        logger.info("Subscribed to real-time 5-sec bars for MNQ")

        # ── Initialize AlertManager ──
        alert_mgr = AlertManager(
            CONFIG.alerting,
            rate_limit_seconds=CONFIG.alerting.rate_limit_seconds,
        )
        set_alert_manager(alert_mgr)
        await alert_mgr.start()
        self._alert_manager = alert_mgr

        # ── Start health monitor (writes heartbeat_state.json every 10s) ──
        from Broker.tws_health_monitor import TWSHealthMonitor

        health_adapter = TWSHealthAdapter(self._pipeline._client)
        self._health_monitor = TWSHealthMonitor(
            client=health_adapter,
            launcher=None,  # No IBC launcher — TWS is started manually
            heartbeat_file=LOGS_DIR / "heartbeat_state.json",
            check_interval=10.0,
            failure_threshold=3,
            on_critical_failure=self._handle_critical_failure,
        )
        self._health_monitor.start()
        logger.info("Health monitor started — heartbeat_state.json updating every 10s")

        # ── Write initial state files so website picks up immediately ──
        self._write_state_files()
        logger.info("Initial state files written to logs/")

        # ── Launch publish_stats.py as background process ──
        self._publisher_proc = None
        publisher_script = script_dir / "publish_stats.py"
        if publisher_script.exists():
            try:
                self._publisher_proc = subprocess.Popen(
                    [sys.executable, str(publisher_script), "--interval", "60"],
                    cwd=str(project_dir),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                logger.info(
                    "Stats publisher started (PID %d) — pushing to GitHub every 60s",
                    self._publisher_proc.pid,
                )
            except OSError as e:
                logger.warning("Could not start publish_stats.py: %s", e)
        else:
            logger.warning("publish_stats.py not found at %s — website won't update", publisher_script)

        logger.info("=" * 60)
        logger.info("  TWS RUNNER ACTIVE — TRADING ENABLED")
        logger.info("  Historical warmup complete — indicators primed")
        logger.info("  Health monitor:   RUNNING")
        logger.info("  Stats publisher:  %s",
                     "RUNNING" if self._publisher_proc else "NOT STARTED")
        logger.info("=" * 60)

        # ── Main loop ──
        await self._run_loop()

    async def _backfill_historical(self, contract) -> None:
        """
        Pull 2 days of 2-minute historical bars from TWS and feed them
        through the full pipeline (feature engine, sweep detector, HTF, etc.)
        so all indicators, session levels, and market structure are primed
        before live trading begins.

        This replaces the old 60-minute warmup with instant priming.
        """
        logger.info("=" * 60)
        logger.info("  HISTORICAL BACKFILL — loading 2 days of 2-min bars")
        logger.info("=" * 60)

        try:
            # Request 2 days of 2-min bars (covers prior session + current)
            # TWS returns bars in chronological order
            hist_bars = await self._ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",          # up to now
                durationStr="2 D",       # 2 trading days
                barSizeSetting="2 mins",
                whatToShow="TRADES",
                useRTH=False,            # include ETH for full context
                formatDate=1,
            )

            if not hist_bars:
                logger.warning(
                    "No historical bars returned — falling back to live warmup"
                )
                return

            logger.info("Received %d historical 2-min bars", len(hist_bars))

            # Feed each bar through the feature engine and sweep detector
            # (same as what _process_bar does, minus the trading logic)
            bars_fed = 0
            for ib_bar in hist_bars:
                # Extract timestamp
                raw_ts = getattr(ib_bar, "date", None)
                if isinstance(raw_ts, datetime):
                    ts = (raw_ts.astimezone(timezone.utc)
                          if raw_ts.tzinfo
                          else raw_ts.replace(tzinfo=timezone.utc))
                elif isinstance(raw_ts, str):
                    try:
                        ts = datetime.fromisoformat(raw_ts)
                        if ts.tzinfo is None:
                            from zoneinfo import ZoneInfo
                            ts = ts.replace(tzinfo=ZoneInfo("America/New_York"))
                        ts = ts.astimezone(timezone.utc)
                    except ValueError:
                        ts = datetime.now(timezone.utc)
                else:
                    ts = datetime.now(timezone.utc)

                # Extract open (HistoricalBar uses .open, not .open_)
                open_price = getattr(ib_bar, "open", None)
                if open_price is None:
                    open_price = getattr(ib_bar, "open_", 0.0)

                bar = Bar(
                    timestamp=ts,
                    open=float(open_price),
                    high=float(ib_bar.high),
                    low=float(ib_bar.low),
                    close=float(ib_bar.close),
                    volume=int(ib_bar.volume),
                )

                # Feed through feature engine (primes ATR, VWAP, swings, OBs)
                features = self._pipeline._feature_engine.update(bar)

                # Feed through sweep detector (primes session highs/lows)
                if features:
                    from zoneinfo import ZoneInfo
                    et_time = bar.timestamp.astimezone(ZoneInfo("America/New_York"))
                    h, m = et_time.hour, et_time.minute
                    t = h + m / 60.0
                    is_rth = 9.5 <= t < 16.0

                    htf_bias = self._pipeline._htf_bias
                    self._pipeline._sweep_detector.update_bar(
                        bar=bar,
                        vwap=features.session_vwap,
                        htf_bias=htf_bias,
                        is_rth=is_rth,
                    )

                # Feed into HTF aggregator (builds 5m/15m during backfill)
                if self._htf_aggregator:
                    self._htf_aggregator.on_bar(bar)

                # Add to candle buffer for website (last 40 bars)
                self._candle_buffer.append({
                    "t": bar.timestamp.isoformat(),
                    "o": round(bar.open, 2),
                    "h": round(bar.high, 2),
                    "l": round(bar.low, 2),
                    "c": round(bar.close, 2),
                    "vol": bar.volume,
                })

                bars_fed += 1

            # Trim candle buffer to last 200
            if len(self._candle_buffer) > self._candle_buffer_max:
                self._candle_buffer = self._candle_buffer[-self._candle_buffer_max:]

            # Set last price and bars count from backfill
            if self._candle_buffer:
                self._last_price = self._candle_buffer[-1]["c"]
            self._bars_processed = bars_fed

            # Mark warmup as complete since we've primed everything
            self._warmup_complete = True
            self._warmup_count = bars_fed

            logger.info(
                "Backfill complete: %d bars fed through pipeline", bars_fed
            )
            logger.info(
                "  Feature engine: %d bars in rolling window",
                len(self._pipeline._feature_engine._bars),
            )

            # Log HTF state after backfill
            if self._htf_aggregator:
                logger.info(
                    "  HTF aggregator: %d candles emitted during backfill",
                    self._htf_aggregator._htf_candles_emitted,
                )
            htf_bias = self._pipeline._htf_bias
            if htf_bias:
                logger.info(
                    "  HTF bias: %s (strength=%.2f) "
                    "allows_long=%s allows_short=%s",
                    htf_bias.consensus_direction,
                    htf_bias.consensus_strength,
                    htf_bias.htf_allows_long,
                    htf_bias.htf_allows_short,
                )
                logger.info("  HTF per-TF: %s", htf_bias.tf_biases)
            else:
                logger.warning(
                    "  HTF bias: STILL NONE after backfill — "
                    "check HTF_TIMEFRAMES vs aggregator output"
                )

        except Exception as e:
            logger.error(
                "Historical backfill failed: %s — falling back to live warmup", e
            )
            # If backfill fails, the old warmup logic still works as fallback

    def _handle_critical_failure(self) -> None:
        """Called by health monitor when consecutive failures exceed threshold."""
        logger.critical(
            "HEALTH MONITOR: Critical failure detected — TWS connection may be down"
        )
        # Log but don't auto-shutdown — let the user decide
        # Future: integrate with IBC auto-restart

    def _on_realtime_bar(self, bars, has_new_bar) -> None:
        """Handle each 5-second bar from TWS."""
        if not has_new_bar or not bars:
            return

        # Update heartbeat timestamp so health monitor knows bars are flowing
        self._pipeline._client._last_heartbeat = time.monotonic()

        ib_bar = bars[-1]

        # Extract timestamp
        raw_ts = getattr(ib_bar, "time", None) or getattr(ib_bar, "date", None)
        if isinstance(raw_ts, datetime):
            ts = (raw_ts.astimezone(timezone.utc)
                  if raw_ts.tzinfo else raw_ts.replace(tzinfo=timezone.utc))
        elif hasattr(raw_ts, "timestamp"):
            ts = datetime.fromtimestamp(raw_ts.timestamp(), tz=timezone.utc)
        else:
            ts = datetime.now(timezone.utc)

        # Extract open price (RealTimeBar uses .open_)
        open_price = getattr(ib_bar, "open_", None) or getattr(ib_bar, "open", None)

        bar = Bar(
            timestamp=ts,
            open=open_price,
            high=ib_bar.high,
            low=ib_bar.low,
            close=ib_bar.close,
            volume=int(ib_bar.volume),
        )

        # Feed into 2-min aggregator
        self._aggregator.on_bar(bar)

    def _on_2min_candle(self, candle: Bar) -> None:
        """Process a completed 2-minute candle."""
        # Warmup phase — prime indicators only
        if not self._warmup_complete:
            self._warmup_count += 1
            self._pipeline._feature_engine.update(candle)

            if self._warmup_count % 5 == 0:
                logger.info("WARMUP: %d/%d candles", self._warmup_count, self.WARMUP_BARS)

            if self._warmup_count >= self.WARMUP_BARS:
                self._warmup_complete = True
                logger.info("=" * 60)
                logger.info("  WARMUP COMPLETE — TRADING ACTIVE")
                logger.info("  Indicators primed with %d candles", self.WARMUP_BARS)
                logger.info("=" * 60)
            return

        # Dry run — log only
        if self._dry_run:
            logger.info(
                "DRY RUN bar: close=%.2f vol=%d time=%s",
                candle.close, candle.volume, candle.timestamp.isoformat(),
            )
            return

        # Forward to pipeline for processing
        self._bars_processed += 1
        self._last_price = candle.close

        # Track candle in buffer for website
        self._candle_buffer.append({
            "t": candle.timestamp.isoformat(),
            "o": round(candle.open, 2),
            "h": round(candle.high, 2),
            "l": round(candle.low, 2),
            "c": round(candle.close, 2),
            "vol": candle.volume,
        })
        if len(self._candle_buffer) > self._candle_buffer_max:
            self._candle_buffer = self._candle_buffer[-self._candle_buffer_max:]

        # Feed into HTF aggregator (builds 5m/15m candles → HTF bias)
        if self._htf_aggregator:
            self._htf_aggregator.on_bar(candle)

        loop = asyncio.get_event_loop()
        if loop.is_running():
            task = loop.create_task(self._safe_process_bar(candle))
            task.add_done_callback(self._task_exception_handler)

    async def _safe_process_bar(self, candle: Bar) -> None:
        """Wrapper around _process_bar_guarded with exception logging."""
        try:
            await self._pipeline._process_bar_guarded(candle)
        except Exception:
            logger.error(
                "CRITICAL: _process_bar failed on candle %s",
                candle.timestamp.isoformat(),
                exc_info=True,
            )

    @staticmethod
    def _task_exception_handler(task: asyncio.Task) -> None:
        """Ensure any unhandled task exceptions get logged."""
        if not task.cancelled() and task.exception():
            logger.error("Unhandled task exception: %s", task.exception())

    def _on_htf_candle(self, timeframe: str, bar: Bar) -> None:
        """A higher-timeframe candle (5m or 15m) has completed."""
        self._pipeline.process_htf_bar(timeframe, bar)
        bias = self._pipeline._htf_bias
        if bias:
            logger.info(
                "HTF %s candle → bias=%s strength=%.2f "
                "(allows_long=%s allows_short=%s)",
                timeframe,
                bias.consensus_direction,
                bias.consensus_strength,
                bias.htf_allows_long,
                bias.htf_allows_short,
            )

    def _write_state_files(self) -> None:
        """
        Write paper_trading_state.json and candle_buffer.json for publish_stats.py.
        These are the two files that determine 'LIVE' vs 'OFFLINE' on the website.
        """
        import json as _json

        pm = self._pipeline._position_manager
        pm_status = pm.get_status()
        exec_status = self._pipeline._executor.get_status()

        state = {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "trade_count": pm_status.get("trade_count", 0),
            "active_positions": len(pm.open_positions) if pm.open_positions else 0,
            "bars_processed": self._bars_processed,
            "total_pnl": pm_status.get("daily_realized_pnl", 0.0),
            "win_rate": pm_status.get("win_rate", 0.0),
            "profit_factor": pm_status.get("profit_factor", 0.0),
            "is_halted": exec_status.get("is_halted", False),
            "last_price": self._last_price,
        }

        # Active trade for live chart
        active_trade = None
        positions = pm.open_positions
        if positions:
            pos_list = list(positions.values())
            p = pos_list[0]
            # Get stop/target from orchestrator (where they actually live)
            orch = self._pipeline
            stop_price = orch._initial_stop if orch._initial_stop else None
            target_price = orch._c2_target_price if orch._c2_target_price else None
            side_str = p.side.value if hasattr(p.side, 'value') else str(p.side)
            direction_mult = 1 if 'LONG' in side_str.upper() else -1
            total_contracts = sum(pp.contracts for pp in pos_list)
            unrealized = 0.0
            if hasattr(p, 'entry_price') and self._last_price:
                unrealized = round(
                    (self._last_price - p.entry_price) * direction_mult
                    * 5 * total_contracts, 2
                )
            active_trade = {
                "side": side_str,
                "contracts": total_contracts,
                "entry_price": round(p.entry_price, 2) if hasattr(p, 'entry_price') else 0.0,
                "stop_price": round(stop_price, 2) if stop_price else None,
                "target_price": round(target_price, 2) if target_price else None,
                "unrealized_pnl": unrealized,
                "entry_time": p.entry_time.isoformat() if hasattr(p, 'entry_time') and p.entry_time else "",
            }

        try:
            # active_trade.json (atomic write)
            trade_path = LOGS_DIR / "active_trade.json"
            tmp_t = trade_path.with_suffix(".json.tmp")
            with open(tmp_t, "w", encoding="utf-8") as f:
                _json.dump(active_trade or {}, f, indent=2, default=str)
            os.replace(str(tmp_t), str(trade_path))

            # paper_trading_state.json (atomic write)
            state_path = LOGS_DIR / "paper_trading_state.json"
            tmp = state_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(state, f, indent=2, default=str)
            os.replace(str(tmp), str(state_path))

            # candle_buffer.json (atomic write)
            buf_path = LOGS_DIR / "candle_buffer.json"
            tmp2 = buf_path.with_suffix(".json.tmp")
            with open(tmp2, "w", encoding="utf-8") as f:
                _json.dump(self._candle_buffer, f, default=str)
            os.replace(str(tmp2), str(buf_path))

        except OSError as e:
            logger.warning("Failed to write state files: %s", e)

    async def _run_loop(self) -> None:
        """Main loop — ib_insync processes events via the shared asyncio loop."""
        try:
            while not self._shutdown_event.is_set():
                await asyncio.sleep(0.2)  # ib_insync events auto-dispatch on this loop

                now = time.monotonic()

                # Write state files every 30 seconds for the website
                if now - self._last_state_write >= 30:
                    if self._warmup_complete:
                        self._write_state_files()
                    self._last_state_write = now

                # Dashboard every 2 minutes
                if now - self._last_dashboard_time >= 120:
                    if self._warmup_complete:
                        self._print_dashboard()
                    self._last_dashboard_time = now

        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    def _print_banner(self) -> None:
        logger.info("=" * 60)
        logger.info("  IBKR TWS DIRECT RUNNER (Socket API)")
        logger.info("  TWS:          %s:%d", self._host, self._port)
        logger.info("  Client ID:    %d", self._client_id)
        logger.info("  Mode:         PAPER (simulated fills)")
        logger.info("  Dry run:      %s", "ON" if self._dry_run else "OFF")
        logger.info("=" * 60)

    def _print_dashboard(self) -> None:
        """Print status to terminal."""
        if not self._pipeline:
            return

        pm = self._pipeline._position_manager
        pm_status = pm.get_status()
        positions = pm.open_positions
        exec_status = self._pipeline._executor.get_status()

        if positions:
            pos_list = list(positions.values())
            side = pos_list[0].side.value
            total_contracts = sum(p.contracts for p in pos_list)
            pos_line = f"{side} {total_contracts}x"
        else:
            pos_line = "FLAT"

        realized = pm_status["daily_realized_pnl"]
        trade_count = pm_status["trade_count"]
        halted = exec_status["is_halted"]

        logger.info(
            "DASHBOARD | Position: %s | Realized: $%.2f | Trades: %d | "
            "Bars: %d | Halted: %s",
            pos_line, realized, trade_count, self._bars_processed,
            "YES" if halted else "No",
        )

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("=" * 60)
        logger.info("  SHUTDOWN INITIATED")
        logger.info("=" * 60)

        # Stop health monitor
        if hasattr(self, '_health_monitor') and self._health_monitor:
            self._health_monitor.stop()
            logger.info("Health monitor stopped")

        # Stop stats publisher
        if hasattr(self, '_publisher_proc') and self._publisher_proc:
            self._publisher_proc.terminate()
            try:
                self._publisher_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._publisher_proc.kill()
            logger.info("Stats publisher stopped")

        if self._ib and self._ib.isConnected():
            self._ib.disconnect()
            logger.info("Disconnected from TWS")

        if hasattr(self, '_alert_manager') and self._alert_manager:
            await self._alert_manager.stop()

        # Flush logs
        self._decision_log.flush()
        self._trade_log.flush()

        logger.info("Shutdown complete")

    def request_shutdown(self) -> None:
        self._shutdown_event.set()


# ═══════════════════════════════════════════════════════════════
# AUTO-LAUNCH + CRASH-RECOVERY SUPERVISOR
# ═══════════════════════════════════════════════════════════════

class TWSSupervisor:
    """
    Wraps TWSLiveRunner with:
      1. Auto-launch TWS via IBC (if not already running)
      2. Crash detection — monitors runner health
      3. Auto-restart — kills TWS, relaunches via IBC, reconnects bot

    This lets the bot run completely unattended:
      TWS crash/disconnect → supervisor detects → IBC relaunches TWS
      → bot reconnects → backfill → resume trading

    Usage:
        python scripts/run_tws.py --auto
    """

    MAX_RESTART_ATTEMPTS = 10
    RESTART_COOLDOWN = 60        # seconds between restarts
    HEALTH_POLL_INTERVAL = 30    # seconds between health checks

    def __init__(self, host, port, client_id, dry_run=False):
        self._host = host
        self._port = port
        self._client_id = client_id
        self._dry_run = dry_run
        self._restart_count = 0
        self._user_shutdown = False

        # TWSLauncher handles TWS process + IBC login
        from config.tws_auto_config import TWSAutoConfig
        self._auto_config = TWSAutoConfig()
        self._launcher = None  # created on first use

    def run(self) -> None:
        """
        Main supervisor loop. Keeps restarting the bot until:
          - User presses Ctrl+C
          - Max restart attempts exhausted
        """
        import nest_asyncio
        nest_asyncio.apply()

        self._print_supervisor_banner()

        while not self._user_shutdown:
            if self._restart_count >= self.MAX_RESTART_ATTEMPTS:
                logger.critical(
                    "Max restart attempts (%d) reached — giving up. "
                    "Manual intervention required.",
                    self.MAX_RESTART_ATTEMPTS,
                )
                break

            # Step 1: Ensure TWS is running (launch via IBC if needed)
            if not self._ensure_tws_running():
                logger.error(
                    "Cannot start TWS — retrying in %ds...",
                    self.RESTART_COOLDOWN,
                )
                self._restart_count += 1
                time.sleep(self.RESTART_COOLDOWN)
                continue

            # Step 2: Run the bot
            runner = TWSLiveRunner(
                host=self._host,
                port=self._port,
                client_id=self._client_id,
                dry_run=self._dry_run,
            )

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            exit_reason = "unknown"
            try:
                logger.info(
                    "=" * 60 + "\n"
                    "  SUPERVISOR: Starting bot (attempt %d)\n" +
                    "=" * 60,
                    self._restart_count + 1,
                )
                loop.run_until_complete(runner.start())
                exit_reason = "clean_exit"
            except KeyboardInterrupt:
                exit_reason = "user_shutdown"
                self._user_shutdown = True
                try:
                    loop.run_until_complete(
                        asyncio.wait_for(runner.shutdown(), timeout=30)
                    )
                except (asyncio.TimeoutError, Exception):
                    logger.warning("Shutdown timed out during Ctrl+C")
            except Exception as e:
                exit_reason = f"crash: {e}"
                logger.error(
                    "Bot crashed: %s\n%s", e, traceback.format_exc()
                )
                try:
                    loop.run_until_complete(
                        asyncio.wait_for(runner.shutdown(), timeout=15)
                    )
                except (asyncio.TimeoutError, Exception):
                    pass
            finally:
                loop.close()

            if self._user_shutdown:
                logger.info("SUPERVISOR: User requested shutdown — exiting")
                break

            # Step 3: Bot exited — decide whether to restart
            self._restart_count += 1
            logger.warning(
                "SUPERVISOR: Bot exited (%s). Restart %d/%d in %ds...",
                exit_reason,
                self._restart_count,
                self.MAX_RESTART_ATTEMPTS,
                self.RESTART_COOLDOWN,
            )

            # Kill TWS so IBC can do a clean relaunch
            self._kill_tws()

            # Cooldown before restart
            for remaining in range(self.RESTART_COOLDOWN, 0, -10):
                if self._user_shutdown:
                    break
                logger.info("  Restarting in %ds...", remaining)
                time.sleep(min(10, remaining))

        logger.info("SUPERVISOR: Exiting")

    def _ensure_tws_running(self) -> bool:
        """Check if TWS port is open; if not, launch via IBC."""
        import socket as _socket

        # Quick port check
        try:
            with _socket.create_connection(
                (self._host, self._port), timeout=3.0
            ):
                logger.info("SUPERVISOR: TWS already running on port %d", self._port)
                return True
        except (ConnectionRefusedError, OSError, _socket.timeout):
            pass

        # Not running — try to launch via IBC
        logger.info("SUPERVISOR: TWS not running — launching via IBC...")

        if not self._auto_config.ibc_available:
            logger.warning(
                "SUPERVISOR: IBC not found at %s. "
                "Please install IBC and set IBKR_IBC_PATH in .env, "
                "OR start TWS manually before running the bot.",
                self._auto_config.ibc_path or "(not set)",
            )
            # Fall back: check if TWS is available for direct launch
            if not self._auto_config.tws_available:
                logger.error("SUPERVISOR: Neither IBC nor TWS found")
                return False
            logger.info(
                "SUPERVISOR: Launching TWS directly — you must log in manually"
            )

        from Broker.tws_launcher import TWSLauncher
        self._launcher = TWSLauncher(self._auto_config)

        if not self._launcher.launch():
            logger.error("SUPERVISOR: Failed to launch TWS")
            return False

        if not self._launcher.wait_for_ready(timeout=180):
            logger.error("SUPERVISOR: TWS did not become ready in 180s")
            self._launcher.kill()
            return False

        logger.info("SUPERVISOR: TWS is ready")
        return True

    def _kill_tws(self) -> None:
        """Kill TWS process for a clean restart."""
        if self._launcher:
            logger.info("SUPERVISOR: Killing TWS for clean restart...")
            self._launcher.kill()
            self._launcher.cleanup()
            self._launcher = None
            time.sleep(5)  # give Windows time to release the port

    def _print_supervisor_banner(self) -> None:
        ibc_status = "AVAILABLE" if self._auto_config.ibc_available else "NOT FOUND"
        tws_status = "AVAILABLE" if self._auto_config.tws_available else "NOT FOUND"
        print("\n" + "=" * 60)
        print("  IBKR TWS AUTO-PILOT SUPERVISOR")
        print(f"  TWS:     {tws_status}")
        print(f"  IBC:     {ibc_status}")
        print(f"  Port:    {self._port}")
        print(f"  Restart: up to {self.MAX_RESTART_ATTEMPTS} attempts")
        print(f"  Mode:    {'DRY-RUN' if self._dry_run else 'PAPER TRADING'}")
        print("=" * 60)
        print("  Press Ctrl+C to stop\n")


# ═══════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="IBKR TWS Direct Runner — MNQ via socket API"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Connect and process bars but don't execute trades",
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="Enable auto-pilot: launch TWS via IBC, auto-restart on crash",
    )
    parser.add_argument(
        "--host", type=str, default=None,
        help="TWS host (default: from .env or 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="TWS port (default: from .env or 7497)",
    )
    parser.add_argument(
        "--client-id", type=int, default=None,
        help="TWS client ID (default: from .env or 1)",
    )
    args = parser.parse_args()

    host = args.host or os.environ.get("IBKR_TWS_HOST", "127.0.0.1")
    port = args.port or int(os.environ.get("IBKR_TWS_PORT", "7497"))
    client_id = args.client_id or int(os.environ.get("IBKR_CLIENT_ID", "1"))

    # Create log directory
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Configure logging
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    root_logger.addHandler(console_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        str(LOGS_DIR / "tws_trading.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root_logger.addHandler(file_handler)

    # ── AUTO-PILOT MODE ──
    if args.auto:
        supervisor = TWSSupervisor(
            host=host, port=port,
            client_id=client_id, dry_run=args.dry_run,
        )
        supervisor.run()
        return

    # ── MANUAL MODE (original behavior) ──
    import nest_asyncio
    nest_asyncio.apply()

    runner = TWSLiveRunner(
        host=host,
        port=port,
        client_id=client_id,
        dry_run=args.dry_run,
    )

    loop = asyncio.get_event_loop()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, runner.request_shutdown)
    else:
        logger.info("Windows detected — using KeyboardInterrupt for Ctrl+C shutdown")

    SHUTDOWN_TIMEOUT = 30

    try:
        loop.run_until_complete(runner.start())
    except KeyboardInterrupt:
        try:
            loop.run_until_complete(
                asyncio.wait_for(runner.shutdown(), timeout=SHUTDOWN_TIMEOUT)
            )
        except asyncio.TimeoutError:
            logger.critical("Shutdown timed out — MANUAL POSITION CHECK REQUIRED")
    except Exception:
        logger.critical("UNHANDLED EXCEPTION\n%s", traceback.format_exc())
        try:
            loop.run_until_complete(
                asyncio.wait_for(runner.shutdown(), timeout=SHUTDOWN_TIMEOUT)
            )
        except asyncio.TimeoutError:
            logger.critical("Shutdown timed out — MANUAL POSITION CHECK REQUIRED")
        sys.exit(1)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
