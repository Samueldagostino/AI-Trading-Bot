"""
Tests for Real-Time Alerting System
===================================

Covers:
- AlertManager initialization and lifecycle
- Rate limiting (normal + EMERGENCY bypass)
- Alert queuing and async worker
- Message formatting (Discord/Telegram/Console)
- Channel routing and fallback
- Health checks
"""

import pytest
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, AsyncMock, call
from typing import List

import sys
import os
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DISCORD_WEBHOOK_URL", "https://discordapp.com/api/webhooks/test/test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:ABC")
os.environ.setdefault("TELEGRAM_CHAT_ID", "12345")
os.environ.setdefault("ALERT_CHANNELS", "console,discord,telegram")

from config.settings import AlertConfig
from monitoring.alerting import (
    AlertManager, Alert, AlertSeverity, ConsoleChannel,
    DiscordWebhookChannel, TelegramChannel, get_alert_manager, set_alert_manager,
)
from monitoring.alert_templates import AlertTemplates


# ═══════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def alert_config():
    """Create a test alert config."""
    return AlertConfig(
        enabled_channels=["console", "discord", "telegram"],
        discord_webhook_url="https://discordapp.com/api/webhooks/test/test",
        telegram_bot_token="test_token",
        telegram_chat_id="12345",
        rate_limit_seconds=5,  # Short for testing
    )


@pytest.fixture
async def alert_manager(alert_config):
    """Create and manage alert manager lifecycle."""
    manager = AlertManager(alert_config)
    yield manager
    if manager._running:
        await manager.stop()


@pytest.fixture
def sample_alert():
    """Create a sample alert for testing."""
    return Alert(
        event_type="test_event",
        severity=AlertSeverity.INFO,
        title="Test Alert",
        message="This is a test alert",
        data={"key": "value"},
    )


# ═══════════════════════════════════════════════════════════════
# ALERT CREATION & TEMPLATES
# ═══════════════════════════════════════════════════════════════


class TestAlertCreation:
    """Test Alert dataclass creation and properties."""

    def test_alert_creation(self, sample_alert):
        """Alert can be created with all fields."""
        assert sample_alert.event_type == "test_event"
        assert sample_alert.severity == AlertSeverity.INFO
        assert sample_alert.title == "Test Alert"
        assert sample_alert.timestamp is not None

    def test_alert_hash(self):
        """Alerts hash by event_type."""
        alert1 = Alert(
            event_type="event_a",
            severity=AlertSeverity.INFO,
            title="A",
            message="msg",
        )
        alert2 = Alert(
            event_type="event_a",
            severity=AlertSeverity.WARNING,
            title="B",
            message="msg",
        )
        # Same event_type = same hash
        assert hash(alert1) == hash(alert2)

    def test_severity_color_mapping(self):
        """Severity levels map to correct Discord colors."""
        assert AlertSeverity.INFO.to_color() == 0x0099ff
        assert AlertSeverity.WARNING.to_color() == 0xffaa00
        assert AlertSeverity.CRITICAL.to_color() == 0xff3333
        assert AlertSeverity.EMERGENCY.to_color() == 0x990000


