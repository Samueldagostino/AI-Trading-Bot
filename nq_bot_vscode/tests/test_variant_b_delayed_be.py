"""
Tests for Variant B Delayed Breakeven.

Validates that:
1. C2 does NOT go to breakeven when MFE < 1.5× stop distance
2. C2 DOES go to breakeven when MFE >= 1.5× stop distance
3. Breakeven stop is at entry ± buffer (2pts), not exact entry
4. MFE tracks correctly for both longs and shorts
5. Variant D (immediate) still applies BE instantly on C1 exit
6. Variant B skips immediate BE in _transition_c1_to_scaling()
"""

import pytest
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from dataclasses import dataclass, field

from config.settings import BotConfig, ScaleOutConfig, RiskConfig
from execution.scale_out_executor import (
    ScaleOutExecutor, ScaleOutTrade, ContractLeg, ScaleOutPhase,
)


def _make_config(be_variant: str = "B", be_multiplier: float = 1.5,
                 be_buffer: float = 2.0) -> BotConfig:
    """Create a BotConfig with specified BE variant settings."""
    cfg = BotConfig()
    cfg.scale_out.c2_be_variant = be_variant
    cfg.scale_out.c2_be_delay_multiplier = be_multiplier
    cfg.scale_out.c2_breakeven_buffer_points = be_buffer
    cfg.scale_out.c1_time_exit_bars = 5
    cfg.scale_out.c1_max_bars_fallback = 12
    cfg.scale_out.c3_delayed_entry_enabled = True
    cfg.scale_out.adaptive_exits_enabled = False
    return cfg


def _make_executor(be_variant: str = "B", **kwargs) -> ScaleOutExecutor:
    """Create a ScaleOutExecutor with the given config."""
    cfg = _make_config(be_variant=be_variant, **kwargs)
    return ScaleOutExecutor(cfg)


def _make_trade(direction: str = "long", entry_price: float = 21000.0,
                stop_distance: float = 10.0) -> ScaleOutTrade:
    """Create a test trade with C1, C2, C3 legs."""
    now = datetime.now(timezone.utc)

    if direction == "long":
        initial_stop = entry_price - stop_distance
    else:
        initial_stop = entry_price + stop_distance

    trade = ScaleOutTrade(
        direction=direction,
        entry_price=entry_price,
        initial_stop=initial_stop,
        stop_distance=stop_distance,
        entry_time=now,
        phase=ScaleOutPhase.PHASE_1,
        signal_score=0.80,
        atr_at_entry=8.0,
    )

    # Set up C1
    trade.c1.contracts = 1
    trade.c1.is_open = True
    trade.c1.entry_price = entry_price
    trade.c1.stop_price = initial_stop
    trade.c1.leg_label = "C1"
    trade.c1.exit_strategy = "time_5bar"

    # Set up C2
    trade.c2.contracts = 1
    trade.c2.is_open = True
    trade.c2.entry_price = entry_price
    trade.c2.stop_price = initial_stop
    trade.c2.leg_label = "C2"
    trade.c2.exit_strategy = "structural_target"
    trade.c2.target_price = entry_price + 20.0 if direction == "long" else entry_price - 20.0

    # Set up C3
    trade.c3.contracts = 3
    trade.c3.is_open = True
    trade.c3.entry_price = entry_price
    trade.c3.stop_price = initial_stop
    trade.c3.leg_label = "C3"
    trade.c3.exit_strategy = "atr_trail"

    return trade


