"""
IBKR TWS API Client via ib_insync
==================================
Direct socket connection to TWS / IB Gateway for MNQ futures trading.

Replaces the Client Portal Gateway HTTP approach with a persistent socket
connection. No browser, no SSL, no session timeouts.

Port reference:
  7497 = TWS paper trading
  7496 = TWS live trading
  4002 = IB Gateway paper trading
  4001 = IB Gateway live trading

Env vars:
  IBKR_TWS_HOST       (default: 127.0.0.1)
  IBKR_TWS_PORT       (default: 7497)
  IBKR_CLIENT_ID      (default: 1)
  IBKR_SYMBOL         (default: MNQ)

SECURITY: This module NEVER logs credentials or account numbers.
"""

import nest_asyncio
nest_asyncio.apply()

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from ib_insync import IB, Contract, Future, MarketOrder, LimitOrder, StopOrder, util

from features.engine import Bar

logger = logging.getLogger(__name__)

# Re-export Client Portal classes for backward compatibility.
# Other modules (orchestrator, order_executor, run_ibkr, tests) still import
# these names from Broker.ibkr_client. The Client Portal code is preserved
# in ibkr_client_portal.py but is no longer the primary path.
try:
    from Broker.ibkr_client_portal import (  # noqa: F401
        IBKRClient as IBKRClientPortal,
        IBKRConfig,
        IBKRDataFeed,
        IBKRWebSocket,
        CandleAggregator,
        ContractInfo,
        MarketSnapshot,
        SessionType,
        get_session_type,
        BACKFILL_BAR_SIZE,
        BACKFILL_PERIOD,
        CANDLE_INTERVAL_SECONDS,
        CONSECUTIVE_GAP_ALERT_THRESHOLD,
        SNAPSHOT_FIELDS,
        WS_FALLBACK_THRESHOLD,
    )
    from Broker.ibkr_client_portal import ET_TZ as ET_OFFSET  # noqa: F401
except ImportError:
    pass

# ================================================================
# CONSTANTS
# ================================================================

RECONNECT_MAX_RETRIES = 3
RECONNECT_BASE_DELAY = 2.0  # seconds, exponential backoff
HEARTBEAT_INTERVAL = 30.0   # seconds


# ================================================================
# TWS CLIENT
# ================================================================

