#!/usr/bin/env python3
"""
Train ML Entry Classifier
===========================
CLI script for training and evaluating the LightGBM entry classifier.

Usage:
    # Train on backtest trade log
    python scripts/train_ml_model.py --trade-log logs/backtest_trades.json \
        --output models/entry_classifier.lgb

    # Walk-forward evaluation
    python scripts/train_ml_model.py --trade-log logs/backtest_trades.json \
        --walk-forward --report reports/ml_eval.html
"""

import argparse
import json
import logging
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "nq_bot_vscode"))

import numpy as np

from nq_bot_vscode.ml.feature_builder import MLFeatureBuilder, FEATURE_NAMES
from nq_bot_vscode.ml.trainer import WalkForwardTrainer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("train_ml_model")


def load_trade_log(path: str) -> list:
    """Load trade log from JSON file."""
    p = Path(path)
    if not p.exists():
        logger.error("Trade log not found: %s", p)
        sys.exit(1)
    with open(p, "r") as f:
        data = json.load(f)
    if isinstance(data, dict) and "trades" in data:
        trades = data["trades"]
    elif isinstance(data, list):
        trades = data
    else:
        logger.error("Unexpected trade log format")
        sys.exit(1)
    logger.info("Loaded %d trades from %s", len(trades), p)
    return trades


def generate_html_report(results, output_path: str) -> None:
    """Generate a simple HTML walk-forward evaluation report."""
    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    rows = ""
    for r in results:
        rows += f"""
        <tr>
            <td>{r.fold_idx}</td>
            <td>{r.train_start} – {r.train_end}</td>
            <td>{r.test_start} – {r.test_end}</td>
            <td>{r.n_train}</td>
            <td>{r.n_test}</td>
            <td>{r.accuracy:.3f}</td>
            <td>{r.precision:.3f}</td>
            <td>{r.recall:.3f}</td>
            <td>{r.auc:.3f}</td>
        </tr>"""

    avg_acc = np.mean([r.accuracy for r in results]) if results else 0
    avg_auc = np.mean([r.auc for r in results]) if results else 0

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>ML Walk-Forward Evaluation</title>
    <style>
        body {{ font-family: monospace; background: #1a1a2e; color: #e0e0e0; padding: 20px; }}
        h1 {{ color: #00d4ff; }}
        table {{ border-collapse: collapse; width: 100%; margin-top: 20px; }}
        th, td {{ border: 1px solid #333; padding: 8px; text-align: center; }}
        th {{ background: #16213e; color: #00d4ff; }}
        tr:nth-child(even) {{ background: #0f3460; }}
        .summary {{ margin-top: 20px; font-size: 1.2em; }}
    </style>
</head>
<body>
    <h1>ML Entry Classifier — Walk-Forward Evaluation</h1>
    <div class="summary">
        <p>Folds: {len(results)} | Avg Accuracy: {avg_acc:.3f} | Avg AUC: {avg_auc:.3f}</p>
    </div>
    <table>
        <tr>
            <th>Fold</th><th>Train Period</th><th>Test Period</th>
            <th>N Train</th><th>N Test</th>
            <th>Accuracy</th><th>Precision</th><th>Recall</th><th>AUC</th>
        </tr>
        {rows}
    </table>
</body>
</html>"""

    with open(p, "w") as f:
        f.write(html)
    logger.info("Report written to %s", p)


def main():
    parser = argparse.ArgumentParser(description="Train ML entry classifier")
    parser.add_argument("--trade-log", required=True, help="Path to backtest trade log JSON")
    parser.add_argument("--output", default="models/entry_classifier.lgb", help="Output model path")
    parser.add_argument("--walk-forward", action="store_true", help="Run walk-forward evaluation")
    parser.add_argument("--report", default="reports/ml_eval.html", help="Walk-forward report path")
    parser.add_argument("--train-months", type=int, default=3, help="Training window months")
    parser.add_argument("--retrain-interval", type=int, default=1, help="Retrain interval months")
    args = parser.parse_args()

    trades = load_trade_log(args.trade_log)

    trainer = WalkForwardTrainer(
        train_months=args.train_months,
        retrain_interval_months=args.retrain_interval,
    )

    if args.walk_forward:
        logger.info("Running walk-forward evaluation...")
        results = trainer.walk_forward_train(
            trades,
            feature_names=FEATURE_NAMES,
        )
        if results:
            avg_acc = np.mean([r.accuracy for r in results])
            avg_auc = np.mean([r.auc for r in results])
            logger.info("Walk-forward complete: %d folds, avg accuracy=%.3f, avg AUC=%.3f",
                        len(results), avg_acc, avg_auc)
            generate_html_report(results, args.report)
        else:
            logger.warning("No folds produced — insufficient data")
    else:
        logger.info("Training on full dataset...")
        X, y = trainer.build_training_set(trades)
        if X.shape[0] == 0:
            logger.error("No valid training samples found")
            sys.exit(1)

        model = trainer.train(X, y, feature_names=FEATURE_NAMES)
        if model is None:
            logger.error("Training failed")
            sys.exit(1)

        trainer.save_model(model, args.output)
        logger.info("Model saved to %s", args.output)

        # Print OOS accuracy on last 20%
        val_size = max(int(X.shape[0] * 0.2), 1)
        X_val = X[-val_size:]
        y_val = y[-val_size:]
        y_prob = model.predict(X_val)
        y_pred = (y_prob >= 0.5).astype(int)
        acc = (y_pred == y_val).mean()
        logger.info("Holdout accuracy (last 20%%): %.3f", acc)


if __name__ == "__main__":
    main()
