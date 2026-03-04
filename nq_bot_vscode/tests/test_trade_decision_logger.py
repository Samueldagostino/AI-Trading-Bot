"""
Tests for TradeDecisionLogger
================================
Tests rejection logging, approval logging, session summary,
JSON append mode, and human-readable format output.
"""

import json
import os
import tempfile
import pytest
from pathlib import Path

from monitoring.trade_decision_logger import TradeDecisionLogger


@pytest.fixture
def log_dir(tmp_path):
    """Create a temporary log directory."""
    return str(tmp_path)


@pytest.fixture
def logger(log_dir):
    """Create a TradeDecisionLogger with temp directory."""
    return TradeDecisionLogger(log_dir)


# ================================================================
# REJECTION LOGGING
# ================================================================

class TestRejectionLogging:
    """Test rejection logging with full details."""

    def test_log_rejection_basic(self, logger, log_dir):
        """Test basic rejection entry is created correctly."""
        entry = logger.log_rejection(
            price_at_signal=21450.25,
            signal_direction="SHORT",
            rejection_stage="HTF_GATE",
            rejection_details={
                "htf_biases": {
                    "1D": "BULLISH",
                    "4H": "BULLISH",
                    "1H": "BEARISH",
                    "30m": "BEARISH",
                    "15m": "BEARISH",
                    "5m": "BEARISH",
                },
                "conflicting_timeframes": ["1D", "4H"],
                "confluence_score": None,
                "confluence_threshold": 0.75,
            },
        )

        assert entry["decision"] == "REJECTED"
        assert entry["signal_direction"] == "SHORT"
        assert entry["price_at_signal"] == 21450.25
        assert entry["rejection_stage"] == "HTF_GATE"
        assert entry["id"]  # UUID exists
        assert entry["timestamp"]  # Timestamp exists
        assert entry["what_would_have_happened"] is None

    def test_log_rejection_details(self, logger):
        """Test rejection details are populated correctly."""
        entry = logger.log_rejection(
            price_at_signal=21000.0,
            signal_direction="LONG",
            rejection_stage="CONFLUENCE",
            rejection_details={
                "htf_biases": {"1D": "BULLISH", "4H": "NEUTRAL"},
                "conflicting_timeframes": [],
                "confluence_score": 0.65,
                "confluence_threshold": 0.75,
                "modifier_values": {
                    "overnight": 1.2,
                    "fomc": 1.0,
                    "gamma": 0.85,
                    "volatility": 1.0,
                    "total": 1.02,
                },
            },
        )

        details = entry["rejection_details"]
        assert details["confluence_score"] == 0.65
        assert details["confluence_threshold"] == 0.75
        assert details["htf_biases"]["1D"] == "BULLISH"
        assert details["modifier_values"]["overnight"] == 1.2

    def test_log_rejection_modifier_standside(self, logger):
        """Test modifier stand-aside rejection."""
        entry = logger.log_rejection(
            price_at_signal=21200.0,
            signal_direction="LONG",
            rejection_stage="MODIFIER_STANDSIDE",
            rejection_details={
                "stand_aside_reason": "FOMC in 0.25h — stand aside",
                "modifier_values": {
                    "overnight": 1.0,
                    "fomc": 0.0,
                    "gamma": 1.0,
                    "volatility": 1.0,
                    "total": 0.0,
                },
            },
        )

        assert entry["rejection_stage"] == "MODIFIER_STANDSIDE"
        details = entry["rejection_details"]
        assert details["stand_aside_reason"] == "FOMC in 0.25h — stand aside"

    def test_log_rejection_safety_rail(self, logger):
        """Test safety rail triggered rejection."""
        entry = logger.log_rejection(
            price_at_signal=21300.0,
            signal_direction="SHORT",
            rejection_stage="SAFETY_RAIL",
            rejection_details={
                "safety_rail_triggered": "MaxDailyLoss breaker tripped: -$523",
            },
        )

        assert entry["rejection_stage"] == "SAFETY_RAIL"
        details = entry["rejection_details"]
        assert "MaxDailyLoss" in details["safety_rail_triggered"]

    def test_rejection_writes_json_file(self, logger, log_dir):
        """Test that rejection is written to JSON file."""
        logger.log_rejection(
            price_at_signal=21000.0,
            signal_direction="LONG",
            rejection_stage="HTF_GATE",
        )

        json_path = Path(log_dir) / "trade_decisions.json"
        assert json_path.exists()

        with open(json_path) as f:
            line = f.readline().strip()
            data = json.loads(line)

        assert data["decision"] == "REJECTED"
        assert data["signal_direction"] == "LONG"

    def test_rejection_writes_readable_file(self, logger, log_dir):
        """Test that rejection is written to readable file."""
        logger.log_rejection(
            price_at_signal=21450.25,
            signal_direction="SHORT",
            rejection_stage="HTF_GATE",
            rejection_details={
                "htf_biases": {
                    "1D": "BULLISH",
                    "4H": "BULLISH",
                    "1H": "BEARISH",
                },
            },
        )

        readable_path = Path(log_dir) / "trade_decisions_readable.txt"
        assert readable_path.exists()

        content = readable_path.read_text()
        assert "[REJECTED]" in content
        assert "SHORT" in content
        assert "21,450.25" in content
        assert "HTF_GATE" in content