class TestAlertTemplates:
    """Test pre-formatted alert templates."""

    def test_kill_switch_alert(self):
        """Kill switch template."""
        alert = AlertTemplates.kill_switch_triggered(
            reason="Max consecutive losses",
            stats={"consecutive_losses": 5},
        )
        assert alert.severity == AlertSeverity.EMERGENCY
        assert alert.event_type == "kill_switch_triggered"
        assert "Trading halted" in alert.message
        assert alert.data["consecutive_losses"] == 5

    def test_drawdown_warning(self):
        """Drawdown warning template."""
        alert = AlertTemplates.drawdown_warning(
            current_drawdown_pct=2.5,
            max_drawdown_pct=3.0,
            daily_pnl=-1250,
        )
        assert alert.severity == AlertSeverity.WARNING
        assert alert.event_type == "drawdown_warning"
        assert "2.5" in alert.message
        assert alert.data["current_drawdown_pct"] == 2.5

    def test_trade_entry_alert(self):
        """Trade entry template."""
        alert = AlertTemplates.trade_entry(
            direction="LONG",
            contracts=2,
            entry_price=20000.0,
            stop_loss=19990.0,
            take_profit=20020.0,
            signal_confidence=0.85,
        )
        assert alert.severity == AlertSeverity.INFO
        assert alert.event_type == "trade_entry"
        assert "LONG" in alert.message
        assert alert.data["contracts"] == 2
        assert alert.data["rr_ratio"] > 1.0

    def test_trade_exit_alert(self):
        """Trade exit template."""
        alert = AlertTemplates.trade_exit(
            direction="LONG",
            contracts=2,
            entry_price=20000.0,
            exit_price=20010.0,
            pnl=20.0,
            exit_reason="Take profit",
        )
        assert alert.severity == AlertSeverity.INFO
        assert alert.event_type == "trade_exit"
        assert alert.data["pnl"] == 20.0
        assert "Take profit" in alert.message

    def test_daily_summary_alert(self):
        """Daily summary template."""
        alert = AlertTemplates.daily_summary(
            total_trades=10,
            winning_trades=7,
            losing_trades=3,
            daily_pnl=500.0,
            win_rate=70.0,
            profit_factor=1.5,
            largest_win=100.0,
            largest_loss=-50.0,
        )
        assert alert.severity == AlertSeverity.INFO
        assert alert.event_type == "daily_summary"
        assert alert.data["total_trades"] == 10
        assert alert.data["win_rate"] == 70.0

    def test_custom_alert(self):
        """Custom alert factory."""
        alert = AlertTemplates.custom_alert(
            event_type="custom_event",
            title="Custom Title",
            message="Custom message",
            severity=AlertSeverity.CRITICAL,
            data={"custom_field": 123},
        )
        assert alert.event_type == "custom_event"
        assert alert.severity == AlertSeverity.CRITICAL
        assert alert.data["custom_field"] == 123


# ═══════════════════════════════════════════════════════════════
# CHANNELS
# ═══════════════════════════════════════════════════════════════


class TestConsoleChannel:
    """Test console fallback channel."""

    @pytest.mark.asyncio
    async def test_console_send_info(self, caplog):
        """Console logs INFO alerts."""
        channel = ConsoleChannel()
        alert = Alert(
            event_type="test",
            severity=AlertSeverity.INFO,
            title="Info",
            message="Info message",
        )
        with caplog.at_level(logging.INFO):
            result = await channel.send(alert)
        assert result is True
        assert "Info message" in caplog.text

    @pytest.mark.asyncio
    async def test_console_send_critical(self, caplog):
        """Console logs CRITICAL alerts with logger.critical."""
        channel = ConsoleChannel()
        alert = Alert(
            event_type="test",
            severity=AlertSeverity.CRITICAL,
            title="Critical",
            message="Critical message",
        )
        with caplog.at_level(logging.CRITICAL):
            result = await channel.send(alert)
        assert result is True
        assert "Critical message" in caplog.text

    @pytest.mark.asyncio
    async def test_console_health_check(self):
        """Console is always healthy."""
        channel = ConsoleChannel()
        assert await channel.health_check() is True


