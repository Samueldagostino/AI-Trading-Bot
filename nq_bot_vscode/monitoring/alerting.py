"""
Real-Time Alerting System
==========================
Institutional-grade async alert management with multiple notification channels.

Architecture:
- AlertManager: Central coordinator (singleton pattern)
- NotificationChannel: Abstract base for different channels
- DiscordWebhookChannel: Rich embeds via Discord webhook
- TelegramChannel: Formatted messages via Telegram Bot API
- ConsoleChannel: Fallback structured logging

Features:
- Alert severity levels: INFO, WARNING, CRITICAL, EMERGENCY
- Rate limiting (1 alert per type per 5 min, except EMERGENCY)
- Async queue with retry logic (3 attempts, exponential backoff)
- Non-blocking: alerts queue independently, never block trading loop
"""

import asyncio
import logging
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional, Dict, List, Callable
from collections import defaultdict

import aiohttp

logger = logging.getLogger(__name__)


class AlertSeverity(Enum):
    """Alert severity levels."""
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    EMERGENCY = "EMERGENCY"

    def to_color(self) -> int:
        """Convert to Discord embed color."""
        colors = {
            self.INFO: 0x0099ff,        # Blue
            self.WARNING: 0xffaa00,     # Orange
            self.CRITICAL: 0xff3333,    # Red
            self.EMERGENCY: 0x990000,   # Dark Red
        }
        return colors.get(self, 0x0099ff)


@dataclass
class Alert:
    """Single alert message."""
    event_type: str              # e.g., "kill_switch", "drawdown_warning"
    severity: AlertSeverity
    title: str                   # Short title
    message: str                 # Main message
    data: Dict = field(default_factory=dict)  # Additional context
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __hash__(self):
        """Hash by event_type for rate limiting."""
        return hash(self.event_type)