# ================================================================
# APPROVAL LOGGING
# ================================================================

class TestApprovalLogging:
    """Test approval logging with modifier values."""

    def test_log_approval_basic(self, logger):
        """Test basic approval entry is created correctly."""
        entry = logger.log_approval(
            price_at_signal=21500.0,
            signal_direction="LONG",
            confluence_score=0.82,
            modifier_values={
                "overnight": 1.4,
                "fomc": 1.0,
                "gamma": 0.85,
                "volatility": 1.0,
                "total": 1.19,
            },
            position_size=2.0,
            stop_width=18.5,
            runner_trail_width=12.0,
            entry_price=21500.25,
            c1_target=21503.25,
            c2_trail_start=21512.25,
        )

        assert entry["decision"] == "APPROVED"
        assert entry["signal_direction"] == "LONG"
        assert entry["price_at_signal"] == 21500.0
        assert entry["confluence_score"] == 0.82
        assert entry["position_size"] == 2.0
        assert entry["stop_width"] == 18.5
        assert entry["entry_price"] == 21500.25
        assert entry["id"]
        assert entry["timestamp"]

    def test_log_approval_modifier_values(self, logger):
        """Test approval modifier values are stored correctly."""
        entry = logger.log_approval(
            price_at_signal=21500.0,
            signal_direction="SHORT",
            confluence_score=0.90,
            modifier_values={
                "overnight": 0.6,
                "fomc": 1.15,
                "gamma": 1.3,
                "volatility": 0.75,
                "total": 0.67,
            },
        )

        mods = entry["modifier_values"]
        assert mods["overnight"] == 0.6
        assert mods["fomc"] == 1.15
        assert mods["gamma"] == 1.3
        assert mods["volatility"] == 0.75
        assert mods["total"] == 0.67

    def test_approval_writes_json_file(self, logger, log_dir):
        """Test that approval is written to JSON file."""
        logger.log_approval(
            price_at_signal=21500.0,
            signal_direction="LONG",
            confluence_score=0.82,
        )

        json_path = Path(log_dir) / "trade_decisions.json"
        assert json_path.exists()

        with open(json_path) as f:
            data = json.loads(f.readline().strip())

        assert data["decision"] == "APPROVED"

    def test_approval_writes_readable_file(self, logger, log_dir):
        """Test that approval is written to readable file."""
        logger.log_approval(
            price_at_signal=21500.0,
            signal_direction="LONG",
            confluence_score=0.82,
            position_size=2.0,
            stop_width=18.5,
            entry_price=21500.25,
        )

        readable_path = Path(log_dir) / "trade_decisions_readable.txt"
        assert readable_path.exists()

        content = readable_path.read_text()
        assert "[APPROVED]" in content
        assert "LONG" in content
        assert "0.82" in content


