"""
Tradovate Demo Paper Trading Connector
========================================
Wraps the existing TradovateClient with paper-trading safety guards.

Safety rules (hard-coded, non-negotiable):
  1. ENVIRONMENT=demo — assertion blocks live trading
  2. Max position = 2 contracts — assertion enforced
  3. Daily loss limit = $500 — auto-halts trading
  4. Connection drop > 60s — flattens all positions and stops

This connector:
  - Authenticates to Tradovate demo API
  - Subscribes to real-time MNQ 1-minute bars via WebSocket
  - Aggregates 1m bars into 2m execution bars
  - Routes orders through TradovateClient (demo endpoint only)
  - Logs every order event to logs/paper_trades.json
  - Monitors connection health with 60s timeout

Does NOT modify the HC filter, HTF engine, or process_bar() pipeline.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Callable, Dict, List, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Resolve project paths
_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _THIS_DIR.parent
_LOGS_DIR = _PROJECT_DIR / "logs"


# ═══════════════════════════════════════════════════════════════
# SAFETY CONSTANTS — DO NOT CHANGE
# ═══════════════════════════════════════════════════════════════
ENVIRONMENT = "demo"
MAX_POSITION_CONTRACTS = 2
DAILY_LOSS_LIMIT_DOLLARS = 500.0
CONNECTION_TIMEOUT_SECONDS = 60
HEARTBEAT_INTERVAL_SECONDS = 10

# NQ session boundaries (all times in US/Eastern)
# Globex opens Sunday 6:00 PM ET, closes Friday 5:00 PM ET
# Daily maintenance: 5:00 PM – 6:00 PM ET
SESSION_OPEN_HOUR_ET = 18       # 6:00 PM ET
SESSION_OPEN_MINUTE_ET = 1      # 6:01 PM ET (skip first minute)
SESSION_CLOSE_HOUR_ET = 16      # 4:00 PM ET
SESSION_CLOSE_MINUTE_ET = 30    # 4:30 PM ET (flat before maintenance)
MAINTENANCE_START_HOUR_ET = 17  # 5:00 PM ET
MAINTENANCE_END_HOUR_ET = 18    # 6:00 PM ET


@dataclass
class PaperTradingState:
    """Tracks daily trading state for safety enforcement."""
    daily_pnl: float = 0.0
    daily_trades: int = 0
    daily_blocked: int = 0
    daily_loss_limit_hit: bool = False
    connection_lost_at: Optional[float] = None
    last_heartbeat: float = 0.0
    is_halted: bool = False
    halt_reason: str = ""
    session_date: str = ""

    # Bar aggregation state (1m → 2m)
    pending_1m_bar: Optional[Dict] = None
    bars_in_current_2m: int = 0
    current_2m_open: float = 0.0
    current_2m_high: float = 0.0
    current_2m_low: float = 0.0
    current_2m_volume: int = 0
    current_2m_start: Optional[datetime] = None


class TradovatePaperConnector:
    """
    Paper trading connector wrapping TradovateClient.

    Enforces demo-only operation, position limits, loss limits,
    and connection health monitoring.
    """

    def __init__(self, config):
        # ── SAFETY ASSERTION: DEMO ONLY ──
        assert config.tradovate.environment == ENVIRONMENT, (
            f"BLOCKED: TradovatePaperConnector requires environment='demo', "
            f"got '{config.tradovate.environment}'. "
            f"This connector is for paper trading ONLY."
        )

        self.config = config
        self.state = PaperTradingState()

        # Import here to avoid circular deps
        from Broker.tradovate_client import TradovateClient
        self._client = TradovateClient(config.tradovate)

        # Callbacks set by the paper runner
        self._on_2m_bar: Optional[Callable] = None
        self._on_fill: Optional[Callable] = None
        self._on_position_update: Optional[Callable] = None
        self._on_connection_lost: Optional[Callable] = None

        # Trade log
        self._trade_log: List[Dict] = []
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._trade_log_path = str(_LOGS_DIR / "paper_trades.json")

        # Connection monitor task
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False

    # ================================================================
    # CONNECTION LIFECYCLE
    # ================================================================
    async def connect(self) -> bool:
        """Connect to Tradovate demo, set up callbacks, start monitoring."""
        logger.info(f"Connecting to Tradovate DEMO ({self.config.tradovate.base_url})...")

        # Reset daily state
        self.state = PaperTradingState(
            last_heartbeat=time.time(),
            session_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )

        # Connect the underlying client
        success = await self._client.connect()
        if not success:
            logger.error("Failed to connect to Tradovate demo")
            return False

        # Verify we're on demo
        status = self._client.get_status()
        assert status["environment"] == "demo", (
            f"Connected to {status['environment']} instead of demo!"
        )

        # Register callbacks
        self._client.on_bar(self._handle_raw_bar)
        self._client.on_fill(self._handle_fill)
        self._client.on_position_update(self._handle_position_update)

        # Subscribe to market data
        await self._client.subscribe_market_data()

        # Start connection health monitor
        self._running = True
        self._monitor_task = asyncio.create_task(self._connection_monitor())

        logger.info("Paper trading connector ready")
        self._log_event("connection", {"status": "connected", "environment": "demo"})
        return True

    async def disconnect(self) -> None:
        """Graceful disconnect."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        await self._client.disconnect()
        self._flush_trade_log()
        logger.info("Paper trading connector disconnected")

    # ================================================================
    # BAR AGGREGATION (1m → 2m)
    # ================================================================
    async def _handle_raw_bar(self, data: Dict) -> None:
        """
        Handle incoming 1-minute bar from Tradovate WebSocket.

        Tradovate chart data format:
          {"charts": [{"id": ..., "td": ..., "bars": [{"timestamp": ..., "open": ..., ...}]}]}

        We aggregate two consecutive 1m bars into one 2m bar, then route
        to the registered callback for process_bar().
        """
        self.state.last_heartbeat = time.time()
        self.state.connection_lost_at = None

        # Parse bar data from Tradovate format
        bars = []
        if isinstance(data, dict):
            for chart in data.get("charts", [data]):
                for bar in chart.get("bars", []):
                    bars.append(bar)
                # Also handle flat bar format
                if "timestamp" in chart and "open" in chart:
                    bars.append(chart)

        if not bars:
            return

        for raw_bar in bars:
            await self._aggregate_1m_bar(raw_bar)

    async def _aggregate_1m_bar(self, raw: Dict) -> None:
        """Aggregate a single 1m bar into the current 2m window."""
        try:
            ts = raw.get("timestamp")
            if isinstance(ts, (int, float)):
                bar_time = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts, tz=timezone.utc)
            elif isinstance(ts, str):
                bar_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                return

            o = float(raw.get("open", 0))
            h = float(raw.get("high", 0))
            lo = float(raw.get("low", 0))
            c = float(raw.get("close", 0))
            v = int(raw.get("volume", raw.get("upVolume", 0)) or 0) + int(raw.get("downVolume", 0) or 0)

            if o <= 0 or h < lo:
                return
        except (ValueError, TypeError, KeyError):
            return

        s = self.state

        if s.bars_in_current_2m == 0:
            # First bar of the 2m window
            s.current_2m_open = o
            s.current_2m_high = h
            s.current_2m_low = lo
            s.current_2m_volume = v
            s.current_2m_start = bar_time
            s.bars_in_current_2m = 1
        else:
            # Second bar — complete the 2m bar
            s.current_2m_high = max(s.current_2m_high, h)
            s.current_2m_low = min(s.current_2m_low, lo)
            s.current_2m_volume += v
            s.bars_in_current_2m = 0

            bar_2m = {
                "timestamp": s.current_2m_start,
                "open": s.current_2m_open,
                "high": s.current_2m_high,
                "low": s.current_2m_low,
                "close": c,
                "volume": s.current_2m_volume,
            }

            if self._on_2m_bar:
                await self._on_2m_bar(bar_2m)

    # ================================================================
    # FILL / POSITION HANDLING
    # ================================================================
    async def _handle_fill(self, fill) -> None:
        """Handle order fill from Tradovate."""
        self._log_event("fill", {
            "order_id": fill.order_id,
            "price": fill.fill_price,
            "qty": fill.filled_qty,
            "action": fill.action,
            "time": datetime.now(timezone.utc).isoformat(),
        })

        if self._on_fill:
            await self._on_fill(fill)

    async def _handle_position_update(self, data: Dict) -> None:
        """Handle position update — enforce max contracts."""
        net_pos = abs(data.get("netPos", 0))
        if net_pos > MAX_POSITION_CONTRACTS:
            logger.critical(
                f"POSITION LIMIT BREACH: {net_pos} contracts > {MAX_POSITION_CONTRACTS} max. "
                f"Flattening immediately."
            )
            await self.emergency_flatten("position_limit_breach")

        if self._on_position_update:
            await self._on_position_update(data)

    # ================================================================
    # ORDER ROUTING (all go through TradovateClient to demo)
    # ================================================================
    async def place_scale_out_entry(
        self,
        direction: str,
        c2_initial_stop: float,
        entry_price: float = 0.0,
    ) -> Dict:
        """Place 2-contract entry via Tradovate demo. Enforces safety checks."""
        # ── SAFETY: Check daily loss limit ──
        if self.state.daily_loss_limit_hit:
            logger.warning("BLOCKED: Daily loss limit hit, no new entries")
            self._log_event("blocked", {"reason": "daily_loss_limit"})
            return {"success": False, "reason": "daily_loss_limit"}

        # ── SAFETY: Check halt state ──
        if self.state.is_halted:
            logger.warning(f"BLOCKED: Trading halted — {self.state.halt_reason}")
            self._log_event("blocked", {"reason": self.state.halt_reason})
            return {"success": False, "reason": self.state.halt_reason}

        # ── SAFETY: Max contracts assertion ──
        assert MAX_POSITION_CONTRACTS == 2, "Max position must be 2 contracts"

        self._log_event("entry_attempt", {
            "direction": direction,
            "c2_initial_stop": c2_initial_stop,
            "entry_price": entry_price,
        })

        result = await self._client.place_scale_out_entry(
            direction=direction,
            c2_initial_stop=c2_initial_stop,
            entry_price=entry_price,
        )

        self._log_event("entry_result", {
            "success": result.get("success", False),
            "c1_order": str(result.get("c1_order")),
            "c2_order": str(result.get("c2_order")),
        })

        if result.get("success"):
            self.state.daily_trades += 1

        return result

    async def modify_stop(self, order_id: int, new_stop_price: float) -> Optional[Dict]:
        """Modify a stop order via Tradovate demo."""
        self._log_event("modify_stop", {
            "order_id": order_id,
            "new_stop_price": new_stop_price,
        })
        return await self._client.modify_stop(order_id, new_stop_price)

    async def cancel_order(self, order_id: int) -> Optional[Dict]:
        """Cancel an order via Tradovate demo."""
        self._log_event("cancel_order", {"order_id": order_id})
        return await self._client.cancel_order(order_id)

    async def flatten_position(self) -> Optional[Dict]:
        """Flatten all positions via Tradovate demo."""
        self._log_event("flatten", {"reason": "requested"})
        return await self._client.flatten_position()

    async def emergency_flatten(self, reason: str) -> None:
        """Emergency flatten + halt trading."""
        logger.critical(f"EMERGENCY FLATTEN: {reason}")
        self.state.is_halted = True
        self.state.halt_reason = f"emergency: {reason}"
        self._log_event("emergency_flatten", {"reason": reason})

        # Retry flatten up to 3 times — positions MUST be closed
        for attempt in range(1, 4):
            try:
                await self._client.flatten_position()
                logger.info("Emergency flatten succeeded on attempt %d", attempt)
                return
            except Exception as e:
                logger.error(
                    "Emergency flatten attempt %d/3 failed: %s", attempt, e
                )
                if attempt < 3:
                    await asyncio.sleep(2.0)

        logger.critical(
            "EMERGENCY FLATTEN FAILED after 3 attempts — "
            "MANUAL INTERVENTION REQUIRED. Positions may still be open."
        )

    # ================================================================
    # DAILY PnL TRACKING
    # ================================================================
    def record_trade_pnl(self, pnl: float, trade_data: Dict = None) -> None:
        """Record a completed trade's PnL and check daily loss limit."""
        self.state.daily_pnl += pnl

        self._log_event("trade_closed", {
            "pnl": pnl,
            "daily_pnl": self.state.daily_pnl,
            "daily_trades": self.state.daily_trades,
            **(trade_data or {}),
        })

        if self.state.daily_pnl <= -DAILY_LOSS_LIMIT_DOLLARS:
            self.state.daily_loss_limit_hit = True
            logger.critical(
                f"DAILY LOSS LIMIT HIT: ${self.state.daily_pnl:.2f} "
                f"exceeds -${DAILY_LOSS_LIMIT_DOLLARS:.2f}. "
                f"No more trades today."
            )
            self._log_event("daily_loss_limit", {
                "daily_pnl": self.state.daily_pnl,
                "limit": DAILY_LOSS_LIMIT_DOLLARS,
            })

    def reset_daily_state(self) -> None:
        """Reset at start of new session day."""
        prev_pnl = self.state.daily_pnl
        prev_trades = self.state.daily_trades
        self.state.daily_pnl = 0.0
        self.state.daily_trades = 0
        self.state.daily_blocked = 0
        self.state.daily_loss_limit_hit = False
        self.state.session_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._log_event("daily_reset", {
            "previous_pnl": prev_pnl,
            "previous_trades": prev_trades,
        })

    # ================================================================
    # SESSION TIME CHECKS
    # ================================================================
    @staticmethod
    def get_et_now() -> datetime:
        """Get current time in US/Eastern (UTC-5 or UTC-4 for DST)."""
        utc_now = datetime.now(timezone.utc)
        # Simple EST offset (UTC-5). For DST, this would need pytz/zoneinfo.
        # Futures sessions are defined in ET.
        et_offset = timezone(timedelta(hours=-5))
        return utc_now.astimezone(et_offset)

    @staticmethod
    def is_within_session(et_time: datetime = None) -> bool:
        """Check if current ET time is within trading session.

        Session: 6:01 PM ET → 4:30 PM ET next day.
        Maintenance: 5:00 PM – 6:00 PM ET.
        """
        if et_time is None:
            et_time = TradovatePaperConnector.get_et_now()

        h, m = et_time.hour, et_time.minute

        # Maintenance window: 5:00 PM – 6:00 PM ET
        if h == MAINTENANCE_START_HOUR_ET:
            return False

        # After 6:01 PM ET (session open)
        if h == SESSION_OPEN_HOUR_ET and m >= SESSION_OPEN_MINUTE_ET:
            return True
        if h > SESSION_OPEN_HOUR_ET:
            return True

        # Before 4:30 PM ET (must be flat)
        if h < SESSION_CLOSE_HOUR_ET:
            return True
        if h == SESSION_CLOSE_HOUR_ET and m < SESSION_CLOSE_MINUTE_ET:
            return True

        return False

    @staticmethod
    def should_be_flat(et_time: datetime = None) -> bool:
        """Check if we should be flat (approaching close or maintenance)."""
        if et_time is None:
            et_time = TradovatePaperConnector.get_et_now()

        h, m = et_time.hour, et_time.minute

        # Flat by 4:30 PM ET
        if h == SESSION_CLOSE_HOUR_ET and m >= SESSION_CLOSE_MINUTE_ET:
            return True
        if h == MAINTENANCE_START_HOUR_ET:
            return True

        return False

    # ================================================================
    # CONNECTION HEALTH MONITOR
    # ================================================================
    async def _connection_monitor(self) -> None:
        """Monitor connection health. Flatten if disconnected > 60s."""
        while self._running:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

            if not self._running:
                break

            now = time.time()
            since_heartbeat = now - self.state.last_heartbeat

            # Check if client websockets are still alive
            status = self._client.get_status()
            md_ok = status.get("md_ws_connected", False)
            order_ok = status.get("order_ws_connected", False)

            if not md_ok or not order_ok:
                if self.state.connection_lost_at is None:
                    self.state.connection_lost_at = now
                    logger.warning(
                        f"Connection issue detected: md_ws={md_ok}, order_ws={order_ok}"
                    )
                    self._log_event("connection_warning", {
                        "md_ws": md_ok, "order_ws": order_ok,
                    })

                elapsed = now - self.state.connection_lost_at
                if elapsed > CONNECTION_TIMEOUT_SECONDS:
                    logger.critical(
                        f"Connection lost for {elapsed:.0f}s > {CONNECTION_TIMEOUT_SECONDS}s. "
                        f"Emergency flatten triggered."
                    )
                    await self.emergency_flatten("connection_timeout")
                    break
            else:
                if self.state.connection_lost_at is not None:
                    logger.info("Connection restored")
                    self._log_event("connection_restored", {})
                self.state.connection_lost_at = None

            # Check if no data received for a long time
            if since_heartbeat > CONNECTION_TIMEOUT_SECONDS:
                logger.critical(
                    f"No market data for {since_heartbeat:.0f}s. "
                    f"Emergency flatten triggered."
                )
                await self.emergency_flatten("no_data_timeout")
                break

    # ================================================================
    # LOGGING
    # ================================================================
    def _log_event(self, event_type: str, data: Dict) -> None:
        """Append an event to the trade log."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            **data,
        }
        self._trade_log.append(entry)

        # Flush periodically
        if len(self._trade_log) % 10 == 0:
            self._flush_trade_log()

    def _flush_trade_log(self) -> None:
        """Write trade log to disk."""
        if not self._trade_log:
            return

        try:
            # Read existing log
            existing = []
            if os.path.exists(self._trade_log_path):
                with open(self._trade_log_path, "r") as f:
                    existing = json.load(f)

            existing.extend(self._trade_log)

            with open(self._trade_log_path, "w") as f:
                json.dump(existing, f, indent=2, default=str)

            self._trade_log.clear()
        except Exception as e:
            logger.error(f"Failed to write trade log: {e}")

    # ================================================================
    # STATUS
    # ================================================================
    def get_status(self) -> Dict:
        """Get current paper trading status."""
        client_status = self._client.get_status()
        return {
            **client_status,
            "paper_trading": True,
            "daily_pnl": self.state.daily_pnl,
            "daily_trades": self.state.daily_trades,
            "daily_loss_limit_hit": self.state.daily_loss_limit_hit,
            "is_halted": self.state.is_halted,
            "halt_reason": self.state.halt_reason,
            "session_date": self.state.session_date,
            "within_session": self.is_within_session(),
        }

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected

    @property
    def account(self):
        return self._client.account
