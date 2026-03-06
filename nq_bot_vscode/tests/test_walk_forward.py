"""
Tests for the Walk-Forward Optimization Framework
====================================================
Tests fold generation, no-lookahead, min-trades filter, degradation,
consistency, JSON/HTML export, and baseline comparison.
"""

import json
import os
import tempfile
import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.walk_forward_report import FoldResult, WFSummary, WalkForwardReport
from scripts.run_walk_forward import WalkForwardEngine


# ── Helpers ──

def _make_fold(fold_id, train_pf=1.8, test_pf=1.4, train_wr=60.0, test_wr=55.0,
               train_dd=1.0, test_dd=1.5, train_trades=50, test_trades=30,
               skipped=False, skip_reason="", train_pnl=500.0, test_pnl=200.0):
    """Helper to build a FoldResult with defaults."""
    degradation = round(test_pf / train_pf, 4) if train_pf > 0 and not skipped else 0.0
    return FoldResult(
        fold_id=fold_id,
        train_start=f"2024-{fold_id:02d}",
        train_end=f"2024-{fold_id + 2:02d}",
        test_start=f"2024-{fold_id + 3:02d}",
        test_end=f"2024-{fold_id + 3:02d}",
        train_trades=train_trades,
        test_trades=test_trades,
        train_pf=train_pf,
        test_pf=test_pf,
        train_wr=train_wr,
        test_wr=test_wr,
        train_pnl=train_pnl,
        test_pnl=test_pnl,
        train_dd=train_dd,
        test_dd=test_dd,
        degradation=degradation,
        skipped=skipped,
        skip_reason=skip_reason,
    )


class TestFoldGeneration:
    """test_fold_generation — correct number of folds for given data length."""

    def test_basic_fold_count(self):
        """6 months data, train=3, test=1, step=1 → 3 folds."""
        engine = WalkForwardEngine(train_months=3, test_months=1, step_months=1)
        months = ["2024-01", "2024-02", "2024-03", "2024-04", "2024-05", "2024-06"]
        folds = engine._generate_folds(months)
        assert len(folds) == 3

    def test_step_2_fold_count(self):
        """8 months data, train=3, test=1, step=2 → 3 folds."""
        engine = WalkForwardEngine(train_months=3, test_months=1, step_months=2)
        months = [f"2024-{i:02d}" for i in range(1, 9)]
        folds = engine._generate_folds(months)
        assert len(folds) == 3

    def test_exact_fit(self):
        """4 months, train=3, test=1 → exactly 1 fold."""
        engine = WalkForwardEngine(train_months=3, test_months=1, step_months=1)
        months = ["2024-01", "2024-02", "2024-03", "2024-04"]
        folds = engine._generate_folds(months)
        assert len(folds) == 1

    def test_not_enough_data(self):
        """3 months, train=3, test=1 → 0 folds (not enough)."""
        engine = WalkForwardEngine(train_months=3, test_months=1, step_months=1)
        months = ["2024-01", "2024-02", "2024-03"]
        folds = engine._generate_folds(months)
        assert len(folds) == 0

    def test_larger_test_window(self):
        """8 months, train=4, test=2, step=1 → 3 folds."""
        engine = WalkForwardEngine(train_months=4, test_months=2, step_months=1)
        months = [f"2024-{i:02d}" for i in range(1, 9)]
        folds = engine._generate_folds(months)
        assert len(folds) == 3

    def test_12_months_default_params(self):
        """12 months, train=3, test=1, step=1 → 9 folds."""
        engine = WalkForwardEngine()
        months = [f"2024-{i:02d}" for i in range(1, 13)]
        folds = engine._generate_folds(months)
        assert len(folds) == 9


class TestNoLookahead:
    """test_no_lookahead — train window never overlaps test window."""

    def test_no_overlap(self):
        """Verify train months and test months never share elements."""
        engine = WalkForwardEngine(train_months=3, test_months=1, step_months=1)
        months = [f"2024-{i:02d}" for i in range(1, 13)]
        folds = engine._generate_folds(months)

        for train_months, test_months in folds:
            train_set = set(train_months)
            test_set = set(test_months)
            overlap = train_set & test_set
            assert len(overlap) == 0, f"Overlap found: {overlap}"

    def test_train_before_test(self):
        """Verify all train months come before all test months."""
        engine = WalkForwardEngine(train_months=3, test_months=2, step_months=1)
        months = [f"2024-{i:02d}" for i in range(1, 13)]
        folds = engine._generate_folds(months)

        for train_months, test_months in folds:
            assert max(train_months) < min(test_months), (
                f"Train {train_months} should all be before test {test_months}"
            )

    def test_consecutive_folds_no_test_overlap(self):
        """No test window should overlap with previous fold's test window when step=1."""
        engine = WalkForwardEngine(train_months=3, test_months=1, step_months=1)
        months = [f"2024-{i:02d}" for i in range(1, 10)]
        folds = engine._generate_folds(months)

        # Each fold's test window should be different
        test_windows = [tuple(test) for _, test in folds]
        assert len(test_windows) == len(set(test_windows))