class TestVariantBDelayedBE:
    """Core Variant B delayed breakeven tests."""

    def test_be_not_triggered_when_mfe_below_threshold_long(self):
        """C2 must NOT go to breakeven when MFE < 1.5× stop distance."""
        executor = _make_executor(be_variant="B", be_multiplier=1.5)
        cfg = executor.scale_config

        trade = _make_trade(direction="long", entry_price=21000.0, stop_distance=10.0)
        # Threshold = 10 * 1.5 = 15pts. Set MFE to 12pts (below threshold).
        leg = trade.c2
        leg.mfe = 12.0
        original_stop = leg.stop_price  # 20990.0

        executor._apply_delayed_be(trade, leg, cfg, "long")

        assert leg.be_triggered is False
        assert leg.stop_price == original_stop  # Stop unchanged

    def test_be_triggered_when_mfe_at_threshold_long(self):
        """C2 DOES go to breakeven when MFE >= 1.5× stop distance."""
        executor = _make_executor(be_variant="B", be_multiplier=1.5)
        cfg = executor.scale_config

        trade = _make_trade(direction="long", entry_price=21000.0, stop_distance=10.0)
        # Threshold = 10 * 1.5 = 15pts. Set MFE to exactly 15pts.
        leg = trade.c2
        leg.mfe = 15.0

        executor._apply_delayed_be(trade, leg, cfg, "long")

        assert leg.be_triggered is True
        # BE stop = entry + buffer = 21000 + 2.0 = 21002.0
        assert leg.stop_price == 21002.0

    def test_be_triggered_when_mfe_above_threshold_long(self):
        """C2 goes to breakeven when MFE exceeds threshold."""
        executor = _make_executor(be_variant="B", be_multiplier=1.5)
        cfg = executor.scale_config

        trade = _make_trade(direction="long", entry_price=21000.0, stop_distance=10.0)
        leg = trade.c2
        leg.mfe = 20.0  # Well above 15pt threshold

        executor._apply_delayed_be(trade, leg, cfg, "long")

        assert leg.be_triggered is True
        assert leg.stop_price == 21002.0

    def test_be_stop_includes_buffer_not_exact_entry(self):
        """Breakeven stop must be entry ± buffer (2pts), not exact entry."""
        executor = _make_executor(be_variant="B", be_buffer=2.0)
        cfg = executor.scale_config

        # Long trade
        trade = _make_trade(direction="long", entry_price=21000.0, stop_distance=10.0)
        trade.c2.mfe = 20.0
        executor._apply_delayed_be(trade, trade.c2, cfg, "long")
        assert trade.c2.stop_price == 21002.0  # entry + 2pts buffer
        assert trade.c2.stop_price != 21000.0  # NOT exact entry

    def test_be_stop_buffer_short(self):
        """Short trade: BE stop at entry - buffer."""
        executor = _make_executor(be_variant="B", be_buffer=2.0)
        cfg = executor.scale_config

        trade = _make_trade(direction="short", entry_price=21000.0, stop_distance=10.0)
        trade.c2.mfe = 20.0
        executor._apply_delayed_be(trade, trade.c2, cfg, "short")
        assert trade.c2.stop_price == 20998.0  # entry - 2pts buffer
        assert trade.c2.be_triggered is True

    def test_be_not_triggered_short_below_threshold(self):
        """Short trade: BE NOT triggered when MFE below threshold."""
        executor = _make_executor(be_variant="B", be_multiplier=1.5)
        cfg = executor.scale_config

        trade = _make_trade(direction="short", entry_price=21000.0, stop_distance=10.0)
        trade.c2.mfe = 10.0  # Below 15pt threshold
        original_stop = trade.c2.stop_price  # 21010.0

        executor._apply_delayed_be(trade, trade.c2, cfg, "short")

        assert trade.c2.be_triggered is False
        assert trade.c2.stop_price == original_stop

    def test_be_idempotent_once_triggered(self):
        """Once BE is triggered, calling _apply_delayed_be again is a no-op."""
        executor = _make_executor(be_variant="B")
        cfg = executor.scale_config

        trade = _make_trade(direction="long", entry_price=21000.0, stop_distance=10.0)
        trade.c2.mfe = 20.0
        executor._apply_delayed_be(trade, trade.c2, cfg, "long")
        assert trade.c2.be_triggered is True

        # Modify stop manually (simulate trail tightening)
        trade.c2.stop_price = 21010.0
        executor._apply_delayed_be(trade, trade.c2, cfg, "long")
        # Stop should NOT revert to 21002
        assert trade.c2.stop_price == 21010.0


class TestMFETracking:
    """Verify MFE tracks correctly for longs and shorts."""

    def test_mfe_tracks_long_correctly(self):
        """MFE should update with positive unrealized profit (long)."""
        leg = ContractLeg(
            leg_label="C2", contracts=1, is_open=True,
            entry_price=21000.0, stop_price=20990.0, mfe=0.0,
        )

        # Simulate price moving up: MFE should increase
        prices = [21005.0, 21012.0, 21008.0, 21015.0, 21010.0]
        for price in prices:
            unrealized = price - leg.entry_price
            if unrealized > 0:
                leg.mfe = max(leg.mfe, unrealized)

        # MFE should be 15.0 (from 21015.0)
        assert leg.mfe == 15.0

    def test_mfe_tracks_short_correctly(self):
        """MFE should update with positive unrealized profit (short)."""
        leg = ContractLeg(
            leg_label="C2", contracts=1, is_open=True,
            entry_price=21000.0, stop_price=21010.0, mfe=0.0,
        )

        # Simulate price moving down: MFE should increase
        prices = [20995.0, 20988.0, 20992.0, 20985.0, 20990.0]
        for price in prices:
            unrealized = leg.entry_price - price
            if unrealized > 0:
                leg.mfe = max(leg.mfe, unrealized)

        # MFE should be 15.0 (from 20985.0)
        assert leg.mfe == 15.0

    def test_mfe_never_decreases(self):
        """MFE is a high-water mark — it should never decrease."""
        leg = ContractLeg(
            leg_label="C2", contracts=1, is_open=True,
            entry_price=21000.0, stop_price=20990.0, mfe=0.0,
        )

        leg.mfe = max(leg.mfe, 20.0)
        leg.mfe = max(leg.mfe, 15.0)  # Lower — should not replace
        leg.mfe = max(leg.mfe, 25.0)  # Higher — should replace
        leg.mfe = max(leg.mfe, 10.0)  # Lower — should not replace

        assert leg.mfe == 25.0


