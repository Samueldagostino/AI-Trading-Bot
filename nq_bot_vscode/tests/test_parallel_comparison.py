"""
Tests for the paper-to-live parallel comparison framework.
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "nq_bot_vscode"))

from nq_bot_vscode.monitoring.divergence_tracker import (
    ComparisonResult,
    Divergence,
    DivergenceCategory,
    DivergenceSeverity,
    DivergenceTracker,
    FillComparison,
)


# ================================================================
# Fixtures
# ================================================================


@pytest.fixture
def tracker():
    return DivergenceTracker()


@pytest.fixture
def ts():
    return datetime(2025, 10, 15, 10, 0, tzinfo=timezone.utc)


def make_entry_result(direction="long", score=0.80, entry_price=21000.0, stop=20980.0):
    """Create a mock process_bar result for an entry."""
    return {
        "action": "entry",
        "timestamp": "2025-10-15T10:00:00+00:00",
        "direction": direction,
        "contracts": 2,
        "entry_price": entry_price,
        "stop": stop,
        "signal_score": score,
        "signal_source": "signal",
        "regime": "ranging",
    }


# ================================================================
# Test: Identical bars produce identical signals (no divergence)
# ================================================================


class TestIdenticalSignals:

    def test_identical_bars_produce_identical_signals(self, tracker, ts):
        """When both instances return the same result, no divergences."""
        paper = make_entry_result()
        live = make_entry_result()

        result = tracker.compare_decisions(ts, 1, paper, live)

        assert result.is_clean is True
        assert len(result.divergences) == 0
        assert result.paper_entry is True
        assert result.live_entry is True
        assert result.paper_direction == "long"
        assert result.live_direction == "long"

    def test_both_no_signal_is_clean(self, tracker, ts):
        """Both returning None is clean (no trade, no divergence)."""
        result = tracker.compare_decisions(ts, 1, None, None)
        assert result.is_clean is True
        assert len(result.divergences) == 0

    def test_both_non_entry_results_clean(self, tracker, ts):
        """Both returning non-entry results is clean."""
        paper = {"action": "trade_closed", "total_pnl": 25.0, "direction": "long"}
        live = {"action": "trade_closed", "total_pnl": 25.0, "direction": "long"}
        result = tracker.compare_decisions(ts, 1, paper, live)
        assert result.is_clean is True


# ================================================================
# Test: Signal mismatch detection
# ================================================================


class TestSignalMismatch:

    def test_divergence_detection_signal_mismatch(self, tracker, ts):
        """Different signal presence is flagged as SIGNAL_MISMATCH."""
        paper = {"action": "no_signal"}
        live = None

        result = tracker.compare_decisions(ts, 1, paper, live)

        assert result.is_clean is False
        assert len(result.divergences) == 1
        assert result.divergences[0].category == DivergenceCategory.SIGNAL_MISMATCH
        assert result.divergences[0].severity == DivergenceSeverity.WARNING


# ================================================================
# Test: Direction mismatch detection
# ================================================================


class TestDirectionMismatch:

    def test_divergence_detection_direction_mismatch(self, tracker, ts):
        """Same entry signal but different direction is CRITICAL."""
        paper = make_entry_result(direction="long")
        live = make_entry_result(direction="short")

        result = tracker.compare_decisions(ts, 1, paper, live)

        assert result.is_clean is False
        divs = [d for d in result.divergences
                 if d.category == DivergenceCategory.DIRECTION_MISMATCH]
        assert len(divs) == 1
        assert divs[0].severity == DivergenceSeverity.CRITICAL


# ================================================================
# Test: Missing trade detection
# ================================================================


class TestMissingTrade:

    def test_paper_entry_live_none(self, tracker, ts):
        """Paper took trade but live didn't → MISSING_TRADE."""
        paper = make_entry_result()
        live = None

        result = tracker.compare_decisions(ts, 1, paper, live)

        assert result.is_clean is False
        divs = [d for d in result.divergences
                 if d.category == DivergenceCategory.MISSING_TRADE]
        assert len(divs) == 1
        assert divs[0].severity == DivergenceSeverity.CRITICAL

    def test_live_entry_paper_none(self, tracker, ts):
        """Live took trade but paper didn't → MISSING_TRADE."""
        paper = None
        live = make_entry_result()

        result = tracker.compare_decisions(ts, 1, paper, live)

        assert result.is_clean is False
        divs = [d for d in result.divergences
                 if d.category == DivergenceCategory.MISSING_TRADE]
        assert len(divs) == 1


# ================================================================
# Test: Score drift detection
# ================================================================


