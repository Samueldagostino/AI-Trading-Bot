"""
QuantData Correlation Analysis
================================
After 100+ paper trades with market context logged, run this script
to determine if QuantData metrics predict trade outcomes.

Usage:
    python scripts/analyze_quantdata_correlation.py
    python scripts/analyze_quantdata_correlation.py --min-trades 50

Analyzes:
  1. Win rate by gamma regime (negative vs positive vs neutral)
  2. Profit Factor by gamma regime
  3. Win rate when flow aligns vs doesn't align with trade direction
  4. Fat-tail trade distribution by gamma regime
  5. Average PnL by skew regime

Output: statistical correlation report with p-values and recommendations.
"""

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent / "nq_bot_vscode"
if not PROJECT_DIR.exists():
    PROJECT_DIR = SCRIPT_DIR.parent
LOGS_DIR = PROJECT_DIR / "logs"


def load_all_trades() -> List[dict]:
    """Load all trades from paper journal files."""
    all_trades = []
    for f in sorted(LOGS_DIR.glob("paper_journal_????-??-??.json")):
        try:
            trades = json.loads(f.read_text())
            all_trades.extend(trades)
        except (json.JSONDecodeError, ValueError):
            continue
    return all_trades


def filter_trades_with_context(trades: List[dict]) -> List[dict]:
    """Filter to only trades that have market_context data."""
    return [t for t in trades if t.get("market_context") is not None]


def compute_metrics(trades: List[dict]) -> dict:
    """Compute WR, PF, avg PnL for a group of trades."""
    if not trades:
        return {"count": 0, "wr": 0, "pf": 0, "avg_pnl": 0, "total_pnl": 0}

    wins = [t for t in trades if t.get("total_pnl", 0) > 0]
    losses = [t for t in trades if t.get("total_pnl", 0) <= 0]
    total_pnl = sum(t.get("total_pnl", 0) for t in trades)
    gross_win = sum(t.get("total_pnl", 0) for t in wins)
    gross_loss = abs(sum(t.get("total_pnl", 0) for t in losses))

    return {
        "count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "wr": (len(wins) / len(trades) * 100) if trades else 0,
        "pf": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "avg_pnl": total_pnl / len(trades) if trades else 0,
        "total_pnl": total_pnl,
    }


def chi_squared_p_value(observed: List[int], expected: List[float]) -> float:
    """
    Simple chi-squared goodness-of-fit p-value approximation.
    For proper analysis, use scipy.stats.chi2_contingency.
    This is a rough approximation for when scipy is not available.
    """
    if not observed or not expected or len(observed) != len(expected):
        return 1.0

    chi2 = 0.0
    for obs, exp in zip(observed, expected):
        if exp > 0:
            chi2 += (obs - exp) ** 2 / exp

    # Approximate p-value using chi2 with df = len(observed) - 1
    df = len(observed) - 1
    if df <= 0:
        return 1.0

    # Rough p-value approximation (Wilson-Hilferty)
    try:
        z = (chi2 / df) ** (1 / 3) - (1 - 2 / (9 * df))
        z /= math.sqrt(2 / (9 * df))
        # Standard normal CDF approximation
        p = 0.5 * (1 + math.erf(-z / math.sqrt(2)))
        return max(0, min(1, p))
    except (ValueError, ZeroDivisionError):
        return 1.0


def proportion_test_p(n1: int, k1: int, n2: int, k2: int) -> float:
    """
    Two-proportion z-test p-value approximation.
    n1, k1: sample size and successes for group 1.
    n2, k2: sample size and successes for group 2.
    """
    if n1 == 0 or n2 == 0:
        return 1.0

    p1 = k1 / n1
    p2 = k2 / n2
    p_pooled = (k1 + k2) / (n1 + n2)

    if p_pooled == 0 or p_pooled == 1:
        return 1.0

    try:
        se = math.sqrt(p_pooled * (1 - p_pooled) * (1 / n1 + 1 / n2))
        if se == 0:
            return 1.0
        z = abs(p1 - p2) / se
        # Two-tailed p-value from standard normal
        p = 2 * 0.5 * (1 + math.erf(-z / math.sqrt(2)))
        return max(0, min(1, p))
    except (ValueError, ZeroDivisionError):
        return 1.0