class NotificationChannel(ABC):
    """Abstract base for notification channels."""

    @abstractmethod
    async def send(self, alert: Alert) -> bool:
        """
        Send an alert.

        Args:
            alert: Alert to send

        Returns:
            True if successful, False otherwise
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """
        Check if channel is healthy and ready.

        Returns:
            True if healthy, False otherwise
        """
        pass


class ConsoleChannel(NotificationChannel):
    """Fallback console logging channel."""

    async def send(self, alert: Alert) -> bool:
        """Log alert to console."""
        try:
            emoji = {
                AlertSeverity.INFO: "ℹ",
                AlertSeverity.WARNING: "⚠",
                AlertSeverity.CRITICAL: "🔴",
                AlertSeverity.EMERGENCY: "🚨",
            }.get(alert.severity, "•")

            log_msg = (
                f"{emoji} [{alert.severity.value}] {alert.title}\n"
                f"   {alert.message}"
            )

            if alert.data:
                log_msg += f"\n   Data: {alert.data}"

            if alert.severity == AlertSeverity.CRITICAL:
                logger.critical(log_msg)
            elif alert.severity == AlertSeverity.EMERGENCY:
                logger.critical(log_msg)
            elif alert.severity == AlertSeverity.WARNING:
                logger.warning(log_msg)
            else:
                logger.info(log_msg)

            return True
        except Exception as e:
            logger.error(f"ConsoleChannel error: {e}")
            return False

    async def health_check(self) -> bool:
        """Console is always available."""
        return True


class DiscordWebhookChannel(NotificationChannel):
    """Discord webhook notifications with rich embeds."""

    def __init__(self, webhook_url: str):
        """
        Initialize Discord channel.

        Args:
            webhook_url: Discord webhook URL
        """
        self.webhook_url = webhook_url
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send(self, alert: Alert) -> bool:
        """
        Send alert via Discord webhook.

        Implements exponential backoff retry (3 attempts).
        """
        if not self.webhook_url:
            logger.debug("Discord webhook URL not configured")
            return False

        payload = self._build_embed(alert)

        for attempt in range(3):
            try:
                session = await self._ensure_session()
                async with session.post(
                    self.webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 204:  # Discord returns 204 on success
                        logger.debug(f"Alert sent to Discord: {alert.event_type}")
                        return True
                    else:
                        logger.warning(
                            f"Discord webhook returned {resp.status}: "
                            f"{await resp.text()}"
                        )

            except asyncio.TimeoutError:
                logger.warning(f"Discord timeout (attempt {attempt + 1}/3)")
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
            except aiohttp.ClientError as e:
                logger.warning(f"Discord error (attempt {attempt + 1}/3): {e}")
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Unexpected error sending to Discord: {e}")
                return False

        logger.error(f"Failed to send alert to Discord after 3 attempts")
        return False

    def _build_embed(self, alert: Alert) -> Dict:
        """Build Discord embed JSON."""
        # Truncate message if too long (Discord limit: 4096 chars)
        message = alert.message
        if len(message) > 2000:
            message = message[:1997] + "..."

        embed = {
            "title": alert.title,
            "description": message,
            "color": alert.severity.to_color(),
            "timestamp": alert.timestamp.isoformat(),
            "fields": [],
        }

        if alert.data:
            # Add data as fields (max 25 fields per embed)
            for key, value in list(alert.data.items())[:20]:
                embed["fields"].append({
                    "name": str(key),
                    "value": str(value)[:1024],  # Field value limit
                    "inline": True,
                })

        return {"embeds": [embed]}

    async def health_check(self) -> bool:
        """Test webhook connectivity."""
        if not self.webhook_url:
            return False

        try:
            session = await self._ensure_session()
            # Send minimal test (Discord allows GET on webhook)
            async with session.get(
                self.webhook_url,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return resp.status in (200, 204)
        except Exception as e:
            logger.debug(f"Discord health check failed: {e}")
            return False

    async def close(self):
        """Clean up session."""
        if self._session and not self._session.closed:
            await self._session.close()


class TelegramChannel(NotificationChannel):
    """Telegram Bot API notifications."""

    def __init__(self, bot_token: str, chat_id: str):
        """
        Initialize Telegram channel.

        Args:
            bot_token: Telegram bot token
            chat_id: Chat ID to send to
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send(self, alert: Alert) -> bool:
        """
        Send alert via Telegram.

        Implements exponential backoff retry (3 attempts).
        """
        if not self.bot_token or not self.chat_id:
            logger.debug("Telegram credentials not configured")
            return False

        text = self._format_message(alert)

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        for attempt in range(3):
            try:
                session = await self._ensure_session()
                async with session.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("ok"):
                            logger.debug(f"Alert sent to Telegram: {alert.event_type}")
                            return True
                    else:
                        logger.warning(f"Telegram API returned {resp.status}")

            except asyncio.TimeoutError:
                logger.warning(f"Telegram timeout (attempt {attempt + 1}/3)")
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            except aiohttp.ClientError as e:
                logger.warning(f"Telegram error (attempt {attempt + 1}/3): {e}")
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Unexpected error sending to Telegram: {e}")
                return False

        logger.error(f"Failed to send alert to Telegram after 3 attempts")
        return False

    def _format_message(self, alert: Alert) -> str:
        """Format alert as Telegram HTML message."""
        severity_emoji = {
            AlertSeverity.INFO: "ℹ️",
            AlertSeverity.WARNING: "⚠️",
            AlertSeverity.CRITICAL: "🔴",
            AlertSeverity.EMERGENCY: "🚨",
        }.get(alert.severity, "•")

        lines = [
            f"<b>{severity_emoji} {alert.severity.value}</b>",
            f"<b>{alert.title}</b>",
            alert.message,
        ]

        if alert.data:
            lines.append("\n<b>Details:</b>")
            for key, value in alert.data.items():
                # Escape HTML entities
                safe_value = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                lines.append(f"• <code>{key}</code>: {safe_value}")

        # Truncate if over Telegram limit (4096 chars)
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3997] + "..."

        return text

    async def health_check(self) -> bool:
        """Test Telegram connectivity."""
        if not self.bot_token or not self.chat_id:
            return False

        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/getMe"
            session = await self._ensure_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("ok", False)
                return False
        except Exception as e:
            logger.debug(f"Telegram health check failed: {e}")
            return False

    async def close(self):
        """Clean up session."""
        if self._session and not self._session.closed:
            await self._session.close()


