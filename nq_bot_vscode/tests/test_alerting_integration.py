"""
Tests for alerting integration across the trading pipeline.
Verifies AlertManager wiring into RiskEngine, ScaleOutExecutor,
RegimeDetector, and TradingOrchestrator without modifying AlertManager
or AlertTemplates themselves.
"""

import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass, field
from typing import List, Optional

import pytest

# Add project paths
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "nq_bot_vscode"))

from monitoring.alerting import (
    AlertManager,
    Alert,
    AlertSeverity,
    set_alert_manager,
    get_alert_manager,
)
from monitoring.alert_templates import AlertTemplates
from config.settings import BotConfig, AlertConfig


# ── Helpers ──

class AlertCapture:
    """Captures alerts enqueued to AlertManager for test assertions."""

    def __init__(self):
        self.alerts: List[Alert] = []

    def enqueue(self, alert: Alert) -> None:
        self.alerts.append(alert)

    def find(self, event_type: str) -> Optional[Alert]:
        for a in self.alerts:
            if a.event_type == event_type:
                return a
        return None

    def count(self, event_type: str) -> int:
        return sum(1 for a in self.alerts if a.event_type == event_type)


@pytest.fixture
def alert_capture():
    """Install a capturing AlertManager mock into the global singleton."""
    capture = AlertCapture()
    mock_mgr = MagicMock()
    mock_mgr.enqueue = capture.enqueue
    set_alert_manager(mock_mgr)
    yield capture
    set_alert_manager(None)


@pytest.fixture
def bot_config():
    return BotConfig()


# ================================================================
# AlertConfig Tests
# ================================================================


class TestAlertConfig:

    def test_alert_config_exists_in_bot_config(self):
        """AlertConfig should be present in BotConfig."""
        config = BotConfig()
        assert hasattr(config, "alerting")
        assert isinstance(config.alerting, AlertConfig)

    def test_alert_config_defaults(self):
        """AlertConfig defaults should have console channel enabled."""
        config = AlertConfig()
        assert "console" in config.enabled_channels
        assert config.rate_limit_seconds == 300
        assert config.discord_webhook_url == "" or isinstance(config.discord_webhook_url, str)
        assert config.telegram_bot_token == "" or isinstance(config.telegram_bot_token, str)


# ================================================================
# RiskEngine Alert Integration Tests
# ================================================================


class TestRiskEngineAlerts:

    def test_kill_switch_fires_emergency_alert(self, alert_capture, bot_config):
        """_activate_kill_switch should enqueue an EMERGENCY alert."""
        from risk.engine import RiskEngine

        engine = RiskEngine(bot_config)
        engine._activate_kill_switch("Test kill switch", datetime.now(timezone.utc))

        alert = alert_capture.find("kill_switch_triggered")
        assert alert is not None
        assert alert.severity == AlertSeverity.EMERGENCY
        assert "Test kill switch" in alert.message

    def test_consecutive_loss_alert(self, alert_capture, bot_config):
        """3+ consecutive losses should fire a WARNING alert."""
        from risk.engine import RiskEngine

        engine = RiskEngine(bot_config)
        # Record 3 losses
        for _ in range(3):
            engine.record_trade_result(-50.0, "long")

        alert = alert_capture.find("consecutive_losses")
        assert alert is not None
        assert alert.severity == AlertSeverity.WARNING
        assert alert.data["consecutive_losses"] == 3

    def test_drawdown_warning_alert(self, alert_capture, bot_config):
        """Drawdown >= 50% of max should fire a WARNING alert."""
        from risk.engine import RiskEngine

        engine = RiskEngine(bot_config)
        # Simulate drawdown: reduce equity to trigger >= 5% drawdown (50% of 10% max)
        engine.state.peak_equity = 50000.0
        engine.state.current_equity = 47000.0  # 6% drawdown
        engine.record_trade_result(-100.0, "short")

        alert = alert_capture.find("drawdown_warning")
        assert alert is not None
        assert alert.severity == AlertSeverity.WARNING

    def test_no_alert_below_loss_streak_threshold(self, alert_capture, bot_config):
        """Fewer than 3 consecutive losses should NOT fire an alert."""
        from risk.engine import RiskEngine

        engine = RiskEngine(bot_config)
        engine.record_trade_result(-50.0, "long")
        engine.record_trade_result(-50.0, "long")

        alert = alert_capture.find("consecutive_losses")
        assert alert is None


