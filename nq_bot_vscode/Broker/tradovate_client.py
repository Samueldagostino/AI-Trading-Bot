"""
Tradovate Broker Client
========================
Full integration with Tradovate REST + WebSocket API.

Architecture:
- REST API: Authentication, account info, order placement
- Market Data WebSocket: Real-time quotes, charts, DOM
- Order WebSocket: Order fills, position updates, account events

Docs: https://api.tradovate.com/
Demo: https://demo.tradovate.com (paper trading)
Live: https://live.tradovate.com

CRITICAL: This client defaults to DEMO environment.
Switching to live requires explicit config change + confirmation.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Callable, Any
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)

# These are imported conditionally to avoid startup failures
try:
    import aiohttp
except ImportError:
    aiohttp = None
    logger.warning("aiohttp not installed. Install with: pip install aiohttp")


class TradovateOrderAction(Enum):
    BUY = "Buy"
    SELL = "Sell"


class TradovateOrderType(Enum):
    MARKET = "Market"
    LIMIT = "Limit"
    STOP = "Stop"
    STOP_LIMIT = "StopLimit"
    TRAILING_STOP = "TrailingStop"


@dataclass
class TradovateAccount:
    """Tradovate account info."""
    account_id: int = 0
    name: str = ""
    net_liquidity: float = 0.0
    cash_balance: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    margin_used: float = 0.0


@dataclass
class TradovateFill:
    """Order fill information."""
    order_id: int = 0
    contract_id: int = 0
    fill_price: float = 0.0
    filled_qty: int = 0
    fill_time: Optional[datetime] = None
    action: str = ""


class TradovateClient:
    """
    Async Tradovate API client.
    
    Usage:
        client = TradovateClient(config.tradovate)
        await client.connect()
        await client.subscribe_market_data("MNQM5")
        # ... trading loop ...
        await client.disconnect()
    """

    def __init__(self, config):
        self.config = config
        self._session: Optional[Any] = None
        self._access_token: str = ""
        self._token_expiry: float = 0
        self._md_ws = None          # Market data WebSocket
        self._order_ws = None       # Order WebSocket
        self._account: Optional[TradovateAccount] = None
        self._contract_id: int = 0  # Resolved contract ID for our symbol
        self._position: Dict = {}
        
        # Callbacks
        self._on_quote: Optional[Callable] = None
        self._on_bar: Optional[Callable] = None
        self._on_fill: Optional[Callable] = None
        self._on_position_update: Optional[Callable] = None
        
        # State
        self._connected = False
        self._md_request_id = 1
        self._reconnecting = False

    # ================================================================
    # CONNECTION LIFECYCLE
    # ================================================================
    async def connect(self) -> bool:
        """
        Full connection sequence:
        1. Create HTTP session
        2. Authenticate (get access token)
        3. Resolve contract ID for symbol
        4. Connect market data WebSocket
        5. Connect order WebSocket
        6. Fetch account info
        """
        if aiohttp is None:
            raise ImportError("aiohttp required: pip install aiohttp")

        logger.info(f"Connecting to Tradovate ({self.config.environment})...")
        logger.info(f"Base URL: {self.config.base_url}")

        self._session = aiohttp.ClientSession()

        # Step 1: Authenticate
        if not await self._authenticate():
            logger.error("Authentication failed")
            return False

        # Step 2: Resolve contract
        self._contract_id = await self._resolve_contract(self.config.symbol)
        if not self._contract_id:
            logger.error(f"Could not resolve contract: {self.config.symbol}")
            return False
        logger.info(f"Contract resolved: {self.config.symbol} -> ID {self._contract_id}")

        # Step 3: Get account info
        self._account = await self._get_account()
        if self._account:
            logger.info(f"Account: {self._account.name} | Balance: ${self._account.cash_balance:,.2f}")

        # Step 4: Connect WebSockets
        asyncio.create_task(self._connect_market_data_ws())
        asyncio.create_task(self._connect_order_ws())

        self._connected = True
        logger.info("Tradovate client connected successfully")
        return True

    async def disconnect(self) -> None:
        """Graceful disconnect."""
        self._connected = False
        if self._md_ws:
            await self._md_ws.close()
        if self._order_ws:
            await self._order_ws.close()
        if self._session:
            await self._session.close()
        logger.info("Tradovate client disconnected")

    # ================================================================
    # AUTHENTICATION
    # ================================================================
    async def _authenticate(self) -> bool:
        """
        Authenticate via Tradovate REST API.
        Returns access token valid for ~24 hours.
        """
        url = f"{self.config.base_url}/auth/accesstokenrequest"
        payload = {
            "name": self.config.username,
            "password": self.config.password,
            "appId": self.config.app_id,
            "appVersion": self.config.app_version,
            "cid": self.config.cid,
            "sec": self.config.sec,
            "deviceId": self.config.device_id,
        }

        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logger.error("Auth failed [%d]", resp.status)
                    return False

                data = await resp.json()
                self._access_token = data.get("accessToken", "")
                expiry = data.get("expirationTime", "")
                
                if not self._access_token:
                    logger.error("No access token in auth response (keys: %s)", list(data.keys()))
                    return False

                # Parse expiry
                if expiry:
                    try:
                        exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                        self._token_expiry = exp_dt.timestamp()
                    except Exception:
                        self._token_expiry = time.time() + 86400

                logger.info("Authenticated successfully")
                return True

        except Exception as e:
            logger.error(f"Authentication error: {e}")
            return False

    async def _ensure_token(self) -> None:
        """Refresh token if expired or close to expiry."""
        if time.time() >= self._token_expiry - 300:  # Refresh 5 min before expiry
            logger.info("Refreshing access token...")
            await self._authenticate()

    @property
    def _auth_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    # ================================================================
    # REST API HELPERS
    # ================================================================
    async def _get(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """GET request with auth."""
        await self._ensure_token()
        url = f"{self.config.base_url}{endpoint}"
        try:
            async with self._session.get(url, headers=self._auth_headers, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    body = await resp.text()
                    logger.error(f"GET {endpoint} [{resp.status}]: {body}")
                    return None
        except Exception as e:
            logger.error(f"GET {endpoint} error: {e}")
            return None

    async def _post(self, endpoint: str, payload: dict = None) -> Optional[dict]:
        """POST request with auth."""
        await self._ensure_token()
        url = f"{self.config.base_url}{endpoint}"
        try:
            async with self._session.post(url, headers=self._auth_headers, json=payload or {}) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    body = await resp.text()
                    logger.error(f"POST {endpoint} [{resp.status}]: {body}")
                    return None
        except Exception as e:
            logger.error(f"POST {endpoint} error: {e}")
            return None

    # ================================================================
    # CONTRACT RESOLUTION
    # ================================================================
    async def _resolve_contract(self, symbol: str) -> int:
        """Resolve a symbol name to a Tradovate contract ID."""
        data = await self._get("/contract/find", {"name": symbol})
        if data and isinstance(data, dict):
            return data.get("id", 0)
        elif data and isinstance(data, list) and len(data) > 0:
            return data[0].get("id", 0)
        return 0

    # ================================================================
    # ACCOUNT INFO
    # ================================================================
    async def _get_account(self) -> Optional[TradovateAccount]:
        """Fetch primary trading account."""
        accounts = await self._get("/account/list")
        if not accounts or not isinstance(accounts, list):
            return None

        acct = accounts[0]  # Use first account
        
        # Get cash balance
        balance = await self._get(f"/cashBalance/getCashBalanceSnapshot", {"accountId": acct["id"]})
        
        return TradovateAccount(
            account_id=acct.get("id", 0),
            name=acct.get("name", ""),
            cash_balance=balance.get("cashBalance", 0) if balance else 0,
            realized_pnl=balance.get("realizedPnL", 0) if balance else 0,
            unrealized_pnl=balance.get("unrealizedPnL", 0) if balance else 0,
        )

    async def get_account_balance(self) -> float:
        """Get current account cash balance."""
        if not self._account:
            return 0.0
        balance = await self._get(f"/cashBalance/getCashBalanceSnapshot", 
                                  {"accountId": self._account.account_id})
        if balance:
            return balance.get("cashBalance", 0.0)
        return 0.0

    # ================================================================
    # MARKET DATA WEBSOCKET
    # ================================================================
    async def _connect_market_data_ws(self) -> None:
        """Connect to market data WebSocket for live quotes and charts."""
        while self._connected:
            try:
                self._md_ws = await self._session.ws_connect(
                    self.config.md_ws_url,
                    headers={"Authorization": f"Bearer {self._access_token}"},
                )
                logger.info("Market data WebSocket connected")

                # Authorize the WebSocket
                await self._md_ws.send_str(f"authorize\n0\n\n{self._access_token}")

                async for msg in self._md_ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_md_message(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error(f"MD WS error: {self._md_ws.exception()}")
                        break

            except Exception as e:
                logger.error(f"MD WebSocket error: {e}")

            if self._connected:
                logger.info(f"MD WebSocket reconnecting in {self.config.reconnect_delay_seconds}s...")
                await asyncio.sleep(self.config.reconnect_delay_seconds)

    async def _handle_md_message(self, raw: str) -> None:
        """Parse Tradovate market data WebSocket messages."""
        # Tradovate WS format: "event_type\nrequest_id\n\njson_body"
        parts = raw.split("\n", 3)
        if len(parts) < 4:
            return

        event_type = parts[0]
        body = parts[3] if len(parts) > 3 else ""

        try:
            if not body:
                return
            data = json.loads(body)
        except json.JSONDecodeError:
            return

        if event_type == "md" and "quotes" in str(data):
            # Quote update
            if self._on_quote:
                await self._on_quote(data)
        elif event_type == "chart":
            # Chart bar update
            if self._on_bar:
                await self._on_bar(data)

    async def subscribe_market_data(self, symbol: str = None) -> None:
        """Subscribe to real-time market data for our contract."""
        if not self._md_ws:
            logger.warning("MD WebSocket not connected yet")
            return

        contract_id = self._contract_id
        
        # Subscribe to quotes
        req_id = self._md_request_id
        self._md_request_id += 1
        subscribe_msg = f"md/subscribeQuote\n{req_id}\n\n{{\"symbol\":\"{symbol or self.config.symbol}\"}}"
        await self._md_ws.send_str(subscribe_msg)
        
        # Subscribe to 1-minute chart
        req_id = self._md_request_id
        self._md_request_id += 1
        chart_msg = (
            f"md/getChart\n{req_id}\n\n"
            f"{{\"symbol\":\"{symbol or self.config.symbol}\","
            f"\"chartDescription\":{{\"underlyingType\":\"MinuteBar\",\"elementSize\":1,\"elementSizeUnit\":\"UnderlyingUnits\"}},"
            f"\"timeRange\":{{\"asMuchAsElements\":200}}}}"
        )
        await self._md_ws.send_str(chart_msg)
        
        logger.info(f"Subscribed to market data: {symbol or self.config.symbol}")

    # ================================================================
    # ORDER WEBSOCKET
    # ================================================================
    async def _connect_order_ws(self) -> None:
        """Connect to order/execution WebSocket."""
        while self._connected:
            try:
                self._order_ws = await self._session.ws_connect(
                    self.config.order_ws_url,
                    headers={"Authorization": f"Bearer {self._access_token}"},
                )
                logger.info("Order WebSocket connected")

                await self._order_ws.send_str(f"authorize\n0\n\n{self._access_token}")

                async for msg in self._order_ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_order_message(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        break

            except Exception as e:
                logger.error(f"Order WebSocket error: {e}")

            if self._connected:
                await asyncio.sleep(self.config.reconnect_delay_seconds)

    async def _handle_order_message(self, raw: str) -> None:
        """Handle order/fill WebSocket messages."""
        parts = raw.split("\n", 3)
        if len(parts) < 4:
            return

        event_type = parts[0]
        body = parts[3] if len(parts) > 3 else ""

        try:
            if not body:
                return
            data = json.loads(body)
        except json.JSONDecodeError:
            return

        # Fill events
        if "fill" in event_type.lower() or (isinstance(data, dict) and data.get("entityType") == "fill"):
            fill = TradovateFill(
                order_id=data.get("orderId", 0),
                contract_id=data.get("contractId", 0),
                fill_price=data.get("price", 0.0),
                filled_qty=data.get("qty", 0),
                action=data.get("action", ""),
            )
            logger.info(f"FILL: {fill.action} {fill.filled_qty}x @ {fill.fill_price}")
            if self._on_fill:
                await self._on_fill(fill)

        # Position updates
        if isinstance(data, dict) and data.get("entityType") == "position":
            self._position = data
            if self._on_position_update:
                await self._on_position_update(data)

    # ================================================================
    # ORDER PLACEMENT
    # ================================================================
    async def place_order(
        self,
        action: str,           # "Buy" or "Sell"
        qty: int,
        order_type: str = "Market",
        price: float = 0.0,
        stop_price: float = 0.0,
        bracket: bool = False,
        take_profit: float = 0.0,
        stop_loss: float = 0.0,
    ) -> Optional[dict]:
        """
        Place an order via Tradovate REST API.
        
        For the scale-out strategy, we place 2 separate orders
        (or use OCO/bracket) so each contract has its own exit.
        """
        if not self._account:
            logger.error("No account — cannot place order")
            return None

        payload = {
            "accountSpec": self._account.name,
            "accountId": self._account.account_id,
            "action": action,
            "symbol": self.config.symbol,
            "orderQty": qty,
            "orderType": order_type,
            "isAutomated": True,
        }

        if order_type == "Limit" and price > 0:
            payload["price"] = price
        if order_type in ("Stop", "StopLimit") and stop_price > 0:
            payload["stopPrice"] = stop_price

        endpoint = "/order/placeOrder"

        # If bracket order (entry + TP + SL)
        if bracket and take_profit > 0 and stop_loss > 0:
            endpoint = "/order/placeOSO"
            # OSO = Order Sends Order (bracket)
            payload["bracket1"] = {
                "action": "Sell" if action == "Buy" else "Buy",
                "orderType": "Limit",
                "price": take_profit,
            }
            payload["bracket2"] = {
                "action": "Sell" if action == "Buy" else "Buy",
                "orderType": "Stop",
                "stopPrice": stop_loss,
            }

        result = await self._post(endpoint, payload)
        if result:
            order_id = result.get("orderId", result.get("id", "?"))
            logger.info(f"Order placed: {action} {qty}x {self.config.symbol} [{order_type}] -> ID {order_id}")
        return result

    async def place_scale_out_entry(
        self,
        direction: str,       # "long" or "short"
        c1_target: float,     # Contract 1 take-profit price
        c2_initial_stop: float,  # Both contracts' initial stop
        entry_price: float = 0.0,  # For limit orders
    ) -> dict:
        """
        Place the 2-contract scale-out entry.
        
        Strategy:
        - Contract 1: Market entry + Bracket (TP at c1_target, SL at stop)
        - Contract 2: Market entry + Stop only (SL at stop, no TP yet)
        
        After C1 fills its target, we modify C2's stop to breakeven+1.
        """
        action = "Buy" if direction == "long" else "Sell"
        exit_action = "Sell" if direction == "long" else "Buy"

        results = {"c1_order": None, "c2_order": None, "success": False}

        # Contract 1 — Bracket order (entry + TP + SL)
        c1 = await self.place_order(
            action=action,
            qty=1,
            order_type="Market",
            bracket=True,
            take_profit=c1_target,
            stop_loss=c2_initial_stop,
        )
        results["c1_order"] = c1

        # Contract 2 — Entry + Stop only (no TP, we manage trailing manually)
        c2 = await self.place_order(
            action=action,
            qty=1,
            order_type="Market",
            bracket=True,
            take_profit=0,  # No fixed TP — we trail this one
            stop_loss=c2_initial_stop,
        )
        results["c2_order"] = c2

        results["success"] = bool(c1 and c2)
        return results

    async def modify_stop(self, order_id: int, new_stop_price: float) -> Optional[dict]:
        """Modify an existing stop order price (for trailing / breakeven)."""
        payload = {
            "orderId": order_id,
            "stopPrice": new_stop_price,
        }
        return await self._post("/order/modifyOrder", payload)

    async def cancel_order(self, order_id: int) -> Optional[dict]:
        """Cancel an open order."""
        return await self._post("/order/cancelOrder", {"orderId": order_id})

    async def flatten_position(self) -> Optional[dict]:
        """Emergency flatten — close all positions immediately."""
        if not self._account:
            return None
        logger.critical("FLATTEN: Closing all positions immediately")
        return await self._post("/order/liquidatePosition", {
            "accountId": self._account.account_id,
            "contractId": self._contract_id,
        })

    # ================================================================
    # HISTORICAL DATA
    # ================================================================
    async def get_historical_bars(
        self,
        symbol: str = None,
        bar_size: int = 1,       # Minutes
        num_bars: int = 2000,
    ) -> List[dict]:
        """
        Fetch historical bars from Tradovate.
        Returns list of OHLCV bar dicts.
        """
        payload = {
            "symbol": symbol or self.config.symbol,
            "chartDescription": {
                "underlyingType": "MinuteBar",
                "elementSize": bar_size,
                "elementSizeUnit": "UnderlyingUnits",
            },
            "timeRange": {
                "asMuchAsElements": num_bars,
            },
        }

        # Historical chart data comes through WebSocket, not REST
        # We send the request and collect the response
        # For simplicity, use REST tick chart endpoint if available
        data = await self._post("/md/getChart", payload)
        if data and "bars" in data:
            return data["bars"]
        
        logger.warning("Historical bar fetch returned no data — use CSV import fallback")
        return []

    # ================================================================
    # CALLBACKS
    # ================================================================
    def on_quote(self, callback: Callable):
        """Register callback for real-time quote updates."""
        self._on_quote = callback

    def on_bar(self, callback: Callable):
        """Register callback for new bar completion."""
        self._on_bar = callback

    def on_fill(self, callback: Callable):
        """Register callback for order fills."""
        self._on_fill = callback

    def on_position_update(self, callback: Callable):
        """Register callback for position changes."""
        self._on_position_update = callback

    # ================================================================
    # STATUS
    # ================================================================
    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def account(self) -> Optional[TradovateAccount]:
        return self._account

    def get_status(self) -> dict:
        return {
            "connected": self._connected,
            "environment": self.config.environment,
            "symbol": self.config.symbol,
            "contract_id": self._contract_id,
            "account_id": self._account.account_id if self._account else 0,
            "account_name": self._account.name if self._account else "",
            "md_ws_connected": self._md_ws is not None and not self._md_ws.closed if self._md_ws else False,
            "order_ws_connected": self._order_ws is not None and not self._order_ws.closed if self._order_ws else False,
        }