class AlertManager:
    """
    Central alert management engine.

    Handles:
    - Rate limiting (max 1 per event type per X seconds, except EMERGENCY)
    - Async queue (background task)
    - Retry logic with exponential backoff
    - Channel routing (console always on, others configurable)
    """

    def __init__(
        self,
        config,
        enabled_channels: Optional[List[str]] = None,
        rate_limit_seconds: int = 300,  # 5 minutes default
    ):
        """
        Initialize AlertManager.

        Args:
            config: AlertConfig from settings
            enabled_channels: List of channel names to enable
            rate_limit_seconds: Rate limit window (default 300s = 5 min)
        """
        self.config = config
        self.rate_limit_seconds = rate_limit_seconds

        # Initialize channels
        self.channels: Dict[str, NotificationChannel] = {
            "console": ConsoleChannel(),
        }

        if enabled_channels is None:
            enabled_channels = config.enabled_channels or ["console"]

        if "discord" in enabled_channels and config.discord_webhook_url:
            self.channels["discord"] = DiscordWebhookChannel(
                config.discord_webhook_url
            )

        if "telegram" in enabled_channels and config.telegram_bot_token:
            self.channels["telegram"] = TelegramChannel(
                config.telegram_bot_token,
                config.telegram_chat_id,
            )

        # Rate limiting: event_type -> last_sent_timestamp
        self._last_sent: Dict[str, datetime] = {}

        # Alert queue
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        """Start background alert worker."""
        if self._running:
            logger.warning("AlertManager already running")
            return

        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info(f"AlertManager started with channels: {list(self.channels.keys())}")

    async def stop(self):
        """Stop background alert worker."""
        self._running = False
        if self._worker_task:
            await self._worker_task

        # Close Discord/Telegram sessions
        if "discord" in self.channels:
            await self.channels["discord"].close()
        if "telegram" in self.channels:
            await self.channels["telegram"].close()

        logger.info("AlertManager stopped")

    async def _worker_loop(self):
        """Background task that processes alert queue."""
        logger.debug("Alert worker started")

        try:
            while self._running:
                try:
                    alert = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                    await self._send_alert(alert)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error(f"Error in alert worker: {e}")

        finally:
            logger.debug("Alert worker stopped")

    async def _send_alert(self, alert: Alert):
        """Send alert to all active channels."""
        # Check rate limit (skip for EMERGENCY)
        if alert.severity != AlertSeverity.EMERGENCY:
            if not self._check_rate_limit(alert.event_type):
                logger.debug(
                    f"Alert '{alert.event_type}' rate-limited, skipping send"
                )
                return

        # Record send time
        self._last_sent[alert.event_type] = datetime.now(timezone.utc)

        # Send to all channels concurrently
        tasks = []
        for channel_name, channel in self.channels.items():
            tasks.append(self._send_to_channel(channel_name, channel, alert))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Log results
        for (channel_name, _), result in zip(self.channels.items(), results):
            if isinstance(result, Exception):
                logger.error(f"Channel {channel_name} error: {result}")
            elif result:
                logger.debug(f"Channel {channel_name} sent alert")

    async def _send_to_channel(
        self,
        channel_name: str,
        channel: NotificationChannel,
        alert: Alert,
    ) -> bool:
        """Send alert to a single channel with error handling."""
        try:
            return await channel.send(alert)
        except Exception as e:
            logger.error(f"Error sending to {channel_name}: {e}")
            return False

    def _check_rate_limit(self, event_type: str) -> bool:
        """
        Check if an alert type should be sent based on rate limit.

        Returns:
            True if should send, False if rate-limited
        """
        now = datetime.now(timezone.utc)
        last_sent = self._last_sent.get(event_type)

        if last_sent is None:
            return True

        elapsed = (now - last_sent).total_seconds()
        return elapsed >= self.rate_limit_seconds

    def enqueue(self, alert: Alert) -> None:
        """
        Queue an alert for sending (non-blocking).

        This is the primary method to use from trading loop.
        Never blocks, always returns immediately.
        """
        if not self._running:
            logger.warning("AlertManager not running, alert dropped")
            return

        try:
            self._queue.put_nowait(alert)
        except asyncio.QueueFull:
            logger.warning(f"Alert queue full, dropping: {alert.event_type}")

    async def send_immediate(self, alert: Alert) -> bool:
        """
        Send an alert synchronously (for critical paths).

        Use sparingly - blocks the calling coroutine.
        """
        if alert.severity != AlertSeverity.EMERGENCY:
            if not self._check_rate_limit(alert.event_type):
                logger.debug(f"Alert '{alert.event_type}' rate-limited")
                return False

        self._last_sent[alert.event_type] = datetime.now(timezone.utc)
        await self._send_alert(alert)
        return True

    async def health_check(self) -> Dict[str, bool]:
        """
        Check health of all channels.

        Returns:
            Dict mapping channel name -> health status
        """
        results = {}
        for channel_name, channel in self.channels.items():
            try:
                results[channel_name] = await channel.health_check()
            except Exception as e:
                logger.error(f"Health check failed for {channel_name}: {e}")
                results[channel_name] = False
        return results


# Singleton instance (created in main orchestrator)
_alert_manager: Optional[AlertManager] = None


def get_alert_manager() -> Optional[AlertManager]:
    """Get the global alert manager instance."""
    return _alert_manager


def set_alert_manager(manager: AlertManager) -> None:
    """Set the global alert manager instance."""
    global _alert_manager
    _alert_manager = manager
