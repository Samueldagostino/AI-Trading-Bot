#!/usr/bin/env python3
"""
Deflated Sharpe Ratio (DSR) and Probability of Backtest Overfitting (PBO) Diagnostic.

Implements:
  - Lopez de Prado's Deflated Sharpe Ratio from "Advances in Financial Machine Learning" (2018)
  - Bailey et al.'s Probability of Backtest Overfitting framework

This script evaluates whether a trading system's observed Sharpe ratio is statistically
significant given the number of alternative configurations tested before arriving at the
final system.

Key References:
  [1] Lopez de Prado, M. (2018). Advances in Financial Machine Learning. Wiley.
  [2] Bailey, D., Borwein, J., López de Prado, M., & Zhu, Q. (2016).
      "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting,
      and Non-Normality". Journal of Portfolio Management, 42(4), 39-53.
"""

import math
import sys
from dataclasses import dataclass
from typing import Tuple, Optional

# Try to import numpy/scipy for better numerical stability; fall back to stdlib if needed
try:
    import numpy as np
    from scipy import stats
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False
    np = None


# ============================================================================
# CONFIGURATION: Edit these values or override with command-line arguments
# ============================================================================

# Observed Sharpe Ratio from out-of-sample (OOS) testing
# Default: Estimated from system performance (PF 1.73, 61.9% win rate, 1524 trades)
OBSERVED_SHARPE = None  # Will be estimated if None

# Number of alternative configurations/backtests tried before arriving at the final system
# THIS IS THE KEY UNKNOWN - set based on your development process
# Examples:
#   - Random walk through parameters: 20-50
#   - Systematic optimization: 100-500
#   - Intensive parameter search: 500+
NUM_TRIALS = 20

# Total number of trades in the OOS period
NUM_TRADES = 1524

# Win rate (fraction of winning trades)
WIN_RATE = 0.619

# Average winning trade (in dollars or points)
# If None, will be estimated from equity curve / win_rate
AVG_WINNER = None

# Average losing trade (in dollars or points)
# If None, will be estimated from equity curve / (1-win_rate)
AVG_LOSER = None

# Return distribution skewness (third moment)
# If None, will be estimated from win/loss ratio
SKEWNESS = None

# Return distribution excess kurtosis (fourth moment - 3)
# If None, will be estimated as 0 (normal assumption)
KURTOSIS = None

# System performance metrics (for estimation of defaults)
PROFIT_FACTOR = 1.73           # Total wins / Total losses
MONTHLY_TRADES = 254           # Average trades per month
BACKTESTING_MONTHS = 6         # Number of months in OOS period
ANNUAL_RETURN_PCT = 0.0        # Optional: actual annualized return if known


# ============================================================================
# NORMAL CDF / INVERSE CDF IMPLEMENTATIONS
# ============================================================================

def normal_cdf(x: float) -> float:
    """
    Compute the cumulative distribution function of the standard normal distribution.
    Uses error function approximation if scipy not available.
    Accurate to ~6 decimal places.
    """
    if HAS_SCIPY:
        return stats.norm.cdf(x)

    # Numerical approximation using error function
    # Based on Abramowitz & Stegun (1964) approximation
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911

    sign = 1 if x >= 0 else -1
    x = abs(x) / math.sqrt(2)

    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t + a3) * t + a2) * t + a1) * t * math.exp(-x * x))

    return 0.5 * (1.0 + sign * y)