# ================================================================
# ScaleOutExecutor Alert Integration Tests
# ================================================================


class TestScaleOutExecutorAlerts:

    @pytest.fixture
    def executor(self, bot_config):
        from execution.scale_out_executor import ScaleOutExecutor
        return ScaleOutExecutor(bot_config)

    @pytest.mark.asyncio
    async def test_entry_fires_trade_entry_alert(self, alert_capture, executor):
        """enter_trade should enqueue a trade_entry alert."""
        trade = await executor.enter_trade(
            direction="long",
            entry_price=21000.0,
            stop_distance=20.0,
            atr=15.0,
            signal_score=0.82,
            regime="ranging",
        )

        assert trade is not None
        alert = alert_capture.find("trade_entry")
        assert alert is not None
        assert alert.severity == AlertSeverity.INFO
        assert alert.data["direction"] == "long"
        assert abs(alert.data["entry_price"] - 21000.0) <= 1.0  # slippage tolerance

    @pytest.mark.asyncio
    async def test_trade_close_fires_exit_alert(self, alert_capture, executor):
        """Closing a trade (stop hit) should fire a trade_exit alert."""
        trade = await executor.enter_trade(
            direction="long",
            entry_price=21000.0,
            stop_distance=20.0,
            atr=15.0,
        )

        # Simulate stop hit
        result = await executor.update(
            current_price=20979.0,  # Below stop at 20980
            current_time=datetime.now(timezone.utc),
        )

        alert = alert_capture.find("trade_exit")
        assert alert is not None
        assert alert.severity == AlertSeverity.INFO
        assert "pnl" in alert.data

    @pytest.mark.asyncio
    async def test_c1_exit_fires_partial_exit_alert(self, alert_capture, executor):
        """C1 trail-from-profit exit should fire a partial_exit alert."""
        trade = await executor.enter_trade(
            direction="long",
            entry_price=21000.0,
            stop_distance=20.0,
            atr=15.0,
        )

        now = datetime.now(timezone.utc)
        # Move price up to activate trailing (>= 3pts profit)
        for i in range(5):
            await executor.update(21004.0 + i, now + timedelta(minutes=i * 2))

        # Now drop to trigger the trail stop (HWM was ~21008, trail at HWM - 2.5)
        await executor.update(21004.0, now + timedelta(minutes=12))

        alert = alert_capture.find("partial_exit")
        assert alert is not None
        assert alert.data["remaining_contracts"] == 1


# ================================================================
# RegimeDetector Alert Integration Tests
# ================================================================


