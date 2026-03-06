"""
Tests for post-fix validation and baseline update scripts.
"""

import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add scripts dir to path
import sys
SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from run_post_fix_validation import (
    compare_metrics,
    compute_verdict,
    extract_baseline_metrics,
    extract_metrics,
    load_baseline,
)
from update_baseline import (
    build_baseline_from_results,
    create_backup,
    load_baseline as ub_load_baseline,
    print_diff,
)


# ── Fixtures ──

SAMPLE_BASELINE = {
    "_comment": "Test baseline",
    "profit_factor": 1.73,
    "win_rate_pct": 61.9,
    "trades_per_month": 254,
    "expectancy_per_trade": 16.79,
    "max_drawdown_pct": 1.4,
    "total_pnl": 25581.00,
    "c1_pnl": 10008.00,
    "c2_pnl": 15573.00,
    "total_trades": 1524,
    "months": 6,
    "account_size": 50000,
    "avg_slippage_pts": 0.96,
    "hc_filter": {"min_score": 0.75, "max_stop_pts": 30.0},
    "monthly": [
        {"month": "2025-09", "trades": 240, "wr": 56.7, "pf": 1.79, "pnl": 3608},
        {"month": "2025-10", "trades": 292, "wr": 57.5, "pf": 1.36, "pnl": 2798},
    ],
}

IMPROVED_RESULTS = {
    "total_trades": 1600,
    "win_rate": 63.5,
    "profit_factor": 1.85,
    "total_pnl": 28000.00,
    "max_drawdown_pct": 1.2,
    "expectancy_per_trade": 17.50,
}

DEGRADED_RESULTS = {
    "total_trades": 1400,
    "win_rate": 55.0,
    "profit_factor": 1.40,
    "total_pnl": 18000.00,
    "max_drawdown_pct": 3.5,
    "expectancy_per_trade": 12.86,
}

SEVERELY_DEGRADED_RESULTS = {
    "total_trades": 1200,
    "win_rate": 50.0,
    "profit_factor": 1.10,
    "total_pnl": 10000.00,
    "max_drawdown_pct": 5.0,
    "expectancy_per_trade": 8.33,
}


@pytest.fixture
def tmp_baseline(tmp_path):
    """Create a temporary baseline file."""
    baseline_file = tmp_path / "backtest_baseline.json"
    with open(baseline_file, "w") as f:
        json.dump(SAMPLE_BASELINE, f, indent=4)
    return baseline_file