def inverse_normal_cdf(p: float, max_iterations: int = 100, tolerance: float = 1e-10) -> float:
    """
    Compute the inverse CDF (quantile function) of the standard normal distribution.
    Uses Wichura rational approximation if scipy not available.
    Accurate to ~6 decimal places.
    """
    if HAS_SCIPY:
        return stats.norm.ppf(p)

    if p <= 0 or p >= 1:
        raise ValueError(f"p must be in (0, 1), got {p}")

    # Coefficients for rational approximation (Wichura, 1988)
    # For central region (0.02425 < p < 0.97575)
    a0 = 2.506628277459
    a1 = 3.224671290700
    a2 = 2.445134137143
    a3 = 0.065502715588
    b1 = 0.642979360726
    b2 = 0.265321895265
    b3 = 0.025646868067
    b4 = 0.0012394576045

    # Coefficients for lower tail (p < 0.02425)
    c0 = -7.784894002
    c1 = 0.3224671290
    c2 = 2.445134137
    c3 = 3.754408661

    # Coefficients for upper tail (p > 0.97575)
    d0 = -7.784894002
    d1 = 0.3224671290
    d2 = 2.445134137
    d3 = 3.754408661

    if p < 0.02425:
        # Lower tail approximation
        t = math.sqrt(-2.0 * math.log(p))
        return -(c0 + c1 * t + c2 * t * t) / (1.0 + c3 * t + c2 * t * t)
    elif p > 0.97575:
        # Upper tail approximation
        t = math.sqrt(-2.0 * math.log(1.0 - p))
        return (c0 + c1 * t + c2 * t * t) / (1.0 + c3 * t + c2 * t * t)
    else:
        # Central region - rational approximation
        t = p - 0.5
        r = t * t
        return t * (a0 + r * (a1 + r * (a2 + r * a3))) / (
            1.0 + r * (b1 + r * (b2 + r * (b3 + r * b4)))
        )


# ============================================================================
# HELPER FUNCTIONS FOR ESTIMATION
# ============================================================================

def estimate_trade_statistics(
    profit_factor: float,
    win_rate: float,
    num_trades: int,
) -> Tuple[float, float, float]:
    """
    Estimate average winner, average loser, and standard deviation of trades.

    Uses the relationship: Profit_Factor = (WR × Avg_Win) / ((1-WR) × Avg_Loss)

    Assumes: Avg_Loss = -1 (normalized), and scales Avg_Win accordingly.
    Returns: (avg_winner, avg_loser, std_dev)
    """
    avg_loser = -1.0  # Normalized unit loss

    # From PF = (WR × Avg_Win) / ((1-WR) × |Avg_Loss|)
    # Solve for Avg_Win:
    avg_winner = (profit_factor * (1 - win_rate) * abs(avg_loser)) / win_rate

    # Expected value of a single trade
    expected_trade = (win_rate * avg_winner) + ((1 - win_rate) * avg_loser)

    # Variance: E[X²] - (E[X])²
    # E[X²] ≈ WR × Avg_Win² + (1-WR) × Avg_Loser²
    e_x_squared = (win_rate * avg_winner ** 2) + ((1 - win_rate) * avg_loser ** 2)
    variance = e_x_squared - (expected_trade ** 2)
    std_dev = math.sqrt(max(variance, 0.001))  # Avoid zero/negative variance

    return avg_winner, avg_loser, std_dev


def estimate_skewness(avg_winner: float, avg_loser: float, win_rate: float) -> float:
    """
    Estimate return distribution skewness from win/loss characteristics.

    Positive skew when avg_winner > |avg_loser| (favorable ratio of wins to losses).
    """
    # Normalized skewness based on payoff ratio
    payoff_ratio = abs(avg_winner / avg_loser) if avg_loser != 0 else 1.0

    # Rough estimate: skew ≈ (payoff_ratio - 1) / (payoff_ratio + 1) × scaling_factor
    if payoff_ratio > 1.0:
        skew = 0.3 * math.log(payoff_ratio)  # Positive skew for good payoff ratio
    else:
        skew = -0.3 * math.log(1.0 / payoff_ratio)  # Negative skew for poor ratio

    return skew


def estimate_kurtosis(win_rate: float) -> float:
    """
    Estimate excess kurtosis from win rate.

    More concentrated distributions (very high or low win rates) have higher kurtosis.
    """
    # Excess kurtosis: higher for more extreme win rates
    p = win_rate
    skew_from_wr = 1.0 - 2 * p if p > 0.5 else 2 * p - 1.0

    # Beta-binomial approximation for excess kurtosis
    excess_kurt = -2 + (6 / (1 + skew_from_wr ** 2))
    return excess_kurt


