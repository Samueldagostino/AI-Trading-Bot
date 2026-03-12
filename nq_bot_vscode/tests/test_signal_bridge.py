"""
Tests for SignalBridge -- the signal-to-execution translation layer.

Covers:
  - Long signal -> correct BUY-side OrderRequest with C1+C2
  - Short signal -> correct SELL-side OrderRequest with C1+C2
  - Stop/target price calculations match config
  - Signal metadata preserved on order audit trail
  - Rejected signals (below threshold) produce no orders
  - HTF gate enforcement
  - Min R:R ratio enforcement
  - Max stop distance enforcement
"""

import pytest
from datetime import datetime, timezone

from execution.signal_bridge import (
    SignalBridge,
    TradeDecision,
    ScaleOutParams,
    BridgeResult,
    MIN_SIGNAL_SCORE,
    MAX_STOP_DISTANCE_PTS,
)
from config.settings import RiskConfig


# ═══════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def risk_config():
    return RiskConfig()


@pytest.fixture
def bridge(risk_config):
    return SignalBridge(risk_config)


def _long_decision(**overrides) -> TradeDecision:
    """Factory for a standard long trade decision."""
    defaults = dict(
        direction="long",
        entry_price=21000.0,
        signal_score=0.82,
        atr=10.0,
        htf_bias="bullish",
        htf_allows_long=True,
        htf_allows_short=True,
        entry_source="signal",
        market_regime="trending_up",
        timestamp=datetime(2026, 3, 1, 14, 30, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return TradeDecision(**defaults)


def _short_decision(**overrides) -> TradeDecision:
    """Factory for a standard short trade decision."""
    defaults = dict(
        direction="short",
        entry_price=21000.0,
        signal_score=0.85,
        atr=10.0,
        htf_bias="bearish",
        htf_allows_long=True,
        htf_allows_short=True,
        entry_source="signal",
        market_regime="trending_down",
        timestamp=datetime(2026, 3, 1, 14, 30, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return TradeDecision(**defaults)


# ═══════════════════════════════════════════════════════════════
# LONG SIGNAL -> BUY OrderRequest with C1+C2
# ═══════════════════════════════════════════════════════════════

class TestLongSignal:
    """Long signal translates into correct scale-out entry params."""

    def test_approved(self, bridge):
        result = bridge.translate(_long_decision())
        assert result.approved is True
        assert result.params is not None

    def test_direction_is_long(self, bridge):
        result = bridge.translate(_long_decision())
        assert result.params.direction == "long"

    def test_market_entry(self, bridge):
        result = bridge.translate(_long_decision())
        assert result.params.limit_price == 0.0

    def test_stop_loss_below_entry(self, bridge):
        """Long stop = entry - (ATR × atr_multiplier_stop)."""
        result = bridge.translate(_long_decision())
        # ATR=10, multiplier=2.0 -> stop distance = 20pts
        # Entry 21000 - 20 = 20980
        assert result.params.stop_loss == 20980.0

    def test_c1_target_above_entry(self, bridge):
        """Long target = entry + (ATR × atr_multiplier_target), with min R:R."""
        result = bridge.translate(_long_decision())
        # ATR=10, target multiplier=1.5 -> raw target distance = 15pts
        # Stop distance = 20pts, min R:R = 1.5 -> min target = 20*1.5 = 30pts
        # 30 > 15, so R:R enforcement kicks in -> target distance = 30pts
        # Entry 21000 + 30 = 21030
        assert result.params.c1_take_profit == 21030.0

    def test_params_unpackable_to_executor(self, bridge):
        """ScaleOutParams maps 1:1 to place_scale_out_entry() signature."""
        result = bridge.translate(_long_decision())
        p = result.params
        # These are the exact kwargs for place_scale_out_entry()
        assert hasattr(p, "direction")
        assert hasattr(p, "limit_price")
        assert hasattr(p, "stop_loss")
        assert hasattr(p, "c1_take_profit")


# ═══════════════════════════════════════════════════════════════
# SHORT SIGNAL -> SELL OrderRequest with C1+C2
# ═══════════════════════════════════════════════════════════════

class TestShortSignal:
    """Short signal translates into correct scale-out entry params."""

    def test_approved(self, bridge):
        result = bridge.translate(_short_decision())
        assert result.approved is True
        assert result.params is not None

    def test_direction_is_short(self, bridge):
        result = bridge.translate(_short_decision())
        assert result.params.direction == "short"

    def test_stop_loss_above_entry(self, bridge):
        """Short stop = entry + (ATR × atr_multiplier_stop)."""
        result = bridge.translate(_short_decision())
        # Entry 21000 + 20 = 21020
        assert result.params.stop_loss == 21020.0

    def test_c1_target_below_entry(self, bridge):
        """Short target = entry - target distance."""
        result = bridge.translate(_short_decision())
        # Same R:R enforcement -> target distance = 30pts
        # Entry 21000 - 30 = 20970
        assert result.params.c1_take_profit == 20970.0

    def test_market_entry(self, bridge):
        result = bridge.translate(_short_decision())
        assert result.params.limit_price == 0.0


# ═══════════════════════════════════════════════════════════════
# STOP / TARGET PRICE CALCULATIONS
# ═══════════════════════════════════════════════════════════════

class TestStopTargetCalculation:
    """Verify stop/target math matches RiskConfig values."""

    def test_stop_uses_atr_multiplier_stop(self, bridge, risk_config):
        """stop_distance = atr × atr_multiplier_stop."""
        decision = _long_decision(atr=12.0)
        result = bridge.translate(decision)
        expected_stop_dist = 12.0 * risk_config.atr_multiplier_stop  # 24.0
        assert result.params.stop_loss == round(
            21000.0 - expected_stop_dist, 2
        )

    def test_target_uses_atr_multiplier_target(self):
        """When R:R is already satisfied, target = atr × atr_multiplier_target."""
        # Use config where min_rr_ratio won't override
        config = RiskConfig(
            atr_multiplier_stop=1.0,
            atr_multiplier_target=3.0,
            min_rr_ratio=1.5,
        )
        bridge = SignalBridge(config)
        decision = _long_decision(atr=10.0)
        result = bridge.translate(decision)
        # Stop dist = 10, target dist = 30, R:R = 3.0 > 1.5 -> no override
        assert result.params.c1_take_profit == 21030.0

    def test_min_rr_ratio_enforced(self, bridge, risk_config):
        """If raw R:R < min_rr_ratio, target is bumped up."""
        # Default: stop mult=2.0, target mult=1.5
        # Raw R:R = 1.5/2.0 = 0.75 < min 1.5
        # -> target_dist = stop_dist * 1.5 = 20 * 1.5 = 30
        decision = _long_decision(atr=10.0)
        result = bridge.translate(decision)
        assert result.metadata["rr_ratio"] == 1.5
        assert result.metadata["target_distance_pts"] == 30.0

    def test_stop_distance_in_metadata(self, bridge):
        result = bridge.translate(_long_decision(atr=10.0))
        assert result.metadata["stop_distance_pts"] == 20.0

    def test_target_distance_in_metadata(self, bridge):
        result = bridge.translate(_long_decision(atr=10.0))
        # R:R enforced -> 30pts
        assert result.metadata["target_distance_pts"] == 30.0

    def test_prices_rounded_to_2dp(self, bridge):
        decision = _long_decision(atr=7.33)
        result = bridge.translate(decision)
        # Stop dist = 7.33 * 2.0 = 14.66
        # Target dist = 7.33 * 1.5 = 10.995 -> 11.0 rounded
        # R:R = 11.0 / 14.66 = 0.75 < 1.5 -> enforced: 14.66 * 1.5 = 21.99
        assert result.params.stop_loss == round(21000.0 - 14.66, 2)
        assert result.params.c1_take_profit == round(21000.0 + 21.99, 2)

    def test_small_atr_still_computes(self, bridge):
        """Even with tiny ATR, math works correctly."""
        decision = _long_decision(atr=1.0)
        result = bridge.translate(decision)
        # Stop dist = 2.0pts, target enforced to 3.0pts (R:R)
        assert result.params.stop_loss == 20998.0
        assert result.params.c1_take_profit == 21003.0

    def test_short_stop_above_entry(self, bridge):
        decision = _short_decision(entry_price=21500.0, atr=8.0)
        result = bridge.translate(decision)
        # Stop dist = 16pts -> stop at 21516
        assert result.params.stop_loss == 21516.0

    def test_short_target_below_entry(self, bridge):
        decision = _short_decision(entry_price=21500.0, atr=8.0)
        result = bridge.translate(decision)
        # Target enforced: 16 * 1.5 = 24pts -> target at 21476
        assert result.params.c1_take_profit == 21476.0

    def test_config_values_match_defaults(self, risk_config):
        """Verify test assumptions match actual RiskConfig defaults."""
        assert risk_config.atr_multiplier_stop == 2.0
        assert risk_config.atr_multiplier_target == 1.5
        assert risk_config.min_rr_ratio == 1.5


# ═══════════════════════════════════════════════════════════════
# SIGNAL METADATA PRESERVED FOR AUDIT
# ═══════════════════════════════════════════════════════════════

class TestMetadataAudit:
    """Signal score and HTF bias state attached as metadata."""

    def test_signal_score_in_metadata(self, bridge):
        result = bridge.translate(_long_decision(signal_score=0.88))
        assert result.metadata["signal_score"] == 0.88

    def test_htf_bias_in_metadata(self, bridge):
        result = bridge.translate(_long_decision(htf_bias="bullish"))
        assert result.metadata["htf_bias"] == "bullish"

    def test_htf_allows_flags_in_metadata(self, bridge):
        result = bridge.translate(_long_decision())
        assert result.metadata["htf_allows_long"] is True
        assert result.metadata["htf_allows_short"] is True

    def test_entry_source_in_metadata(self, bridge):
        result = bridge.translate(_long_decision(entry_source="confluence"))
        assert result.metadata["entry_source"] == "confluence"

    def test_market_regime_in_metadata(self, bridge):
        result = bridge.translate(_long_decision(market_regime="ranging"))
        assert result.metadata["market_regime"] == "ranging"

    def test_atr_in_metadata(self, bridge):
        result = bridge.translate(_long_decision(atr=12.5))
        assert result.metadata["atr"] == 12.5

    def test_direction_in_metadata(self, bridge):
        result = bridge.translate(_long_decision())
        assert result.metadata["direction"] == "long"

    def test_entry_price_in_metadata(self, bridge):
        result = bridge.translate(_long_decision(entry_price=21234.75))
        assert result.metadata["entry_price"] == 21234.75

    def test_timestamp_in_metadata(self, bridge):
        ts = datetime(2026, 3, 1, 14, 30, tzinfo=timezone.utc)
        result = bridge.translate(_long_decision(timestamp=ts))
        assert result.metadata["timestamp"] == ts.isoformat()

    def test_computed_values_in_metadata(self, bridge):
        result = bridge.translate(_long_decision(atr=10.0))
        assert "stop_distance_pts" in result.metadata
        assert "target_distance_pts" in result.metadata
        assert "rr_ratio" in result.metadata
        assert "stop_loss" in result.metadata
        assert "c1_take_profit" in result.metadata

    def test_metadata_present_on_rejection(self, bridge):
        """Metadata populated even when signal is rejected."""
        result = bridge.translate(_long_decision(signal_score=0.50))
        assert result.approved is False
        assert result.metadata["signal_score"] == 0.50
        assert result.metadata["direction"] == "long"

    def test_sweep_source_preserved(self, bridge):
        result = bridge.translate(_long_decision(entry_source="sweep"))
        assert result.metadata["entry_source"] == "sweep"


# ═══════════════════════════════════════════════════════════════
# REJECTED SIGNALS -- BELOW THRESHOLD, NO ORDERS
# ═══════════════════════════════════════════════════════════════

class TestRejectedSignals:
    """Signals below HC threshold produce no orders."""

    def test_score_below_threshold_rejected(self, bridge):
        result = bridge.translate(_long_decision(signal_score=0.50))
        assert result.approved is False
        assert result.params is None
        assert "score" in result.rejection_reason

    def test_score_at_boundary_rejected(self, bridge):
        """Score exactly 0.749 is below 0.75 threshold."""
        result = bridge.translate(_long_decision(signal_score=0.749))
        assert result.approved is False

    def test_score_at_threshold_approved(self, bridge):
        """Score exactly 0.75 meets the minimum."""
        result = bridge.translate(_long_decision(signal_score=0.75))
        assert result.approved is True

    def test_score_zero_rejected(self, bridge):
        result = bridge.translate(_long_decision(signal_score=0.0))
        assert result.approved is False

    def test_htf_blocks_long(self, bridge):
        result = bridge.translate(_long_decision(
            htf_allows_long=False,
            htf_bias="bearish",
        ))
        assert result.approved is False
        assert "HTF blocks long" in result.rejection_reason

    def test_htf_blocks_short(self, bridge):
        result = bridge.translate(_short_decision(
            htf_allows_short=False,
            htf_bias="bullish",
        ))
        assert result.approved is False
        assert "HTF blocks short" in result.rejection_reason

    def test_htf_allows_opposite_still_approved(self, bridge):
        """HTF blocking short does NOT block long."""
        result = bridge.translate(_long_decision(htf_allows_short=False))
        assert result.approved is True

    def test_stop_distance_exceeds_max(self, bridge):
        """ATR so large that stop distance > 30pts -> rejected."""
        result = bridge.translate(_long_decision(atr=20.0))
        # stop_dist = 20 * 2.0 = 40pts > 30
        assert result.approved is False
        assert "stop" in result.rejection_reason
        assert "30" in result.rejection_reason

    def test_stop_distance_at_max_approved(self, bridge):
        """Stop distance exactly 30pts is allowed."""
        result = bridge.translate(_long_decision(atr=15.0))
        # stop_dist = 15 * 2.0 = 30pts = max
        assert result.approved is True

    def test_zero_atr_rejected(self, bridge):
        result = bridge.translate(_long_decision(atr=0.0))
        assert result.approved is False
        assert "ATR" in result.rejection_reason

    def test_negative_atr_rejected(self, bridge):
        result = bridge.translate(_long_decision(atr=-5.0))
        assert result.approved is False

    def test_invalid_direction_rejected(self, bridge):
        result = bridge.translate(_long_decision(direction="sideways"))
        assert result.approved is False
        assert "direction" in result.rejection_reason

    def test_rejection_counter_increments(self, bridge):
        bridge.translate(_long_decision(signal_score=0.50))
        bridge.translate(_long_decision(signal_score=0.40))
        assert bridge.rejections == 2

    def test_approval_counter_increments(self, bridge):
        bridge.translate(_long_decision())
        bridge.translate(_short_decision())
        assert bridge.translations == 2


# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════

class TestConstants:
    """Verify constants match documented HC filter values."""

    def test_min_signal_score(self):
        assert MIN_SIGNAL_SCORE == 0.75

    def test_max_stop_distance(self):
        assert MAX_STOP_DISTANCE_PTS == 30.0


# ═══════════════════════════════════════════════════════════════
# DEFAULT CONFIG
# ═══════════════════════════════════════════════════════════════

class TestDefaultConfig:
    """Bridge works with default RiskConfig when none provided."""

    def test_no_config_uses_defaults(self):
        bridge = SignalBridge()
        result = bridge.translate(_long_decision())
        assert result.approved is True

    def test_custom_config_respected(self):
        config = RiskConfig(atr_multiplier_stop=3.0)
        bridge = SignalBridge(config)
        decision = _long_decision(atr=10.0)
        result = bridge.translate(decision)
        # Stop dist = 10 * 3.0 = 30pts
        assert result.params.stop_loss == 20970.0
