"""
Tests for config_validator -- v3 backtest replication guard.

Validates:
1. Default config passes all checks (matches v3 baseline)
2. Changing a critical param causes validation failure
3. Non-critical mismatch produces WARN but passes
4. force=True overrides critical failures
5. All v3 baseline values are correct
"""

import pytest

from config.settings import BotConfig
from config.config_validator import (
    validate_config, print_config_table,
    V3_BASELINE, CRITICAL_PARAMS, ConfigCheck,
)


class TestConfigValidatorPass:
    """Default config should pass validation (matches v3)."""

    def test_default_config_passes(self):
        """BotConfig() with current constants should match v3 baseline."""
        cfg = BotConfig()
        checks = validate_config(cfg)
        failures = [c for c in checks if not c.match and c.critical]
        assert len(failures) == 0, (
            f"Critical failures: {[(c.param, c.expected, c.actual) for c in failures]}"
        )

    def test_all_checks_returned(self):
        """validate_config should return checks for all baseline params."""
        cfg = BotConfig()
        checks = validate_config(cfg)
        assert len(checks) >= 15  # At least 15 parameters checked

    def test_print_config_table_returns_true(self, capsys):
        """print_config_table should return True for valid config."""
        cfg = BotConfig()
        result = print_config_table(cfg)
        assert result is True
        captured = capsys.readouterr()
        assert "ALL CHECKS PASSED" in captured.out


class TestConfigValidatorFail:
    """Modified config should fail validation on critical params."""

    def test_hc_max_stop_mismatch(self):
        """Changing HC max stop should cause critical failure."""
        import config.constants as c
        original = c.HIGH_CONVICTION_MAX_STOP_PTS
        try:
            c.HIGH_CONVICTION_MAX_STOP_PTS = 50.0
            cfg = BotConfig()
            checks = validate_config(cfg)
            failures = [ch for ch in checks if ch.param == "hc_max_stop_pts"]
            assert len(failures) == 1
            assert failures[0].match is False
            assert failures[0].critical is True
        finally:
            c.HIGH_CONVICTION_MAX_STOP_PTS = original

    def test_total_contracts_mismatch(self):
        """Wrong contract count should cause critical failure."""
        cfg = BotConfig()
        cfg.scale_out.total_contracts = 2
        checks = validate_config(cfg)
        failures = [c for c in checks if c.param == "total_contracts" and not c.match]
        assert len(failures) == 1
        assert failures[0].critical is True

    def test_force_overrides_failure(self, capsys):
        """force=True should return True even with critical mismatches."""
        cfg = BotConfig()
        cfg.scale_out.total_contracts = 2
        result = print_config_table(cfg, force=True)
        assert result is True
        captured = capsys.readouterr()
        assert "force-config" in captured.out.lower() or "override" in captured.out.lower()

    def test_no_force_blocks_on_failure(self, capsys):
        """force=False should return False with critical mismatches."""
        cfg = BotConfig()
        cfg.scale_out.total_contracts = 2
        result = print_config_table(cfg, force=False)
        assert result is False
        captured = capsys.readouterr()
        assert "BLOCKED" in captured.out


class TestV3BaselineValues:
    """Verify the v3 baseline constants are correct."""

    def test_hc_max_stop_is_30(self):
        assert V3_BASELINE["hc_max_stop_pts"] == 30.0

    def test_hc_min_score_is_075(self):
        assert V3_BASELINE["hc_min_score"] == 0.75

    def test_htf_gate_is_03(self):
        assert V3_BASELINE["htf_gate_threshold"] == 0.3

    def test_htf_timeframes_5m_15m(self):
        assert V3_BASELINE["htf_timeframes"] == frozenset({"15m", "5m"})

    def test_total_contracts_is_5(self):
        assert V3_BASELINE["total_contracts"] == 5

    def test_c3_contracts_is_3(self):
        assert V3_BASELINE["c3_contracts"] == 3

    def test_c2_be_variant_is_b(self):
        assert V3_BASELINE["c2_be_variant"] == "B"

    def test_daily_loss_pct_is_3(self):
        assert V3_BASELINE["daily_loss_pct"] == 3.0

    def test_kill_switch_enabled(self):
        assert V3_BASELINE["kill_switch_enabled"] is True

    def test_critical_params_include_key_fields(self):
        """All essential trading parameters must be in CRITICAL_PARAMS."""
        for param in ["hc_max_stop_pts", "hc_min_score", "htf_gate_threshold",
                       "total_contracts", "c2_be_variant", "kill_switch_enabled"]:
            assert param in CRITICAL_PARAMS, f"{param} missing from CRITICAL_PARAMS"