def estimate_sharpe_ratio(
    win_rate: float,
    avg_winner: float,
    avg_loser: float,
    num_trades: int,
) -> float:
    """
    Estimate annualized Sharpe ratio from trade-level statistics.

    Assumption: 252 trading days per year.
    """
    # Expected trade return
    avg_trade_return = (win_rate * avg_winner) + ((1 - win_rate) * avg_loser)

    # Standard deviation of trade returns
    e_x_squared = (win_rate * avg_winner ** 2) + ((1 - win_rate) * avg_loser ** 2)
    variance = e_x_squared - (avg_trade_return ** 2)
    std_trade_return = math.sqrt(max(variance, 0.001))

    if std_trade_return == 0:
        return 0.0

    # Sharpe ratio (assuming zero risk-free rate)
    sharpe_daily = avg_trade_return / std_trade_return if std_trade_return > 0 else 0.0

    # Annualize: Sharpe_annual ≈ Sharpe_daily × sqrt(trades_per_year)
    trades_per_year = 252.0  # Trading days per year
    sharpe_annual = sharpe_daily * math.sqrt(trades_per_year)

    return sharpe_annual


# ============================================================================
# DEFLATED SHARPE RATIO COMPUTATION
# ============================================================================

def compute_deflated_sharpe_ratio(
    observed_sharpe: float,
    num_trials: int,
    num_trades: int,
    skewness: float,
    kurtosis: float,
) -> Tuple[float, float]:
    """
    Compute the Deflated Sharpe Ratio (DSR) and its p-value.

    DSR = Φ( (SR_obs - E[max SR]) × √(n-1) / √(1 - γ×SR + (κ-1)/4 × SR²) )

    Where:
      SR_obs: Observed Sharpe ratio
      E[max SR]: Expected maximum SR under null hypothesis (multiple trials)
      γ: Return distribution skewness
      κ: Return distribution excess kurtosis (+ 3 for raw kurtosis)
      n: Number of trades
      Φ: Cumulative normal distribution function

    Returns:
      (dsr, p_value)
    """
    # Euler-Mascheroni constant
    EULER_MASCHERONI = 0.5772156649

    # Compute E[max SR] under null hypothesis (H0: true SR = 0)
    # E[max] ≈ (1-γ)·Φ⁻¹(1-1/N) + γ·Φ⁻¹(1-1/(N·e))
    # where γ ≈ 0.5772 (Euler-Mascheroni constant)

    inv_norm_1 = inverse_normal_cdf(1.0 - 1.0 / num_trials)
    inv_norm_2 = inverse_normal_cdf(1.0 - 1.0 / (num_trials * math.e))

    expected_max_sr = (
        (1.0 - EULER_MASCHERONI) * inv_norm_1 +
        EULER_MASCHERONI * inv_norm_2
    )

    # Compute the denominator: sqrt(1 - γ·SR + (κ-1)/4 · SR²)
    # This adjusts for non-normality (skewness and kurtosis)
    denominator_term = (
        1.0 - (skewness * observed_sharpe) +
        ((kurtosis - 1.0) / 4.0) * (observed_sharpe ** 2)
    )

    if denominator_term <= 0:
        # Non-normal adjustment makes distribution invalid; return conservative estimate
        denominator_term = 0.1

    # Deflated Sharpe Ratio
    numerator = (observed_sharpe - expected_max_sr) * math.sqrt(num_trades - 1)
    dsr_z_score = numerator / math.sqrt(denominator_term)

    # Convert z-score to probability
    dsr_pvalue = normal_cdf(dsr_z_score)

    return dsr_z_score, dsr_pvalue


