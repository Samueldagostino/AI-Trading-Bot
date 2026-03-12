"""
Walk-Forward ML Trainer
========================
Trains a LightGBM binary classifier to predict entry setup quality.
Uses walk-forward (sliding window) to prevent lookahead bias.
"""

import json
import logging
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

try:
    import lightgbm as lgb
    HAS_LGB = True
except ImportError:
    lgb = None  # type: ignore[assignment]
    HAS_LGB = False

try:
    from sklearn.metrics import accuracy_score, precision_score, recall_score, roc_auc_score
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False


@dataclass
class FoldResult:
    """Results from one walk-forward fold."""
    fold_idx: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    n_train: int
    n_test: int
    accuracy: float = 0.0
    precision: float = 0.0
    recall: float = 0.0
    auc: float = 0.0


class WalkForwardTrainer:
    """
    Walk-forward trainer for entry quality classification.

    Trains LightGBM on a rolling window of trade data and evaluates
    on the subsequent period. Prevents any future-data leakage.
    """

    DEFAULT_PARAMS = {
        "objective": "binary",
        "metric": "binary_logloss",
        "max_depth": 4,
        "num_leaves": 15,
        "learning_rate": 0.05,
        "n_estimators": 200,
        "is_unbalance": True,
        "verbose": -1,
        "seed": 42,
    }

    def __init__(
        self,
        train_months: int = 3,
        retrain_interval_months: int = 1,
        params: Optional[dict] = None,
    ):
        if not HAS_LGB:
            raise ImportError(
                "lightgbm is required for ML training. "
                "Install with: pip install lightgbm>=4.0.0"
            )
        self.train_months = train_months
        self.retrain_interval_months = retrain_interval_months
        self.params = {**self.DEFAULT_PARAMS, **(params or {})}

    def build_training_set(
        self,
        trade_log: List[dict],
        feature_key: str = "features",
        pnl_key: str = "net_pnl",
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Build X, y arrays from a trade log.

        Each trade dict must have:
          - feature_key: list/array of numeric features at entry time
          - pnl_key: numeric PnL of the trade

        Returns:
            (X, y) where X is (n_trades, n_features) and y is (n_trades,)
            Labels: 1 if PnL > 0, 0 otherwise
        """
        features = []
        labels = []
        for trade in trade_log:
            feat = trade.get(feature_key)
            pnl = trade.get(pnl_key, 0.0)
            if feat is None:
                continue
            feat_arr = np.asarray(feat, dtype=np.float64).flatten()
            features.append(feat_arr)
            labels.append(1 if pnl > 0 else 0)

        if not features:
            return np.empty((0, 0)), np.empty((0,))

        X = np.vstack(features)
        y = np.array(labels, dtype=np.int32)

        # Clean NaN/Inf
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

        logger.info(
            "Training set: %d samples, %d features, %.1f%% positive",
            X.shape[0], X.shape[1], y.mean() * 100,
        )
        return X, y

    def train(
        self,
        X: np.ndarray,
        y: np.ndarray,
        feature_names: Optional[List[str]] = None,
    ):
        """
        Train a LightGBM binary classifier.

        Uses 20% holdout from the training window for early stopping.

        Returns:
            Trained lgb.Booster
        """
        if X.shape[0] < 20:
            logger.warning("Too few samples (%d) -- skipping training", X.shape[0])
            return None

        # Split: last 20% for validation (time-ordered, no shuffle)
        val_size = max(int(X.shape[0] * 0.2), 1)
        X_train, X_val = X[:-val_size], X[-val_size:]
        y_train, y_val = y[:-val_size], y[-val_size:]

        dtrain = lgb.Dataset(X_train, label=y_train, feature_name=feature_names)
        dval = lgb.Dataset(X_val, label=y_val, reference=dtrain)

        callbacks = [
            lgb.early_stopping(stopping_rounds=20),
            lgb.log_evaluation(period=0),  # suppress per-iteration logging
        ]

        booster = lgb.train(
            self.params,
            dtrain,
            num_boost_round=self.params.get("n_estimators", 200),
            valid_sets=[dval],
            callbacks=callbacks,
        )

        logger.info(
            "Model trained: %d iterations, best_score=%.4f",
            booster.best_iteration, booster.best_score.get("valid_0", {}).get("binary_logloss", -1),
        )
        return booster

    def walk_forward_train(
        self,
        trade_log: List[dict],
        feature_key: str = "features",
        pnl_key: str = "net_pnl",
        date_key: str = "entry_time",
        feature_names: Optional[List[str]] = None,
    ) -> List[FoldResult]:
        """
        Walk-forward cross-validation.

        Splits trade_log by date into rolling train/test windows.
        Train on `train_months` of data, test on the next
        `retrain_interval_months`.

        Returns:
            List of FoldResult with OOS metrics per fold.
        """
        if not HAS_SKLEARN:
            raise ImportError(
                "scikit-learn is required for walk-forward metrics. "
                "Install with: pip install scikit-learn>=1.3.0"
            )

        # Sort by date
        dated_trades = []
        for t in trade_log:
            dt_str = t.get(date_key, "")
            feat = t.get(feature_key)
            pnl = t.get(pnl_key, 0.0)
            if not dt_str or feat is None:
                continue
            dated_trades.append((dt_str, np.asarray(feat, dtype=np.float64).flatten(), 1 if pnl > 0 else 0))

        dated_trades.sort(key=lambda x: x[0])
        if len(dated_trades) < 30:
            logger.warning("Insufficient trades (%d) for walk-forward", len(dated_trades))
            return []

        dates = [d[0] for d in dated_trades]
        X_all = np.vstack([d[1] for d in dated_trades])
        y_all = np.array([d[2] for d in dated_trades], dtype=np.int32)
        X_all = np.nan_to_num(X_all, nan=0.0, posinf=0.0, neginf=0.0)

        # Build monthly boundaries
        unique_months = sorted(set(d[:7] for d in dates))  # "YYYY-MM"
        if len(unique_months) < self.train_months + self.retrain_interval_months:
            logger.warning(
                "Not enough months (%d) for walk-forward (need %d)",
                len(unique_months), self.train_months + self.retrain_interval_months,
            )
            return []

        results = []
        fold_idx = 0

        for i in range(self.train_months, len(unique_months), self.retrain_interval_months):
            train_months_list = unique_months[i - self.train_months:i]
            test_end = min(i + self.retrain_interval_months, len(unique_months))
            test_months_list = unique_months[i:test_end]
            if not test_months_list:
                break

            train_mask = np.array([d[:7] in set(train_months_list) for d in dates])
            test_mask = np.array([d[:7] in set(test_months_list) for d in dates])

            X_train = X_all[train_mask]
            y_train = y_all[train_mask]
            X_test = X_all[test_mask]
            y_test = y_all[test_mask]

            if X_train.shape[0] < 20 or X_test.shape[0] < 5:
                continue

            model = self.train(X_train, y_train, feature_names=feature_names)
            if model is None:
                continue

            y_prob = model.predict(X_test)
            y_pred = (y_prob >= 0.5).astype(int)

            fold_result = FoldResult(
                fold_idx=fold_idx,
                train_start=train_months_list[0],
                train_end=train_months_list[-1],
                test_start=test_months_list[0],
                test_end=test_months_list[-1],
                n_train=int(X_train.shape[0]),
                n_test=int(X_test.shape[0]),
                accuracy=float(accuracy_score(y_test, y_pred)),
                precision=float(precision_score(y_test, y_pred, zero_division=0)),
                recall=float(recall_score(y_test, y_pred, zero_division=0)),
                auc=float(roc_auc_score(y_test, y_prob)) if len(set(y_test)) > 1 else 0.0,
            )
            results.append(fold_result)
            fold_idx += 1

            logger.info(
                "Fold %d: train=%s-%s (%d) test=%s-%s (%d) | "
                "acc=%.3f prec=%.3f rec=%.3f auc=%.3f",
                fold_result.fold_idx,
                fold_result.train_start, fold_result.train_end, fold_result.n_train,
                fold_result.test_start, fold_result.test_end, fold_result.n_test,
                fold_result.accuracy, fold_result.precision,
                fold_result.recall, fold_result.auc,
            )

        return results

    @staticmethod
    def save_model(model, path: str) -> None:
        """Save trained LightGBM model to disk."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        model.save_model(str(p))
        logger.info("Model saved to %s", p)

    @staticmethod
    def load_model(path: str):
        """Load a trained LightGBM model from disk."""
        if not HAS_LGB:
            raise ImportError("lightgbm is required to load models")
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Model file not found: {p}")
        booster = lgb.Booster(model_file=str(p))
        logger.info("Model loaded from %s", p)
        return booster