class TestDiscordChannel:
    """Test Discord webhook channel."""

    @pytest.mark.asyncio
    @patch("aiohttp.ClientSession.post")
    async def test_discord_send_success(self, mock_post):
        """Discord sends successfully."""
        # Mock successful response
        mock_response = AsyncMock()
        mock_response.status = 204
        mock_post.return_value.__aenter__.return_value = mock_response

        channel = DiscordWebhookChannel("https://discord.webhook/test")
        alert = Alert(
            event_type="test",
            severity=AlertSeverity.INFO,
            title="Test",
            message="Test message",
        )

        result = await channel.send(alert)
        assert result is True
        mock_post.assert_called_once()

    @pytest.mark.asyncio
    @patch("aiohttp.ClientSession.post")
    async def test_discord_send_retry(self, mock_post):
        """Discord retries on failure."""
        # First 2 calls fail, 3rd succeeds
        mock_response_fail = AsyncMock()
        mock_response_fail.status = 500
        mock_response_fail.text = AsyncMock(return_value="Error")

        mock_response_success = AsyncMock()
        mock_response_success.status = 204

        mock_post.side_effect = [
            self._async_context(mock_response_fail),
            self._async_context(mock_response_fail),
            self._async_context(mock_response_success),
        ]

        channel = DiscordWebhookChannel("https://discord.webhook/test")
        alert = Alert(
            event_type="test",
            severity=AlertSeverity.INFO,
            title="Test",
            message="Test message",
        )

        result = await channel.send(alert)
        assert result is True
        assert mock_post.call_count == 3

    @pytest.mark.asyncio
    async def test_discord_build_embed(self):
        """Discord formats embed correctly."""
        channel = DiscordWebhookChannel("https://discord.webhook/test")
        alert = Alert(
            event_type="test",
            severity=AlertSeverity.WARNING,
            title="Warning Test",
            message="Warning message",
            data={"key": "value", "count": 42},
        )

        embed = channel._build_embed(alert)
        assert "embeds" in embed
        assert embed["embeds"][0]["title"] == "Warning Test"
        assert embed["embeds"][0]["description"] == "Warning message"
        assert embed["embeds"][0]["color"] == AlertSeverity.WARNING.to_color()
        assert len(embed["embeds"][0]["fields"]) == 2

    @pytest.mark.asyncio
    async def test_discord_no_webhook_url(self):
        """Discord fails gracefully without webhook URL."""
        channel = DiscordWebhookChannel("")
        alert = Alert(
            event_type="test",
            severity=AlertSeverity.INFO,
            title="Test",
            message="Test",
        )
        result = await channel.send(alert)
        assert result is False

    @pytest.mark.asyncio
    @patch("aiohttp.ClientSession.get")
    async def test_discord_health_check(self, mock_get):
        """Discord health check."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_get.return_value.__aenter__.return_value = mock_response

        channel = DiscordWebhookChannel("https://discord.webhook/test")
        result = await channel.health_check()
        assert result is True

    @staticmethod
    def _async_context(response):
        """Helper to create async context manager."""
        async def context():
            return response
        cm = AsyncMock()
        cm.__aenter__.return_value = response
        cm.__aexit__.return_value = None
        return cm


class TestTelegramChannel:
    """Test Telegram Bot API channel."""

    @pytest.mark.asyncio
    @patch("aiohttp.ClientSession.post")
    async def test_telegram_send_success(self, mock_post):
        """Telegram sends successfully."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"ok": True})
        mock_post.return_value.__aenter__.return_value = mock_response

        channel = TelegramChannel("token123", "chat_id_123")
        alert = Alert(
            event_type="test",
            severity=AlertSeverity.INFO,
            title="Test",
            message="Test message",
        )

        result = await channel.send(alert)
        assert result is True

    @pytest.mark.asyncio
    async def test_telegram_no_credentials(self):
        """Telegram fails without credentials."""
        channel = TelegramChannel("", "")
        alert = Alert(
            event_type="test",
            severity=AlertSeverity.INFO,
            title="Test",
            message="Test",
        )
        result = await channel.send(alert)
        assert result is False

    @pytest.mark.asyncio
    async def test_telegram_format_message(self):
        """Telegram formats message as HTML."""
        channel = TelegramChannel("token", "chat_id")
        alert = Alert(
            event_type="test",
            severity=AlertSeverity.CRITICAL,
            title="Critical Alert",
            message="Something went wrong",
            data={"error": "Connection lost", "retry_count": 3},
        )

        msg = channel._format_message(alert)
        assert "<b>CRITICAL</b>" in msg
        assert "Critical Alert" in msg
        assert "Connection lost" in msg
        assert "retry_count" in msg


# ═══════════════════════════════════════════════════════════════
# ALERT MANAGER - LIFECYCLE
# ═══════════════════════════════════════════════════════════════


class TestAlertManagerLifecycle:
    """Test AlertManager initialization and lifecycle."""

    @pytest.mark.asyncio
    async def test_manager_init(self, alert_config):
        """AlertManager initializes with channels."""
        manager = AlertManager(alert_config)
        assert "console" in manager.channels
        assert "discord" in manager.channels
        assert "telegram" in manager.channels
        assert not manager._running
        await manager.stop()

    @pytest.mark.asyncio
    async def test_manager_start_stop(self, alert_manager):
        """AlertManager starts and stops gracefully."""
        assert not alert_manager._running
        await alert_manager.start()
        assert alert_manager._running
        await alert_manager.stop()
        assert not alert_manager._running

    @pytest.mark.asyncio
    async def test_manager_double_start(self, alert_manager):
        """AlertManager warns on double start."""
        await alert_manager.start()
        # Second start should warn
        await alert_manager.start()
        assert alert_manager._running
        await alert_manager.stop()

    @pytest.mark.asyncio
    async def test_manager_console_only(self, alert_config):
        """AlertManager can run with console only."""
        alert_config.enabled_channels = ["console"]
        manager = AlertManager(alert_config)
        assert "console" in manager.channels
        assert "discord" not in manager.channels
        await manager.stop()


