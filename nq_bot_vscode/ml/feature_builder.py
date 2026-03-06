"""
ML Feature Builder
===================
Converts FeatureSnapshot + HTFBiasResult + RiskState into a flat
numeric feature vector for the LightGBM entry classifier.
"""

import math
import numpy as np
from dataclasses import dataclass
from typing import List, Optional


# Feature names for interpretability and model inspection
FEATURE_NAMES: List[str] = [
    "atr_normalized",
    "vwap_distance_normalized",
    "ob_proximity_bull_pts",
    "ob_proximity_bear_pts",
    "fvg_proximity_bull_pts",
    "fvg_proximity_bear_pts",
    "cumulative_delta_normalized",
    "htf_consensus_strength",
    "htf_consensus_direction",
    "num_aligned_signals",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
    "vix_level",
    "regime_trending_up",
    "regime_trending_down",
    "regime_ranging",
    "regime_high_volatility",
    "regime_low_liquidity",
    "regime_event_driven",
    "regime_crash",
    "regime_unknown",
    "volume_ratio",
    "consecutive_losses",
]

N_FEATURES = len(FEATURE_NAMES)


class MLFeatureBuilder:
    """
    Extracts a flat feature vector from trading state objects.

    Takes a FeatureSnapshot, HTFBiasResult, and RiskState and produces
    a (1, n_features) numpy array suitable for LightGBM prediction.
    """

    REGIME_LABELS = [
        "trending_up", "trending_down", "ranging", "high_volatility",
        "low_liquidity", "event_driven", "crash", "unknown",
    ]

    @staticmethod
    def feature_names() -> List[str]:
        return list(FEATURE_NAMES)

    @staticmethod
    def n_features() -> int:
        return N_FEATURES

    def build(
        self,
        feature_snapshot,
        htf_bias=None,
        risk_state=None,
    ) -> np.ndarray:
        """
        Build a flat feature vector from the given state objects.

        Args:
            feature_snapshot: FeatureSnapshot from features/engine.py
            htf_bias: Optional HTFBiasResult from features/htf_engine.py
            risk_state: Optional RiskState from risk/engine.py

        Returns:
            np.ndarray of shape (1, N_FEATURES)
        """
        fs = feature_snapshot
        price = getattr(fs, "session_vwap", 0.0) or 1.0  # fallback to avoid /0

        # --- Volatility ---
        atr_norm = self._safe_div(getattr(fs, "atr_14", 0.0), price)

        # --- VWAP distance ---
        vwap_dist = self._safe_div(getattr(fs, "price_vs_vwap", 0.0), price)

        # --- Order block proximity (points) ---
        ob_bull_pts = self._nearest_ob_distance(fs, "bullish")
        ob_bear_pts = self._nearest_ob_distance(fs, "bearish")

        # --- FVG proximity (points) ---
        fvg_bull_pts = self._nearest_fvg_distance(fs, "bullish")
        fvg_bear_pts = self._nearest_fvg_distance(fs, "bearish")

        # --- Cumulative delta (normalized by recent volume proxy) ---
        cum_delta = getattr(fs, "cumulative_delta", 0)
        # Normalize by a volume proxy: use abs(cum_delta) + 1 to keep bounded
        delta_norm = self._safe_div(cum_delta, abs(cum_delta) + 1000)

        # --- HTF bias ---
        htf_strength = 0.0
        htf_dir_encoded = 0  # -1=bearish, 0=neutral, 1=bullish
        if htf_bias is not None:
            htf_strength = getattr(htf_bias, "consensus_strength", 0.0)
            d = getattr(htf_bias, "consensus_direction", "neutral")
            htf_dir_encoded = {"bullish": 1, "bearish": -1}.get(d, 0)

        # --- Number of aligned signals ---
        # Count how many proximity signals are active
        aligned = sum([
            getattr(fs, "near_bullish_ob", False),
            getattr(fs, "near_bearish_ob", False),
            getattr(fs, "inside_bullish_fvg", False),
            getattr(fs, "inside_bearish_fvg", False),
            getattr(fs, "recent_buy_sweep", False),
            getattr(fs, "recent_sell_sweep", False),
            getattr(fs, "delta_divergence", False),
        ])

        # --- Time features (cyclical encoding) ---
        ts = getattr(fs, "timestamp", None)
        if ts is not None:
            hour = ts.hour + ts.minute / 60.0
            dow = ts.weekday()
        else:
            hour = 12.0
            dow = 2
        hour_sin = math.sin(2 * math.pi * hour / 24)
        hour_cos = math.cos(2 * math.pi * hour / 24)
        dow_sin = math.sin(2 * math.pi * dow / 7)
        dow_cos = math.cos(2 * math.pi * dow / 7)

        # --- VIX ---
        vix = getattr(fs, "vix_level", 0.0)

        # --- Regime (one-hot) ---
        detected = getattr(fs, "detected_regime", "unknown")
        regime_vec = [1.0 if detected == label else 0.0 for label in self.REGIME_LABELS]

        # --- Volume ratio (current / 20-bar avg) ---
        # Not directly available from snapshot; use volume_imbalance as proxy
        vol_ratio = abs(getattr(fs, "volume_imbalance", 0.0)) + 0.5

        # --- Consecutive losses ---
        consec_losses = 0
        if risk_state is not None:
            consec_losses = getattr(risk_state, "consecutive_losses", 0)

        features = [
            atr_norm,
            vwap_dist,
            ob_bull_pts,
            ob_bear_pts,
            fvg_bull_pts,
            fvg_bear_pts,
            delta_norm,
            htf_strength,
            float(htf_dir_encoded),
            float(aligned),
            hour_sin,
            hour_cos,
            dow_sin,
            dow_cos,
            vix,
            *regime_vec,
            vol_ratio,
            float(consec_losses),
        ]

        arr = np.array(features, dtype=np.float64).reshape(1, -1)
        # Replace any NaN/Inf with 0
        arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
        return arr

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_div(a: float, b: float) -> float:
        if b == 0 or not math.isfinite(b):
            return 0.0
        val = a / b
        return val if math.isfinite(val) else 0.0

    @staticmethod
    def _nearest_ob_distance(fs, direction: str) -> float:
        """Distance in points to the nearest active OB of the given direction."""
        obs = getattr(fs, "active_order_blocks", [])
        if not obs:
            return 0.0
        price = getattr(fs, "session_vwap", 0.0) or 0.0
        if price == 0:
            return 0.0
        distances = []
        for ob in obs:
            if getattr(ob, "direction", "") == direction and getattr(ob, "is_valid", False):
                mid = (ob.zone_high + ob.zone_low) / 2
                distances.append(abs(price - mid))
        return min(distances) if distances else 0.0

    @staticmethod
    def _nearest_fvg_distance(fs, gap_type: str) -> float:
        """Distance in points to the nearest active FVG of the given type."""
        fvgs = getattr(fs, "active_fvgs", [])
        if not fvgs:
            return 0.0
        price = getattr(fs, "session_vwap", 0.0) or 0.0
        if price == 0:
            return 0.0
        distances = []
        for fvg in fvgs:
            if getattr(fvg, "gap_type", "") == gap_type and getattr(fvg, "is_valid", False):
                mid = (fvg.gap_high + fvg.gap_low) / 2
                distances.append(abs(price - mid))
        return min(distances) if distances else 0.0