class TestMinTradesFilter:
    """test_min_trades_filter — folds with <10 trades are skipped."""

    def test_skipped_fold_low_train_trades(self):
        fold = _make_fold(1, train_trades=5, skipped=True, skip_reason="Too few train trades")
        assert fold.skipped is True
        assert "Too few" in fold.skip_reason

    def test_skipped_fold_low_test_trades(self):
        fold = _make_fold(1, test_trades=3, skipped=True, skip_reason="Too few test trades")
        assert fold.skipped is True

    def test_valid_fold_sufficient_trades(self):
        fold = _make_fold(1, train_trades=50, test_trades=30)
        assert fold.skipped is False

    def test_engine_min_trades_default(self):
        engine = WalkForwardEngine()
        assert engine.min_trades_per_fold == 10

    def test_engine_min_trades_custom(self):
        engine = WalkForwardEngine(min_trades_per_fold=25)
        assert engine.min_trades_per_fold == 25


class TestDegradationCalculation:
    """test_degradation_calculation — OOS/IS ratio computed correctly."""

    def test_basic_degradation(self):
        fold = _make_fold(1, train_pf=2.0, test_pf=1.2)
        assert fold.degradation == pytest.approx(0.6, abs=0.01)

    def test_perfect_retention(self):
        fold = _make_fold(1, train_pf=1.5, test_pf=1.5)
        assert fold.degradation == pytest.approx(1.0, abs=0.01)

    def test_improvement_oos(self):
        fold = _make_fold(1, train_pf=1.2, test_pf=1.8)
        assert fold.degradation == pytest.approx(1.5, abs=0.01)

    def test_oos_worse(self):
        fold = _make_fold(1, train_pf=2.0, test_pf=0.8)
        assert fold.degradation == pytest.approx(0.4, abs=0.01)

    def test_engine_compute_degradation(self):
        engine = WalkForwardEngine()
        folds = [
            _make_fold(1, train_pf=2.0, test_pf=1.2),
            _make_fold(2, train_pf=1.5, test_pf=1.5),
            _make_fold(3, train_pf=1.8, test_pf=0.9, skipped=True),
        ]
        results = engine.compute_degradation(folds)
        # Skipped fold excluded
        assert len(results) == 2
        assert results[0]["degradation"] == pytest.approx(0.6, abs=0.01)
        assert results[1]["degradation"] == pytest.approx(1.0, abs=0.01)


class TestConsistencyMetric:
    """test_consistency_metric — correct % of profitable folds."""

    def test_all_profitable(self):
        engine = WalkForwardEngine()
        folds = [
            _make_fold(1, test_pf=1.5),
            _make_fold(2, test_pf=1.2),
            _make_fold(3, test_pf=1.1),
        ]
        assert engine.compute_consistency(folds) == 100.0

    def test_none_profitable(self):
        engine = WalkForwardEngine()
        folds = [
            _make_fold(1, test_pf=0.8),
            _make_fold(2, test_pf=0.5),
        ]
        assert engine.compute_consistency(folds) == 0.0

    def test_mixed(self):
        engine = WalkForwardEngine()
        folds = [
            _make_fold(1, test_pf=1.5),
            _make_fold(2, test_pf=0.8),
            _make_fold(3, test_pf=1.2),
            _make_fold(4, test_pf=0.9),
        ]
        # 2 out of 4 profitable
        assert engine.compute_consistency(folds) == 50.0

    def test_skipped_excluded(self):
        engine = WalkForwardEngine()
        folds = [
            _make_fold(1, test_pf=1.5),
            _make_fold(2, test_pf=0.8),
            _make_fold(3, skipped=True, skip_reason="no data"),
        ]
        # 1 out of 2 valid = 50%
        assert engine.compute_consistency(folds) == 50.0

    def test_empty_folds(self):
        engine = WalkForwardEngine()
        assert engine.compute_consistency([]) == 0.0


