"""
Startup Config Validator -- v3 Backtest Replication Guard
==========================================================
Compares live runner configuration against the validated v3 backtest
parameters. Refuses to trade if any critical parameter mismatches
unless --force-config is passed.

Usage:
    from config.config_validator import validate_config, print_config_table
    issues = validate_config(bot_config)
    print_config_table(bot_config)
    if issues and not force_config:
        sys.exit(1)
"""

import logging
from dataclasses import dataclass
from typing import List, Tuple

from config.constants import (
    HIGH_CONVICTION_MIN_SCORE,
    HIGH_CONVICTION_MAX_STOP_PTS,
    HTF_STRENGTH_GATE,
    HTF_TIMEFRAMES,
    MIN_RR_OVERRIDE,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# V3 BASELINE -- the validated backtest config
# 396 trades, PF 2.86, 70.5% WR, +$47,236, 1.60% max DD
# ═══════════════════════════════════════════════════════════════

V3_BASELINE = {
    # Signal filters
    "hc_min_score":       0.75,
    "hc_max_stop_pts":    30.0,
    "htf_gate_threshold": 0.3,
    "htf_timeframes":     frozenset({"15m", "5m"}),

    # Execution
    "total_contracts":    4,
    "c1_contracts":       1,
    "c2_contracts":       1,
    "c3_contracts":       2,
    "c1_time_exit_bars":  5,
    "c1_max_bars_fallback": 12,
    "c2_be_variant":      "B",
    "c2_be_delay_multiplier": 1.5,
    "c2_breakeven_buffer_pts": 2.0,
    "c3_delayed_entry":   True,

    # Risk
    "daily_loss_pct":     3.0,
    "max_drawdown_pct":   10.0,
    "kill_switch_enabled": True,
    "max_contracts":      4,

    # Engine
    "execution_tf":       "2m",
    "point_value":        2.0,
    "commission":         1.50,
}

# Parameters that MUST match -- mismatch = refuse to trade
CRITICAL_PARAMS = {
    "hc_max_stop_pts", "hc_min_score", "htf_gate_threshold",
    "htf_timeframes", "total_contracts", "c2_be_variant",
    "daily_loss_pct", "kill_switch_enabled", "c3_delayed_entry",
}


@dataclass
class ConfigCheck:
    """Result of a single parameter check."""
    param: str
    expected: object
    actual: object
    match: bool
    critical: bool

    @property
    def status(self) -> str:
        return "MATCH" if self.match else "MISMATCH"

    @property
    def icon(self) -> str:
        if self.match:
            return "[PASS]"
        return "[FAIL]" if self.critical else "[WARN]"


def validate_config(bot_config) -> List[ConfigCheck]:
    """
    Compare live config against v3 baseline.

    Returns list of ConfigCheck results. Any critical mismatch
    means the runner should refuse to start (unless --force-config).
    """
    checks = []

    def _check(param: str, actual, expected=None):
        if expected is None:
            expected = V3_BASELINE[param]
        is_critical = param in CRITICAL_PARAMS
        match = actual == expected
        checks.append(ConfigCheck(param, expected, actual, match, is_critical))

    sc = bot_config.scale_out
    rc = bot_config.risk

    # Signal filters
    _check("hc_min_score", HIGH_CONVICTION_MIN_SCORE)
    _check("hc_max_stop_pts", HIGH_CONVICTION_MAX_STOP_PTS)
    _check("htf_gate_threshold", HTF_STRENGTH_GATE)
    _check("htf_timeframes", HTF_TIMEFRAMES)

    # Execution
    _check("total_contracts", sc.total_contracts)
    _check("c1_contracts", sc.c1_contracts)
    _check("c2_contracts", sc.c2_contracts)
    _check("c3_contracts", sc.c3_contracts)
    _check("c1_time_exit_bars", sc.c1_time_exit_bars)
    _check("c1_max_bars_fallback", sc.c1_max_bars_fallback)
    _check("c2_be_variant", sc.c2_be_variant)
    _check("c2_be_delay_multiplier", sc.c2_be_delay_multiplier)
    _check("c2_breakeven_buffer_pts", sc.c2_breakeven_buffer_points)
    _check("c3_delayed_entry", sc.c3_delayed_entry_enabled)

    # Risk
    _check("daily_loss_pct", rc.max_daily_loss_pct)
    _check("max_drawdown_pct", rc.max_total_drawdown_pct)
    _check("kill_switch_enabled", rc.kill_switch_enabled)
    _check("max_contracts", rc.max_contracts_micro)

    # Engine
    _check("point_value", rc.nq_point_value_micro)
    _check("commission", rc.commission_per_contract)

    return checks


def print_config_table(bot_config, force: bool = False) -> bool:
    """
    Print config comparison table and return True if all critical checks pass.

    If force=True, logs warnings but returns True regardless.
    """
    checks = validate_config(bot_config)

    # Header
    print()
    print("=" * 72)
    print("  CONFIG VALIDATION -- v3 Backtest Replication Check")
    print("=" * 72)
    print(f"  {'Parameter':<30s} {'Expected':<15s} {'Actual':<15s} {'Status'}")
    print("-" * 72)

    critical_failures = []

    for c in checks:
        exp_str = str(c.expected)[:14]
        act_str = str(c.actual)[:14]
        marker = c.icon
        print(f"  {c.param:<30s} {exp_str:<15s} {act_str:<15s} {marker}")
        if not c.match and c.critical:
            critical_failures.append(c)

    print("-" * 72)

    if critical_failures:
        print(f"  CRITICAL MISMATCHES: {len(critical_failures)}")
        for c in critical_failures:
            print(f"    {c.param}: expected {c.expected}, got {c.actual}")
        print()
        if not force:
            print("  TRADING BLOCKED -- config does not match v3 backtest.")
            print("  Use --force-config to override (NOT RECOMMENDED).")
        else:
            print("  WARNING: --force-config active -- trading allowed despite mismatches.")
        print("=" * 72)
        logger.warning(
            "Config validation FAILED: %d critical mismatches",
            len(critical_failures),
        )
        return force  # Only pass if force=True
    else:
        print("  ALL CHECKS PASSED -- config matches v3 backtest.")
        print("=" * 72)
        logger.info("Config validation PASSED: all parameters match v3 baseline")
        return True