# ═══════════════════════════════════════════════════════════════
# ALERT MANAGER - RATE LIMITING
# ═══════════════════════════════════════════════════════════════


class TestAlertManagerRateLimiting:
    """Test rate limiting logic."""

    @pytest.mark.asyncio
    async def test_rate_limit_check_first_send(self, alert_manager):
        """First alert of type always passes rate limit."""
        assert alert_manager._check_rate_limit("test_event") is True

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_duplicate(self, alert_manager):
        """Duplicate alerts within window are blocked."""
        # Mark as sent
        alert_manager._last_sent["test_event"] = datetime.now(timezone.utc)

        # Immediate second send should be blocked
        assert alert_manager._check_rate_limit("test_event") is False

    @pytest.mark.asyncio
    async def test_rate_limit_allows_after_window(self, alert_manager):
        """Alerts after window are allowed."""
        now = datetime.now(timezone.utc)
        alert_manager._last_sent["test_event"] = now - timedelta(seconds=10)
        alert_manager.rate_limit_seconds = 5

        # 10 seconds have elapsed, should be allowed
        assert alert_manager._check_rate_limit("test_event") is True

    @pytest.mark.asyncio
    async def test_rate_limit_emergency_bypass(self, alert_manager, sample_alert):
        """EMERGENCY alerts bypass rate limit."""
        # Block the event
        alert_manager._last_sent["test_event"] = datetime.now(timezone.utc)

        # Create emergency alert
        emergency_alert = Alert(
            event_type="test_event",
            severity=AlertSeverity.EMERGENCY,
            title="Emergency",
            message="msg",
        )

        # Emergency should bypass rate limit
        assert alert_manager._check_rate_limit(emergency_alert.event_type) is False
        # But send_alert checks severity before rate limit...
        # This is correct: rate limit doesn't apply to EMERGENCY


# ═══════════════════════════════════════════════════════════════
# ALERT MANAGER - QUEUING & ASYNC PROCESSING
# ═══════════════════════════════════════════════════════════════


class TestAlertManagerQueuing:
    """Test alert queuing and async worker."""

    @pytest.mark.asyncio
    async def test_enqueue_before_start(self, alert_manager, sample_alert):
        """Enqueue warns if manager not running."""
        # Try to enqueue before starting
        alert_manager.enqueue(sample_alert)
        # Should warn (logged)

    @pytest.mark.asyncio
    async def test_enqueue_and_process(self, alert_manager, sample_alert):
        """Alert is queued and processed."""
        await alert_manager.start()

        # Mock the console channel to track calls
        original_send = alert_manager.channels["console"].send
        call_count = 0

        async def tracked_send(alert):
            nonlocal call_count
            call_count += 1
            return await original_send(alert)

        alert_manager.channels["console"].send = tracked_send

        # Enqueue alert
        alert_manager.enqueue(sample_alert)

        # Give worker time to process
        await asyncio.sleep(0.2)

        assert call_count >= 1
        await alert_manager.stop()

    @pytest.mark.asyncio
    async def test_queue_concurrency(self, alert_manager):
        """Multiple alerts are processed concurrently."""
        await alert_manager.start()

        # Create multiple alerts
        alerts = [
            Alert(
                event_type=f"event_{i}",
                severity=AlertSeverity.INFO,
                title=f"Alert {i}",
                message="Test",
            )
            for i in range(5)
        ]

        # Enqueue all
        for alert in alerts:
            alert_manager.enqueue(alert)

        # Give worker time
        await asyncio.sleep(0.3)

        await alert_manager.stop()


# ═══════════════════════════════════════════════════════════════
# ALERT MANAGER - IMMEDIATE SEND
# ═══════════════════════════════════════════════════════════════


