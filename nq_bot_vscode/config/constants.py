"""
Trading Constants — Single Source of Truth
===========================================
All hard-gate constants live here.  Every module that needs these values
MUST import from this file.  Do NOT redefine them locally.

Derived from backtest forensics + C1 exit research (Feb 2026).
Only the intersection of tight stops + strong signals showed durable edge.

Do not loosen these gates without new backtested evidence across the full
6-month OOS window with calibrated slippage.
"""

# ── HIGH-CONVICTION FILTER ────────────────────────────────────────
#   Rule 1 – Min signal score >= 0.60  (allows 2+ bonus signals through)
#   Rule 2 – Max stop distance <= 30 pts (caps tail risk per trade)
HIGH_CONVICTION_MIN_SCORE: float = 0.60
HIGH_CONVICTION_MAX_STOP_PTS: float = 50.0
HIGH_CONVICTION_MIN_STOP_PTS: float = 30.0  # Wider stops = room for 5-bar exit (30-50pt sweet spot)

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
    "1D":  2880,  # 2x bar period (48h — accounts for weekends gracefully)
}

# ── UNIVERSAL CONFIRMATION LAYER (UCL) v2 ────────────────────────
# v2 removes weak-signal rescue (0.60-0.74 → net negative, PF 0.54).
# Instead: FVG confluence boosts strong signals, and wide-stop sweeps
# (score >= 0.75, stop > 30pt) get converted to tight-stop entries
# via post-sweep confirmation.
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

# ── TIMEFRAMES ────────────────────────────────────────────────────
# Intraday-only HTF bias: 5m + 15m (relevant to 10-min C1 scalp window)
# Higher TFs (30m, 1H, 4H, 1D) removed — irrelevant for scalp timing.
HTF_TIMEFRAMES: frozenset = frozenset({"15m", "5m"})
EXECUTION_TIMEFRAMES: frozenset = frozenset({"2m", "3m", "1m"})