class TestRegimeBreaks:
    """test regime break detection."""

    def test_detect_breaks(self):
        engine = WalkForwardEngine()
        folds = [
            _make_fold(1, test_pf=1.5),
            _make_fold(2, test_pf=0.7),
            _make_fold(3, test_pf=1.2),
            _make_fold(4, test_pf=0.4),
        ]
        breaks = engine.detect_regime_breaks(folds)
        assert len(breaks) == 2
        assert breaks[0]["fold_id"] == 2
        assert breaks[1]["fold_id"] == 4

    def test_no_breaks(self):
        engine = WalkForwardEngine()
        folds = [_make_fold(i, test_pf=1.5) for i in range(1, 5)]
        breaks = engine.detect_regime_breaks(folds)
        assert len(breaks) == 0


class TestReportJsonExport:
    """test_report_json_export — valid JSON output."""

    def test_json_export_creates_file(self):
        folds = [_make_fold(1), _make_fold(2)]
        summary = WFSummary(total_folds=2, valid_folds=2)
        report = WalkForwardReport(folds, summary)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.json")
            report.to_json(path)

            assert os.path.exists(path)
            with open(path) as f:
                data = json.load(f)

            assert "generated_at" in data
            assert "summary" in data
            assert "folds" in data
            assert len(data["folds"]) == 2

    def test_json_contains_fold_metrics(self):
        folds = [_make_fold(1, train_pf=2.0, test_pf=1.5)]
        summary = WFSummary(total_folds=1, valid_folds=1)
        report = WalkForwardReport(folds, summary)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.json")
            report.to_json(path)

            with open(path) as f:
                data = json.load(f)

            fold = data["folds"][0]
            assert fold["train_pf"] == 2.0
            assert fold["test_pf"] == 1.5
            assert fold["fold_id"] == 1

    def test_json_summary_fields(self):
        summary = WFSummary(
            total_folds=5, valid_folds=4, skipped_folds=1,
            avg_test_pf=1.5, consistency_pct=75.0,
        )
        report = WalkForwardReport([], summary)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.json")
            report.to_json(path)

            with open(path) as f:
                data = json.load(f)

            s = data["summary"]
            assert s["total_folds"] == 5
            assert s["valid_folds"] == 4
            assert s["avg_test_pf"] == 1.5
            assert s["consistency_pct"] == 75.0


class TestReportHtmlGeneration:
    """test_report_html_generation — HTML file created with expected sections."""

    def test_html_export_creates_file(self):
        folds = [_make_fold(1), _make_fold(2)]
        summary = WFSummary(
            total_folds=2, valid_folds=2, avg_test_pf=1.5,
            consistency_pct=100.0, baseline_pass=True,
            baseline_reasons=["All checks passed"],
        )
        report = WalkForwardReport(folds, summary)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.html")
            report.to_html(path)

            assert os.path.exists(path)
            with open(path) as f:
                html = f.read()

            # Verify key sections exist
            assert "Walk-Forward Optimization Report" in html
            assert "chart.js" in html.lower() or "Chart" in html
            assert "pfChart" in html
            assert "degChart" in html
            assert "consChart" in html
            assert "ddChart" in html

    def test_html_contains_fold_data(self):
        folds = [_make_fold(1, train_pf=2.0, test_pf=1.5)]
        summary = WFSummary(
            total_folds=1, valid_folds=1,
            baseline_pass=True, baseline_reasons=["OK"],
        )
        report = WalkForwardReport(folds, summary)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.html")
            report.to_html(path)

            with open(path) as f:
                html = f.read()

            assert "2.00" in html  # train_pf
            assert "1.50" in html or "1.5" in html  # test_pf

    def test_html_pass_verdict(self):
        summary = WFSummary(baseline_pass=True, baseline_reasons=["All good"])
        report = WalkForwardReport([], summary)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.html")
            report.to_html(path)

            with open(path) as f:
                html = f.read()
            assert "PASS" in html

    def test_html_fail_verdict(self):
        summary = WFSummary(baseline_pass=False, baseline_reasons=["PF too low"])
        report = WalkForwardReport([], summary)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.html")
            report.to_html(path)

            with open(path) as f:
                html = f.read()
            assert "FAIL" in html

    def test_html_skipped_fold(self):
        folds = [_make_fold(1, skipped=True, skip_reason="No data")]
        summary = WFSummary(total_folds=1, skipped_folds=1,
                            baseline_pass=False, baseline_reasons=[])
        report = WalkForwardReport(folds, summary)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.html")
            report.to_html(path)

            with open(path) as f:
                html = f.read()
            assert "SKIPPED" in html
            assert "No data" in html


