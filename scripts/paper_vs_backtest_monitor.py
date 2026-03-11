"""
Paper vs Backtest Statistical Monitor
=======================================
Compares paper trading results against backtested baseline expectations.
Flags statistical divergences that require investigation.

Usage:
  python scripts/paper_vs_backtest_monitor.py
  python scripts/paper_vs_backtest_monitor.py --baseline config/backtest_baseline.json

Reads from:
  logs/paper_journal_*.json  — paper trading results
  config/backtest_baseline.json — baseline expectations (or defaults)
"""

import json
import math
import sys
import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent / "nq_bot_vscode"
if not PROJECT_DIR.exists():
    PROJECT_DIR = SCRIPT_DIR.parent

LOGS_DIR = PROJECT_DIR / "logs"
BASELINE_PATH = PROJECT_DIR / "config" / "backtest_baseline.json"


@dataclass
class Baseline:
    """Backtested performance baseline."""
    profit_factor: float = 1.53
    win_rate_pct: float = 58.3
    avg_pnl_per_trade: float = 11.25
    trades_per_month: float = 254.0
    total_trades: int = 14848
    avg_slippage_ticks: float = 1.0

    @classmethod
    def from_json(cls, path: Path) -> "Baseline":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
            return cls(
                profit_factor=data.get("profit_factor", cls.profit_factor),
                win_rate_pct=data.get("win_rate_pct", cls.win_rate_pct),
                avg_pnl_per_trade=data.get("avg_pnl_per_trade", cls.avg_pnl_per_trade),
                trades_per_month=data.get("trades_per_month", cls.trades_per_month),
                total_trades=data.get("total_trades", cls.total_trades),
                avg_slippage_ticks=data.get("avg_slippage_ticks", cls.avg_slippage_ticks),
            )
        except (json.JSONDecodeError, ValueError):
            return cls()


def load_paper_trades() -> List[dict]:
    """Load all paper trading journal entries."""
    all_trades = []
    for f in sorted(LOGS_DIR.glob("paper_journal_????-??-??.json")):
        try:
            trades = json.loads(f.read_text())
            all_trades.extend(trades)
        except (json.JSONDecodeError, ValueError):
            continue
    return all_trades


def binomial_p_value(n: int, k: int, p: float) -> float:
    """Two-sided p-value for observing k successes in n trials with prob p.

    Uses normal approximation for n >= 20.
    """
    if n < 5 or p <= 0 or p >= 1:
        return 1.0

    expected = n * p
    std = math.sqrt(n * p * (1 - p))
    if std == 0:
        return 1.0

    z = abs(k - expected) / std
    # Normal approximation for p-value
    # Using erf approximation
    p_val = 2 * (1 - _norm_cdf(z))
    return p_val