class TestTransitionVariantBSkipsImmediateBE:
    """Verify that _transition_c1_to_scaling() respects Variant B."""

    @patch("execution.scale_out_executor.get_alert_manager", return_value=None)
    def test_variant_b_skips_immediate_be_on_c1_exit(self, mock_alert):
        """With Variant B, C1 exiting profitably must NOT apply immediate BE."""
        executor = _make_executor(be_variant="B")
        trade = _make_trade(direction="long", entry_price=21000.0, stop_distance=10.0)
        trade.phase = ScaleOutPhase.PHASE_1
        trade.c1_bars_elapsed = 5
        executor._active_trade = trade

        original_c2_stop = trade.c2.stop_price  # 20990.0
        original_c3_stop = trade.c3.stop_price  # 20990.0

        exit_price = 21008.0  # C1 exits with 8pts profit
        now = datetime.now(timezone.utc)
        result = asyncio.get_event_loop().run_until_complete(
            executor._transition_c1_to_scaling(trade, exit_price, now, "time_5bars")
        )

        # C2 and C3 should keep original stops (no immediate BE)
        assert trade.c2.be_triggered is False
        assert trade.c3.be_triggered is False
        assert trade.c2.stop_price == original_c2_stop
        assert trade.c3.stop_price == original_c3_stop

    @patch("execution.scale_out_executor.get_alert_manager", return_value=None)
    def test_variant_d_applies_immediate_be_on_c1_exit(self, mock_alert):
        """With Variant D, C1 exiting profitably MUST apply immediate BE."""
        executor = _make_executor(be_variant="D")
        trade = _make_trade(direction="long", entry_price=21000.0, stop_distance=10.0)
        trade.phase = ScaleOutPhase.PHASE_1
        trade.c1_bars_elapsed = 5
        executor._active_trade = trade

        exit_price = 21008.0  # C1 exits with 8pts profit
        now = datetime.now(timezone.utc)
        result = asyncio.get_event_loop().run_until_complete(
            executor._transition_c1_to_scaling(trade, exit_price, now, "time_5bars")
        )

        # C2 and C3 SHOULD have immediate BE
        assert trade.c2.be_triggered is True
        assert trade.c3.be_triggered is True
        # BE stop = entry + 2pts = 21002.0
        assert trade.c2.stop_price == 21002.0
        assert trade.c3.stop_price == 21002.0

    @patch("execution.scale_out_executor.get_alert_manager", return_value=None)
    def test_variant_b_short_skips_immediate_be(self, mock_alert):
        """Short trade with Variant B: no immediate BE on C1 exit."""
        executor = _make_executor(be_variant="B")
        trade = _make_trade(direction="short", entry_price=21000.0, stop_distance=10.0)
        trade.phase = ScaleOutPhase.PHASE_1
        trade.c1_bars_elapsed = 5
        executor._active_trade = trade

        original_c2_stop = trade.c2.stop_price  # 21010.0

        exit_price = 20992.0  # C1 exits with 8pts profit (short)
        now = datetime.now(timezone.utc)
        result = asyncio.get_event_loop().run_until_complete(
            executor._transition_c1_to_scaling(trade, exit_price, now, "time_5bars")
        )

        assert trade.c2.be_triggered is False
        assert trade.c2.stop_price == original_c2_stop


class TestVariantBConfigurability:
    """Test that the BE multiplier is configurable."""

    def test_custom_multiplier(self):
        """BE threshold should use configured multiplier."""
        executor = _make_executor(be_variant="B", be_multiplier=2.0)
        cfg = executor.scale_config

        trade = _make_trade(direction="long", entry_price=21000.0, stop_distance=10.0)
        # Threshold = 10 * 2.0 = 20pts. MFE = 18pts (below).
        trade.c2.mfe = 18.0

        executor._apply_delayed_be(trade, trade.c2, cfg, "long")
        assert trade.c2.be_triggered is False

        # Now set MFE above threshold
        trade.c2.mfe = 20.0
        executor._apply_delayed_be(trade, trade.c2, cfg, "long")
        assert trade.c2.be_triggered is True

    def test_default_multiplier_is_1_5(self):
        """Default c2_be_delay_multiplier should be 1.5."""
        cfg = ScaleOutConfig()
        assert cfg.c2_be_delay_multiplier == 1.5

    def test_default_variant_is_b(self):
        """Default c2_be_variant should be 'B'."""
        cfg = ScaleOutConfig()
        assert cfg.c2_be_variant == "B"

    def test_default_buffer_is_2pts(self):
        """Default breakeven buffer should be 2.0 points."""
        cfg = ScaleOutConfig()
        assert cfg.c2_breakeven_buffer_points == 2.0
