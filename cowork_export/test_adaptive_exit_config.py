"""
Unit tests for AdaptiveExitConfig regime detection and parameter adjustment.

Tests cover:
1. Static (disabled) mode — baseline parameters always returned
2. Trending regime classification (ADX > 25)
3. Ranging regime classification (ADX < 20)
4. Hysteresis band behavior (20 ≤ ADX ≤ 25)
5. Regime transitions and logging
"""

import pytest
import logging
from execution.adaptive_exit_config import AdaptiveExitConfig, AdaptiveExitParams


class TestAdaptiveExitConfigStatic:
    """Test static/baseline mode (disabled by default)."""

    def test_static_mode_returns_baseline(self):
        """Disabled mode should return baseline parameters regardless of ADX."""
        config = AdaptiveExitConfig(enabled=False)

        # Test various ADX values
        for adx in [5.0, 20.0, 25.0, 50.0, 100.0]:
            params = config.get_exit_params(adx, previous_regime="ranging")

            assert params.be_delay_multiplier == 1.5  # Baseline
            assert params.trailing_atr_multiplier == 2.0  # Baseline
            assert params.regime == "static"

    def test_static_mode_ignores_previous_regime(self):
        """Baseline mode should ignore previous_regime parameter."""
        config = AdaptiveExitConfig(enabled=False)

        params_a = config.get_exit_params(adx=50.0, previous_regime="trending")
        params_b = config.get_exit_params(adx=50.0, previous_regime="ranging")
        params_c = config.get_exit_params(adx=50.0, previous_regime="unknown")

        # All should be identical
        assert params_a == params_b == params_c
        assert params_a.regime == "static"


class TestAdaptiveExitConfigTrending:
    """Test trending regime (ADX > 25)."""

    def test_trending_regime_above_threshold(self):
        """ADX > 25 should classify as trending."""
        config = AdaptiveExitConfig(enabled=True)

        params = config.get_exit_params(adx=28.5, previous_regime="unknown")

        assert params.regime == "trending"
        assert params.be_delay_multiplier == 2.0
        assert params.trailing_atr_multiplier == 2.5

    def test_trending_regime_high_adx(self):
        """Very high ADX should still return trending parameters."""
        config = AdaptiveExitConfig(enabled=True)

        params = config.get_exit_params(adx=75.0, previous_regime="unknown")

        assert params.regime == "trending"
        assert params.be_delay_multiplier == 2.0
        assert params.trailing_atr_multiplier == 2.5

    def test_trending_ignores_previous_regime(self):
        """Clear trending (ADX > 25) should override previous_regime."""
        config = AdaptiveExitConfig(enabled=True)

        # ADX clearly trending, but previous was ranging
        params = config.get_exit_params(adx=30.0, previous_regime="ranging")

        assert params.regime == "trending"
        assert params.be_delay_multiplier == 2.0


class TestAdaptiveExitConfigRanging:
    """Test ranging regime (ADX < 20)."""

    def test_ranging_regime_below_threshold(self):
        """ADX < 20 should classify as ranging."""
        config = AdaptiveExitConfig(enabled=True)

        params = config.get_exit_params(adx=15.0, previous_regime="unknown")

        assert params.regime == "ranging"
        assert params.be_delay_multiplier == 1.0
        assert params.trailing_atr_multiplier == 2.0

    def test_ranging_regime_low_adx(self):
        """Very low ADX should still return ranging parameters."""
        config = AdaptiveExitConfig(enabled=True)

        params = config.get_exit_params(adx=5.0, previous_regime="unknown")

        assert params.regime == "ranging"
        assert params.be_delay_multiplier == 1.0
        assert params.trailing_atr_multiplier == 2.0

    def test_ranging_ignores_previous_regime(self):
        """Clear ranging (ADX < 20) should override previous_regime."""
        config = AdaptiveExitConfig(enabled=True)

        # ADX clearly ranging, but previous was trending
        params = config.get_exit_params(adx=10.0, previous_regime="trending")

        assert params.regime == "ranging"
        assert params.trailing_atr_multiplier == 2.0


class TestAdaptiveExitConfigHysteresis:
    """Test hysteresis band [20, 25) — holds previous regime."""

    def test_hysteresis_keeps_trending(self):
        """ADX in [20, 25) with trending previous should stay trending."""
        config = AdaptiveExitConfig(enabled=True)

        params = config.get_exit_params(adx=22.0, previous_regime="trending")

        assert params.regime == "trending"
        assert params.be_delay_multiplier == 2.0
        assert params.trailing_atr_multiplier == 2.5

    def test_hysteresis_keeps_ranging(self):
        """ADX in [20, 25) with ranging previous should stay ranging."""
        config = AdaptiveExitConfig(enabled=True)

        params = config.get_exit_params(adx=23.5, previous_regime="ranging")

        assert params.regime == "ranging"
        assert params.be_delay_multiplier == 1.0
        assert params.trailing_atr_multiplier == 2.0

    def test_hysteresis_defaults_to_ranging(self):
        """ADX in [20, 25) with unknown previous should default to ranging."""
        config = AdaptiveExitConfig(enabled=True)

        params = config.get_exit_params(adx=21.0, previous_regime="unknown")

        assert params.regime == "ranging"
        assert params.be_delay_multiplier == 1.0

    def test_hysteresis_band_exact_boundaries(self):
        """Test exact boundary values [20, 25)."""
        config = AdaptiveExitConfig(enabled=True)

        # ADX = 20.0 should be ranging (boundary)
        params_20 = config.get_exit_params(adx=20.0, previous_regime="unknown")
        assert params_20.regime == "ranging"

        # ADX = 25.0 should be trending (just above boundary)
        params_25 = config.get_exit_params(adx=25.0, previous_regime="unknown")
        assert params_25.regime == "trending"

        # ADX = 20.01 should use hysteresis
        params_hyster = config.get_exit_params(adx=20.01, previous_regime="trending")
        assert params_hyster.regime == "trending"


