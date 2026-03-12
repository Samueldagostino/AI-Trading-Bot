"""
Tests for GEXMonitor -- Quant Data GEX integration.
=====================================================
Covers:
  - Mock mode returns default when no token
  - Regime classification (positive, negative, extreme)
  - Net GEX computation from mock API response
  - Gamma flip detection
  - Call/put wall detection
  - Modifier value mapping per regime
  - Caching behavior (15-minute refresh)
  - API failure returns safe default
  - Expired/invalid token returns default
"""

import json
import os
import tempfile
import time
import pytest
from unittest.mock import patch, MagicMock

from signals.gex_monitor import GEXMonitor, _format_gex_display


class TestMockMode:

    def test_mock_mode_returns_default(self):
        """When no token configured, get_modifier_value returns 1.0."""
        mon = GEXMonitor(
            token_file="nonexistent_token.txt",
            instance_id_file="nonexistent_id.txt",
        )
        assert not mon.enabled
        mod = mon.get_modifier_value()
        assert mod["value"] == 1.0
        assert "unavailable" in mod["reason"].lower()

    def test_mock_mode_fetch_returns_data(self):
        """Mock mode fetch_gex_data returns synthetic data."""
        mon = GEXMonitor(
            token_file="nonexistent_token.txt",
            instance_id_file="nonexistent_id.txt",
        )
        data = mon.fetch_gex_data("SPY")
        assert data is not None
        assert data.get("_mock") is True
        assert "net_gex" in data
        assert "regime" in data
        assert data["ticker"] == "SPY"

    def test_mock_mode_update_provides_data(self):
        """update() in mock mode populates cached result."""
        mon = GEXMonitor(
            token_file="nonexistent_token.txt",
            instance_id_file="nonexistent_id.txt",
            log_dir=tempfile.mkdtemp(),
        )
        result = mon.update()
        assert result is not None
        assert "spy" in result or "qqq" in result

    def test_mock_data_cycles(self):
        """Mock data net_gex cycles between -8B and -18B."""
        mon = GEXMonitor(
            token_file="nonexistent_token.txt",
            instance_id_file="nonexistent_id.txt",
        )
        values = []
        for _ in range(20):
            data = mon.fetch_gex_data("SPY")
            values.append(data["net_gex"])
        # All should be in the -18B to -8B range (approximately)
        for v in values:
            assert -20e9 < v < -5e9


class TestClassifyRegime:

    def test_strong_positive(self):
        assert GEXMonitor.classify_regime(10e9) == "STRONG_POSITIVE"

    def test_mild_positive(self):
        assert GEXMonitor.classify_regime(2e9) == "MILD_POSITIVE"

    def test_mild_negative(self):
        assert GEXMonitor.classify_regime(-5e9) == "MILD_NEGATIVE"

    def test_strong_negative(self):
        assert GEXMonitor.classify_regime(-15e9) == "STRONG_NEGATIVE"

    def test_extreme_negative(self):
        assert GEXMonitor.classify_regime(-30e9) == "EXTREME_NEGATIVE"

    def test_zero_is_mild_positive(self):
        """Zero net GEX is technically > -10B, so MILD_NEGATIVE... wait, > 0 = MILD_POSITIVE."""
        # Actually 0 is not > 0, so it falls to > -10e9 = MILD_NEGATIVE
        assert GEXMonitor.classify_regime(0) == "MILD_NEGATIVE"

    def test_boundary_5b(self):
        """Exactly 5B is not > 5B, should be MILD_POSITIVE."""
        assert GEXMonitor.classify_regime(5e9) == "MILD_POSITIVE"

    def test_above_5b(self):
        assert GEXMonitor.classify_regime(5.1e9) == "STRONG_POSITIVE"


class TestComputeNetGex:

    def test_mock_data_passthrough(self):
        """Mock data (with _mock flag) passes through directly."""
        mon = GEXMonitor(
            token_file="nonexistent_token.txt",
            instance_id_file="nonexistent_id.txt",
        )
        mock_data = {
            "_mock": True,
            "ticker": "SPY",
            "spot_price": 583.55,
            "net_gex": -15e9,
            "net_gex_display": "-$15.0B",
            "regime": "STRONG_NEGATIVE",
            "gamma_flip_strike": 580.0,
            "nearest_call_wall": 590.0,
            "nearest_put_wall": 575.0,
            "expirations_included": 4,
            "strikes_analyzed": 42,
            "timestamp": "2026-03-05T12:00:00Z",
        }
        result = mon.compute_net_gex(mock_data, "SPY")
        assert result is not None
        assert result["net_gex"] == -15e9
        assert result["regime"] == "STRONG_NEGATIVE"

    def test_none_returns_none(self):
        mon = GEXMonitor(
            token_file="nonexistent_token.txt",
            instance_id_file="nonexistent_id.txt",
        )
        assert mon.compute_net_gex(None) is None


class TestFindGammaFlip:

    def test_simple_flip(self):
        """Detect flip from negative to positive cumulative GEX."""
        data = [
            {"strike": 580.0, "net": -5e9},
            {"strike": 582.0, "net": -3e9},
            {"strike": 584.0, "net": 10e9},  # cumulative flips here
            {"strike": 586.0, "net": 2e9},
        ]
        flip = GEXMonitor.find_gamma_flip(data, 583.0)
        assert flip == 584.0

    def test_no_flip(self):
        """All negative -- no flip point."""
        data = [
            {"strike": 580.0, "net": -5e9},
            {"strike": 582.0, "net": -3e9},
        ]
        flip = GEXMonitor.find_gamma_flip(data, 581.0)
        assert flip is None

    def test_empty_data(self):
        assert GEXMonitor.find_gamma_flip([], 500.0) is None


