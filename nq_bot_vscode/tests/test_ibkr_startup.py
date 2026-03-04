"""
Tests for IBKR Startup Automation
====================================
Tests gateway check, startup checklist generation,
and graceful shutdown summary.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

# Ensure project path is available
script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))

from scripts.ibkr_startup import (
    check_gateway_status,
    StartupChecklist,
    IBKRStartupRunner,
)
from monitoring.trade_decision_logger import TradeDecisionLogger


# ================================================================
# GATEWAY CHECK TESTS
# ================================================================

class TestGatewayCheck:
    """Test gateway connectivity check with mock HTTP."""

    def test_gateway_not_running(self):
        """Test detection of gateway not running."""
        # Use a port that definitely isn't listening
        result = check_gateway_status("localhost", 59999)
        assert result["connected"] is False
        assert result["authenticated"] is False
        assert result["error"] is not None

    @patch("urllib.request.urlopen")
    def test_gateway_connected_authenticated(self, mock_urlopen):
        """Test gateway connected and authenticated."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "authenticated": True,
            "competing": False,
            "connected": True,
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = check_gateway_status("localhost", 5000)
        assert result["connected"] is True
        assert result["authenticated"] is True
        assert result["error"] is None

    @patch("urllib.request.urlopen")
    def test_gateway_connected_not_authenticated(self, mock_urlopen):
        """Test gateway connected but not authenticated."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "authenticated": False,
            "competing": False,
            "connected": True,
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = check_gateway_status("localhost", 5000)
        assert result["connected"] is True
        assert result["authenticated"] is False

    @patch("urllib.request.urlopen")
    def test_gateway_timeout(self, mock_urlopen):
        """Test gateway connection timeout."""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("timed out")

        result = check_gateway_status("localhost", 5000)
        assert result["connected"] is False
        assert result["error"] is not None


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
        checklist.add("IBKR Gateway", "OK", "Connected")
        checklist.add("Authentication", "OK", "Active")
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
        checklist.add("IBKR Gateway", "OK", "Connected")
        checklist.add("Authentication", "FAIL", "Not authenticated")

        assert checklist.all_ok is False

    def test_checklist_with_warning(self):
        """Test checklist treats warnings as OK."""
        checklist = StartupChecklist()
        checklist.add("IBKR Gateway", "OK", "Connected")
        checklist.add("VIX Data", "WARN", "Using fallback")

        assert checklist.all_ok is True

    def test_checklist_data_format(self):
        """Test checklist data returns correct format."""
        checklist = StartupChecklist()
        checklist.add("IBKR Gateway", "OK", "Connected")
        checklist.add("Auth", "FAIL", "Expired")

        data = checklist.get_checklist_data()
        assert len(data) == 2
        assert data[0]["label"] == "IBKR Gateway"
        assert data[0]["status"] == "OK"
        assert data[0]["detail"] == "Connected"
        assert data[1]["status"] == "FAIL"

    def test_print_checklist(self, capsys):
        """Test checklist prints correctly."""
        checklist = StartupChecklist()
        checklist.add("IBKR Gateway", "OK", "Connected")
        checklist.add("Authentication", "FAIL", "Not authenticated")

        checklist.print_checklist()

        captured = capsys.readouterr()
        assert "[OK] IBKR Gateway: Connected" in captured.out
        assert "[FAIL] Authentication: Not authenticated" in captured.out

    def test_full_startup_checklist_format(self, capsys):
        """Test the full startup checklist format matches spec."""
        checklist = StartupChecklist()
        checklist.add("IBKR Gateway", "OK", "Connected")
        checklist.add("Authentication", "OK", "Active")
        checklist.add("MNQ Data Feed", "OK", "Subscribed")
        checklist.add("HTF Engine", "OK", "Initialized")
        checklist.add("Modifier Engine", "OK", "4 modifiers loaded")
        checklist.add("Safety Rails", "OK", "Armed")
        checklist.add("Decision Logger", "OK", "Active")
        checklist.add("Paper Trading Mode", "OK", "ENABLED")

        checklist.print_checklist()

        captured = capsys.readouterr()
        assert "[OK] IBKR Gateway: Connected" in captured.out
        assert "[OK] Authentication: Active" in captured.out
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
        os.environ["IBKR_GATEWAY_HOST"] = "localhost"
        os.environ["IBKR_GATEWAY_PORT"] = "5000"

        runner = IBKRStartupRunner(
            dry_run=True,
            max_daily_loss=300.0,
            log_level="DEBUG",
        )

        assert runner._dry_run is True
        assert runner._max_daily_loss == 300.0
        assert runner._log_level == "DEBUG"
        assert runner.decision_logger is not None

    def test_runner_dry_run_checklist(self, tmp_path):
        """Test dry-run mode populates checklist correctly."""
        runner = IBKRStartupRunner(dry_run=True)

        # Run the initialize_engines step
        loop = asyncio.new_event_loop()
        loop.run_until_complete(runner._initialize_engines())
        loop.close()

        # Dry-run adds gateway/auth as skipped
        runner._checklist.add("IBKR Gateway", "OK", "Skipped (dry-run)")
        runner._checklist.add("Authentication", "OK", "Skipped (dry-run)")

        assert runner.checklist.all_ok is True
        data = runner.checklist.get_checklist_data()
        # Should have engines + gateway/auth
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