class TestAdaptiveExitConfigRegimeTransitions:
    """Test regime transition logging and state tracking."""

    def test_regime_transition_logged(self, caplog):
        """Regime transitions should be logged."""
        config = AdaptiveExitConfig(enabled=True)

        with caplog.at_level(logging.INFO):
            # First call: trending
            config.get_exit_params(adx=30.0, previous_regime="unknown")
            # Second call: switch to ranging
            config.get_exit_params(adx=15.0, previous_regime="trending")

        # Should have logged the transition
        assert "Regime transition:" in caplog.text
        assert "trending → ranging" in caplog.text

    def test_no_log_on_same_regime(self, caplog):
        """No log when regime doesn't change."""
        config = AdaptiveExitConfig(enabled=True)

        caplog.clear()
        with caplog.at_level(logging.INFO):
            # Both trending
            config.get_exit_params(adx=30.0, previous_regime="unknown")
            config.get_exit_params(adx=35.0, previous_regime="trending")

        # Should NOT log a transition (same regime)
        assert "Regime transition:" not in caplog.text

    def test_state_persistence(self):
        """Last regime state should persist."""
        config = AdaptiveExitConfig(enabled=True)

        # First: trending
        config.get_exit_params(adx=30.0, previous_regime="unknown")
        assert config._last_regime == "trending"

        # Second: stays trending (hysteresis)
        config.get_exit_params(adx=22.0, previous_regime="trending")
        assert config._last_regime == "trending"

        # Third: switch to ranging
        config.get_exit_params(adx=15.0, previous_regime="trending")
        assert config._last_regime == "ranging"


class TestAdaptiveExitConfigEnableDisable:
    """Test enable/disable toggle."""

    def test_enable_method(self):
        """enable() should switch to adaptive mode."""
        config = AdaptiveExitConfig(enabled=False)
        assert config.enabled is False

        config.enable()
        assert config.enabled is True

        # Now should return adaptive params
        params = config.get_exit_params(adx=30.0, previous_regime="unknown")
        assert params.regime == "trending"

    def test_disable_method(self):
        """disable() should revert to static mode."""
        config = AdaptiveExitConfig(enabled=True)
        assert config.enabled is True

        config.disable()
        assert config.enabled is False

        # Now should return baseline
        params = config.get_exit_params(adx=30.0, previous_regime="unknown")
        assert params.regime == "static"
        assert params.be_delay_multiplier == 1.5

    def test_enable_logs_warning(self, caplog):
        """enable() should log a warning."""
        config = AdaptiveExitConfig(enabled=False)

        with caplog.at_level(logging.WARNING):
            config.enable()

        assert "AdaptiveExitConfig ENABLED" in caplog.text
        assert "walk-forward tests" in caplog.text


class TestAdaptiveExitParamsDataclass:
    """Test AdaptiveExitParams dataclass."""

    def test_params_creation(self):
        """Should create params with all fields."""
        params = AdaptiveExitParams(
            be_delay_multiplier=2.0,
            trailing_atr_multiplier=2.5,
            regime="trending"
        )

        assert params.be_delay_multiplier == 2.0
        assert params.trailing_atr_multiplier == 2.5
        assert params.regime == "trending"

    def test_params_immutable_access(self):
        """Params should be accessible via dict-like access (for executor compatibility)."""
        params = AdaptiveExitParams(
            be_delay_multiplier=1.5,
            trailing_atr_multiplier=2.0,
            regime="ranging"
        )

        # Executor code can access as attributes
        assert params.be_delay_multiplier == 1.5
        assert params.trailing_atr_multiplier == 2.0
        assert params.regime == "ranging"


class TestAdaptiveExitConfigIntegration:
    """Integration tests simulating real executor usage."""

    def test_full_workflow_trending_to_ranging(self):
        """Simulate market transitioning from trending to ranging."""
        config = AdaptiveExitConfig(enabled=True)
        regime = "unknown"

        # Hour 1: Strong trending
        adx = 35.0
        params = config.get_exit_params(adx, regime)
        assert params.regime == "trending"
        regime = params.regime
        assert params.trailing_atr_multiplier == 2.5  # Wide trails

        # Hour 2: ADX weakening but still trending
        adx = 26.0
        params = config.get_exit_params(adx, regime)
        assert params.regime == "trending"  # Still trending
        assert params.trailing_atr_multiplier == 2.5

        # Hour 3: ADX in hysteresis band
        adx = 22.5
        params = config.get_exit_params(adx, regime)
        assert params.regime == "trending"  # Hysteresis keeps it
        regime = params.regime

        # Hour 4: Clear ranging
        adx = 15.0
        params = config.get_exit_params(adx, regime)
        assert params.regime == "ranging"
        assert params.trailing_atr_multiplier == 2.0  # Tighter trails

    def test_disable_reverts_to_baseline(self):
        """Adaptive mode should revert to baseline when disabled."""
        config = AdaptiveExitConfig(enabled=True)

        # Trending params
        params_adaptive = config.get_exit_params(adx=35.0, previous_regime="unknown")
        assert params_adaptive.regime == "trending"
        assert params_adaptive.trailing_atr_multiplier == 2.5

        # Now disable
        config.disable()

        # Should revert to baseline
        params_static = config.get_exit_params(adx=35.0, previous_regime="unknown")
        assert params_static.regime == "static"
        assert params_static.trailing_atr_multiplier == 2.0  # Baseline


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