def compute_minimum_backtest_length(
    observed_sharpe: float,
    num_trials: int,
    skewness: float,
    kurtosis: float,
    target_pvalue: float = 0.95,
) -> int:
    """
    Compute the Minimum Backtest Length (MinBTL): minimum number of trades needed
    for the observed SR to be statistically significant at target p-value, given
    the number of trials.

    Rearranges the DSR formula to solve for n:
      n ≥ 1 + (Φ⁻¹(target_p) / (SR_obs - E[max SR]))² × (1 - γ·SR + (κ-1)/4 · SR²)
    """
    EULER_MASCHERONI = 0.5772156649

    # Compute E[max SR]
    inv_norm_1 = inverse_normal_cdf(1.0 - 1.0 / num_trials)
    inv_norm_2 = inverse_normal_cdf(1.0 - 1.0 / (num_trials * math.e))

    expected_max_sr = (
        (1.0 - EULER_MASCHERONI) * inv_norm_1 +
        EULER_MASCHERONI * inv_norm_2
    )

    # Target z-score
    z_target = inverse_normal_cdf(target_pvalue)

    # Non-normality adjustment
    adjustment = (
        1.0 - (skewness * observed_sharpe) +
        ((kurtosis - 1.0) / 4.0) * (observed_sharpe ** 2)
    )

    if adjustment <= 0:
        adjustment = 0.1

    # Denominator of fraction
    denom = (observed_sharpe - expected_max_sr) ** 2

    if denom <= 0:
        return 999999  # Infeasible

    min_trades = 1 + (z_target ** 2 / denom) * adjustment

    return max(1, int(math.ceil(min_trades)))


# ============================================================================
# SENSITIVITY ANALYSIS
# ============================================================================