@pytest.fixture
def tmp_config_dir(tmp_path):
    """Create a temporary config directory with baseline."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    baseline_file = config_dir / "backtest_baseline.json"
    with open(baseline_file, "w") as f:
        json.dump(SAMPLE_BASELINE, f, indent=4)
    return config_dir


# ── test_baseline_loads_correctly ──

class TestBaselineLoadsCorrectly:
    def test_loads_valid_baseline(self, tmp_baseline):
        """Baseline loads and contains expected fields."""
        baseline = load_baseline(str(tmp_baseline))
        assert baseline["profit_factor"] == 1.73
        assert baseline["win_rate_pct"] == 61.9
        assert baseline["total_trades"] == 1524
        assert baseline["total_pnl"] == 25581.00
        assert baseline["max_drawdown_pct"] == 1.4
        assert baseline["expectancy_per_trade"] == 16.79

    def test_extracts_metrics_from_baseline(self):
        """extract_baseline_metrics returns correct dict."""
        metrics = extract_baseline_metrics(SAMPLE_BASELINE)
        assert metrics["total_trades"] == 1524
        assert metrics["win_rate_pct"] == 61.9
        assert metrics["profit_factor"] == 1.73
        assert metrics["total_pnl"] == 25581.00
        assert metrics["max_drawdown_pct"] == 1.4
        assert metrics["expectancy_per_trade"] == 16.79

    def test_missing_baseline_raises(self, tmp_path):
        """FileNotFoundError raised for missing baseline."""
        with pytest.raises(FileNotFoundError):
            load_baseline(str(tmp_path / "nonexistent.json"))

    def test_loads_baseline_with_monthly(self, tmp_baseline):
        """Monthly data is preserved."""
        baseline = load_baseline(str(tmp_baseline))
        assert "monthly" in baseline
        assert len(baseline["monthly"]) == 2
        assert baseline["monthly"][0]["month"] == "2025-09"


# ── test_comparison_detects_improvement ──

class TestComparisonDetectsImprovement:
    def test_all_metrics_improved(self):
        """All metrics show improvement status."""
        old = extract_baseline_metrics(SAMPLE_BASELINE)
        new = extract_metrics(IMPROVED_RESULTS)
        rows = compare_metrics(old, new)

        status_map = {r["metric"]: r["status"] for r in rows}
        assert status_map["profit_factor"] == "improved"
        assert status_map["win_rate_pct"] == "improved"
        assert status_map["total_pnl"] == "improved"
        assert status_map["total_trades"] == "improved"
        # Drawdown decreased = improved (lower is better)
        assert status_map["max_drawdown_pct"] == "improved"

    def test_verdict_passes_on_improvement(self):
        """Verdict is PASS when metrics improve."""
        old = extract_baseline_metrics(SAMPLE_BASELINE)
        new = extract_metrics(IMPROVED_RESULTS)
        rows = compare_metrics(old, new)
        passed, reasons = compute_verdict(rows)
        assert passed is True
        assert any("PASS" in r for r in reasons)

    def test_deltas_are_positive_for_improvements(self):
        """Delta values are correct direction."""
        old = extract_baseline_metrics(SAMPLE_BASELINE)
        new = extract_metrics(IMPROVED_RESULTS)
        rows = compare_metrics(old, new)

        pf_row = next(r for r in rows if r["metric"] == "profit_factor")
        assert pf_row["delta"] > 0
        assert pf_row["pct_change"] > 0


# ── test_comparison_detects_degradation ──

class TestComparisonDetectsDegradation:
    def test_degraded_metrics_flagged(self):
        """Degraded metrics show degraded status."""
        old = extract_baseline_metrics(SAMPLE_BASELINE)
        new = extract_metrics(DEGRADED_RESULTS)
        rows = compare_metrics(old, new)

        status_map = {r["metric"]: r["status"] for r in rows}
        assert status_map["profit_factor"] == "degraded"
        assert status_map["win_rate_pct"] == "degraded"
        assert status_map["total_pnl"] == "degraded"
        # Drawdown increased = degraded (lower is better)
        assert status_map["max_drawdown_pct"] == "degraded"

    def test_severe_pf_drop_fails(self):
        """Verdict is FAIL when profit factor drops >10%."""
        old = extract_baseline_metrics(SAMPLE_BASELINE)
        new = extract_metrics(SEVERELY_DEGRADED_RESULTS)
        rows = compare_metrics(old, new)
        passed, reasons = compute_verdict(rows)
        assert passed is False
        assert any("Profit factor dropped" in r for r in reasons)

    def test_severe_dd_increase_fails(self):
        """Verdict is FAIL when max drawdown increases >20%."""
        old = extract_baseline_metrics(SAMPLE_BASELINE)
        # Max DD from 1.4 to 5.0 = +257% increase
        new = extract_metrics(SEVERELY_DEGRADED_RESULTS)
        rows = compare_metrics(old, new)
        passed, reasons = compute_verdict(rows)
        assert passed is False
        assert any("drawdown increased" in r.lower() for r in reasons)

    def test_moderate_degradation_passes(self):
        """Moderate degradation within thresholds still passes."""
        old = extract_baseline_metrics(SAMPLE_BASELINE)
        # PF drops from 1.73 to 1.60 = -7.5% (within 10% threshold)
        moderate = {
            "total_trades": 1500,
            "win_rate": 60.0,
            "profit_factor": 1.60,
            "total_pnl": 24000.00,
            "max_drawdown_pct": 1.6,  # +14% increase (within 20% threshold)
            "expectancy_per_trade": 16.00,
        }
        new = extract_metrics(moderate)
        rows = compare_metrics(old, new)
        passed, reasons = compute_verdict(rows)
        assert passed is True


# ── test_backup_created_with_timestamp ──

class TestBackupCreatedWithTimestamp:
    def test_backup_created(self, tmp_config_dir):
        """Backup file is created with timestamp in name."""
        baseline_path = tmp_config_dir / "backtest_baseline.json"
        backup_path = create_backup(baseline_path)

        assert backup_path is not None
        assert os.path.exists(backup_path)

        today = datetime.now().strftime("%Y%m%d")
        assert today in backup_path
        assert "backtest_baseline_" in backup_path

    def test_backup_preserves_content(self, tmp_config_dir):
        """Backup contains same content as original."""
        baseline_path = tmp_config_dir / "backtest_baseline.json"
        backup_path = create_backup(baseline_path)

        with open(backup_path) as f:
            backup_data = json.load(f)
        assert backup_data == SAMPLE_BASELINE

    def test_multiple_backups_same_day(self, tmp_config_dir):
        """Multiple backups on same day get unique names."""
        baseline_path = tmp_config_dir / "backtest_baseline.json"
        backup1 = create_backup(baseline_path)
        backup2 = create_backup(baseline_path)

        assert backup1 != backup2
        assert os.path.exists(backup1)
        assert os.path.exists(backup2)

    def test_no_backup_if_no_baseline(self, tmp_path):
        """No backup created if baseline doesn't exist."""
        result = create_backup(tmp_path / "nonexistent.json")
        assert result is None