# ================================================================
# SESSION SUMMARY
# ================================================================

class TestSessionSummary:
    """Test session summary calculation."""

    def test_empty_session(self, logger):
        """Test summary with no decisions."""
        summary = logger.get_session_summary()
        assert summary["total_signals"] == 0
        assert summary["approved"] == 0
        assert summary["rejected"] == 0
        assert summary["approval_rate"] == 0.0
        assert summary["most_common_rejection_reason"] == ""

    def test_session_summary_counts(self, logger):
        """Test summary counts are correct."""
        # Log 2 rejections and 1 approval
        logger.log_rejection(
            price_at_signal=21000.0,
            signal_direction="LONG",
            rejection_stage="HTF_GATE",
        )
        logger.log_rejection(
            price_at_signal=21100.0,
            signal_direction="SHORT",
            rejection_stage="CONFLUENCE",
            rejection_details={"confluence_score": 0.65, "confluence_threshold": 0.75},
        )
        logger.log_approval(
            price_at_signal=21200.0,
            signal_direction="LONG",
            confluence_score=0.85,
        )

        summary = logger.get_session_summary()
        assert summary["total_signals"] == 3
        assert summary["approved"] == 1
        assert summary["rejected"] == 2
        assert summary["approval_rate"] == pytest.approx(33.33, abs=0.01)

    def test_session_summary_rejection_breakdown(self, logger):
        """Test rejection breakdown by stage."""
        logger.log_rejection(
            price_at_signal=21000.0, signal_direction="LONG",
            rejection_stage="HTF_GATE",
        )
        logger.log_rejection(
            price_at_signal=21100.0, signal_direction="LONG",
            rejection_stage="HTF_GATE",
        )
        logger.log_rejection(
            price_at_signal=21200.0, signal_direction="SHORT",
            rejection_stage="CONFLUENCE",
        )

        summary = logger.get_session_summary()
        breakdown = summary["rejection_breakdown_by_stage"]
        assert breakdown["HTF_GATE"] == 2
        assert breakdown["CONFLUENCE"] == 1

    def test_most_common_rejection_reason(self, logger):
        """Test most common rejection reason is identified."""
        logger.log_rejection(
            price_at_signal=21000.0, signal_direction="LONG",
            rejection_stage="HTF_GATE",
        )
        logger.log_rejection(
            price_at_signal=21100.0, signal_direction="LONG",
            rejection_stage="HTF_GATE",
        )
        logger.log_rejection(
            price_at_signal=21200.0, signal_direction="SHORT",
            rejection_stage="CONFLUENCE",
        )

        summary = logger.get_session_summary()
        assert summary["most_common_rejection_reason"] == "HTF_GATE"

    def test_write_daily_summary(self, logger, log_dir):
        """Test daily summary is written to file."""
        logger.log_rejection(
            price_at_signal=21000.0, signal_direction="LONG",
            rejection_stage="HTF_GATE",
        )
        logger.log_approval(
            price_at_signal=21200.0, signal_direction="LONG",
            confluence_score=0.85,
        )

        logger.write_daily_summary()

        summary_path = Path(log_dir) / "daily_summaries.txt"
        assert summary_path.exists()

        content = summary_path.read_text()
        assert "SESSION SUMMARY" in content
        assert "Total signals evaluated" in content


# ================================================================
# JSON APPEND MODE
# ================================================================