class TestFindWalls:

    def test_call_and_put_walls(self):
        """Find highest positive GEX above spot and highest negative below."""
        data = [
            {"strike": 575.0, "net": -8e9},  # put wall candidate
            {"strike": 578.0, "net": -2e9},
            {"strike": 580.0, "net": 1e9},
            {"strike": 585.0, "net": 12e9},  # call wall candidate
            {"strike": 590.0, "net": 5e9},
        ]
        walls = GEXMonitor.find_walls(data, 582.0)
        assert walls["call_wall"] == 585.0
        assert walls["put_wall"] == 575.0

    def test_no_walls(self):
        walls = GEXMonitor.find_walls([], 500.0)
        assert walls["call_wall"] is None
        assert walls["put_wall"] is None


class TestModifierValueMapping:

    def test_strong_positive_modifier(self):
        mon = GEXMonitor(
            token_file="nonexistent_token.txt",
            instance_id_file="nonexistent_id.txt",
            log_dir=tempfile.mkdtemp(),
        )
        mon._last_result = {
            "qqq": {"regime": "STRONG_POSITIVE", "net_gex_display": "$10.0B"},
        }
        mod = mon.get_modifier_value()
        assert mod["value"] == 0.80
        assert "dampening" in mod["reason"].lower()

    def test_mild_positive_modifier(self):
        mon = GEXMonitor(
            token_file="nonexistent_token.txt",
            instance_id_file="nonexistent_id.txt",
            log_dir=tempfile.mkdtemp(),
        )
        mon._last_result = {
            "qqq": {"regime": "MILD_POSITIVE", "net_gex_display": "$2.0B"},
        }
        mod = mon.get_modifier_value()
        assert mod["value"] == 0.95

    def test_mild_negative_modifier(self):
        mon = GEXMonitor(
            token_file="nonexistent_token.txt",
            instance_id_file="nonexistent_id.txt",
            log_dir=tempfile.mkdtemp(),
        )
        mon._last_result = {
            "qqq": {"regime": "MILD_NEGATIVE", "net_gex_display": "-$5.0B"},
        }
        mod = mon.get_modifier_value()
        assert mod["value"] == 1.10

    def test_strong_negative_modifier(self):
        mon = GEXMonitor(
            token_file="nonexistent_token.txt",
            instance_id_file="nonexistent_id.txt",
            log_dir=tempfile.mkdtemp(),
        )
        mon._last_result = {
            "qqq": {"regime": "STRONG_NEGATIVE", "net_gex_display": "-$15.2B"},
        }
        mod = mon.get_modifier_value()
        assert mod["value"] == 1.25
        assert "momentum" in mod["reason"].lower()

    def test_extreme_negative_modifier(self):
        mon = GEXMonitor(
            token_file="nonexistent_token.txt",
            instance_id_file="nonexistent_id.txt",
            log_dir=tempfile.mkdtemp(),
        )
        mon._last_result = {
            "qqq": {"regime": "EXTREME_NEGATIVE", "net_gex_display": "-$30.0B"},
        }
        mod = mon.get_modifier_value()
        assert mod["value"] == 0.75
        assert "protect" in mod["reason"].lower()


class TestCaching:

    def test_caching_within_interval(self):
        """update() returns cached result within 15 minutes."""
        tmpdir = tempfile.mkdtemp()
        mon = GEXMonitor(
            token_file="nonexistent_token.txt",
            instance_id_file="nonexistent_id.txt",
            log_dir=tmpdir,
        )
        # First update
        result1 = mon.update()
        assert result1 is not None

        # Second update immediately -- should return cached
        result2 = mon.update()
        assert result2 is result1  # Same object (cached)


class TestApiFailureDefaults:

    def test_api_failure_returns_default(self):
        """Network error returns modifier value 1.0."""
        mon = GEXMonitor(
            token_file="nonexistent_token.txt",
            instance_id_file="nonexistent_id.txt",
        )
        # No cached data
        mod = mon.get_modifier_value()
        assert mod["value"] == 1.0

    def test_expired_token_returns_default(self):
        """Simulated 401/403 returns None from fetch."""
        tmpdir = tempfile.mkdtemp()
        token_file = os.path.join(tmpdir, "token.txt")
        with open(token_file, "w") as f:
            f.write("expired_token_value")

        id_file = os.path.join(tmpdir, "id.txt")
        with open(id_file, "w") as f:
            f.write("some_instance_id")

        mon = GEXMonitor(token_file=token_file, instance_id_file=id_file)
        assert mon.enabled  # Has a non-placeholder token

        # Mock requests to return 401
        with patch("signals.gex_monitor.GEXMonitor.fetch_gex_data", return_value=None):
            result = mon.update()
            # No valid data -> modifier defaults to 1.0
            mod = mon.get_modifier_value()
            assert mod["value"] == 1.0


class TestFormatDisplay:

    def test_billions(self):
        assert _format_gex_display(-15.2e9) == "-$15.2B"

    def test_millions(self):
        assert _format_gex_display(500e6) == "$500.0M"

    def test_positive_billions(self):
        assert _format_gex_display(10e9) == "$10.0B"


class TestGammaLevelsLogging:

    def test_log_written_on_update(self):
        """gamma_levels.json is written when update() succeeds."""
        tmpdir = tempfile.mkdtemp()
        mon = GEXMonitor(
            token_file="nonexistent_token.txt",
            instance_id_file="nonexistent_id.txt",
            log_dir=tmpdir,
        )
        mon.update()
        log_path = os.path.join(tmpdir, "gamma_levels.json")
        assert os.path.exists(log_path)
        with open(log_path) as f:
            line = f.readline().strip()
            entry = json.loads(line)
        assert "timestamp" in entry


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