class IBKRClient:
    """
    TWS API client via ib_insync.

    Provides:
      - Socket connection to TWS / IB Gateway
      - Real-time bar subscription for MNQ
      - Order placement (Market, Limit, Stop)
      - Position and account queries
      - Auto-reconnect with exponential backoff
      - Heartbeat monitoring
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 1,
    ):
        self._host = host
        self._port = port
        self._client_id = client_id

        self._ib = IB()
        self._contract: Optional[Contract] = None
        self._bars = None  # RealTimeBarList or BarDataList

        # Callbacks
        self._on_bar_update: List[Callable] = []
        self._on_order_filled: List[Callable] = []
        self._on_error: List[Callable] = []

        # Reconnect state
        self._reconnect_attempts = 0

        # Heartbeat
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._last_heartbeat: float = 0.0

        # Wire up ib_insync events
        self._ib.errorEvent += self._handle_error
        self._ib.disconnectedEvent += self._handle_disconnect
        self._ib.orderStatusEvent += self._handle_order_status

    # ──────────────────────────────────────────────────────
    # CONNECTION
    # ──────────────────────────────────────────────────────

    async def connect(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        client_id: Optional[int] = None,
    ) -> bool:
        """
        Connect to TWS / IB Gateway.

        Args:
            host: Override host (default: self._host)
            port: Override port (default: self._port)
            client_id: Override client ID (default: self._client_id)

        Returns:
            True if connected successfully.
        """
        h = host or self._host
        p = port or self._port
        cid = client_id or self._client_id

        try:
            self._ib.connect(h, p, clientId=cid)
            self._reconnect_attempts = 0
            self._last_heartbeat = time.monotonic()
            logger.info("Connected to TWS at %s:%d (clientId=%d)", h, p, cid)

            # Start heartbeat monitor
            self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())

            return True
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
            logger.error("Failed to connect to TWS at %s:%d — %s", h, p, e)
            return False
        except Exception as e:
            logger.error("Unexpected connection error: %s", e)
            return False

    def disconnect(self) -> None:
        """Disconnect from TWS."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        if self._ib.isConnected():
            self._ib.disconnect()
            logger.info("Disconnected from TWS")

    def is_connected(self) -> bool:
        """Check if connected to TWS."""
        return self._ib.isConnected()

    # ──────────────────────────────────────────────────────
    # CONTRACT
    # ──────────────────────────────────────────────────────

    def get_contract(self, symbol: str = "MNQ", exchange: str = "CME") -> Contract:
        """
        Get futures contract definition for any supported CME Micro instrument.
        Requests all available expiries via reqContractDetails, then picks the
        front month (nearest lastTradeDateOrContractMonth) to avoid the
        "Ambiguous contract" error from TWS.
        """
        # Use an under-specified contract to fetch all available expiries
        generic = Future(symbol, exchange=exchange)
        details_list = self._ib.reqContractDetails(generic)

        if not details_list:
            raise ValueError(f"Could not find contract details for {symbol} on {exchange}")

        # Sort by expiry and pick the nearest (front month)
        details_list.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
        front = details_list[0].contract

        # Qualify the specific front-month contract
        qualified = self._ib.qualifyContracts(front)
        if qualified:
            self._contract = qualified[0]
            logger.info(
                "Contract qualified: %s %s (conId=%d, expiry=%s)",
                self._contract.symbol,
                self._contract.exchange,
                self._contract.conId,
                self._contract.lastTradeDateOrContractMonth,
            )
            return self._contract
        raise ValueError(f"Could not qualify contract {symbol} on {exchange}")

    # ──────────────────────────────────────────────────────
    # MARKET DATA
    # ──────────────────────────────────────────────────────

    async def subscribe_market_data(
        self,
        symbol: str = "MNQ",
        exchange: str = "CME",
    ) -> bool:
        """
        Subscribe to real-time bars for a CME Micro futures instrument.

        Uses reqRealTimeBars for 5-second bars.
        Each completed bar fires on_bar_update callbacks.

        Returns:
            True if subscription started successfully.
        """
        if self._contract is None:
            try:
                self.get_contract(symbol, exchange)
            except ValueError as e:
                logger.error("subscribe_market_data: %s", e)
                return False

        try:
            self._bars = self._ib.reqRealTimeBars(
                self._contract,
                barSize=5,
                whatToShow="TRADES",
                useRTH=False,
            )
            self._bars.updateEvent += self._on_realtime_bar
            logger.info("Subscribed to real-time bars for %s", symbol)
            return True
        except Exception as e:
            logger.error("Failed to subscribe to market data: %s", e)
            return False

    def _on_realtime_bar(self, bars, has_new_bar) -> None:
        """Internal handler for real-time bar updates."""
        if not has_new_bar or not bars:
            return

        self._last_heartbeat = time.monotonic()
        ib_bar = bars[-1]

        # Extract timestamp — RealTimeBar uses .time, HistoricalBar uses .date
        raw_ts = getattr(ib_bar, "time", None) or getattr(ib_bar, "date", None)
        if isinstance(raw_ts, datetime):
            ts = raw_ts.astimezone(timezone.utc) if raw_ts.tzinfo else raw_ts.replace(tzinfo=timezone.utc)
        elif hasattr(raw_ts, "timestamp"):
            ts = datetime.fromtimestamp(raw_ts.timestamp(), tz=timezone.utc)
        else:
            ts = datetime.now(timezone.utc)

        # Extract open — RealTimeBar uses .open_, HistoricalBar uses .open
        open_price = getattr(ib_bar, "open_", None) or getattr(ib_bar, "open", None)

        bar = Bar(
            timestamp=ts,
            open=open_price,
            high=ib_bar.high,
            low=ib_bar.low,
            close=ib_bar.close,
            volume=int(ib_bar.volume),
        )

        for cb in self._on_bar_update:
            try:
                cb(bar)
            except Exception as e:
                logger.error("Bar callback error: %s", e)

    # ──────────────────────────────────────────────────────
    # ORDERS
    # ──────────────────────────────────────────────────────

    async def place_order(
        self,
        action: str,
        quantity: int,
        order_type: str = "MKT",
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
    ) -> Optional[int]:
        """
        Place an order.

        Args:
            action: 'BUY' or 'SELL'
            quantity: Number of contracts
            order_type: 'MKT', 'LMT', or 'STP'
            limit_price: Required for LMT orders
            stop_price: Required for STP orders

        Returns:
            order_id on success, None on failure.
        """
        if self._contract is None:
            logger.error("No contract qualified — call get_contract() first")
            return None

        if order_type == "MKT":
            order = MarketOrder(action, quantity)
        elif order_type == "LMT":
            if limit_price is None:
                logger.error("limit_price required for LMT order")
                return None
            order = LimitOrder(action, quantity, limit_price)
        elif order_type == "STP":
            if stop_price is None:
                logger.error("stop_price required for STP order")
                return None
            order = StopOrder(action, quantity, stop_price)
        else:
            logger.error("Unsupported order type: %s", order_type)
            return None

        try:
            trade = self._ib.placeOrder(self._contract, order)
            logger.info(
                "Order placed: %s %d %s @ %s (orderId=%d)",
                action, quantity, order_type,
                limit_price or stop_price or "MKT",
                trade.order.orderId,
            )
            return trade.order.orderId
        except Exception as e:
            logger.error("Order placement failed: %s", e)
            return None

    async def cancel_order(self, order_id: int) -> bool:
        """Cancel an open order by order ID."""
        for trade in self._ib.openTrades():
            if trade.order.orderId == order_id:
                self._ib.cancelOrder(trade.order)
                logger.info("Cancel requested for orderId=%d", order_id)
                return True
        logger.warning("Order %d not found in open trades", order_id)
        return False

    # ──────────────────────────────────────────────────────
    # POSITIONS & ACCOUNT
    # ──────────────────────────────────────────────────────

    async def get_positions(self) -> list:
        """Get current positions."""
        positions = self._ib.positions()
        result = []
        for pos in positions:
            result.append({
                "account": pos.account,
                "symbol": pos.contract.symbol,
                "exchange": pos.contract.exchange,
                "size": pos.position,
                "avg_cost": pos.avgCost,
            })
        return result

    async def get_account_summary(self) -> dict:
        """Get account summary (buying power, cash, PnL)."""
        summary = {}
        acct_values = self._ib.accountSummary()
        for av in acct_values:
            if av.tag in ("BuyingPower", "TotalCashValue", "UnrealizedPnL",
                          "RealizedPnL", "NetLiquidation"):
                summary[av.tag] = float(av.value) if av.value else 0.0
        return summary

    # ──────────────────────────────────────────────────────
    # CALLBACKS
    # ──────────────────────────────────────────────────────

    def on_bar_update(self, callback: Callable) -> None:
        """Register callback for bar updates. Callback receives Bar."""
        self._on_bar_update.append(callback)

    def on_order_filled(self, callback: Callable) -> None:
        """Register callback for order fills. Callback receives trade dict."""
        self._on_order_filled.append(callback)

    def on_error(self, callback: Callable) -> None:
        """Register callback for errors. Callback receives (reqId, errorCode, errorString)."""
        self._on_error.append(callback)

    # ──────────────────────────────────────────────────────
    # INTERNAL EVENT HANDLERS
    # ──────────────────────────────────────────────────────

    def _handle_error(self, reqId, errorCode, errorString, contract) -> None:
        """Handle IB error events."""
        # Filter out non-critical info messages
        if errorCode in (2104, 2106, 2158, 2119):
            # Data farm connection messages — info only
            logger.debug("IB info [%d]: %s", errorCode, errorString)
            return

        logger.error("IB error [reqId=%d, code=%d]: %s", reqId, errorCode, errorString)
        for cb in self._on_error:
            try:
                cb(reqId, errorCode, errorString)
            except Exception as e:
                logger.error("Error callback failed: %s", e)

    def _handle_disconnect(self) -> None:
        """Handle disconnection — attempt reconnect."""
        logger.warning("Disconnected from TWS")
        asyncio.ensure_future(self._reconnect())

    def _handle_order_status(self, trade) -> None:
        """Handle order status updates — fire on_order_filled for fills."""
        if trade.orderStatus.status == "Filled":
            fill_info = {
                "order_id": trade.order.orderId,
                "action": trade.order.action,
                "quantity": trade.orderStatus.filled,
                "avg_fill_price": trade.orderStatus.avgFillPrice,
                "status": "Filled",
            }
            logger.info(
                "Order FILLED: %s %s @ %.2f (orderId=%d)",
                trade.order.action,
                trade.orderStatus.filled,
                trade.orderStatus.avgFillPrice,
                trade.order.orderId,
            )
            for cb in self._on_order_filled:
                try:
                    cb(fill_info)
                except Exception as e:
                    logger.error("Fill callback error: %s", e)

    # ──────────────────────────────────────────────────────
    # AUTO-RECONNECT
    # ──────────────────────────────────────────────────────

    async def _reconnect(self) -> None:
        """Auto-reconnect with exponential backoff (3 retries)."""
        for attempt in range(1, RECONNECT_MAX_RETRIES + 1):
            delay = RECONNECT_BASE_DELAY * (2 ** (attempt - 1))
            logger.info(
                "Reconnect attempt %d/%d in %.0fs...",
                attempt, RECONNECT_MAX_RETRIES, delay,
            )
            await asyncio.sleep(delay)

            try:
                self._ib.connect(
                    self._host, self._port, clientId=self._client_id
                )
                if self._ib.isConnected():
                    logger.info("Reconnected to TWS on attempt %d", attempt)
                    self._reconnect_attempts = 0
                    return
            except Exception as e:
                logger.warning("Reconnect attempt %d failed: %s", attempt, e)

        logger.critical(
            "Failed to reconnect after %d attempts — manual intervention required",
            RECONNECT_MAX_RETRIES,
        )

    # ──────────────────────────────────────────────────────
    # HEARTBEAT
    # ──────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Check connection health every 30 seconds."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if not self._ib.isConnected():
                    logger.warning("Heartbeat: TWS connection lost")
                    await self._reconnect()
                else:
                    logger.debug("Heartbeat: TWS connection OK")
        except asyncio.CancelledError:
            pass