class TestJsonAppendMode:
    """Test JSON append mode (multiple entries)."""

    def test_multiple_entries_appended(self, logger, log_dir):
        """Test multiple entries are appended to the same file."""
        logger.log_rejection(
            price_at_signal=21000.0, signal_direction="LONG",
            rejection_stage="HTF_GATE",
        )
        logger.log_approval(
            price_at_signal=21200.0, signal_direction="LONG",
            confluence_score=0.85,
        )
        logger.log_rejection(
            price_at_signal=21300.0, signal_direction="SHORT",
            rejection_stage="CONFLUENCE",
        )

        json_path = Path(log_dir) / "trade_decisions.json"
        with open(json_path) as f:
            lines = [l.strip() for l in f if l.strip()]

        assert len(lines) == 3

        # Verify each line is valid JSON
        entries = [json.loads(line) for line in lines]
        assert entries[0]["decision"] == "REJECTED"
        assert entries[1]["decision"] == "APPROVED"
        assert entries[2]["decision"] == "REJECTED"

    def test_read_all_decisions(self, logger, log_dir):
        """Test reading all decisions back from file."""
        logger.log_rejection(
            price_at_signal=21000.0, signal_direction="LONG",
            rejection_stage="HTF_GATE",
        )
        logger.log_approval(
            price_at_signal=21200.0, signal_direction="LONG",
            confluence_score=0.85,
        )

        all_decisions = logger.read_all_decisions()
        assert len(all_decisions) == 2
        assert all_decisions[0]["decision"] == "REJECTED"
        assert all_decisions[1]["decision"] == "APPROVED"

    def test_read_empty_file(self, log_dir):
        """Test reading from non-existent file returns empty list."""
        logger = TradeDecisionLogger(log_dir)
        assert logger.read_all_decisions() == []


# ================================================================
# HUMAN-READABLE FORMAT
# ================================================================

class TestHumanReadableFormat:
    """Test human-readable format output."""

    def test_readable_rejection_format(self, logger, log_dir):
        """Test readable rejection format matches spec."""
        logger.log_rejection(
            price_at_signal=21450.25,
            signal_direction="SHORT",
            rejection_stage="HTF_GATE",
            rejection_details={
                "htf_biases": {
                    "1D": "BULLISH",
                    "4H": "BULLISH",
                    "1H": "BEARISH",
                    "30m": "BEARISH",
                    "15m": "BEARISH",
                    "5m": "BEARISH",
                },
                "conflicting_timeframes": ["1D", "4H"],
            },
        )

        readable_path = Path(log_dir) / "trade_decisions_readable.txt"
        content = readable_path.read_text()

        assert "=== [REJECTED]" in content
        assert "SHORT @ 21,450.25" in content
        assert "Stage: HTF_GATE" in content
        assert "1D=BULL" in content

    def test_readable_approval_format(self, logger, log_dir):
        """Test readable approval format."""
        logger.log_approval(
            price_at_signal=21500.0,
            signal_direction="LONG",
            confluence_score=0.82,
            position_size=2.0,
            stop_width=18.5,
            entry_price=21500.25,
        )

        readable_path = Path(log_dir) / "trade_decisions_readable.txt"
        content = readable_path.read_text()

        assert "=== [APPROVED]" in content
        assert "LONG @ 21,500.00" in content
        assert "0.82" in content


# ================================================================
# SESSION RESET
# ================================================================

class TestSessionReset:
    """Test session reset clears counters."""

    def test_reset_clears_counters(self, logger):
        """Test that reset_session clears all counters."""
        logger.log_rejection(
            price_at_signal=21000.0, signal_direction="LONG",
            rejection_stage="HTF_GATE",
        )
        logger.log_approval(
            price_at_signal=21200.0, signal_direction="LONG",
            confluence_score=0.85,
        )

        assert logger.get_session_summary()["total_signals"] == 2

        logger.reset_session()

        summary = logger.get_session_summary()
        assert summary["total_signals"] == 0
        assert summary["approved"] == 0
        assert summary["rejected"] == 0

    def test_direction_normalization(self, logger):
        """Test that direction is normalized to uppercase."""
        entry = logger.log_rejection(
            price_at_signal=21000.0,
            signal_direction="long",
            rejection_stage="HTF_GATE",
        )
        assert entry["signal_direction"] == "LONG"

        entry = logger.log_approval(
            price_at_signal=21200.0,
            signal_direction="short",
            confluence_score=0.85,
        )
        assert entry["signal_direction"] == "SHORT"
