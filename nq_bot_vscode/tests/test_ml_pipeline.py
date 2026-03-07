"""
Tests for the ML entry classification pipeline.
"""

import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pytest

# Add project paths
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "nq_bot_vscode"))

from nq_bot_vscode.ml.feature_builder import MLFeatureBuilder, N_FEATURES, FEATURE_NAMES
from nq_bot_vscode.ml.predictor import MLPredictor, MLPrediction


# ── Minimal stubs for FeatureSnapshot, HTFBiasResult, RiskState ──


@dataclass
class StubOrderBlock:
    direction: str = "bullish"
    zone_high: float = 21050.0
    zone_low: float = 21040.0
    is_valid: bool = True


@dataclass
class StubFVG:
    gap_type: str = "bullish"
    gap_high: float = 21060.0
    gap_low: float = 21050.0
    is_valid: bool = True


@dataclass
class StubFeatureSnapshot:
    timestamp: datetime = field(default_factory=lambda: datetime(2025, 10, 15, 14, 30, tzinfo=timezone.utc))
    atr_14: float = 12.5
    session_vwap: float = 21000.0
    price_vs_vwap: float = 5.0
    cumulative_delta: int = 500
    volume_imbalance: float = 0.3
    vix_level: float = 18.5
    detected_regime: str = "ranging"
    near_bullish_ob: bool = True
    near_bearish_ob: bool = False
    inside_bullish_fvg: bool = True
    inside_bearish_fvg: bool = False
    recent_buy_sweep: bool = False
    recent_sell_sweep: bool = True
    delta_divergence: bool = True
    trend_direction: str = "up"
    trend_strength: float = 0.6
    active_order_blocks: List = field(default_factory=lambda: [StubOrderBlock()])
    active_fvgs: List = field(default_factory=lambda: [StubFVG()])
    structural_stop_long: Optional[float] = 20990.0
    structural_stop_short: Optional[float] = 21060.0


@dataclass
class StubHTFBias:
    consensus_direction: str = "bullish"
    consensus_strength: float = 0.75
    htf_allows_long: bool = True
    htf_allows_short: bool = False


@dataclass
class StubRiskState:
    consecutive_losses: int = 2
    current_equity: float = 50000.0
    daily_pnl: float = -200.0


# ================================================================
# Feature Builder Tests
# ================================================================


class TestFeatureBuilder:

    def test_feature_builder_output_shape(self):
        """Feature vector should have correct shape (1, N_FEATURES)."""
        builder = MLFeatureBuilder()
        fs = StubFeatureSnapshot()
        htf = StubHTFBias()
        risk = StubRiskState()

        X = builder.build(fs, htf, risk)

        assert X.shape == (1, N_FEATURES), f"Expected (1, {N_FEATURES}), got {X.shape}"

    def test_feature_builder_no_nan_inf(self):
        """Feature vector should never contain NaN or Inf."""
        builder = MLFeatureBuilder()
        fs = StubFeatureSnapshot()
        X = builder.build(fs)
        assert np.all(np.isfinite(X)), "Feature vector contains NaN/Inf"

    def test_feature_builder_handles_missing_fields(self):
        """Feature builder should handle a minimal snapshot gracefully."""
        builder = MLFeatureBuilder()
        # Minimal snapshot with almost all defaults
        fs = StubFeatureSnapshot(
            atr_14=0.0,
            session_vwap=0.0,
            price_vs_vwap=0.0,
            cumulative_delta=0,
            volume_imbalance=0.0,
            vix_level=0.0,
            detected_regime="unknown",
            near_bullish_ob=False,
            near_bearish_ob=False,
            inside_bullish_fvg=False,
            inside_bearish_fvg=False,
            recent_buy_sweep=False,
            recent_sell_sweep=False,
            delta_divergence=False,
            active_order_blocks=[],
            active_fvgs=[],
        )

        X = builder.build(fs)
        assert X.shape == (1, N_FEATURES)
        assert np.all(np.isfinite(X))

    def test_feature_builder_handles_none_htf_and_risk(self):
        """Should produce valid output with no HTF bias or risk state."""
        builder = MLFeatureBuilder()
        fs = StubFeatureSnapshot()
        X = builder.build(fs, htf_bias=None, risk_state=None)
        assert X.shape == (1, N_FEATURES)
        assert np.all(np.isfinite(X))

    def test_feature_names_count(self):
        """Feature names list should match N_FEATURES."""
        assert len(FEATURE_NAMES) == N_FEATURES

    def test_feature_names_accessible(self):
        """Static method returns correct feature names."""
        builder = MLFeatureBuilder()
        names = builder.feature_names()
        assert len(names) == N_FEATURES
        assert "atr_normalized" in names
        assert "vix_level" in names


