"""
Trading Constants -- Single Source of Truth
===========================================
All hard-gate constants live here.  Every module that needs these values
MUST import from this file.  Do NOT redefine them locally.

Derived from backtest forensics + C1 exit research (Feb 2026).
Only the intersection of tight stops + strong signals showed durable edge.

Do not loosen these gates without new backtested evidence across the full
6-month OOS window with calibrated slippage.
"""

# ── HIGH-CONVICTION FILTER ────────────────────────────────────────
#   Rule 1 - Min signal score >= 0.75  (eliminates low-conviction noise)
#   Rule 2 - Max stop distance <= 30 pts (caps tail risk per trade)
#   Reverted to 30pt to match v3 validated config (PF 2.86, 396 trades).
#   Dollar-based stop tiers handle conviction-scaled risk within the 30pt cap.
HIGH_CONVICTION_MIN_SCORE: float = 0.75
HIGH_CONVICTION_MAX_STOP_PTS: float = 30.0   # v3 validated: 30pt hard cap (reverted from 50pt -- matches PF 2.86 backtest)
HIGH_CONVICTION_MIN_STOP_PTS: float = 12.0   # Floor: prevents micro-stops that get clipped by noise

# ── DOLLAR-BASED STOP TIERS ────────────────────────────────────────
#   Conviction score determines max dollar risk per trade (all contracts combined).
#   Within each tier, dollar budget interpolates linearly from min->max.
#   Higher conviction = more room (wider stop) because the signal is stronger.
#
#   With 5 MNQ contracts @ $2/pt = $10/pt total:
#     $150 -> 15 pts | $200 -> 20 pts | $300 -> 30 pts (HC cap)
#     Higher tiers still exist but are capped by HC_MAX_STOP_PTS = 30.0
#
#   Format: (score_min, score_max, dollar_min, dollar_max)
DOLLAR_STOP_TIERS: list = [
    (0.75, 0.80, 150.0, 200.0),   # Standard conviction
    (0.80, 0.85, 200.0, 300.0),   # High conviction
    (0.85, 1.01, 300.0, 400.0),   # Very high conviction
]
DOLLAR_STOP_DEFAULT: float = 150.0  # Fallback if score doesn't match any tier


def get_dollar_risk_budget(score: float) -> float:
    """Return the max dollar risk for a trade based on conviction score.

    Interpolates linearly within each tier so there are no cliff edges.
    Example: score=0.77 -> halfway through Standard tier -> ~$175.
    """
    for min_s, max_s, min_d, max_d in DOLLAR_STOP_TIERS:
        if min_s <= score < max_s:
            t = (score - min_s) / (max_s - min_s)
            return round(min_d + t * (max_d - min_d), 2)
    return DOLLAR_STOP_DEFAULT

# ── MIN R:R GATE (DISABLED for C1 time-exit strategy) ──────────────
# R:R is irrelevant when using time-based exits instead of profit targets.
# Set to 0.0 to effectively disable the gate.
MIN_RR_OVERRIDE: float = 0.0

# ── LIQUIDITY SWEEP DETECTOR ─────────────────────────────────────
#   Sweep score >= 0.50: base sweep passes (HC gate is the real filter at 0.60)
#   Sweep + existing signal fire together: HC score boosted by +0.05
SWEEP_MIN_SCORE: float = 0.50
SWEEP_CONFLUENCE_BONUS: float = 0.05

# ── HTF BIAS ENGINE ────────────────────────────────────────────────
#   Config D threshold, validated Feb 2026.
#   gate=0.7 silently degrades PF from 1.29 to 0.79.  Do NOT change
#   without full backtest validation across 6-month OOS window.
HTF_STRENGTH_GATE: float = 0.3

