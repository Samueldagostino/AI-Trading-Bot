"""
IBKR Client Portal API Data Connector
======================================
Connects to Interactive Brokers Client Portal Gateway for MNQ futures data.

Architecture:
- HTTP REST: Authentication, contract search, market data snapshots, historical bars
- Session keepalive via /tickle endpoint every 60 seconds
- Polls market data snapshots at configurable interval (default 2s)

The Client Portal Gateway must be running locally (or on a reachable host).
Download from: https://www.interactivebrokers.com/en/trading/ib-api.php

Env vars:
- IBKR_GATEWAY_HOST  (default: localhost)
- IBKR_GATEWAY_PORT  (default: 5000)
- IBKR_ACCOUNT_TYPE  (paper | live)
- IBKR_SYMBOL        (default: MNQ)

SECURITY: This module NEVER logs tokens, credentials, or session cookies.
"""

import asyncio
import json
import logging
import math
import os
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional, Dict, List, Callable, Any

from features.engine import Bar

logger = logging.getLogger(__name__)

try:
    import aiohttp
except ImportError:
    aiohttp = None
    logger.warning("aiohttp not installed. Install with: pip install aiohttp")


# ================================================================
# CONFIGURATION
# ================================================================

@dataclass
class IBKRConfig:
    """Configuration for IBKR Client Portal Gateway connection."""
    gateway_host: str = os.getenv("IBKR_GATEWAY_HOST", "localhost")
    gateway_port: int = int(os.getenv("IBKR_GATEWAY_PORT", "5000"))
    account_type: str = os.getenv("IBKR_ACCOUNT_TYPE", "paper")  # paper | live
    symbol: str = os.getenv("IBKR_SYMBOL", "MNQ")
    poll_interval_seconds: float = 2.0
    keepalive_interval_seconds: float = 60.0
    reconnect_delay_seconds: float = 5.0
    max_reconnect_attempts: int = 10

    @property
    def base_url(self) -> str:
        return f"https://{self.gateway_host}:{self.gateway_port}/v1/api"

    @property
    def is_live(self) -> bool:
        return self.account_type == "live"


# ================================================================
# MARKET DATA SNAPSHOT FIELDS
# ================================================================
# IBKR Client Portal field IDs for /iserver/marketdata/snapshot
SNAPSHOT_FIELDS = {
    31: "last_price",
    84: "bid",
    85: "ask",
    86: "high",
    88: "low",
}


# ================================================================
# DATA CLASSES
# ================================================================

@dataclass
class MarketSnapshot:
    """Parsed market data snapshot."""
    conid: int = 0
    last_price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    high: float = 0.0
    low: float = 0.0
    timestamp: Optional[datetime] = None


@dataclass
class ContractInfo:
    """Resolved IBKR contract details."""
    conid: int = 0
    symbol: str = ""
    exchange: str = ""
    expiry: str = ""       # YYYYMMDD
    description: str = ""


# ================================================================
# SESSION TYPE
# ================================================================

# US Eastern timezone offset (standard: UTC-5, daylight: UTC-4).
# Using fixed UTC-5 matches main.py process_bar() logic.
ET_OFFSET = timezone(timedelta(hours=-5))


class SessionType(Enum):
    RTH = "RTH"    # Regular Trading Hours: 9:30–16:00 ET
    ETH = "ETH"    # Extended Trading Hours: 18:00–9:29 ET


def get_session_type(ts: datetime) -> SessionType:
    """
    Determine RTH vs ETH for a given timestamp.
    RTH = 9:30–16:00 ET (same logic as main.py process_bar).
    Everything else is ETH.
    """
    et_time = ts.astimezone(ET_OFFSET)
    t = et_time.hour + et_time.minute / 60.0
    if 9.5 <= t < 16.0:
        return SessionType.RTH
    return SessionType.ETH


# ================================================================
# CANDLE AGGREGATOR
# ================================================================

# Data quality alert threshold: if this many consecutive 2-minute
# windows are missed, an alert is logged.
CONSECUTIVE_GAP_ALERT_THRESHOLD = 3

# Candle interval in seconds (2 minutes).
CANDLE_INTERVAL_SECONDS = 120