def analyze_gamma_regime(trades: List[dict]) -> dict:
    """Analyze trade performance by gamma regime."""
    groups = defaultdict(list)
    for t in trades:
        ctx = t.get("market_context", {})
        regime = ctx.get("gamma_regime", "unknown") if ctx else "unknown"
        groups[regime].append(t)

    results = {}
    for regime, group_trades in sorted(groups.items()):
        results[regime] = compute_metrics(group_trades)

    # Statistical test: compare negative vs positive win rates
    neg = results.get("negative", {"count": 0, "wins": 0})
    pos = results.get("positive", {"count": 0, "wins": 0})
    p_value = proportion_test_p(
        neg.get("count", 0), neg.get("wins", 0),
        pos.get("count", 0), pos.get("wins", 0),
    )
    results["_p_value"] = p_value

    return results


def analyze_flow_alignment(trades: List[dict]) -> dict:
    """Analyze trade performance by flow alignment."""
    aligned = []
    misaligned = []

    for t in trades:
        flow_aligned = t.get("flow_aligned_with_trade")
        if flow_aligned is True:
            aligned.append(t)
        elif flow_aligned is False:
            misaligned.append(t)

    aligned_metrics = compute_metrics(aligned)
    misaligned_metrics = compute_metrics(misaligned)

    p_value = proportion_test_p(
        aligned_metrics["count"], aligned_metrics.get("wins", 0),
        misaligned_metrics["count"], misaligned_metrics.get("wins", 0),
    )

    return {
        "aligned": aligned_metrics,
        "misaligned": misaligned_metrics,
        "_p_value": p_value,
    }