# ================================================================
# Predictor Tests
# ================================================================


class TestPredictor:

    def test_predictor_returns_neutral_without_model(self):
        """With no model loaded, predictor returns neutral."""
        predictor = MLPredictor()
        fs = StubFeatureSnapshot()
        result = predictor.predict(fs)

        assert isinstance(result, MLPrediction)
        assert result.direction == "neutral"
        assert result.confidence == 0.0

    def test_predictor_is_loaded_false_without_model(self):
        """is_loaded should be False when no model loaded."""
        predictor = MLPredictor()
        assert predictor.is_loaded is False

    def test_predictor_confidence_range(self):
        """Confidence should always be in [0.0, 1.0]."""
        predictor = MLPredictor()
        fs = StubFeatureSnapshot()

        # Without model, confidence is 0.0
        result = predictor.predict(fs)
        assert 0.0 <= result.confidence <= 1.0

    def test_predictor_handles_bad_model_path(self):
        """Should gracefully handle a nonexistent model path."""
        predictor = MLPredictor(model_path="/nonexistent/model.lgb")
        assert predictor.is_loaded is False
        result = predictor.predict(StubFeatureSnapshot())
        assert result.direction == "neutral"

    def test_predictor_with_mock_model(self):
        """Test prediction with a mocked LightGBM model."""
        predictor = MLPredictor()
        mock_model = MagicMock()
        mock_model.predict.return_value = np.array([0.85])
        predictor._model = mock_model

        fs = StubFeatureSnapshot()
        htf = StubHTFBias(consensus_direction="bullish")

        result = predictor.predict(fs, htf_bias=htf)
        assert result.direction == "long"
        assert result.confidence > 0.5

    def test_predictor_neutral_low_confidence(self):
        """Model returning near 0.5 should yield neutral (low confidence)."""
        predictor = MLPredictor()
        mock_model = MagicMock()
        mock_model.predict.return_value = np.array([0.52])
        predictor._model = mock_model

        fs = StubFeatureSnapshot()
        htf = StubHTFBias()

        result = predictor.predict(fs, htf_bias=htf)
        # confidence = abs(0.52 - 0.5) * 2 = 0.04 < 0.5 -> neutral
        assert result.direction == "neutral"


# ================================================================
# Trainer Tests (require lightgbm)
# ================================================================