class TestScoreDrift:

    def test_score_drift_flagged(self, tracker, ts):
        """Score difference > 0.05 is flagged."""
        paper = make_entry_result(score=0.85)
        live = make_entry_result(score=0.78)

        result = tracker.compare_decisions(ts, 1, paper, live)

        assert result.is_clean is False
        divs = [d for d in result.divergences
                 if d.category == DivergenceCategory.SCORE_DRIFT]
        assert len(divs) == 1

    def test_small_score_diff_clean(self, tracker, ts):
        """Score difference <= 0.05 is clean."""
        paper = make_entry_result(score=0.82)
        live = make_entry_result(score=0.80)

        result = tracker.compare_decisions(ts, 1, paper, live)

        assert result.is_clean is True


# ================================================================
# Test: Fill slippage calculation
# ================================================================


class TestFillSlippage:

    def test_fill_slippage_calculation(self, tracker):
        """Slippage is correctly calculated for long entries."""
        t = datetime(2025, 10, 15, 10, 0, tzinfo=timezone.utc)
        fc = tracker.compare_fills(
            trade_id="test_1",
            direction="long",
            paper_entry_price=21000.0,
            live_entry_price=21001.5,  # 1.5pts slippage (> 2 ticks = 0.50pts)
            paper_fill_time=t,
            live_fill_time=t + timedelta(milliseconds=50),
            paper_stop=20980.0,
            live_stop=20980.0,
        )

        assert fc.entry_slippage_pts == 1.5
        assert fc.fill_latency_ms == 50.0
        assert fc.stop_match is True

        # Should have flagged FILL_SLIPPAGE (1.5 > 0.5)
        divs = [d for d in tracker.divergences
                 if d.category == DivergenceCategory.FILL_SLIPPAGE]
        assert len(divs) == 1

    def test_fill_slippage_short_direction(self, tracker):
        """Slippage for shorts: paper_entry - live_entry."""
        t = datetime(2025, 10, 15, 10, 0, tzinfo=timezone.utc)
        fc = tracker.compare_fills(
            trade_id="test_2",
            direction="short",
            paper_entry_price=21000.0,
            live_entry_price=20999.0,  # 1.0pts slippage for short
            paper_fill_time=t,
            live_fill_time=t,
        )
        assert fc.entry_slippage_pts == 1.0

    def test_fill_within_tolerance_no_divergence(self, tracker):
        """Small slippage (< 2 ticks) should NOT flag FILL_SLIPPAGE."""
        t = datetime(2025, 10, 15, 10, 0, tzinfo=timezone.utc)
        tracker.compare_fills(
            trade_id="test_3",
            direction="long",
            paper_entry_price=21000.0,
            live_entry_price=21000.25,  # Only 0.25pts = 1 tick
            paper_fill_time=t,
            live_fill_time=t,
        )
        divs = [d for d in tracker.divergences
                 if d.category == DivergenceCategory.FILL_SLIPPAGE]
        assert len(divs) == 0

    def test_timing_divergence(self, tracker):
        """Latency > 200ms flagged as TIMING_DIVERGENCE."""
        t = datetime(2025, 10, 15, 10, 0, tzinfo=timezone.utc)
        tracker.compare_fills(
            trade_id="test_4",
            direction="long",
            paper_entry_price=21000.0,
            live_entry_price=21000.0,
            paper_fill_time=t,
            live_fill_time=t + timedelta(milliseconds=350),
        )
        divs = [d for d in tracker.divergences
                 if d.category == DivergenceCategory.TIMING_DIVERGENCE]
        assert len(divs) == 1


# ================================================================
# Test: Report generation (via save_log + load)
# ================================================================


class TestReportGeneration:

    def test_report_generation(self, tracker, ts, tmp_path):
        """save_log produces valid JSON with all sections."""
        paper = make_entry_result()
        live = make_entry_result()
        tracker.compare_decisions(ts, 1, paper, live)

        log_path = str(tmp_path / "test_log.json")
        tracker.save_log(log_path)

        with open(log_path, "r") as f:
            data = json.load(f)

        assert "summary" in data
        assert "verdict" in data
        assert "comparisons" in data
        assert "fill_comparisons" in data
        assert "divergences" in data

    def test_html_report_generation(self, tmp_path):
        """comparison_report.py generates valid HTML."""
        # Create a minimal log
        log_data = {
            "summary": {
                "bars_compared": 10,
                "total_comparisons": 10,
                "clean_bars": 10,
                "agreement_rate": 100.0,
                "total_divergences": 0,
                "by_category": {},
                "by_severity": {},
                "total_fill_comparisons": 0,
                "avg_entry_slippage_pts": 0.0,
                "avg_fill_latency_ms": 0.0,
            },
            "verdict": {
                "verdict": "PASS",
                "checks": {
                    "signal_agreement": True,
                    "no_direction_mismatch": True,
                    "avg_slippage_ok": True,
                    "no_missing_trades": True,
                    "pnl_correlation_ok": True,
                },
                "details": {},
                "fail_reasons": [],
            },
            "comparisons": [],
            "fill_comparisons": [],
            "divergences": [],
        }

        log_path = str(tmp_path / "test_log.json")
        with open(log_path, "w") as f:
            json.dump(log_data, f)

        # Import and run report generator
        sys.path.insert(0, str(project_root / "scripts"))
        from comparison_report import generate_report

        output_path = str(tmp_path / "test_report.html")
        generate_report(log_data, output_path)

        assert Path(output_path).exists()
        content = Path(output_path).read_text()
        assert "PASS" in content
        assert "Paper-to-Live Comparison Report" in content


