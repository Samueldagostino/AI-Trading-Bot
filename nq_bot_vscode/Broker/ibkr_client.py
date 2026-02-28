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
import logging
import os
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Callable, Any

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
            return round(float(value), 2)
        if isinstance(value, str):
            # IBKR sometimes prefixes with 'C' for closing price
            cleaned = value.lstrip("C").strip()
            try:
                return round(float(cleaned), 2)
            except (ValueError, TypeError):
                return 0.0
        return 0.0