# ── HTF HYSTERESIS (anti-flip-flop) ─────────────────────────────
#   With only 2 TFs (5m + 15m), a single 5m blip can flip the consensus
#   from bearish→neutral→allows longs.  Hysteresis prevents this:
#
#   1) MARGIN: opposing direction must exceed this strength ratio to even
#      START the confirmation timer.  0.3 = 30% stronger than neutral.
#   2) CONFIRM BARS: opposing direction must hold for N consecutive
#      get_bias() calls before the lock actually flips.  Prevents fakeouts
#      where one candle spikes against the trend then reverses.
#
#   Example on a bearish day:
#     - 15m bearish, 5m flips bullish → tie → neutral, but lock stays bearish
#     - 5m stays bullish for 3 consecutive calls → NOW lock flips to neutral/bullish
#     - Single 5m bull candle then reversal → timer resets, lock stays bearish
HTF_HYSTERESIS_MARGIN: float = 0.3
HTF_HYSTERESIS_CONFIRM_BARS: int = 3   # consecutive bars opposing must hold

# ── HTF STALENESS LIMITS ──────────────────────────────────────────
#   Maximum age (minutes) before a TF's bars are considered stale.
#   If no bar arrives within this window, that TF's bias is downgraded
#   to "neutral" and a warning is logged.
HTF_STALENESS_LIMITS: dict = {
    "5m":  15,    # 3x bar period
    "15m": 45,    # 3x bar period
    "30m": 90,    # 3x bar period
    "1H":  180,   # 3x bar period
    "4H":  720,   # 3x bar period
    "1D":  2880,  # 2x bar period (48h -- accounts for weekends gracefully)
}

# ── UNIVERSAL CONFIRMATION LAYER (UCL) v2 ────────────────────────
# v2 removes weak-signal rescue (0.60-0.74 → net negative, PF 0.54).
# FVG confluence boosts strong signals.
# NOTE: Wide-stop post-sweep confirmation was planned but never implemented.
# Max stop raised to 45pt to compensate (Mar 2026).
UCL_FVG_CONFLUENCE_BOOST: float = 0.05   # score boost when entry is near active FVG
UCL_CONFIRMATION_BOOST: float = 0.10
UCL_FVG_BOOST: float = 0.05
UCL_FAST_CONFIRM_BOOST: float = 0.05
UCL_HTF_ALIGN_BOOST: float = 0.05

# ── LAYER 2 CONTEXT BOOSTS (PATH C architecture) ───────────────────
# Non-sweep signal sources are demoted to contextual score modifiers.
# They boost sweep scores but CANNOT independently trigger trades.
CONTEXT_AGGREGATOR_BOOST: float = 0.05   # aggregator direction agrees with sweep
CONTEXT_OB_BOOST: float = 0.05           # order block near sweep price
CONTEXT_FVG_BOOST: float = 0.05          # FVG near sweep price

# ── AGGREGATOR STANDALONE TRIGGER (PATH C+ dual-trigger) ──────────
# Re-enables aggregator as an independent entry trigger when it reaches
# high conviction.  Backtest data showed aggregator-only trades produced
# +$12,626 across 1,025 trades -- more total profit than sweep-only.
# PATH C demoted the aggregator to context-only, but there's no documented
# evidence that removing standalone triggers improved anything.
# Mar 2026: Re-enabled alongside sweeps as a dual-trigger architecture.
AGGREGATOR_STANDALONE_ENABLED: bool = True
AGGREGATOR_STANDALONE_MIN_SCORE: float = 0.75  # Must meet HC gate independently

# ── TIMEFRAMES ────────────────────────────────────────────────────
# Intraday-only HTF bias: 5m + 15m (relevant to 10-min C1 scalp window)
# Higher TFs (30m, 1H, 4H, 1D) removed -- irrelevant for scalp timing.
HTF_TIMEFRAMES: frozenset = frozenset({"15m", "5m"})
EXECUTION_TIMEFRAMES: frozenset = frozenset({"2m", "3m", "1m"})

# ── V1.3.3 GAINZALGO SUITE CONSTANTS ────────────────────────────
#   GainzAlgo Framework integration (Mar 2026).
#   These control the 5 enhancement modules that feed into the existing
#   aggregator as additive signal sources.  The core PATH C+ dual-trigger
#   architecture is unchanged.