# ================================================================
# Test: Pass/Fail verdict
# ================================================================


class TestVerdict:

    def test_pass_fail_verdict_clean_session(self, tracker, ts):
        """Clean session produces PASS verdict."""
        for i in range(10):
            tracker.compare_decisions(ts, i, None, None)

        verdict = tracker.get_verdict()
        assert verdict["verdict"] == "PASS"
        assert len(verdict["fail_reasons"]) == 0

    def test_pass_fail_verdict_with_direction_mismatch(self, tracker, ts):
        """Direction mismatch produces FAIL verdict."""
        paper = make_entry_result(direction="long")
        live = make_entry_result(direction="short")
        tracker.compare_decisions(ts, 1, paper, live)

        verdict = tracker.get_verdict()
        assert verdict["verdict"] == "FAIL"
        assert verdict["checks"]["no_direction_mismatch"] is False

    def test_pass_fail_verdict_with_missing_trade(self, tracker, ts):
        """Missing trade produces FAIL verdict."""
        paper = make_entry_result()
        tracker.compare_decisions(ts, 1, paper, None)

        verdict = tracker.get_verdict()
        assert verdict["verdict"] == "FAIL"
        assert verdict["checks"]["no_missing_trades"] is False

    def test_pass_fail_verdict_low_agreement(self, tracker, ts):
        """Low agreement rate (< 99%) produces FAIL verdict."""
        # 2 divergent out of 10 = 80% agreement
        for i in range(8):
            tracker.compare_decisions(ts, i, None, None)
        tracker.compare_decisions(ts, 8, {"action": "test"}, None)
        tracker.compare_decisions(ts, 9, {"action": "test"}, None)

        verdict = tracker.get_verdict()
        assert verdict["verdict"] == "FAIL"
        assert verdict["checks"]["signal_agreement"] is False


# ================================================================
# Test: Alert on critical divergence
# ================================================================


class TestAlertOnCritical:

    def test_alert_on_critical_divergence(self, ts):
        """CRITICAL divergences trigger alert via AlertManager."""
        mock_alert_mgr = MagicMock()
        tracker = DivergenceTracker(alert_manager=mock_alert_mgr)

        paper = make_entry_result(direction="long")
        live = make_entry_result(direction="short")
        tracker.compare_decisions(ts, 1, paper, live)

        # AlertManager.enqueue should have been called
        mock_alert_mgr.enqueue.assert_called()
        call_args = mock_alert_mgr.enqueue.call_args
        alert = call_args[0][0]
        assert "DIRECTION_MISMATCH" in alert.title

    def test_no_alert_on_info_divergence(self, ts):
        """INFO-level divergences should NOT trigger alert."""
        mock_alert_mgr = MagicMock()
        tracker = DivergenceTracker(alert_manager=mock_alert_mgr)

        # Score drift is INFO severity
        paper = make_entry_result(score=0.90)
        live = make_entry_result(score=0.82)
        tracker.compare_decisions(ts, 1, paper, live)

        # enqueue should NOT have been called (SCORE_DRIFT is INFO)
        mock_alert_mgr.enqueue.assert_not_called()


# ================================================================
# Test: Summary statistics
# ================================================================


class TestSummary:

    def test_summary_counts(self, tracker, ts):
        """Summary correctly counts bars and divergences."""
        # 3 clean + 1 divergent
        for i in range(3):
            tracker.compare_decisions(ts, i, None, None)
        paper = make_entry_result()
        tracker.compare_decisions(ts, 3, paper, None)

        summary = tracker.get_summary()
        assert summary["bars_compared"] == 4
        assert summary["total_comparisons"] == 4
        assert summary["clean_bars"] == 3
        assert summary["total_divergences"] == 1
        assert summary["agreement_rate"] == 75.0

    def test_pnl_correlation_identical(self, tracker):
        """Identical PnLs should give correlation of 1.0."""
        t = datetime(2025, 10, 15, 10, 0, tzinfo=timezone.utc)
        for i in range(5):
            pnl = (i + 1) * 10.0
            tracker.compare_fills(
                trade_id=f"t{i}", direction="long",
                paper_entry_price=21000.0, live_entry_price=21000.0,
                paper_pnl=pnl, live_pnl=pnl,
                paper_fill_time=t, live_fill_time=t,
            )

        verdict = tracker.get_verdict()
        assert verdict["details"]["pnl_correlation"] == 1.0