# ── test_ci_fails_on_pf_drop ──

class TestCIFailsOnPFDrop:
    def test_ci_fails_on_large_pf_drop(self):
        """CI should fail when profit factor drops >10%."""
        old = extract_baseline_metrics(SAMPLE_BASELINE)
        # PF drops from 1.73 to 1.10 = -36.4%
        new = extract_metrics(SEVERELY_DEGRADED_RESULTS)
        rows = compare_metrics(old, new)
        passed, reasons = compute_verdict(rows)

        assert passed is False
        pf_fail = [r for r in reasons if "Profit factor" in r and "FAIL" in r]
        assert len(pf_fail) > 0

    def test_ci_fails_on_large_dd_increase(self):
        """CI should fail when max drawdown increases >20%."""
        old = extract_baseline_metrics(SAMPLE_BASELINE)
        new = extract_metrics(SEVERELY_DEGRADED_RESULTS)
        rows = compare_metrics(old, new)
        passed, reasons = compute_verdict(rows)

        assert passed is False
        dd_fail = [r for r in reasons if "drawdown" in r.lower() and "FAIL" in r]
        assert len(dd_fail) > 0


# ── test_ci_passes_on_improvement ──

class TestCIPassesOnImprovement:
    def test_ci_passes_when_all_improved(self):
        """CI passes when all metrics improve."""
        old = extract_baseline_metrics(SAMPLE_BASELINE)
        new = extract_metrics(IMPROVED_RESULTS)
        rows = compare_metrics(old, new)
        passed, reasons = compute_verdict(rows)

        assert passed is True
        assert not any("FAIL" in r for r in reasons)

    def test_ci_passes_on_unchanged(self):
        """CI passes when metrics are unchanged."""
        old = extract_baseline_metrics(SAMPLE_BASELINE)
        new = extract_baseline_metrics(SAMPLE_BASELINE)
        rows = compare_metrics(old, new)
        passed, reasons = compute_verdict(rows)

        assert passed is True


# ── Additional edge case tests ──

class TestEdgeCases:
    def test_extract_metrics_handles_alternate_keys(self):
        """extract_metrics handles both 'win_rate' and 'win_rate_pct'."""
        results_with_win_rate = {"win_rate": 65.0, "profit_factor": 1.5,
                                  "total_trades": 100, "total_pnl": 5000,
                                  "max_drawdown_pct": 2.0, "expectancy": 50.0}
        metrics = extract_metrics(results_with_win_rate)
        assert metrics["win_rate_pct"] == 65.0
        assert metrics["expectancy_per_trade"] == 50.0

    def test_build_baseline_format(self):
        """build_baseline_from_results produces valid baseline format."""
        results = {
            "total_trades": 1500,
            "win_rate": 62.0,
            "profit_factor": 1.75,
            "total_pnl": 26000.0,
            "max_drawdown_pct": 1.5,
            "expectancy_per_trade": 17.33,
            "c1_pnl": 10000.0,
            "c2_pnl": 16000.0,
            "months": 6,
        }
        baseline = build_baseline_from_results(results)

        assert "profit_factor" in baseline
        assert "win_rate_pct" in baseline
        assert "total_trades" in baseline
        assert "total_pnl" in baseline
        assert "hc_filter" in baseline
        assert baseline["hc_filter"]["min_score"] == 0.75
        assert baseline["account_size"] == 50000

    def test_compare_with_zero_old_value(self):
        """Handle division by zero when old value is 0."""
        old = {"total_trades": 0, "win_rate_pct": 0, "profit_factor": 0,
               "total_pnl": 0, "max_drawdown_pct": 0, "expectancy_per_trade": 0}
        new = extract_metrics(IMPROVED_RESULTS)
        rows = compare_metrics(old, new)
        # Should not raise — pct_change should be 0 when old is 0
        for row in rows:
            assert row["pct_change"] == 0 or row["old_value"] != 0

    def test_print_diff_with_none_old(self, capsys):
        """print_diff handles None old baseline gracefully."""
        new = build_baseline_from_results(IMPROVED_RESULTS)
        print_diff(None, new)
        captured = capsys.readouterr()
        assert "creating new baseline" in captured.out.lower()