class CandleAggregator:
    """
    Aggregates raw tick/snapshot data into 2-minute OHLCV candles.

    Output candles are dicts matching the features.engine.Bar constructor:
        {
            "timestamp": datetime (UTC, bar open time),
            "open":   float (first tick price, 2dp),
            "high":   float (max tick price, 2dp),
            "low":    float (min tick price, 2dp),
            "close":  float (last tick price, 2dp),
            "volume": int   (tick count in window),
            "tick_count": int,
            "session_type": SessionType.RTH | SessionType.ETH,
        }

    Data quality checks:
    - Rejects candles with zero volume (no ticks received)
    - Rejects candles where high < low (should be impossible, safety net)
    - Logs warnings for gaps > 2 minutes between candles
    - Triggers alert if 3+ consecutive candle windows are missed
    """

    def __init__(self, on_candle: Optional[Callable] = None):
        self._on_candle = on_candle

        # Current candle being built
        self._current_open: float = 0.0
        self._current_high: float = -math.inf
        self._current_low: float = math.inf
        self._current_close: float = 0.0
        self._current_volume: int = 0
        self._current_tick_count: int = 0
        self._current_window_start: Optional[datetime] = None

        # Tracking
        self._last_candle_time: Optional[datetime] = None
        self._consecutive_gaps: int = 0
        self._candles_emitted: int = 0
        self._candles_rejected: int = 0
        self._ticks_processed: int = 0

    def on_candle(self, callback: Callable) -> None:
        """Register callback for completed candles."""
        self._on_candle = callback

    def process_tick(self, price: float, volume: int, timestamp: datetime) -> Optional[dict]:
        """
        Feed a single tick/snapshot into the aggregator.

        Args:
            price: Last trade price (already rounded to 2dp by caller).
            volume: Volume for this tick (1 for a single snapshot).
            timestamp: UTC timestamp of the tick.

        Returns:
            Completed candle dict if a 2-minute boundary was crossed, else None.
        """
        if not math.isfinite(price) or price <= 0:
            return None

        self._ticks_processed += 1
        window_start = self._get_window_start(timestamp)

        # First tick ever — start the first window
        if self._current_window_start is None:
            self._start_new_window(price, volume, window_start)
            return None

        # Same window — update running OHLCV
        if window_start == self._current_window_start:
            self._update_current(price, volume)
            return None

        # New window — close the current candle and start a new one
        completed = self._close_current_candle()
        self._start_new_window(price, volume, window_start)
        return completed

    def flush(self) -> Optional[dict]:
        """
        Force-emit the current partial candle (e.g. at session close).
        Returns the candle if valid, None otherwise.
        """
        if self._current_window_start is None or self._current_volume == 0:
            return None
        return self._close_current_candle()

    def reset(self) -> None:
        """Reset aggregator state (e.g. on reconnect)."""
        self._current_open = 0.0
        self._current_high = -math.inf
        self._current_low = math.inf
        self._current_close = 0.0
        self._current_volume = 0
        self._current_tick_count = 0
        self._current_window_start = None
        self._consecutive_gaps = 0

    def get_stats(self) -> dict:
        return {
            "candles_emitted": self._candles_emitted,
            "candles_rejected": self._candles_rejected,
            "ticks_processed": self._ticks_processed,
            "consecutive_gaps": self._consecutive_gaps,
        }

    # ----------------------------------------------------------------
    # INTERNAL
    # ----------------------------------------------------------------

    @staticmethod
    def _get_window_start(ts: datetime) -> datetime:
        """
        Compute the 2-minute window start for a given timestamp.
        E.g. 10:03:47 → 10:02:00, 10:04:01 → 10:04:00.
        """
        floored_minute = ts.minute - (ts.minute % 2)
        return ts.replace(minute=floored_minute, second=0, microsecond=0)

    def _start_new_window(self, price: float, volume: int, window_start: datetime) -> None:
        self._current_window_start = window_start
        self._current_open = price
        self._current_high = price
        self._current_low = price
        self._current_close = price
        self._current_volume = volume
        self._current_tick_count = 1

    def _update_current(self, price: float, volume: int) -> None:
        if price > self._current_high:
            self._current_high = price
        if price < self._current_low:
            self._current_low = price
        self._current_close = price
        self._current_volume += volume
        self._current_tick_count += 1

    def _close_current_candle(self) -> Optional[dict]:
        """Build candle dict, run quality checks, emit if valid."""
        candle = {
            "timestamp": self._current_window_start,
            "open": round(self._current_open, 2),
            "high": round(self._current_high, 2),
            "low": round(self._current_low, 2),
            "close": round(self._current_close, 2),
            "volume": self._current_volume,
            "tick_count": self._current_tick_count,
            "session_type": get_session_type(self._current_window_start),
        }

        # --- Data quality checks ---
        rejection_reason = self._validate_candle(candle)
        if rejection_reason:
            logger.warning("Candle REJECTED (%s): %s %s",
                           rejection_reason,
                           candle["timestamp"].isoformat(),
                           candle)
            self._candles_rejected += 1
            return None

        # --- Gap detection ---
        self._check_gap(candle["timestamp"])

        self._last_candle_time = candle["timestamp"]
        self._candles_emitted += 1

        if self._on_candle:
            self._on_candle(candle)

        return candle

    @staticmethod
    def _validate_candle(candle: dict) -> Optional[str]:
        """
        Return rejection reason string if candle fails quality checks,
        or None if candle is valid.
        """
        if candle["volume"] <= 0:
            return "zero_volume"
        if candle["high"] < candle["low"]:
            return "high_lt_low"
        # NaN/Inf in any OHLC field would corrupt the entire signal pipeline
        for field in ("open", "high", "low", "close"):
            if field in candle and not math.isfinite(candle[field]):
                return f"nan_inf_{field}"
        return None

    def _check_gap(self, current_time: datetime) -> None:
        """Log warnings for gaps between candles."""
        if self._last_candle_time is None:
            self._consecutive_gaps = 0
            return

        expected_next = self._last_candle_time + timedelta(seconds=CANDLE_INTERVAL_SECONDS)
        gap = (current_time - expected_next).total_seconds()

        if gap >= CANDLE_INTERVAL_SECONDS:
            missed = int(gap / CANDLE_INTERVAL_SECONDS)
            self._consecutive_gaps += missed
            logger.warning(
                "Candle gap: %d missed windows (%s → %s, %.0fs gap)",
                missed,
                self._last_candle_time.isoformat(),
                current_time.isoformat(),
                gap + CANDLE_INTERVAL_SECONDS,
            )
            if self._consecutive_gaps >= CONSECUTIVE_GAP_ALERT_THRESHOLD:
                logger.error(
                    "ALERT: %d consecutive candle windows missed — "
                    "possible data feed issue",
                    self._consecutive_gaps,
                )
        else:
            self._consecutive_gaps = 0