# -- Volatility Percentile Normalizer --
VOL_PERCENTILE_LOOKBACK: int = 500           # Rolling ATR history for percentile
VOL_ADAPTIVE_SWEEP_DEPTH_MIN: float = 2.0    # Min sweep depth (pts) at 0th percentile
VOL_ADAPTIVE_SWEEP_DEPTH_MAX: float = 6.0    # Max sweep depth (pts) at 100th percentile

# -- Momentum Acceleration Model (SAMSM) --
SAMSM_VELOCITY_PERIOD: int = 5               # EMA period for velocity smoothing
SAMSM_ACCEL_PERIOD: int = 3                  # EMA period for acceleration smoothing
SAMSM_SURGE_SIGMA: float = 2.0              # σ threshold for surge detection
SAMSM_EXHAUSTION_BARS: int = 5              # Consecutive deceleration bars for exhaustion

# -- Cycle-Slope Trend Analyzer (CSTA) --
CSTA_FAST_PERIOD: int = 8                    # Fast EMA for micro-cycle detection
CSTA_SLOW_PERIOD: int = 21                   # Slow EMA for macro-direction
CSTA_SLOPE_LOOKBACK: int = 3                 # Bars for slope computation

# -- Candle Micro-Reversal Evaluator (CSMRM) --
CSMRM_REJECTION_THRESHOLD: float = 0.55     # Wick ratio to count as rejection
CSMRM_DOJI_THRESHOLD: float = 0.15          # Body/range < this → doji
CSMRM_MIN_RANGE_ATR_RATIO: float = 0.3      # Min candle size vs ATR for evaluation

# -- Adaptive Confidence Engine --
ADAPTIVE_HC_GATE_FLOOR: float = 0.70        # Absolute minimum HC gate
ADAPTIVE_HC_GATE_CEILING: float = 0.82      # Absolute maximum HC gate
ADAPTIVE_MAX_CROSS_SIGNAL_BOOST: float = 0.10  # Max synergy boost from cross-signals

# -- GainzAlgo signal strengths in aggregator --
GAINZ_EXHAUSTION_SIGNAL_STRENGTH: float = 0.70   # Candle exhaustion reversal
GAINZ_MOMENTUM_SIGNAL_STRENGTH: float = 0.65     # Momentum acceleration/deceleration
GAINZ_CYCLE_SIGNAL_STRENGTH: float = 0.60        # Cycle-phase pullback entry

# -- Feature flag (master switch for V1.3.3 modules) --
GAINZ_MODULES_ENABLED: bool = True

# ── REGIME FILTER ────────────────────────────────────────────────
#   Shadow analysis showed ranging longs are toxic: 28.6% WR (needs 32.5%),
#   net -$2,032.  Ranging shorts are acceptable (33.9% WR, slightly above BE).
#   Block ranging longs, allow ranging shorts.
RANGING_BLOCK_LONGS: bool = True   # True = reject long entries during ranging regime

# ── TRADING HOURS & SESSION MANAGEMENT ──────────────────────────
#   RTH entry cutoff: 3:30 PM ET (allow 1 hour 20 minutes to close before maintenance)
#   Maintenance window: 4:50 PM - 6:00 PM ET (forced flatten at 4:50 PM)
#   Evening session opens: 6:00 PM ET (immediately after maintenance)
RTH_ENTRY_CUTOFF_HOUR: int = 15      # 3:00 PM ET
RTH_ENTRY_CUTOFF_MINUTE: int = 30    # 3:30 PM ET
MAINTENANCE_FLATTEN_HOUR: int = 16   # 4:00 PM ET
MAINTENANCE_FLATTEN_MINUTE: int = 50 # 4:50 PM ET (forced flatten start)
EVENING_SESSION_OPEN_HOUR: int = 18  # 6:00 PM ET (post-maintenance reopening)