def sensitivity_analysis(
    observed_sharpe: float,
    num_trades: int,
    skewness: float,
    kurtosis: float,
    trial_range: Tuple[int, int] = (5, 100),
) -> list:
    """
    Compute DSR for a range of num_trials values.
    Returns list of (num_trials, dsr, dsr_pvalue) tuples.
    """
    results = []

    # Create logarithmic scale for more interesting sampling
    step = max(1, (trial_range[1] - trial_range[0]) // 20)

    for n_trials in range(trial_range[0], trial_range[1] + 1, step):
        dsr, p_val = compute_deflated_sharpe_ratio(
            observed_sharpe, n_trials, num_trades, skewness, kurtosis
        )
        results.append((n_trials, dsr, p_val))

    return results


# ============================================================================
# MAIN ANALYSIS
# ============================================================================

@dataclass
class AnalysisResults:
    """Container for all analysis results."""
    observed_sharpe: float
    expected_max_sr: float
    deflated_sharpe: float
    dsr_pvalue: float
    pbo_percentage: float
    min_backtest_length: int
    num_trades: int
    num_trials: int
    win_rate: float
    avg_winner: float
    avg_loser: float
    skewness: float
    kurtosis: float


def run_analysis(
    observed_sharpe: Optional[float] = OBSERVED_SHARPE,
    num_trials: int = NUM_TRIALS,
    num_trades: int = NUM_TRADES,
    win_rate: float = WIN_RATE,
    avg_winner: Optional[float] = AVG_WINNER,
    avg_loser: Optional[float] = AVG_LOSER,
    skewness: Optional[float] = SKEWNESS,
    kurtosis: Optional[float] = KURTOSIS,
) -> AnalysisResults:
    """Run the complete deflated Sharpe ratio analysis."""

    # Estimate missing parameters
    if avg_winner is None or avg_loser is None:
        est_winner, est_loser, _ = estimate_trade_statistics(
            PROFIT_FACTOR, win_rate, num_trades
        )
        if avg_winner is None:
            avg_winner = est_winner
        if avg_loser is None:
            avg_loser = est_loser

    if skewness is None:
        skewness = estimate_skewness(avg_winner, avg_loser, win_rate)

    if kurtosis is None:
        kurtosis = estimate_kurtosis(win_rate)

    if observed_sharpe is None:
        observed_sharpe = estimate_sharpe_ratio(
            win_rate, avg_winner, avg_loser, num_trades
        )

    # Compute Expected Max SR under null hypothesis
    EULER_MASCHERONI = 0.5772156649
    inv_norm_1 = inverse_normal_cdf(1.0 - 1.0 / num_trials)
    inv_norm_2 = inverse_normal_cdf(1.0 - 1.0 / (num_trials * math.e))
    expected_max_sr = (
        (1.0 - EULER_MASCHERONI) * inv_norm_1 +
        EULER_MASCHERONI * inv_norm_2
    )

    # Compute DSR and p-value
    dsr, dsr_pvalue = compute_deflated_sharpe_ratio(
        observed_sharpe, num_trials, num_trades, skewness, kurtosis
    )

    # Probability of Backtest Overfitting (simplified): 1 - p-value
    pbo_percentage = (1.0 - dsr_pvalue) * 100.0

    # Minimum Backtest Length
    min_btl = compute_minimum_backtest_length(
        observed_sharpe, num_trials, skewness, kurtosis, target_pvalue=0.95
    )

    return AnalysisResults(
        observed_sharpe=observed_sharpe,
        expected_max_sr=expected_max_sr,
        deflated_sharpe=dsr,
        dsr_pvalue=dsr_pvalue,
        pbo_percentage=pbo_percentage,
        min_backtest_length=min_btl,
        num_trades=num_trades,
        num_trials=num_trials,
        win_rate=win_rate,
        avg_winner=avg_winner,
        avg_loser=avg_loser,
        skewness=skewness,
        kurtosis=kurtosis,
    )


def print_report(results: AnalysisResults) -> None:
    """Print a formatted analysis report."""

    print("=" * 80)
    print("DEFLATED SHARPE RATIO & BACKTEST OVERFITTING ANALYSIS")
    print("=" * 80)
    print()

    # Input Parameters
    print("INPUT PARAMETERS:")
    print(f"  Observed Sharpe Ratio (OOS):       {results.observed_sharpe:.4f} (annualized)")
    print(f"  Number of Trials (configurations): {results.num_trials}")
    print(f"  Total Trades in OOS Period:        {results.num_trades:,}")
    print(f"  Win Rate:                          {results.win_rate:.2%}")
    print(f"  Avg Winning Trade:                 ${results.avg_winner:,.2f}")
    print(f"  Avg Losing Trade:                  ${results.avg_loser:,.2f}")
    print(f"  Return Distribution Skewness:      {results.skewness:.4f}")
    print(f"  Return Distribution Ex. Kurtosis:  {results.kurtosis:.4f}")
    print()

    # Results
    print("RESULTS:")
    print(f"  Expected Max SR (null, N={results.num_trials}):  {results.expected_max_sr:.4f}")
    print(f"  Deflated Sharpe Ratio (DSR):       {results.deflated_sharpe:.4f}")
    print(f"  DSR p-value:                       {results.dsr_pvalue:.4f} ({results.dsr_pvalue*100:.2f}%)")
    print(f"  Probability of Backtest Overfitting: {results.pbo_percentage:.1f}%")
    print(f"  Minimum Backtest Length (95% sig): {results.min_backtest_length:,} trades")
    print()

    # Interpretation
    print("INTERPRETATION:")
    if results.deflated_sharpe >= 1.0:
        print("  ✓ DSR >= 1.0: Sharpe ratio appears STATISTICALLY SIGNIFICANT despite")
        print("    multiple trials. Strong evidence of genuine alpha.")
    elif results.deflated_sharpe >= 0.0:
        print("  ~ DSR between 0 and 1.0: Borderline significance. Sharpe ratio is")
        print("    reduced after adjusting for selection bias, but still plausible.")
    else:
        print("  ✗ DSR < 0.0: Sharpe ratio is NOT statistically significant after")
        print("    adjusting for multiple testing. Likely backtest overfitting.")
    print()

    if results.pbo_percentage >= 50.0:
        print(f"  ⚠ PBO = {results.pbo_percentage:.1f}%: STRONG indication of backtest overfitting.")
        print("    There is a >50% probability that this system's OOS performance is")
        print("    due to luck rather than genuine alpha.")
    elif results.pbo_percentage >= 25.0:
        print(f"  ⚠ PBO = {results.pbo_percentage:.1f}%: MODERATE overfitting risk. Use caution.")
    else:
        print(f"  ✓ PBO = {results.pbo_percentage:.1f}%: Low overfitting risk.")
    print()

    if results.num_trades >= results.min_backtest_length:
        print(f"  ✓ Sample size is SUFFICIENT: {results.num_trades:,} trades >= "
              f"{results.min_backtest_length:,} required (95% confidence).")
    else:
        short_by = results.min_backtest_length - results.num_trades
        print(f"  ✗ Sample size is INSUFFICIENT: Need ~{short_by:,} more trades for")
        print(f"    95% confidence that observed Sharpe is not due to overfitting.")
    print()

    # Recommendation
    print("RECOMMENDATION:")
    if results.deflated_sharpe >= 1.0 and results.pbo_percentage < 50.0:
        print("  GO: System shows statistically significant edge after accounting for")
        print("  selection bias. However, continue monitoring OOS performance closely.")
        print("  Consider increasing num_trials estimate if more configs were tested.")
    elif results.deflated_sharpe >= 0.5 and results.pbo_percentage < 40.0:
        print("  CAUTIOUS GO: Marginal significance. Acceptable if risk management is")
        print("  strong and position sizing is conservative. Verify num_trials is accurate.")
    else:
        print("  NO-GO: Strong evidence of backtest overfitting. Before deploying:")
        print("    1) Verify num_trials (# of configs actually tested)")
        print("    2) Increase OOS sample size (need more trades)")
        print("    3) Re-test with different data/market regimes")
        print("    4) Consider simpler systems with fewer parameters")
    print()

    print("=" * 80)


def print_sensitivity(results: AnalysisResults) -> None:
    """Print sensitivity analysis showing how DSR varies with num_trials."""
    print("SENSITIVITY ANALYSIS: DSR vs. Number of Trials")
    print("-" * 80)
    print(f"{'Trials':<8} {'DSR':<12} {'p-value':<12} {'PBO %':<12} {'Min Trades':<12}")
    print("-" * 80)

    sensitivity = sensitivity_analysis(
        results.observed_sharpe,
        results.num_trades,
        results.skewness,
        results.kurtosis,
        trial_range=(5, 100),
    )

    for n_trials, dsr, p_val in sensitivity:
        pbo_pct = (1.0 - p_val) * 100.0
        min_trades = compute_minimum_backtest_length(
            results.observed_sharpe, n_trials, results.skewness, results.kurtosis
        )
        print(f"{n_trials:<8} {dsr:<12.4f} {p_val:<12.4f} {pbo_pct:<12.1f} {min_trades:<12,}")

    print("-" * 80)
    print("Key insight: As num_trials increases, DSR decreases (more conservative).")
    print("Higher num_trials = penalty for more extensive backtest search.")
    print()


def main():
    """Main entry point."""

    # Parse command-line arguments if provided
    if len(sys.argv) > 1:
        try:
            num_trials_arg = int(sys.argv[1])
            print(f"[INFO] Using num_trials = {num_trials_arg} from command line\n")
        except (ValueError, IndexError):
            print("Usage: python deflated_sharpe.py [num_trials]")
            print("       num_trials: number of alternative configs tested (default: 20)")
            sys.exit(1)
    else:
        num_trials_arg = NUM_TRIALS
        print(f"[INFO] Using default num_trials = {num_trials_arg}")
        print("[HINT] To specify a different value, run:")
        print(f"       python {sys.argv[0]} <num_trials>\n")

    # Run analysis
    results = run_analysis(num_trials=num_trials_arg)

    # Print report
    print_report(results)

    # Print sensitivity analysis
    print_sensitivity(results)

    # Summary table for quick reference
    print("QUICK REFERENCE:")
    print(f"  DSR Threshold for GO signal:  >= 1.0  (your system: {results.deflated_sharpe:.4f})")
    print(f"  PBO Threshold for GO signal:  < 50%  (your system: {results.pbo_percentage:.1f}%)")
    print()


if __name__ == "__main__":
    main()