# ================================================================
# WEBSOCKET STREAMING
# ================================================================

# Number of consecutive WebSocket failures before switching to HTTP fallback.
WS_FALLBACK_THRESHOLD = 3

# Seconds between WebSocket reconnection attempts.
WS_RECONNECT_DELAY = 5.0


class IBKRWebSocket:
    """
    WebSocket client for IBKR Client Portal Gateway streaming data.

    Connects to wss://{host}:{port}/v1/api/ws and subscribes to
    market data fields for a given conid. Parses incoming JSON
    messages and forwards ticks to a callback.

    Falls back gracefully — the caller (IBKRDataFeed) handles
    switching to HTTP polling when this fails.
    """

    def __init__(self, config: 'IBKRConfig', conid: int):
        self.config = config
        self._conid = conid
        self._ws: Optional[Any] = None
        self._session: Optional[Any] = None
        self._connected: bool = False
        self._on_tick: Optional[Callable] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._consecutive_failures: int = 0

    @property
    def ws_url(self) -> str:
        return f"wss://{self.config.gateway_host}:{self.config.gateway_port}/v1/api/ws"

    @property
    def is_connected(self) -> bool:
        return self._connected

    def on_tick(self, callback: Callable) -> None:
        """Register callback: fn(price: float, volume: int, timestamp: datetime)."""
        self._on_tick = callback

    async def connect(self) -> bool:
        """Open WebSocket connection and subscribe to market data."""
        if aiohttp is None:
            logger.error("aiohttp required for WebSocket: pip install aiohttp")
            self._consecutive_failures += 1
            return False

        try:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            self._session = aiohttp.ClientSession(connector=connector)
            self._ws = await self._session.ws_connect(self.ws_url, ssl=ssl_ctx)

            # Subscribe to market data for this conid
            sub_msg = f'smd+{self._conid}+{{"fields":["31","84","85","86","88"]}}'
            await self._ws.send_str(sub_msg)

            self._connected = True
            self._consecutive_failures = 0
            logger.info("WebSocket connected to %s, subscribed conid %d",
                        self.ws_url, self._conid)

            # Start background receive loop
            self._receive_task = asyncio.create_task(self._receive_loop())
            return True

        except Exception as e:
            logger.warning("WebSocket connect failed: %s", e)
            self._consecutive_failures += 1
            await self._cleanup()
            return False

    async def disconnect(self) -> None:
        """Unsubscribe and close WebSocket."""
        self._connected = False

        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass

        if self._ws and not self._ws.closed:
            try:
                unsub_msg = f'umd+{self._conid}+{{}}'
                await self._ws.send_str(unsub_msg)
            except Exception:
                pass

        await self._cleanup()
        logger.info("WebSocket disconnected")

    async def _cleanup(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._connected = False

    async def _receive_loop(self) -> None:
        """Read messages from WebSocket and dispatch ticks."""
        while self._connected and self._ws and not self._ws.closed:
            try:
                msg = await self._ws.receive(timeout=30.0)

                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    logger.warning("WebSocket closed by server")
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("WebSocket error: %s", self._ws.exception())
                    break

            except asyncio.TimeoutError:
                # No data in 30s — still connected, just quiet market
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("WebSocket receive error: %s", e)
                self._consecutive_failures += 1
                break

        self._connected = False

    def _handle_message(self, raw: str) -> None:
        """Parse a WebSocket JSON message and fire tick callback."""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        # IBKR WS sends various message types. Market data updates
        # contain the conid as a key or a "conid" field.
        conid_key = str(self._conid)

        # Format 1: list of updates
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and str(item.get("conid", "")) == conid_key:
                    snap = item
                    break
            else:
                return
        # Format 2: {"conid_str": {...fields...}}
        elif isinstance(data, dict) and conid_key in data:
            snap = data[conid_key]
        # Format 3: top-level dict with "conid" field
        elif isinstance(data, dict) and data.get("conid") == self._conid:
            snap = data
        else:
            return

        # Extract last price (field 31)
        last_raw = snap.get("31") or snap.get("last_price")
        if last_raw is None:
            return

        price = IBKRClient._parse_price(last_raw)
        if price <= 0:
            return

        ts = datetime.now(timezone.utc)

        if self._on_tick:
            self._on_tick(price, 1, ts)


# ================================================================
# IBKR DATA FEED — HIGH-LEVEL ORCHESTRATOR
# ================================================================

# Historical backfill: 2 hours of 2-minute bars = 60 bars.
BACKFILL_PERIOD = "2h"
BACKFILL_BAR_SIZE = "2min"


class IBKRDataFeed:
    """
    High-level data feed that ties together:
    - IBKRClient (REST API for auth, contract, historical data)
    - IBKRWebSocket (primary streaming data)
    - CandleAggregator (builds 2m candles from ticks)
    - HTTP snapshot polling (fallback when WebSocket is down)

    Lifecycle:
        feed = IBKRDataFeed(client)
        feed.on_bar(my_callback)
        await feed.start()     # backfill + stream
        ...
        await feed.stop()

    The on_bar callback receives features.engine.Bar instances with
    session_type set to "RTH" or "ETH".
    """

    def __init__(self, client: 'IBKRClient'):
        self._client = client
        self._aggregator = CandleAggregator()
        self._ws: Optional[IBKRWebSocket] = None

        # Callbacks
        self._on_bar: Optional[Callable] = None

        # State
        self._running: bool = False
        self._data_mode: str = "none"  # "websocket" | "polling" | "none"
        self._monitor_task: Optional[asyncio.Task] = None
        self._backfill_bars: List[dict] = []

        # Wire aggregator candle output to our bar dispatch
        self._aggregator.on_candle(self._dispatch_bar)

    def on_bar(self, callback: Callable) -> None:
        """Register callback for completed 2-minute bars."""
        self._on_bar = callback

    def get_current_price(self) -> Optional[Dict[str, float]]:
        """Return latest bid/ask/last from the underlying client."""
        return self._client.get_current_price()

    def is_connected(self) -> bool:
        """True if feed is running and receiving data."""
        if not self._running:
            return False
        if self._data_mode == "websocket":
            return self._ws is not None and self._ws.is_connected
        if self._data_mode == "polling":
            return self._client.is_connected
        return False

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "data_mode": self._data_mode,
            "ws_connected": self._ws.is_connected if self._ws else False,
            "backfill_bars": len(self._backfill_bars),
            "aggregator": self._aggregator.get_stats(),
            "client": self._client.get_status(),
        }

    # ================================================================
    # LIFECYCLE
    # ================================================================

    async def start(self) -> bool:
        """
        Start the data feed:
        1. Verify client is connected
        2. Run historical backfill (2h of 2m bars)
        3. Try WebSocket streaming (primary)
        4. Fall back to HTTP polling if WS fails
        5. Start health monitor
        """
        if not self._client.is_connected:
            logger.error("IBKRDataFeed.start(): client not connected")
            return False

        if not self._client.contract:
            logger.error("IBKRDataFeed.start(): no contract resolved")
            return False

        self._running = True

        # Step 1: Historical backfill
        await self._run_backfill()

        # Step 2: Try WebSocket
        ws_ok = await self._start_websocket()
        if ws_ok:
            self._data_mode = "websocket"
            logger.info("Data feed started — mode: WebSocket")
        else:
            # Step 3: Fallback to HTTP polling
            await self._start_polling()
            self._data_mode = "polling"
            logger.info("Data feed started — mode: HTTP polling (WebSocket unavailable)")

        # Step 4: Health monitor
        self._monitor_task = asyncio.create_task(self._health_monitor())

        return True

    async def stop(self) -> None:
        """Stop all data feed activity."""
        self._running = False

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            await self._ws.disconnect()
            self._ws = None

        await self._client.stop_polling()

        # Flush any partial candle
        self._aggregator.flush()

        self._data_mode = "none"
        logger.info("Data feed stopped")

    # ================================================================
    # HISTORICAL BACKFILL
    # ================================================================

    async def _run_backfill(self) -> None:
        """
        Fetch 2 hours of 2-minute historical bars for indicator warmup.
        Dispatches each bar through the on_bar callback.
        """
        logger.info("Starting historical backfill (%s, %s)...",
                     BACKFILL_PERIOD, BACKFILL_BAR_SIZE)

        raw_bars = await self._client.get_historical_bars(
            period=BACKFILL_PERIOD,
            bar_size=BACKFILL_BAR_SIZE,
        )

        if not raw_bars:
            logger.warning("Backfill returned no data — indicators will cold-start")
            return

        self._backfill_bars = raw_bars
        count = 0

        for bar_dict in raw_bars:
            # Add session_type tag to match CandleAggregator output format
            bar_dict["session_type"] = get_session_type(bar_dict["timestamp"])
            bar_dict.setdefault("tick_count", 0)

            if self._on_bar:
                bar = self.candle_to_bar(bar_dict)
                if bar is not None:
                    self._on_bar(bar)
                    count += 1

        logger.info("Backfill complete: %d bars dispatched for warmup", count)

    # ================================================================
    # WEBSOCKET STREAMING (PRIMARY)
    # ================================================================

    async def _start_websocket(self) -> bool:
        """Attempt to connect WebSocket and wire it to the aggregator."""
        conid = self._client.contract.conid
        self._ws = IBKRWebSocket(self._client.config, conid)
        self._ws.on_tick(self._aggregator.process_tick)

        return await self._ws.connect()

    # ================================================================
    # HTTP POLLING FALLBACK
    # ================================================================

    async def _start_polling(self) -> None:
        """Start HTTP snapshot polling and wire snapshots to the aggregator."""
        self._client.on_snapshot(self._on_poll_snapshot)
        await self._client.start_polling()

    async def _on_poll_snapshot(self, snapshot: 'MarketSnapshot') -> None:
        """Convert HTTP snapshot to tick for the aggregator."""
        if snapshot.last_price > 0:
            self._aggregator.process_tick(
                snapshot.last_price,
                1,
                snapshot.timestamp or datetime.now(timezone.utc),
            )

    # ================================================================
    # HEALTH MONITOR
    # ================================================================

    async def _health_monitor(self) -> None:
        """
        Background loop that monitors data feed health.
        - If WebSocket drops, switch to HTTP polling.
        - If WebSocket recovers, switch back from polling.
        """
        while self._running:
            try:
                await asyncio.sleep(10.0)

                if self._data_mode == "websocket":
                    if self._ws and not self._ws.is_connected:
                        logger.warning("WebSocket lost — attempting reconnect...")
                        reconnected = await self._ws.connect()
                        if not reconnected:
                            if self._ws._consecutive_failures >= WS_FALLBACK_THRESHOLD:
                                logger.warning(
                                    "WebSocket failed %d times — falling back to HTTP polling",
                                    self._ws._consecutive_failures,
                                )
                                await self._start_polling()
                                self._data_mode = "polling"

                elif self._data_mode == "polling":
                    # Periodically try to upgrade back to WebSocket
                    if self._ws and not self._ws.is_connected:
                        reconnected = await self._ws.connect()
                        if reconnected:
                            logger.info("WebSocket reconnected — switching back from polling")
                            await self._client.stop_polling()
                            self._data_mode = "websocket"

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Health monitor error: %s", e)

    # ================================================================
    # BAR DISPATCH
    # ================================================================

    # Fields required to construct a Bar (excluding optional session_type)
    _BAR_REQUIRED_FIELDS = ("timestamp", "open", "high", "low", "close", "volume")
    _BAR_OPTIONAL_FIELDS = ("bid_volume", "ask_volume", "delta", "tick_count", "vwap")

    @staticmethod
    def candle_to_bar(candle: dict) -> Optional[Bar]:
        """
        Convert a candle dict (from CandleAggregator or backfill) to a Bar.

        Steps:
        1. Extract session_type before constructing Bar
        2. Build Bar with explicit field mapping (no **kwargs)
        3. Attach session_type after construction
        4. Log warning for any missing expected field
        """
        # 1. Extract session_type — may be SessionType enum or string
        raw_session = candle.get("session_type")
        if raw_session is not None:
            session_str = raw_session.value if isinstance(raw_session, SessionType) else str(raw_session)
        else:
            session_str = None

        # 4. Check for missing required fields
        for f in IBKRDataFeed._BAR_REQUIRED_FIELDS:
            if f not in candle:
                logger.warning("candle_to_bar: missing required field '%s' — skipping bar", f)
                return None

        # 5. Validate OHLC prices are finite and positive
        for f in ("open", "high", "low", "close"):
            val = candle[f]
            if not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
                logger.warning("candle_to_bar: invalid %s=%.4f — skipping bar", f, val if isinstance(val, (int, float)) else 0)
                return None

        for f in IBKRDataFeed._BAR_OPTIONAL_FIELDS:
            if f not in candle:
                logger.warning("candle_to_bar: missing optional field '%s' — using default", f)

        # 2. Build Bar with explicit field mapping
        bar = Bar(
            timestamp=candle["timestamp"],
            open=candle["open"],
            high=candle["high"],
            low=candle["low"],
            close=candle["close"],
            volume=candle["volume"],
            bid_volume=candle.get("bid_volume", 0),
            ask_volume=candle.get("ask_volume", 0),
            delta=candle.get("delta", 0),
            tick_count=candle.get("tick_count", 0),
            vwap=candle.get("vwap", 0.0),
        )

        # 3. Attach session_type after construction
        bar.session_type = session_str

        return bar

    def _dispatch_bar(self, candle: dict) -> None:
        """Convert candle dict to Bar and forward to the on_bar callback."""
        if self._on_bar:
            bar = self.candle_to_bar(candle)
            if bar is not None:
                self._on_bar(bar)


