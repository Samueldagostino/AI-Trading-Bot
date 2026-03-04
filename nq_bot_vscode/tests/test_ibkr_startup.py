"""
Tests for IBKR Startup Automation (TWS API)
=============================================
Tests TWS connection check, startup checklist generation,
and graceful shutdown summary.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import pytest

# Ensure project path is available
script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))

from scripts.ibkr_startup import (
    check_tws_connection,
    StartupChecklist,
    IBKRStartupRunner,
)
from monitoring.trade_decision_logger import TradeDecisionLogger


# ================================================================
# TWS CONNECTION CHECK TESTS
# ================================================================

class TestTWSConnectionCheck:
    """Test TWS connectivity check."""

    def test_tws_not_running(self):
        """Test detection of TWS not running (connection refused on unused port)."""
        result = check_tws_connection("127.0.0.1", 59999, client_id=99)
        assert result["connected"] is False
        assert result["error"] is not None

    @patch("Broker.ibkr_client.IBKRClient")
    def test_tws_connected(self, MockClient):
        """Test TWS connected successfully."""
        mock_instance = MagicMock()
        mock_instance.connect = AsyncMock(return_value=True)
        mock_instance.disconnect = MagicMock()
        MockClient.return_value = mock_instance

        result = check_tws_connection("127.0.0.1", 7497)
        assert result["connected"] is True
        assert result["error"] is None

    @patch("Broker.ibkr_client.IBKRClient")
    def test_tws_connection_refused(self, MockClient):
        """Test TWS connection refused."""
        mock_instance = MagicMock()
        mock_instance.connect = AsyncMock(return_value=False)
        MockClient.return_value = mock_instance

        result = check_tws_connection("127.0.0.1", 7497)
        assert result["connected"] is False


# ================================================================
# STARTUP CHECKLIST TESTS
# ================================================================

class TestStartupChecklist:
    """Test startup checklist generation."""

    def test_empty_checklist(self):
        """Test empty checklist is all OK."""
        checklist = StartupChecklist()
        assert checklist.all_ok is True

    def test_all_ok_checklist(self):
        """Test checklist with all OK items."""
        checklist = StartupChecklist()
        checklist.add("TWS Connection", "OK", "Connected")
        checklist.add("Account", "OK", "Verified")
        checklist.add("MNQ Data Feed", "OK", "Subscribed")
        checklist.add("HTF Engine", "OK", "Initialized")
        checklist.add("Modifier Engine", "OK", "4 modifiers loaded")
        checklist.add("Safety Rails", "OK", "Armed")
        checklist.add("Decision Logger", "OK", "Active")
        checklist.add("Paper Trading Mode", "OK", "ENABLED")

        assert checklist.all_ok is True

    def test_checklist_with_failure(self):
        """Test checklist detects failure."""
        checklist = StartupChecklist()
        checklist.add("TWS Connection", "OK", "Connected")
        checklist.add("Account", "FAIL", "Not verified")

        assert checklist.all_ok is False

    def test_checklist_with_warning(self):
        """Test checklist treats warnings as OK."""
        checklist = StartupChecklist()
        checklist.add("TWS Connection", "OK", "Connected")
        checklist.add("VIX Data", "WARN", "Using fallback")

        assert checklist.all_ok is True

    def test_checklist_data_format(self):
        """Test checklist data returns correct format."""
        checklist = StartupChecklist()
        checklist.add("TWS Connection", "OK", "Connected")
        checklist.add("Auth", "FAIL", "Expired")

        data = checklist.get_checklist_data()
        assert len(data) == 2
        assert data[0]["label"] == "TWS Connection"
        assert data[0]["status"] == "OK"
        assert data[0]["detail"] == "Connected"
        assert data[1]["status"] == "FAIL"

    def test_print_checklist(self, capsys):
        """Test checklist prints correctly."""
        checklist = StartupChecklist()
        checklist.add("TWS Connection", "OK", "Connected")
        checklist.add("Account", "FAIL", "Not verified")

        checklist.print_checklist()

        captured = capsys.readouterr()
        assert "[OK] TWS Connection: Connected" in captured.out
        assert "[FAIL] Account: Not verified" in captured.out

    def test_full_startup_checklist_format(self, capsys):
        """Test the full startup checklist format matches spec."""
        checklist = StartupChecklist()
        checklist.add("TWS Connection", "OK", "Connected")
        checklist.add("Account", "OK", "Verified")
        checklist.add("MNQ Data Feed", "OK", "Subscribed")
        checklist.add("HTF Engine", "OK", "Initialized")
        checklist.add("Modifier Engine", "OK", "4 modifiers loaded")
        checklist.add("Safety Rails", "OK", "Armed")
        checklist.add("Decision Logger", "OK", "Active")
        checklist.add("Paper Trading Mode", "OK", "ENABLED")

        checklist.print_checklist()

        captured = capsys.readouterr()
        assert "[OK] TWS Connection: Connected" in captured.out
        assert "[OK] Account: Verified" in captured.out
        assert "[OK] MNQ Data Feed: Subscribed" in captured.out
        assert "[OK] HTF Engine: Initialized" in captured.out
        assert "[OK] Modifier Engine: 4 modifiers loaded" in captured.out
        assert "[OK] Safety Rails: Armed" in captured.out
        assert "[OK] Decision Logger: Active" in captured.out
        assert "[OK] Paper Trading Mode: ENABLED" in captured.out


# ================================================================
# IBKR STARTUP RUNNER TESTS
# ================================================================

class TestIBKRStartupRunner:
    """Test the IBKRStartupRunner."""

    def test_runner_initializes(self, tmp_path):
        """Test runner initializes with correct config."""
        runner = IBKRStartupRunner(
            dry_run=True,
            max_daily_loss=300.0,
            log_level="DEBUG",
            port=7497,
        )

        assert runner._dry_run is True
        assert runner._max_daily_loss == 300.0
        assert runner._log_level == "DEBUG"
        assert runner._tws_port == 7497
        assert runner.decision_logger is not None

    def test_runner_custom_port(self):
        """Test runner with IB Gateway port."""
        runner = IBKRStartupRunner(dry_run=True, port=4002)
        assert runner._tws_port == 4002

    def test_runner_dry_run_checklist(self, tmp_path):
        """Test dry-run mode populates checklist correctly."""
        runner = IBKRStartupRunner(dry_run=True)

        # Run the initialize_engines step
        loop = asyncio.new_event_loop()
        loop.run_until_complete(runner._initialize_engines())
        loop.close()

        # Dry-run adds TWS connection/account as skipped
        runner._checklist.add("TWS Connection", "OK", "Skipped (dry-run)")
        runner._checklist.add("Account", "OK", "Skipped (dry-run)")

        assert runner.checklist.all_ok is True
        data = runner.checklist.get_checklist_data()
        # Should have engines + connection/account
        assert len(data) >= 6


# ================================================================
# GRACEFUL SHUTDOWN SUMMARY TESTS
# ================================================================

class TestGracefulShutdown:
    """Test graceful shutdown summary."""

    def test_shutdown_prints_summary(self, tmp_path, capsys):
        """Test shutdown prints session summary."""
        runner = IBKRStartupRunner(dry_run=True)

        # Add some decisions
        runner.decision_logger.log_rejection(
            price_at_signal=21000.0,
            signal_direction="LONG",
            rejection_stage="HTF_GATE",
        )
        runner.decision_logger.log_approval(
            price_at_signal=21200.0,
            signal_direction="LONG",
            confluence_score=0.85,
        )

        # Run shutdown
        loop = asyncio.new_event_loop()
        loop.run_until_complete(runner._shutdown())
        loop.close()

        captured = capsys.readouterr()
        assert "SESSION COMPLETE" in captured.out
        assert "Total signals" in captured.out
        assert "Approved" in captured.out
        assert "Rejected" in captured.out

    def test_shutdown_writes_daily_summary(self, tmp_path):
        """Test shutdown writes daily summary file."""
        runner = IBKRStartupRunner(dry_run=True)

        runner.decision_logger.log_rejection(
            price_at_signal=21000.0,
            signal_direction="LONG",
            rejection_stage="HTF_GATE",
        )

        loop = asyncio.new_event_loop()
        loop.run_until_complete(runner._shutdown())
        loop.close()

        # Check daily summary was written
        summary_path = runner.decision_logger._daily_summary_path
        assert summary_path.exists()
