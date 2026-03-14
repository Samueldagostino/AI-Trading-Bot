"""
Configuration Validator -- Startup Safety Check
================================================
Validates all config values are in sane ranges before the bot starts.
If any config is invalid, prints a clear error and returns False.

Called during TradingOrchestrator.initialize() -- if validation fails,
the bot refuses to start.
"""

import logging
import math

from config.settings import BotConfig
from config.constants import (
    HIGH_CONVICTION_MIN_SCORE,
    HIGH_CONVICTION_MAX_STOP_PTS,
    HTF_STRENGTH_GATE,
    SWEEP_MIN_SCORE,
    SWEEP_CONFLUENCE_BONUS,
)

logger = logging.getLogger(__name__)


class ConfigValidationError:
    """A single config validation failure."""
    def __init__(self, field: str, value, reason: str):
        self.field = field
        self.value = value
        self.reason = reason

    def __str__(self):
        return f"  CONFIG ERROR: {self.field} = {self.value!r} -- {self.reason}"


def validate_config(config: BotConfig) -> list:
    """
    Validate all configuration values are in valid ranges.

    Returns:
        List of ConfigValidationError. Empty list means all OK.
    """
    errors = []

    def _check(field, value, condition, reason):
        if not condition:
            errors.append(ConfigValidationError(field, value, reason))

    def _finite_positive(field, value, label=""):
        if not isinstance(value, (int, float)):
            errors.append(ConfigValidationError(field, value, "must be numeric"))
            return
        if not math.isfinite(value):
            errors.append(ConfigValidationError(field, value, "must be finite (not NaN/Inf)"))
            return
        if value <= 0:
            errors.append(ConfigValidationError(field, value, "must be positive"))

    # ── Risk Config ──
    r = config.risk
    _finite_positive("risk.account_size", r.account_size)
    _finite_positive("risk.max_risk_per_trade_pct", r.max_risk_per_trade_pct)
    _check("risk.max_risk_per_trade_pct", r.max_risk_per_trade_pct,
           0 < r.max_risk_per_trade_pct <= 10,
           "must be 0-10%")
    _finite_positive("risk.max_daily_loss_pct", r.max_daily_loss_pct)
    _check("risk.max_daily_loss_pct", r.max_daily_loss_pct,
           0 < r.max_daily_loss_pct <= 20,
           "must be 0-20%")
    _finite_positive("risk.max_total_drawdown_pct", r.max_total_drawdown_pct)
    _check("risk.max_contracts_micro", r.max_contracts_micro,
           0 < r.max_contracts_micro <= 10,
           "must be 1-10")
    _finite_positive("risk.atr_period", r.atr_period)
    _finite_positive("risk.atr_multiplier_stop", r.atr_multiplier_stop)
    _finite_positive("risk.min_rr_ratio", r.min_rr_ratio)
    _check("risk.kill_switch_max_consecutive_losses", r.kill_switch_max_consecutive_losses,
           r.kill_switch_max_consecutive_losses > 0,
           "must be positive")

    # ── Scale-Out Config ──
    s = config.scale_out
    _check("scale_out.total_contracts", s.total_contracts,
           s.total_contracts > 0, "must be positive")
    _check("scale_out.c1_contracts", s.c1_contracts,
           s.c1_contracts > 0, "must be positive")
    _finite_positive("scale_out.c1_profit_threshold_pts", s.c1_profit_threshold_pts)
    _finite_positive("scale_out.c1_trail_distance_pts", s.c1_trail_distance_pts)
    _check("scale_out.c1_max_bars_fallback", s.c1_max_bars_fallback,
           s.c1_max_bars_fallback > 0, "must be positive")

    # ── HC Constants ──
    _check("HIGH_CONVICTION_MIN_SCORE", HIGH_CONVICTION_MIN_SCORE,
           0 < HIGH_CONVICTION_MIN_SCORE <= 1.0,
           "must be 0-1")
    _check("HIGH_CONVICTION_MAX_STOP_PTS", HIGH_CONVICTION_MAX_STOP_PTS,
           HIGH_CONVICTION_MAX_STOP_PTS > 0,
           "must be positive")
    _check("HTF_STRENGTH_GATE", HTF_STRENGTH_GATE,
           0 <= HTF_STRENGTH_GATE <= 1.0,
           "must be 0-1")
    _check("SWEEP_MIN_SCORE", SWEEP_MIN_SCORE,
           0 < SWEEP_MIN_SCORE <= 1.0,
           "must be 0-1")
    _check("SWEEP_CONFLUENCE_BONUS", SWEEP_CONFLUENCE_BONUS,
           0 <= SWEEP_CONFLUENCE_BONUS <= 0.5,
           "must be 0-0.5")

    # ── Execution Config ──
    e = config.execution
    _check("execution.order_timeout_seconds", e.order_timeout_seconds,
           e.order_timeout_seconds > 0, "must be positive")
    _check("execution.max_retry_attempts", e.max_retry_attempts,
           e.max_retry_attempts >= 0, "must be non-negative")

    # ── Signal Config ──
    sig = config.signals
    _check("signals.min_confluence_score", sig.min_confluence_score,
           0 < sig.min_confluence_score <= 1.0,
           "must be 0-1")

    if errors:
        logger.critical("=" * 60)
        logger.critical("CONFIGURATION VALIDATION FAILED -- %d errors:", len(errors))
        for err in errors:
            logger.critical(str(err))
        logger.critical("=" * 60)
        logger.critical("Fix the above config errors and restart.")

    return errors