# ================================================================
# IBKR CLIENT
# ================================================================

class IBKRClient:
    """
    Async client for IBKR Client Portal Gateway.

    Handles:
    - Session authentication and keepalive
    - MNQ front-month contract resolution
    - Market data snapshot polling
    - Connection health monitoring

    Usage:
        client = IBKRClient(IBKRConfig())
        await client.connect()
        snapshot = await client.get_market_snapshot()
        await client.disconnect()
    """

    def __init__(self, config: Optional[IBKRConfig] = None):
        self.config = config or IBKRConfig()
        self._session: Optional[Any] = None
        self._connected: bool = False
        self._authenticated: bool = False
        self._account_id: str = ""
        self._contract: Optional[ContractInfo] = None

        # Background tasks
        self._keepalive_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None

        # Callbacks
        self._on_snapshot: Optional[Callable] = None

        # Last known data
        self._last_snapshot: Optional[MarketSnapshot] = None
        self._last_keepalive: float = 0.0
        self._session_valid: bool = False

    # ================================================================
    # CONNECTION LIFECYCLE
    # ================================================================

    async def connect(self) -> bool:
        """
        Connect to Client Portal Gateway.

        1. Create HTTP session (with SSL verification disabled for localhost gateway)
        2. Validate existing session via /iserver/auth/status
        3. Resolve MNQ front-month contract
        4. Start keepalive loop
        """
        if aiohttp is None:
            raise ImportError("aiohttp required: pip install aiohttp")

        logger.info("Connecting to IBKR Client Portal Gateway at %s:%d",
                     self.config.gateway_host, self.config.gateway_port)
        logger.info("Account type: %s", self.config.account_type)

        # Client Portal Gateway uses self-signed SSL cert on localhost
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        self._session = aiohttp.ClientSession(connector=connector)

        # Step 1: Check/validate session
        if not await self._check_auth_status():
            logger.error("Gateway session not authenticated. "
                         "Please log in via the Client Portal Gateway web UI.")
            await self._session.close()
            self._session = None
            return False

        self._authenticated = True

        # Step 2: Get account ID
        if not await self._fetch_account_id():
            logger.error("Could not fetch account ID")
            await self._session.close()
            self._session = None
            return False

        # Step 3: Resolve contract
        self._contract = await self._resolve_front_month(self.config.symbol)
        if not self._contract:
            logger.error("Could not resolve front-month contract for %s",
                         self.config.symbol)
            await self._session.close()
            self._session = None
            return False

        logger.info("Contract resolved: %s %s → conid %d",
                     self._contract.symbol, self._contract.expiry,
                     self._contract.conid)

        # Step 4: Start keepalive
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

        self._connected = True
        logger.info("IBKR client connected successfully")
        return True

    async def disconnect(self) -> None:
        """Gracefully disconnect and cancel background tasks."""
        self._connected = False

        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

        if self._session:
            await self._session.close()
            self._session = None

        self._authenticated = False
        self._session_valid = False
        logger.info("IBKR client disconnected")

    # ================================================================
    # AUTHENTICATION & SESSION
    # ================================================================

    async def _check_auth_status(self) -> bool:
        """
        Check if the gateway session is authenticated.
        The user must have already logged in via the CP Gateway web UI.
        """
        data = await self._get("/iserver/auth/status")
        if data is None:
            return False

        authenticated = data.get("authenticated", False)
        competing = data.get("competing", False)

        if competing:
            logger.warning("Competing session detected — calling /iserver/auth/compete")
            await self._post("/iserver/auth/compete")
            # Re-check
            data = await self._get("/iserver/auth/status")
            authenticated = data.get("authenticated", False) if data else False

        self._session_valid = authenticated
        if authenticated:
            logger.info("Gateway session is authenticated")
        else:
            logger.warning("Gateway session NOT authenticated")

        return authenticated

    async def _tickle(self) -> bool:
        """
        Send keepalive to prevent session timeout.
        POST /tickle
        """
        data = await self._post("/tickle")
        if data is None:
            return False

        self._last_keepalive = time.time()
        session_valid = data.get("session", "") != ""
        self._session_valid = session_valid
        return session_valid

    async def _keepalive_loop(self) -> None:
        """Background loop: tickle the gateway every 60 seconds."""
        while self._connected:
            try:
                success = await self._tickle()
                if not success:
                    logger.warning("Keepalive tickle failed — session may have expired")
                    # Try to re-validate
                    if not await self._check_auth_status():
                        logger.error("Session expired. Re-authentication required.")
                        self._session_valid = False
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Keepalive error: %s", e)

            await asyncio.sleep(self.config.keepalive_interval_seconds)

    async def _fetch_account_id(self) -> bool:
        """Fetch the account ID from /portfolio/accounts."""
        data = await self._get("/portfolio/accounts")
        if not data or not isinstance(data, list) or len(data) == 0:
            return False

        # Select first account (or filter by account type)
        for acct in data:
            acct_type = acct.get("type", "")
            acct_id = acct.get("accountId", "")
            # Paper accounts often have 'DEMO' or 'paper' prefix
            if self.config.account_type == "paper" and "DU" in str(acct_id):
                self._account_id = acct_id
                break
            elif self.config.account_type == "live" and "DU" not in str(acct_id):
                self._account_id = acct_id
                break

        # Fallback: use first account
        if not self._account_id:
            self._account_id = data[0].get("accountId", "")

        if self._account_id:
            logger.info("Using account: %s", self._account_id)
            return True

        return False

    # ================================================================
    # CONTRACT RESOLUTION
    # ================================================================

    async def _resolve_front_month(self, symbol: str) -> Optional[ContractInfo]:
        """
        Resolve the continuous front-month futures contract.

        1. Search via /iserver/secdef/search
        2. Get contract details
        3. Select front-month (nearest expiry)
        """
        # Step 1: Search for the symbol
        search_data = await self._post(
            "/iserver/secdef/search",
            {"symbol": symbol, "secType": "FUT", "name": symbol}
        )

        if not search_data or not isinstance(search_data, list):
            logger.error("Contract search returned no results for %s", symbol)
            return None

        # Find the futures entry
        futures_entry = None
        for entry in search_data:
            if entry.get("secType") == "FUT" or "FUT" in str(entry.get("sections", [])):
                futures_entry = entry
                break

        if not futures_entry:
            # Fallback: use first result
            futures_entry = search_data[0]

        conid = futures_entry.get("conid", 0)
        sections = futures_entry.get("sections", [])

        # If sections contain futures months, get them
        if sections:
            for section in sections:
                if isinstance(section, dict) and section.get("secType") == "FUT":
                    months = section.get("months", "")
                    exchange = section.get("exchange", "")
                    break
            else:
                months = ""
                exchange = futures_entry.get("exchange", "")
        else:
            months = ""
            exchange = futures_entry.get("exchange", "")

        # Step 2: Get specific contract details via secdef/info
        if conid:
            info = await self._get(
                "/iserver/contract/info",
                params={"conid": conid}
            )
            if info:
                return ContractInfo(
                    conid=conid,
                    symbol=info.get("symbol", symbol),
                    exchange=info.get("exchange", exchange),
                    expiry=info.get("maturity_date", ""),
                    description=info.get("company_name", ""),
                )

        # Step 3: If no direct conid, try to get front month from secdef/info
        # Use the futures_entry conid as-is (may be the front-month already)
        if conid:
            return ContractInfo(
                conid=conid,
                symbol=futures_entry.get("symbol", symbol),
                exchange=exchange,
                expiry=futures_entry.get("maturity_date", ""),
                description=futures_entry.get("companyHeader", ""),
            )

        return None

    async def check_contract_rollover(self) -> Optional[ContractInfo]:
        """
        Check if the front-month contract has changed (quarterly rollover).
        Returns new ContractInfo if rollover detected, None otherwise.
        """
        if not self._contract:
            return None

        new_contract = await self._resolve_front_month(self.config.symbol)
        if not new_contract:
            return None

        if new_contract.conid != self._contract.conid:
            logger.info(
                "CONTRACT ROLLOVER detected: %s (conid %d) → %s (conid %d)",
                self._contract.expiry, self._contract.conid,
                new_contract.expiry, new_contract.conid,
            )
            old = self._contract
            self._contract = new_contract
            return new_contract

        return None

    # ================================================================
    # MARKET DATA — HTTP SNAPSHOT POLLING
    # ================================================================

    async def get_market_snapshot(self) -> Optional[MarketSnapshot]:
        """
        Fetch a market data snapshot for the resolved contract.
        GET /iserver/marketdata/snapshot?conids={conid}&fields=31,84,85,86,88
        """
        if not self._contract:
            return None

        conid = self._contract.conid
        field_ids = ",".join(str(f) for f in SNAPSHOT_FIELDS.keys())

        data = await self._get(
            "/iserver/marketdata/snapshot",
            params={"conids": str(conid), "fields": field_ids}
        )

        if not data:
            return None

        # Response is a list of snapshots (one per conid)
        if isinstance(data, list) and len(data) > 0:
            snap_data = data[0]
        elif isinstance(data, dict):
            snap_data = data
        else:
            return None

        snapshot = MarketSnapshot(
            conid=conid,
            last_price=self._parse_price(snap_data.get("31")),
            bid=self._parse_price(snap_data.get("84")),
            ask=self._parse_price(snap_data.get("85")),
            high=self._parse_price(snap_data.get("86")),
            low=self._parse_price(snap_data.get("88")),
            timestamp=datetime.now(timezone.utc),
        )

        self._last_snapshot = snapshot
        return snapshot

    async def start_polling(self) -> None:
        """Start background market data polling."""
        if self._poll_task and not self._poll_task.done():
            logger.warning("Polling already active")
            return

        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("Market data polling started (interval: %.1fs)",
                     self.config.poll_interval_seconds)

    async def stop_polling(self) -> None:
        """Stop background polling."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            logger.info("Market data polling stopped")

    async def _poll_loop(self) -> None:
        """Background loop: poll market data snapshots."""
        while self._connected:
            try:
                snapshot = await self.get_market_snapshot()
                if snapshot and self._on_snapshot:
                    await self._on_snapshot(snapshot)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Snapshot poll error: %s", e)

            await asyncio.sleep(self.config.poll_interval_seconds)

    # ================================================================
    # HISTORICAL DATA
    # ================================================================

    async def get_historical_bars(
        self,
        period: str = "2h",
        bar_size: str = "2min",
    ) -> List[dict]:
        """
        Fetch historical bars from Client Portal Gateway.
        GET /iserver/marketdata/history?conid={conid}&period={period}&bar={bar}

        Returns list of OHLCV dicts compatible with the Bar dataclass.
        """
        if not self._contract:
            return []

        data = await self._get(
            "/iserver/marketdata/history",
            params={
                "conid": str(self._contract.conid),
                "period": period,
                "bar": bar_size,
            }
        )

        if not data or "data" not in data:
            logger.warning("Historical bar fetch returned no data")
            return []

        bars = []
        for raw_bar in data["data"]:
            bars.append({
                "timestamp": datetime.fromtimestamp(
                    raw_bar.get("t", 0) / 1000, tz=timezone.utc
                ),
                "open": round(raw_bar.get("o", 0.0), 2),
                "high": round(raw_bar.get("h", 0.0), 2),
                "low": round(raw_bar.get("l", 0.0), 2),
                "close": round(raw_bar.get("c", 0.0), 2),
                "volume": raw_bar.get("v", 0),
            })

        logger.info("Fetched %d historical bars (%s, %s)", len(bars), period, bar_size)
        return bars

    # ================================================================
    # HTTP HELPERS
    # ================================================================

    async def _get(self, endpoint: str, params: dict = None) -> Optional[Any]:
        """GET request to Client Portal Gateway."""
        if not self._session:
            return None

        url = f"{self.config.base_url}{endpoint}"
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 401:
                    logger.warning("GET %s → 401 Unauthorized (session expired)", endpoint)
                    self._session_valid = False
                    return None
                else:
                    body = await resp.text()
                    logger.error("GET %s [%d]: %s", endpoint, resp.status, body[:200])
                    return None
        except aiohttp.ClientConnectorError:
            logger.error("Cannot reach gateway at %s (is it running?)",
                         self.config.base_url)
            return None
        except Exception as e:
            logger.error("GET %s error: %s", endpoint, e)
            return None

    async def _post(self, endpoint: str, payload: dict = None) -> Optional[Any]:
        """POST request to Client Portal Gateway."""
        if not self._session:
            return None

        url = f"{self.config.base_url}{endpoint}"
        try:
            async with self._session.post(url, json=payload or {}) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 401:
                    logger.warning("POST %s → 401 Unauthorized", endpoint)
                    self._session_valid = False
                    return None
                else:
                    body = await resp.text()
                    logger.error("POST %s [%d]: %s", endpoint, resp.status, body[:200])
                    return None
        except aiohttp.ClientConnectorError:
            logger.error("Cannot reach gateway at %s (is it running?)",
                         self.config.base_url)
            return None
        except Exception as e:
            logger.error("POST %s error: %s", endpoint, e)
            return None

    # ================================================================
    # CALLBACKS
    # ================================================================

    def on_snapshot(self, callback: Callable) -> None:
        """Register callback for market data snapshot updates."""
        self._on_snapshot = callback

    def get_session_type(self) -> SessionType:
        """Return current session type (RTH or ETH)."""
        return get_session_type(datetime.now(timezone.utc))

    # ================================================================
    # STATUS & HEALTH
    # ================================================================

    @property
    def is_connected(self) -> bool:
        """True if connected and session is valid."""
        return self._connected and self._session_valid

    @property
    def contract(self) -> Optional[ContractInfo]:
        return self._contract

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def last_snapshot(self) -> Optional[MarketSnapshot]:
        return self._last_snapshot

    def get_current_price(self) -> Optional[Dict[str, float]]:
        """Return latest bid/ask/last from most recent snapshot."""
        if not self._last_snapshot:
            return None
        return {
            "bid": self._last_snapshot.bid,
            "ask": self._last_snapshot.ask,
            "last": self._last_snapshot.last_price,
        }

    def get_status(self) -> dict:
        """Return connection health summary."""
        return {
            "connected": self._connected,
            "session_valid": self._session_valid,
            "authenticated": self._authenticated,
            "account_id": self._account_id,
            "account_type": self.config.account_type,
            "gateway": f"{self.config.gateway_host}:{self.config.gateway_port}",
            "symbol": self.config.symbol,
            "conid": self._contract.conid if self._contract else 0,
            "contract_expiry": self._contract.expiry if self._contract else "",
            "last_keepalive_age_s": round(time.time() - self._last_keepalive, 1)
            if self._last_keepalive else None,
            "last_snapshot_time": self._last_snapshot.timestamp.isoformat()
            if self._last_snapshot and self._last_snapshot.timestamp else None,
            "polling_active": self._poll_task is not None
            and not self._poll_task.done() if self._poll_task else False,
        }

    # ================================================================
    # HELPERS
    # ================================================================

    @staticmethod
    def _parse_price(value: Any) -> float:
        """
        Parse a price value from IBKR snapshot response.
        Values may be str, float, int, or prefixed with 'C' (closing).
        """
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            parsed = round(float(value), 2)
            if not math.isfinite(parsed):
                return 0.0
            return parsed
        if isinstance(value, str):
            # IBKR sometimes prefixes with 'C' for closing price
            cleaned = value.lstrip("C").strip()
            try:
                parsed = round(float(cleaned), 2)
                if not math.isfinite(parsed):
                    return 0.0
                return parsed
            except (ValueError, TypeError):
                return 0.0
        return 0.0
