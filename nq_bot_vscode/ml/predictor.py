"""
ML Predictor
==============
Loads a trained LightGBM model and produces entry quality predictions.
Gracefully degrades to neutral when no model is available.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from nq_bot_vscode.ml.feature_builder import MLFeatureBuilder

logger = logging.getLogger(__name__)

try:
    import lightgbm as lgb
    HAS_LGB = True
except ImportError:
    lgb = None  # type: ignore[assignment]
    HAS_LGB = False


@dataclass
class MLPrediction:
    """Result of ML entry quality prediction."""
    direction: str        # "long", "short", or "neutral"
    confidence: float     # 0.0 - 1.0


class MLPredictor:
    """
    Loads a trained model and predicts entry setup quality.

    If no model is loaded, all predictions return neutral with
    confidence 0.0 — the system works identically to no-ML mode.
    """

    def __init__(self, model_path: Optional[str] = None):
        self._model = None
        self._feature_builder = MLFeatureBuilder()
        if model_path is not None:
            self.load(model_path)

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self, path: str) -> bool:
        """
        Load a trained model from disk.

        Returns True if loaded successfully, False otherwise.
        """
        if not HAS_LGB:
            logger.warning("lightgbm not installed — ML predictor disabled")
            return False
        p = Path(path)
        if not p.exists():
            logger.warning("Model file not found: %s — ML predictor disabled", p)
            return False
        try:
            self._model = lgb.Booster(model_file=str(p))
            logger.info("ML model loaded from %s", p)
            return True
        except Exception as e:
            logger.error("Failed to load ML model from %s: %s", p, e)
            self._model = None
            return False

    def predict(
        self,
        feature_snapshot,
        htf_bias=None,
        risk_state=None,
    ) -> MLPrediction:
        """
        Predict entry quality from current trading state.

        Args:
            feature_snapshot: FeatureSnapshot from features/engine.py
            htf_bias: Optional HTFBiasResult
            risk_state: Optional RiskState

        Returns:
            MLPrediction with direction and confidence.
            Returns neutral if no model loaded or confidence < 0.5.
        """
        if self._model is None:
            return MLPrediction(direction="neutral", confidence=0.0)

        try:
            X = self._feature_builder.build(feature_snapshot, htf_bias, risk_state)
            prob = float(self._model.predict(X)[0])
        except Exception as e:
            logger.error("ML prediction failed: %s", e)
            return MLPrediction(direction="neutral", confidence=0.0)

        # prob is P(win), i.e. P(label=1)
        # Determine direction from the HTF bias context
        confidence = abs(prob - 0.5) * 2  # Map [0,1] -> confidence in [0,1]

        if confidence < 0.5:
            return MLPrediction(direction="neutral", confidence=confidence)

        # Use HTF bias to determine direction; ML only predicts quality
        if htf_bias is not None:
            htf_dir = getattr(htf_bias, "consensus_direction", "neutral")
            if prob >= 0.5:
                # Model thinks this is a good setup
                if htf_dir == "bullish":
                    direction = "long"
                elif htf_dir == "bearish":
                    direction = "short"
                else:
                    direction = "neutral"
            else:
                direction = "neutral"
        else:
            direction = "neutral"

        return MLPrediction(direction=direction, confidence=round(confidence, 4))