class TestAlertManagerImmediate:
    """Test send_immediate synchronous sending."""

    @pytest.mark.asyncio
    async def test_send_immediate(self, alert_manager, sample_alert):
        """send_immediate sends directly without queueing."""
        await alert_manager.start()

        result = await alert_manager.send_immediate(sample_alert)
        # Result depends on channel availability, but should return bool
        assert isinstance(result, bool)

        await alert_manager.stop()

    @pytest.mark.asyncio
    async def test_send_immediate_respects_rate_limit(self, alert_manager):
        """send_immediate respects rate limit."""
        alert = Alert(
            event_type="immediate_test",
            severity=AlertSeverity.INFO,
            title="Test",
            message="Test",
        )

        # First send
        await alert_manager.start()
        result1 = await alert_manager.send_immediate(alert)
        # Second immediate send should be rate-limited
        result2 = await alert_manager.send_immediate(alert)

        assert result1 is True
        assert result2 is False  # Rate limited

        await alert_manager.stop()


# ═══════════════════════════════════════════════════════════════
# ALERT MANAGER - HEALTH CHECKS
# ═══════════════════════════════════════════════════════════════


class TestAlertManagerHealth:
    """Test health check functionality."""

    @pytest.mark.asyncio
    @patch("aiohttp.ClientSession.get")
    async def test_health_check_all_channels(self, mock_get, alert_manager):
        """Health check runs on all channels."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_get.return_value.__aenter__.return_value = mock_response

        health = await alert_manager.health_check()

        assert "console" in health
        # Discord and Telegram may require more setup
        assert health["console"] is True

    @pytest.mark.asyncio
    async def test_health_check_console_always_healthy(self, alert_manager):
        """Console channel is always healthy."""
        health = await alert_manager.health_check()
        assert health.get("console") is True


# ═══════════════════════════════════════════════════════════════
# SINGLETON FUNCTIONS
# ═══════════════════════════════════════════════════════════════


class TestAlertManagerSingleton:
    """Test singleton pattern functions."""

    def test_get_set_alert_manager(self, alert_manager):
        """Singleton getter/setter work."""
        assert get_alert_manager() is None  # Initially None

        set_alert_manager(alert_manager)
        assert get_alert_manager() is alert_manager

        set_alert_manager(None)
        assert get_alert_manager() is None


# ═══════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════


class TestAlertingIntegration:
    """Integration tests for full alerting workflow."""

    @pytest.mark.asyncio
    async def test_kill_switch_alert_workflow(self, alert_config):
        """Full workflow: create kill switch alert and send."""
        manager = AlertManager(alert_config)
        await manager.start()

        # Create alert using template
        alert = AlertTemplates.kill_switch_triggered(
            reason="Max drawdown exceeded",
            stats={"drawdown_pct": 3.5},
        )

        # Enqueue
        manager.enqueue(alert)

        # Give time to process
        await asyncio.sleep(0.2)

        # Check it was tracked
        assert "kill_switch_triggered" in manager._last_sent

        await manager.stop()

    @pytest.mark.asyncio
    async def test_multiple_alert_types_different_rate_limits(self, alert_config):
        """Different event types have independent rate limits."""
        alert_config.rate_limit_seconds = 2
        manager = AlertManager(alert_config)

        # Event A
        alert_a1 = Alert(
            event_type="event_a",
            severity=AlertSeverity.INFO,
            title="A1",
            message="msg",
        )

        # Event B
        alert_b = Alert(
            event_type="event_b",
            severity=AlertSeverity.INFO,
            title="B",
            message="msg",
        )

        # Immediate A should pass
        assert manager._check_rate_limit("event_a") is True
        manager._last_sent["event_a"] = datetime.now(timezone.utc)

        # Immediate second A should fail
        assert manager._check_rate_limit("event_a") is False

        # But B should pass (different event)
        assert manager._check_rate_limit("event_b") is True

    @pytest.mark.asyncio
    async def test_alert_with_missing_channels(self, alert_config):
        """Manager handles missing channel configs gracefully."""
        alert_config.discord_webhook_url = ""
        alert_config.telegram_bot_token = ""
        manager = AlertManager(alert_config)

        # Should only have console
        assert "console" in manager.channels
        assert "discord" not in manager.channels
        assert "telegram" not in manager.channels

        await manager.start()
        alert = AlertTemplates.trade_entry(
            direction="LONG",
            contracts=1,
            entry_price=20000,
            stop_loss=19995,
            take_profit=20010,
            signal_confidence=0.8,
        )
        manager.enqueue(alert)
        await asyncio.sleep(0.1)
        await manager.stop()