def _norm_cdf(x: float) -> float:
    """Standard normal CDF approximation."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def compute_metrics(trades: List[dict]) -> dict:
    """Compute performance metrics from trade records."""
    if not trades:
        return {}

    wins = [t for t in trades if t.get("total_pnl", 0) > 0]
    losses = [t for t in trades if t.get("total_pnl", 0) <= 0]
    total_pnl = sum(t.get("total_pnl", 0) for t in trades)
    gross_win = sum(t.get("total_pnl", 0) for t in wins)
    gross_loss = abs(sum(t.get("total_pnl", 0) for t in losses))

    # Max drawdown
    cum_pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cum_pnl += t.get("total_pnl", 0)
        peak = max(peak, cum_pnl)
        dd = peak - cum_pnl
        max_dd = max(max_dd, dd)

    slippages = [t.get("entry_slippage_pts", 0) for t in trades
                 if t.get("entry_slippage_pts") is not None]

    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(trades) * 100) if trades else 0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "total_pnl": total_pnl,
        "avg_pnl": total_pnl / len(trades) if trades else 0,
        "max_drawdown": max_dd,
        "max_drawdown_pct": (max_dd / 50000) * 100 if max_dd > 0 else 0,
        "avg_slippage": sum(slippages) / len(slippages) if slippages else 0,
    }


def evaluate_status(metrics: dict, baseline: Baseline) -> dict:
    """Evaluate each metric against baseline with adaptive tolerances."""
    n = metrics.get("total_trades", 0)
    if n == 0:
        return {"verdict": "NO DATA", "details": {}}

    details = {}

    # Win rate tolerance: wider for small samples
    wr_tolerance = 8.0 if n < 100 else 5.0
    wr_delta = metrics["win_rate"] - baseline.win_rate_pct
    if abs(wr_delta) <= wr_tolerance:
        details["win_rate"] = "WITHIN TOLERANCE"
    elif wr_delta < 0:
        details["win_rate"] = "BELOW BASELINE"
    else:
        details["win_rate"] = "ABOVE BASELINE"

    # Profit factor
    pf = metrics["profit_factor"]
    pf_delta = pf - baseline.profit_factor
    if pf >= 1.0:
        details["profit_factor"] = "WITHIN TOLERANCE"
    elif pf >= 0.8:
        details["profit_factor"] = "REVIEW NEEDED" if n >= 50 else "WITHIN TOLERANCE"
    else:
        details["profit_factor"] = "HALT AND DIAGNOSE" if n >= 50 else "WITHIN TOLERANCE"

    # $/trade
    avg_delta = metrics["avg_pnl"] - baseline.avg_pnl_per_trade
    if abs(avg_delta) / max(abs(baseline.avg_pnl_per_trade), 0.01) < 0.5:
        details["avg_pnl"] = "WITHIN TOLERANCE"
    elif avg_delta < 0:
        details["avg_pnl"] = "BELOW BASELINE"
    else:
        details["avg_pnl"] = "ABOVE BASELINE"

    # Max drawdown
    dd_pct = metrics.get("max_drawdown_pct", 0)
    if dd_pct <= 3.0:
        details["max_drawdown"] = "OK"
    elif dd_pct <= 5.0:
        details["max_drawdown"] = "ELEVATED"
    else:
        details["max_drawdown"] = "CRITICAL"

    # Slippage
    slip = metrics.get("avg_slippage", 0)
    if slip <= baseline.avg_slippage_ticks:
        details["slippage"] = "BETTER THAN EXPECTED"
    elif slip <= baseline.avg_slippage_ticks * 1.5:
        details["slippage"] = "WITHIN TOLERANCE"
    else:
        details["slippage"] = "WORSE THAN EXPECTED"

    # Overall verdict
    halt_signals = [k for k, v in details.items() if "HALT" in v or "CRITICAL" in v]
    review_signals = [k for k, v in details.items() if "REVIEW" in v or "ELEVATED" in v]

    if halt_signals:
        verdict = "HALT AND DIAGNOSE"
    elif review_signals:
        verdict = "REVIEW NEEDED"
    else:
        verdict = "CONTINUE PAPER TRADING"

    return {"verdict": verdict, "details": details}


def print_report(metrics: dict, baseline: Baseline, status: dict):
    """Print the comparison report."""
    n = metrics.get("total_trades", 0)

    print(f"\n{'=' * 60}")
    print(f"  PAPER vs BACKTEST COMPARISON")
    print(f"{'=' * 60}")
    print(f"  Paper trades to date: {n}")
    print(f"  Backtest baseline: PF={baseline.profit_factor:.2f}, "
          f"WR={baseline.win_rate_pct:.1f}%, "
          f"$/trade=${baseline.avg_pnl_per_trade:.2f}")

    if n == 0:
        print(f"\n  No trades yet — waiting for data.")
        print(f"{'=' * 60}")
        return

    print(f"\n  {'':16} {'Paper':>10}  {'Baseline':>10}  {'Delta':>10}  {'Status'}")
    print(f"  {'-' * 64}")

    wr = metrics["win_rate"]
    pf = metrics["profit_factor"]
    avg = metrics["avg_pnl"]
    dd = metrics.get("max_drawdown_pct", 0)
    slip = metrics.get("avg_slippage", 0)

    pf_str = f"{pf:.2f}" if pf < 100 else "inf"

    print(f"  {'Win Rate:':16} {wr:>9.1f}%  {baseline.win_rate_pct:>9.1f}%  "
          f"{wr - baseline.win_rate_pct:>+9.1f}%  {status['details'].get('win_rate', '')}")
    print(f"  {'Profit Factor:':16} {pf_str:>10}  {baseline.profit_factor:>10.2f}  "
          f"{pf - baseline.profit_factor:>+10.2f}  {status['details'].get('profit_factor', '')}")
    print(f"  {'$/Trade:':16} ${avg:>9.2f}  ${baseline.avg_pnl_per_trade:>9.2f}  "
          f"${avg - baseline.avg_pnl_per_trade:>+9.2f}  {status['details'].get('avg_pnl', '')}")
    print(f"  {'Max Drawdown:':16} {dd:>9.1f}%  {'—':>10}  {'—':>10}  "
          f"{status['details'].get('max_drawdown', '')}")
    print(f"  {'Avg Slippage:':16} {slip:>8.1f} tk  "
          f"{baseline.avg_slippage_ticks:>8.1f} tk  "
          f"{slip - baseline.avg_slippage_ticks:>+8.1f}  "
          f"{status['details'].get('slippage', '')}")

    # Statistical test
    if n >= 10:
        expected_p = baseline.win_rate_pct / 100
        wins = metrics["wins"]
        expected_wins = n * expected_p
        std = math.sqrt(n * expected_p * (1 - expected_p))
        p_val = binomial_p_value(n, wins, expected_p)

        print(f"\n  Statistical test (binomial):")
        print(f"    Expected wins at n={n}: {expected_wins:.1f} +/- {std:.1f}")
        print(f"    Actual wins: {wins}")
        print(f"    p-value: {p_val:.2f}")

        if p_val > 0.05:
            print(f"    CONSISTENT with backtest (p > 0.05)")
        else:
            print(f"    DIVERGENT from backtest (p <= 0.05) — investigate")

    print(f"\n  VERDICT: {status['verdict']}")
    print(f"{'=' * 60}")


def main():
    parser = argparse.ArgumentParser(
        description="Paper vs Backtest Statistical Monitor"
    )
    parser.add_argument("--baseline", type=str, default=str(BASELINE_PATH),
                        help="Path to backtest baseline JSON")
    args = parser.parse_args()

    baseline = Baseline.from_json(Path(args.baseline))
    trades = load_paper_trades()
    metrics = compute_metrics(trades)
    status = evaluate_status(metrics, baseline)
    print_report(metrics, baseline, status)

    # Exit code: 0=continue, 1=review, 2=halt
    if status["verdict"] == "HALT AND DIAGNOSE":
        sys.exit(2)
    elif status["verdict"] == "REVIEW NEEDED":
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