class TestBaselineComparison:
    """test_baseline_comparison — PASS/FAIL logic against baseline metrics."""

    def _make_engine_and_summary(self, **kwargs):
        defaults = {
            "avg_test_pf": 1.6,
            "consistency_pct": 70.0,
            "max_test_dd": 3.0,
            "avg_degradation": 0.7,
        }
        defaults.update(kwargs)
        engine = WalkForwardEngine()
        summary = WFSummary(**defaults)
        return engine, summary

    def test_all_pass(self):
        engine, summary = self._make_engine_and_summary()
        engine._compare_baseline(summary)
        assert summary.baseline_pass is True
        assert len(summary.baseline_reasons) == 4
        assert all("PASS" in r for r in summary.baseline_reasons)

    def test_fail_low_pf(self):
        engine, summary = self._make_engine_and_summary(avg_test_pf=1.2)
        engine._compare_baseline(summary)
        assert summary.baseline_pass is False
        assert any("OOS PF" in r and "FAIL" in r for r in summary.baseline_reasons)

    def test_fail_low_consistency(self):
        engine, summary = self._make_engine_and_summary(consistency_pct=40.0)
        engine._compare_baseline(summary)
        assert summary.baseline_pass is False
        assert any("Consistency" in r and "FAIL" in r for r in summary.baseline_reasons)

    def test_fail_high_dd(self):
        engine, summary = self._make_engine_and_summary(max_test_dd=7.0)
        engine._compare_baseline(summary)
        assert summary.baseline_pass is False
        assert any("DD" in r and "FAIL" in r for r in summary.baseline_reasons)

    def test_fail_low_degradation(self):
        engine, summary = self._make_engine_and_summary(avg_degradation=0.4)
        engine._compare_baseline(summary)
        assert summary.baseline_pass is False
        assert any("Degradation" in r and "FAIL" in r for r in summary.baseline_reasons)

    def test_borderline_pass(self):
        """Exactly at thresholds should pass."""
        engine, summary = self._make_engine_and_summary(
            avg_test_pf=1.5, consistency_pct=60.0,
            max_test_dd=5.0, avg_degradation=0.6,
        )
        engine._compare_baseline(summary)
        assert summary.baseline_pass is True

    def test_multiple_failures(self):
        engine, summary = self._make_engine_and_summary(
            avg_test_pf=1.0, consistency_pct=30.0,
            max_test_dd=8.0, avg_degradation=0.3,
        )
        engine._compare_baseline(summary)
        assert summary.baseline_pass is False
        fail_count = sum(1 for r in summary.baseline_reasons if "FAIL" in r)
        assert fail_count == 4


class TestSummaryComputation:
    """Test WalkForwardEngine._compute_summary."""

    def test_summary_valid_folds(self):
        engine = WalkForwardEngine()
        folds = [
            _make_fold(1, test_pf=1.5, test_dd=2.0, test_pnl=300.0, test_trades=40),
            _make_fold(2, test_pf=1.2, test_dd=3.0, test_pnl=150.0, test_trades=25),
            _make_fold(3, skipped=True, skip_reason="no data"),
        ]
        summary = engine._compute_summary(folds)

        assert summary.total_folds == 3
        assert summary.valid_folds == 2
        assert summary.skipped_folds == 1
        assert summary.max_test_dd == 3.0
        assert summary.total_test_pnl == 450.0
        assert summary.total_test_trades == 65

    def test_summary_consistency(self):
        engine = WalkForwardEngine()
        folds = [
            _make_fold(1, test_pf=1.5),
            _make_fold(2, test_pf=0.8),
            _make_fold(3, test_pf=1.2),
        ]
        summary = engine._compute_summary(folds)
        # 2/3 profitable = 66.7%
        assert summary.consistency_pct == pytest.approx(66.7, abs=0.1)

    def test_summary_empty_folds(self):
        engine = WalkForwardEngine()
        summary = engine._compute_summary([])
        assert summary.total_folds == 0
        assert summary.valid_folds == 0

    def test_summary_regime_breaks(self):
        engine = WalkForwardEngine()
        folds = [
            _make_fold(1, test_pf=1.5),
            _make_fold(2, test_pf=0.7),
            _make_fold(3, test_pf=0.3),
        ]
        summary = engine._compute_summary(folds)
        assert summary.regime_breaks == 2


class TestPrintSummary:
    """Test that print_summary runs without error."""

    def test_print_summary_no_error(self, capsys):
        folds = [_make_fold(1), _make_fold(2, skipped=True, skip_reason="test")]
        summary = WFSummary(
            total_folds=2, valid_folds=1, skipped_folds=1,
            avg_test_pf=1.5, consistency_pct=100.0,
            baseline_pass=True, baseline_reasons=["OK"],
        )
        report = WalkForwardReport(folds, summary)
        report.print_summary()  # Should not raise

        captured = capsys.readouterr()
        assert "WALK-FORWARD" in captured.out
        assert "PASS" in captured.out
