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
#   Rule 1 – Min signal score >= 0.75  (eliminates low-conviction noise)
#   Rule 2 – Max stop distance <= 30 pts (caps tail risk per trade)
HIGH_CONVICTION_MIN_SCORE: float = 0.75
HIGH_CONVICTION_MAX_STOP_PTS: float = 30.0

# ── LIQUIDITY SWEEP DETECTOR ─────────────────────────────────────
#   Sweep score >= 0.70: eligible for HC filter independently
#   Sweep + existing signal fire together: HC score boosted by +0.05
SWEEP_MIN_SCORE: float = 0.70
SWEEP_CONFLUENCE_BONUS: float = 0.05

# ── TIMEFRAMES ────────────────────────────────────────────────────
HTF_TIMEFRAMES: frozenset = frozenset({"1D", "4H", "1H", "30m", "15m", "5m"})
EXECUTION_TIMEFRAMES: frozenset = frozenset({"2m", "3m", "1m"})