class TestRegimeDetectorAlerts:

    @pytest.fixture
    def detector(self, bot_config):
        from risk.regime_detector import RegimeDetector
        return RegimeDetector(bot_config)

    def test_high_vix_fires_alert(self, alert_capture, detector):
        """VIX crossing above 25 should fire a high_vix alert."""
        detector.classify(
            current_atr=15.0,
            current_vix=30.0,
            trend_direction="up",
            trend_strength=0.3,
            current_volume=10000,
            avg_volume=10000.0,
            is_overnight=False,
            near_news_event=False,
        )

        alert = alert_capture.find("high_vix")
        assert alert is not None
        assert alert.severity == AlertSeverity.WARNING
        assert alert.data["current_vix"] == 30.0

    def test_regime_change_fires_alert(self, alert_capture, detector):
        """Regime change should fire a regime_change alert."""
        # First call: unknown -> ranging
        detector.classify(
            current_atr=10.0,
            current_vix=15.0,
            trend_direction="flat",
            trend_strength=0.1,
            current_volume=10000,
            avg_volume=10000.0,
            is_overnight=False,
            near_news_event=False,
        )

        alert = alert_capture.find("regime_change")
        assert alert is not None
        assert alert.data["new_regime"] == "ranging"

    def test_no_duplicate_vix_alert(self, alert_capture, detector):
        """High VIX alert should only fire once per crossing."""
        for _ in range(5):
            detector.classify(
                current_atr=15.0,
                current_vix=30.0,
                trend_direction="up",
                trend_strength=0.3,
                current_volume=10000,
                avg_volume=10000.0,
                is_overnight=False,
                near_news_event=False,
            )

        assert alert_capture.count("high_vix") == 1

    def test_vix_alert_resets_below_threshold(self, alert_capture, detector):
        """VIX alert should re-fire after VIX drops below 25 and rises again."""
        # First crossing
        detector.classify(
            current_atr=15.0, current_vix=30.0,
            trend_direction="up", trend_strength=0.3,
            current_volume=10000, avg_volume=10000.0,
            is_overnight=False, near_news_event=False,
        )
        # Drop below threshold
        detector.classify(
            current_atr=10.0, current_vix=20.0,
            trend_direction="up", trend_strength=0.6,
            current_volume=10000, avg_volume=10000.0,
            is_overnight=False, near_news_event=False,
        )
        # Cross above again
        detector.classify(
            current_atr=15.0, current_vix=28.0,
            trend_direction="up", trend_strength=0.3,
            current_volume=10000, avg_volume=10000.0,
            is_overnight=False, near_news_event=False,
        )

        assert alert_capture.count("high_vix") == 2


# ================================================================
# AlertManager Singleton Tests
# ================================================================


class TestAlertManagerSingleton:

    def test_set_and_get_alert_manager(self):
        """set_alert_manager / get_alert_manager round-trip."""
        mock = MagicMock()
        set_alert_manager(mock)
        assert get_alert_manager() is mock
        set_alert_manager(None)
        assert get_alert_manager() is None

    def test_enqueue_when_no_manager(self):
        """Modules should gracefully handle no alert manager."""
        set_alert_manager(None)
        mgr = get_alert_manager()
        # Should not crash — modules guard with `if mgr:`
        assert mgr is None


# ================================================================
# AlertTemplates Smoke Tests
# ================================================================


class TestAlertTemplatesSmoke:

    def test_all_templates_return_alert(self):
        """Every template method should return a valid Alert."""
        templates = [
            AlertTemplates.kill_switch_triggered("test"),
            AlertTemplates.connection_loss("IBKR"),
            AlertTemplates.system_error("test", "error"),
            AlertTemplates.drawdown_warning(5.0, 10.0, -500.0),
            AlertTemplates.consecutive_loss_streak(3, 50.0, 150.0),
            AlertTemplates.high_vix_alert(30.0, 40.0),
            AlertTemplates.trade_entry("long", 2, 21000.0, 20980.0, 21030.0, 0.85),
            AlertTemplates.trade_exit("long", 2, 21020.0, 21000.0, 40.0, "trail_stop"),
            AlertTemplates.partial_exit(1, 1, 21010.0, 20.0),
            AlertTemplates.daily_summary(10, 6, 4, 500.0, 60.0, 1.5, 200.0, -100.0),
            AlertTemplates.startup_complete("paper", "tradovate"),
            AlertTemplates.shutdown_initiated("test"),
            AlertTemplates.custom_alert("test", "Test", "message"),
        ]

        for alert in templates:
            assert isinstance(alert, Alert)
            assert isinstance(alert.severity, AlertSeverity)
            assert alert.event_type
            assert alert.title