class TestTrainer:

    @pytest.fixture
    def sample_trade_log(self):
        """Generate a synthetic trade log with features."""
        np.random.seed(42)
        trades = []
        for i in range(100):
            month = 9 + (i // 25)
            if month > 12:
                month = 12
            features = np.random.randn(N_FEATURES).tolist()
            pnl = float(np.random.randn() * 50)
            trades.append({
                "entry_time": f"2025-{month:02d}-{(i % 28) + 1:02d}T10:00:00",
                "features": features,
                "net_pnl": pnl,
            })
        return trades

    def test_trainer_produces_model(self, sample_trade_log):
        """Trainer should produce a valid model from sample data."""
        try:
            from nq_bot_vscode.ml.trainer import WalkForwardTrainer
        except ImportError:
            pytest.skip("lightgbm not installed")

        trainer = WalkForwardTrainer()
        X, y = trainer.build_training_set(sample_trade_log)
        assert X.shape[0] == 100
        assert X.shape[1] == N_FEATURES

        model = trainer.train(X, y, feature_names=FEATURE_NAMES)
        assert model is not None

        # Model should produce predictions
        preds = model.predict(X[:5])
        assert len(preds) == 5
        assert all(0 <= p <= 1 for p in preds)

    def test_walk_forward_no_lookahead(self, sample_trade_log):
        """Walk-forward folds must have test period strictly after train period."""
        try:
            from nq_bot_vscode.ml.trainer import WalkForwardTrainer
        except ImportError:
            pytest.skip("lightgbm not installed")

        trainer = WalkForwardTrainer(train_months=2, retrain_interval_months=1)
        results = trainer.walk_forward_train(
            sample_trade_log, feature_names=FEATURE_NAMES,
        )

        for r in results:
            assert r.test_start >= r.train_end, (
                f"Lookahead detected: test_start={r.test_start} < train_end={r.train_end}"
            )

    def test_trainer_empty_log(self):
        """Trainer handles empty trade log gracefully."""
        try:
            from nq_bot_vscode.ml.trainer import WalkForwardTrainer
        except ImportError:
            pytest.skip("lightgbm not installed")

        trainer = WalkForwardTrainer()
        X, y = trainer.build_training_set([])
        assert X.shape[0] == 0

    def test_trainer_save_load(self, sample_trade_log, tmp_path):
        """Model round-trips through save/load."""
        try:
            from nq_bot_vscode.ml.trainer import WalkForwardTrainer
        except ImportError:
            pytest.skip("lightgbm not installed")

        trainer = WalkForwardTrainer()
        X, y = trainer.build_training_set(sample_trade_log)
        model = trainer.train(X, y, feature_names=FEATURE_NAMES)
        assert model is not None

        model_path = str(tmp_path / "test_model.lgb")
        trainer.save_model(model, model_path)
        loaded = trainer.load_model(model_path)

        # Both should produce same predictions
        orig_preds = model.predict(X[:5])
        loaded_preds = loaded.predict(X[:5])
        np.testing.assert_array_almost_equal(orig_preds, loaded_preds)


# ================================================================
# Aggregator Integration Tests
# ================================================================


class TestAggregatorIntegration:

    @dataclass
    class StubSignalConfig:
        min_confluence_score: float = 0.60
        discord_weight: float = 0.25
        technical_weight: float = 0.50
        ml_weight: float = 0.25
        min_signals_aligned: int = 3
        max_signals_required: int = 7

    @dataclass
    class StubConfig:
        signals: object = field(default_factory=lambda: TestAggregatorIntegration.StubSignalConfig())

    def test_aggregator_graceful_without_ml(self):
        """Aggregator works identically when ml_predictor is None."""
        from nq_bot_vscode.signals.aggregator import SignalAggregator

        config = self.StubConfig()
        agg = SignalAggregator(config, ml_predictor=None)

        # Should not crash and should handle no signals
        result = agg.aggregate()
        assert result is None  # No signals = no trade

    def test_aggregator_includes_ml_when_available(self):
        """Aggregator auto-generates ML prediction when predictor is set."""
        from nq_bot_vscode.signals.aggregator import SignalAggregator

        mock_predictor = MagicMock()
        mock_pred = MLPrediction(direction="long", confidence=0.8)
        mock_predictor.predict.return_value = mock_pred

        config = self.StubConfig()
        agg = SignalAggregator(config, ml_predictor=mock_predictor)

        # Build a feature snapshot that produces technical signals
        fs = StubFeatureSnapshot()
        htf = StubHTFBias()

        result = agg.aggregate(
            feature_snapshot=fs,
            htf_bias=htf,
            current_time=datetime(2025, 10, 15, 14, 30, tzinfo=timezone.utc),
        )

        # The predictor should have been called
        mock_predictor.predict.assert_called_once()

    def test_aggregator_ml_prediction_passthrough(self):
        """When ml_prediction is explicitly passed, predictor is NOT called."""
        from nq_bot_vscode.signals.aggregator import SignalAggregator

        mock_predictor = MagicMock()
        config = self.StubConfig()
        agg = SignalAggregator(config, ml_predictor=mock_predictor)

        ml_pred = {"direction": "long", "confidence": 0.9}
        agg.aggregate(ml_prediction=ml_pred)

        # Predictor should NOT have been called since ml_prediction was provided
        mock_predictor.predict.assert_not_called()