def validate_scale_out_config(config: BotConfig) -> list:
    """
    BUG FIX 2.8: Validate all 13 scale-out config fields exist and are in valid ranges.

    Returns:
        List of ConfigValidationError. Empty list means all OK.
    """
    errors = []
    warnings = []

    def _check(field, value, condition, reason):
        if not condition:
            errors.append(ConfigValidationError(field, value, reason))

    def _warn(field, value, reason):
        msg = f"  CONFIG WARNING: {field} = {value!r} -- {reason}"
        warnings.append(msg)
        logger.warning(msg)

    s = config.scale_out

    # Check all 13 scale-out fields exist and are valid
    # 1. c1_time_exit_bars
    if not hasattr(s, 'c1_time_exit_bars'):
        _warn("scale_out.c1_time_exit_bars", None, "missing field, using default 5")
    else:
        _check("scale_out.c1_time_exit_bars", s.c1_time_exit_bars,
               isinstance(s.c1_time_exit_bars, int) and s.c1_time_exit_bars > 0,
               "must be positive integer")

    # 2. c1_max_bars_fallback
    if not hasattr(s, 'c1_max_bars_fallback'):
        _warn("scale_out.c1_max_bars_fallback", None, "missing field, using default 12")
    else:
        _check("scale_out.c1_max_bars_fallback", s.c1_max_bars_fallback,
               isinstance(s.c1_max_bars_fallback, int) and
               s.c1_max_bars_fallback > getattr(s, 'c1_time_exit_bars', 5),
               "must be greater than c1_time_exit_bars")

    # 3. c1_profit_threshold_pts
    if not hasattr(s, 'c1_profit_threshold_pts'):
        _warn("scale_out.c1_profit_threshold_pts", None, "missing field, using default 0")
    else:
        _check("scale_out.c1_profit_threshold_pts", s.c1_profit_threshold_pts,
               isinstance(s.c1_profit_threshold_pts, (int, float)) and s.c1_profit_threshold_pts >= 0,
               "must be non-negative number")

    # 4. c2_time_stop_bars
    if not hasattr(s, 'c2_time_stop_bars'):
        _warn("scale_out.c2_time_stop_bars", None, "missing field, using default 15")
    else:
        _check("scale_out.c2_time_stop_bars", s.c2_time_stop_bars,
               isinstance(s.c2_time_stop_bars, int) and s.c2_time_stop_bars > 0,
               "must be positive integer")

    # 5. c2_be_variant
    if not hasattr(s, 'c2_be_variant'):
        _warn("scale_out.c2_be_variant", None, "missing field, using default 'B'")
    else:
        _check("scale_out.c2_be_variant", s.c2_be_variant,
               s.c2_be_variant in ("A", "B", "C", "D"),
               "must be one of 'A', 'B', 'C', 'D'")

    # 6. c2_be_delay_multiplier
    if not hasattr(s, 'c2_be_delay_multiplier'):
        _warn("scale_out.c2_be_delay_multiplier", None, "missing field, using default 2.0")
    else:
        _check("scale_out.c2_be_delay_multiplier", s.c2_be_delay_multiplier,
               isinstance(s.c2_be_delay_multiplier, (int, float)) and s.c2_be_delay_multiplier > 0,
               "must be positive number")

    # 7. c2_breakeven_buffer_points
    if not hasattr(s, 'c2_breakeven_buffer_points'):
        _warn("scale_out.c2_breakeven_buffer_points", None, "missing field, using default 1.5")
    else:
        _check("scale_out.c2_breakeven_buffer_points", s.c2_breakeven_buffer_points,
               isinstance(s.c2_breakeven_buffer_points, (int, float)) and s.c2_breakeven_buffer_points >= 0,
               "must be non-negative number")

    # 8. c3_trailing_atr_multiplier
    if not hasattr(s, 'c3_trailing_atr_multiplier'):
        _warn("scale_out.c3_trailing_atr_multiplier", None, "missing field, using default 2.0")
    else:
        _check("scale_out.c3_trailing_atr_multiplier", s.c3_trailing_atr_multiplier,
               isinstance(s.c3_trailing_atr_multiplier, (int, float)) and s.c3_trailing_atr_multiplier > 0,
               "must be positive number")

    # 9. c3_max_target_points
    if not hasattr(s, 'c3_max_target_points'):
        _warn("scale_out.c3_max_target_points", None, "missing field, using default 30.0")
    else:
        _check("scale_out.c3_max_target_points", s.c3_max_target_points,
               isinstance(s.c3_max_target_points, (int, float)) and s.c3_max_target_points > 0,
               "must be positive number")

    # 10. c3_time_stop_minutes
    if not hasattr(s, 'c3_time_stop_minutes'):
        _warn("scale_out.c3_time_stop_minutes", None, "missing field, using default 120")
    else:
        _check("scale_out.c3_time_stop_minutes", s.c3_time_stop_minutes,
               isinstance(s.c3_time_stop_minutes, int) and s.c3_time_stop_minutes > 0,
               "must be positive integer")

    # 11. c3_contracts
    if not hasattr(s, 'c3_contracts'):
        _warn("scale_out.c3_contracts", None, "missing field, using default 1")
    else:
        _check("scale_out.c3_contracts", s.c3_contracts,
               isinstance(s.c3_contracts, int) and s.c3_contracts >= 1,
               "must be at least 1")

    # 12. c3_delayed_entry_enabled
    if not hasattr(s, 'c3_delayed_entry_enabled'):
        _warn("scale_out.c3_delayed_entry_enabled", None, "missing field, using default False")
    else:
        _check("scale_out.c3_delayed_entry_enabled", s.c3_delayed_entry_enabled,
               isinstance(s.c3_delayed_entry_enabled, bool),
               "must be boolean")

    # 13. c2_contracts (derived, but check it's consistent)
    if not hasattr(s, 'c2_contracts'):
        _warn("scale_out.c2_contracts", None, "missing field, using default 1")
    else:
        _check("scale_out.c2_contracts", s.c2_contracts,
               isinstance(s.c2_contracts, int) and s.c2_contracts >= 1,
               "must be at least 1")

    if errors:
        logger.critical("=" * 60)
        logger.critical("SCALE-OUT CONFIG VALIDATION FAILED -- %d errors:", len(errors))
        for err in errors:
            logger.critical(str(err))
        logger.critical("=" * 60)
        logger.critical("Fix the above config errors and restart.")

    return errors