def analyze_fat_tails(trades: List[dict]) -> dict:
    """Analyze distribution of top 10% trades by gamma regime."""
    sorted_trades = sorted(trades, key=lambda t: t.get("total_pnl", 0), reverse=True)
    top_10_pct = max(1, len(sorted_trades) // 10)
    top_trades = sorted_trades[:top_10_pct]

    groups = defaultdict(int)
    total_by_regime = defaultdict(int)

    for t in trades:
        ctx = t.get("market_context", {})
        regime = ctx.get("gamma_regime", "unknown") if ctx else "unknown"
        total_by_regime[regime] += 1

    for t in top_trades:
        ctx = t.get("market_context", {})
        regime = ctx.get("gamma_regime", "unknown") if ctx else "unknown"
        groups[regime] += 1

    return {
        "top_count": top_10_pct,
        "by_regime": dict(groups),
        "total_by_regime": dict(total_by_regime),
    }


def analyze_skew_regime(trades: List[dict]) -> dict:
    """Analyze trade performance by volatility skew regime."""
    groups = defaultdict(list)
    for t in trades:
        ctx = t.get("market_context", {})
        skew = ctx.get("skew_regime", "unknown") if ctx else "unknown"
        groups[skew].append(t)

    results = {}
    for regime, group_trades in sorted(groups.items()):
        results[regime] = compute_metrics(group_trades)

    return results


def print_report(
    total_trades: int,
    context_trades: int,
    gamma: dict,
    flow: dict,
    fat_tails: dict,
    skew: dict,
):
    """Print the full correlation analysis report."""
    W = 60
    bar = "=" * W

    print(f"\n{bar}")
    print(f"  QUANTDATA CORRELATION ANALYSIS")
    print(f"{bar}")
    print(f"  Total paper trades:         {total_trades}")
    print(f"  Trades with context data:   {context_trades}")
    print()

    # Gamma Regime
    print(f"  GAMMA REGIME:")
    for regime in ["negative", "positive", "neutral"]:
        m = gamma.get(regime, {"count": 0})
        if m["count"] > 0:
            marker = ""
            if regime == "positive" and m.get("pf", 0) < 1.0:
                marker = "  <- PROBLEM"
            elif regime == "negative" and m.get("pf", 0) > 1.5:
                marker = "  <- EDGE"
            print(
                f"    {regime:>10} gamma: {m['count']:3d} trades, "
                f"WR {m['wr']:.0f}%, PF {m['pf']:.2f}, "
                f"avg ${m['avg_pnl']:.2f}/trade{marker}"
            )
    gamma_p = gamma.get("_p_value", 1.0)
    print(f"    p-value (neg vs pos): {gamma_p:.4f}")
    print()

    # Flow Alignment
    print(f"  FLOW ALIGNMENT:")
    for label in ["aligned", "misaligned"]:
        m = flow.get(label, {"count": 0})
        if m["count"] > 0:
            marker = ""
            if label == "misaligned" and m.get("pf", 0) < 1.0:
                marker = "  <- PROBLEM"
            print(
                f"    {label:>14}: {m['count']:3d} trades, "
                f"WR {m['wr']:.0f}%, PF {m['pf']:.2f}{marker}"
            )
    flow_p = flow.get("_p_value", 1.0)
    print(f"    p-value: {flow_p:.4f}")
    print()

    # Fat Tails
    print(f"  FAT-TAIL ANALYSIS (top {fat_tails['top_count']} trades):")
    for regime, count in sorted(fat_tails["by_regime"].items()):
        total = fat_tails["total_by_regime"].get(regime, 0)
        pct = (count / fat_tails["top_count"] * 100) if fat_tails["top_count"] > 0 else 0
        marker = "  <- EDGE HERE" if regime == "negative" and pct > 50 else ""
        print(
            f"    Top trades in {regime} gamma: "
            f"{count}/{fat_tails['top_count']} ({pct:.0f}%){marker}"
        )
    print()

    # Skew Regime
    print(f"  SKEW REGIME:")
    for regime in ["normal", "elevated", "extreme"]:
        m = skew.get(regime, {"count": 0})
        if m["count"] > 0:
            print(
                f"    {regime:>10}: {m['count']:3d} trades, "
                f"WR {m['wr']:.0f}%, PF {m['pf']:.2f}, "
                f"avg ${m['avg_pnl']:.2f}/trade"
            )
    print()

    # Recommendations
    print(f"  RECOMMENDATION:")
    if gamma_p < 0.05:
        print(f"    - Gamma regime is a STRONG predictor (p={gamma_p:.3f})")
        print(f"    - Enable +0.10 context boost for negative gamma")
        print(f"    - Enable -0.05 context penalty for positive gamma")
    else:
        print(f"    - Gamma regime shows NO significant correlation (p={gamma_p:.2f})")
        print(f"    - Do NOT enable scoring. Continue logging.")

    if flow_p < 0.05:
        print(f"    - Flow alignment is a MODERATE predictor (p={flow_p:.3f})")
        print(f"    - Enable +0.05 context boost when flow aligns")
    else:
        print(f"    - Flow alignment is NOT significant (p={flow_p:.2f})")

    if context_trades < 100:
        remaining = 100 - context_trades
        print(f"    - Need {remaining} more trades with context for reliable analysis")
        print(f"    - Re-analyze at 100+ trades")

    print(f"\n{bar}\n")


def main():
    parser = argparse.ArgumentParser(
        description="QuantData Correlation Analysis — determines if GEX/flow data predicts trade outcomes"
    )
    parser.add_argument(
        "--min-trades", type=int, default=20,
        help="Minimum trades with context required to run analysis (default: 20)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON instead of formatted report",
    )
    args = parser.parse_args()

    # Load trades
    all_trades = load_all_trades()
    context_trades = filter_trades_with_context(all_trades)

    if not all_trades:
        print("No paper trading journal entries found.")
        print(f"Expected journal files in: {LOGS_DIR}")
        sys.exit(1)

    if len(context_trades) < args.min_trades:
        print(f"Only {len(context_trades)} trades have market context data.")
        print(f"Need at least {args.min_trades} for analysis.")
        print("Continue paper trading with QuantData context logging enabled.")
        sys.exit(0)

    # Run analyses
    gamma = analyze_gamma_regime(context_trades)
    flow = analyze_flow_alignment(context_trades)
    fat_tails = analyze_fat_tails(context_trades)
    skew = analyze_skew_regime(context_trades)

    if args.json:
        output = {
            "total_trades": len(all_trades),
            "context_trades": len(context_trades),
            "gamma_regime": {k: v for k, v in gamma.items() if not k.startswith("_")},
            "gamma_p_value": gamma.get("_p_value", 1.0),
            "flow_alignment": {k: v for k, v in flow.items() if not k.startswith("_")},
            "flow_p_value": flow.get("_p_value", 1.0),
            "fat_tails": fat_tails,
            "skew_regime": skew,
        }
        print(json.dumps(output, indent=2, default=str))
    else:
        print_report(
            total_trades=len(all_trades),
            context_trades=len(context_trades),
            gamma=gamma,
            flow=flow,
            fat_tails=fat_tails,
            skew=skew,
        )


if __name__ == "__main__":
    main()
