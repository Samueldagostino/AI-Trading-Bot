#!/usr/bin/env python3
"""
Trade Profile Analysis — Comprehensive 607-Trade Statistical Model
====================================================================
Reads existing backtest data from logs (paper_trades.json + paper_decisions.json)
and produces a complete statistical profile answering:
  "Under what conditions does this system produce edge,
   and under what conditions does it lose?"

Sections:
  1. Trade Outcome Profile (direction, signal source, confluence, HC score)
  2. Time-Based Analysis (hour, day, session)
  3. Volatility Regime Analysis (ATR buckets)
  4. Drawdown Deep Dive (equity curve, streaks, worst days)
  5. C1 vs C2 Performance Split
  6. Edge Concentration Analysis (top-N dependency)
  7. Optimal Filter Identification (filter impact table)

Outputs:
  - logs/trade_profile_analysis.json  (full raw data)
  - docs/trade_profile_summary.md     (human-readable)

READ-ONLY: does NOT modify production code or run new backtests.

Usage:
    python scripts/trade_profile_analysis.py
"""

import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Paths ──
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
PROJECT_DIR = REPO_ROOT / "nq_bot_vscode"
LOGS_DIR = REPO_ROOT / "logs"

TRADES_FILE = PROJECT_DIR / "logs" / "paper_trades.json"
DECISIONS_FILE = PROJECT_DIR / "logs" / "paper_decisions.json"
FORENSIC_FILE = LOGS_DIR / "max_stop_forensic.json"
COMPARISON_FILE = LOGS_DIR / "structural_stop_comparison.json"

OUTPUT_JSON = LOGS_DIR / "trade_profile_analysis.json"
OUTPUT_MD = REPO_ROOT / "docs" / "trade_profile_summary.md"

# ── Constants ──
STARTING_EQUITY = 25_000.0
POINT_VALUE = 2.00  # MNQ $2/point


# =====================================================================
#  DATA LOADING
# =====================================================================

def load_trades() -> List[Dict]:
    """Load and merge paper_trades.json with paper_decisions.json entry data."""
    with open(str(TRADES_FILE)) as f:
        trades = json.load(f)

    with open(str(DECISIONS_FILE)) as f:
        decisions = json.load(f)

    # Build lookup: entry decisions indexed by (timestamp-ish, direction)
    entry_decisions = [d for d in decisions if d.get("decision") == "entry"]

    # Match each trade to its entry decision by sequence
    # Trades and entry decisions are in the same order (607 each)
    merged = []
    for i, trade in enumerate(trades):
        entry = entry_decisions[i] if i < len(entry_decisions) else {}

        # Compute stop distance from entry decision
        stop_price = entry.get("stop", 0)
        entry_price = trade.get("entry_price", 0)
        direction = trade.get("direction", "")
        if direction == "long" and stop_price and entry_price:
            stop_distance = entry_price - stop_price
        elif direction == "short" and stop_price and entry_price:
            stop_distance = stop_price - entry_price
        else:
            stop_distance = 0

        # Parse timestamp to datetime
        ts_str = trade.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            ts = None

        # Parse entry timestamp
        entry_ts_str = entry.get("timestamp", "")
        try:
            entry_ts = datetime.fromisoformat(entry_ts_str)
        except (ValueError, TypeError):
            entry_ts = None

        merged.append({
            "trade_index": i,
            "exit_timestamp": ts,
            "entry_timestamp": entry_ts,
            "direction": direction,
            "entry_price": entry_price,
            "total_pnl": trade.get("total_pnl", 0),
            "c1_pnl": trade.get("c1_pnl", 0),
            "c2_pnl": trade.get("c2_pnl", 0),
            "c1_reason": trade.get("c1_reason", ""),
            "c2_reason": trade.get("c2_reason", ""),
            "signal_source": trade.get("signal_source", "unknown"),
            "signal_score": entry.get("signal_score", 0),
            "regime": entry.get("regime", "unknown"),
            "htf_bias": entry.get("htf_bias", "n/a"),
            "htf_strength": entry.get("htf_strength", 0),
            "stop_distance": stop_distance,
            "stop_price": stop_price,
            "entry_slippage_pts": trade.get("entry_slippage_pts", 0),
            "exit_slippage_pts": trade.get("exit_slippage_pts", 0),
            "total_slippage_pts": trade.get("total_slippage_pts", 0),
            "daily_pnl": trade.get("daily_pnl", 0),
            "sweep_levels": entry.get("sweep_levels", []),
            "sweep_score": entry.get("sweep_score", 0),
            "is_winner": trade.get("total_pnl", 0) > 0,
        })

    return merged


# =====================================================================
#  UTILITY FUNCTIONS
# =====================================================================

def compute_stats(trades: List[Dict]) -> Dict:
    """Compute standard stats for a group of trades."""
    if not trades:
        return {
            "count": 0, "winners": 0, "losers": 0,
            "win_rate": 0, "profit_factor": 0, "total_pnl": 0,
            "avg_pnl": 0, "avg_winner": 0, "avg_loser": 0,
        }

    pnls = [t["total_pnl"] for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    total_pnl = sum(pnls)
    gross_wins = sum(winners)
    gross_losses = abs(sum(losers))
    pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    return {
        "count": len(trades),
        "winners": len(winners),
        "losers": len(losers),
        "win_rate": round(len(winners) / len(trades) * 100, 1) if trades else 0,
        "profit_factor": round(pf, 2) if pf != float("inf") else 999.99,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(total_pnl / len(trades), 2) if trades else 0,
        "avg_winner": round(gross_wins / len(winners), 2) if winners else 0,
        "avg_loser": round(sum(losers) / len(losers), 2) if losers else 0,
    }


def compute_equity_curve(trades: List[Dict], starting: float = STARTING_EQUITY) -> List[Dict]:
    """Build equity curve with peak/trough tracking."""
    equity = starting
    peak = starting
    curve = [{"trade_index": -1, "equity": equity, "peak": peak, "drawdown": 0, "drawdown_pct": 0}]

    for t in trades:
        equity += t["total_pnl"]
        peak = max(peak, equity)
        dd = peak - equity
        dd_pct = dd / peak * 100 if peak > 0 else 0
        curve.append({
            "trade_index": t["trade_index"],
            "timestamp": t["exit_timestamp"].isoformat() if t["exit_timestamp"] else "",
            "equity": round(equity, 2),
            "peak": round(peak, 2),
            "drawdown": round(dd, 2),
            "drawdown_pct": round(dd_pct, 2),
            "pnl": round(t["total_pnl"], 2),
        })

    return curve


def compute_max_drawdown(trades: List[Dict], starting: float = STARTING_EQUITY) -> Dict:
    """Compute max drawdown with timestamps."""
    equity = starting
    peak = starting
    max_dd = 0
    max_dd_pct = 0
    peak_ts = None
    trough_ts = None
    peak_idx = -1
    trough_idx = -1

    for t in trades:
        equity += t["total_pnl"]
        if equity > peak:
            peak = equity
            peak_ts = t["exit_timestamp"]
            peak_idx = t["trade_index"]
        dd = peak - equity
        dd_pct = dd / peak * 100 if peak > 0 else 0
        if dd_pct > max_dd_pct:
            max_dd = dd
            max_dd_pct = dd_pct
            trough_ts = t["exit_timestamp"]
            trough_idx = t["trade_index"]

    return {
        "max_dd_dollars": round(max_dd, 2),
        "max_dd_pct": round(max_dd_pct, 2),
        "peak_timestamp": peak_ts.isoformat() if peak_ts else "",
        "trough_timestamp": trough_ts.isoformat() if trough_ts else "",
        "peak_trade_index": peak_idx,
        "trough_trade_index": trough_idx,
        "final_equity": round(equity, 2),
    }


def get_et_hour(ts: Optional[datetime]) -> Optional[int]:
    """Get hour in ET from a timezone-aware timestamp."""
    if ts is None:
        return None
    # Convert to ET (UTC-4 or UTC-5 depending on DST)
    # Timestamps are UTC or ET already; handle both
    try:
        from zoneinfo import ZoneInfo
        et = ts.astimezone(ZoneInfo("America/New_York"))
        return et.hour
    except Exception:
        # Fallback: assume UTC, subtract 4 for EDT
        return (ts.hour - 4) % 24


def get_et_datetime(ts: Optional[datetime]) -> Optional[datetime]:
    """Convert to ET datetime."""
    if ts is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ts.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return ts


def get_session(hour: int) -> str:
    """Classify hour into trading session."""
    if hour >= 18 or hour < 9:
        return "overnight"
    elif 9 <= hour < 12:
        return "morning"
    elif 12 <= hour < 14:
        return "lunch"
    else:
        return "afternoon"


def format_table(headers: List[str], rows: List[List], alignments: Optional[List[str]] = None) -> str:
    """Format a table as aligned text."""
    if not rows:
        return "  (no data)\n"

    # Compute column widths
    widths = [len(h) for h in headers]
    str_rows = []
    for row in rows:
        str_row = [str(v) for v in row]
        str_rows.append(str_row)
        for i, v in enumerate(str_row):
            if i < len(widths):
                widths[i] = max(widths[i], len(v))

    if alignments is None:
        alignments = ["<"] + [">"] * (len(headers) - 1)

    lines = []
    # Header
    header_parts = []
    for i, h in enumerate(headers):
        w = widths[i] if i < len(widths) else len(h)
        if alignments[i] == "<":
            header_parts.append(f"{h:<{w}}")
        else:
            header_parts.append(f"{h:>{w}}")
    lines.append("  " + "  ".join(header_parts))
    lines.append("  " + "  ".join("-" * w for w in widths))

    # Rows
    for str_row in str_rows:
        parts = []
        for i, v in enumerate(str_row):
            w = widths[i] if i < len(widths) else len(v)
            if i < len(alignments) and alignments[i] == "<":
                parts.append(f"{v:<{w}}")
            else:
                parts.append(f"{v:>{w}}")
        lines.append("  " + "  ".join(parts))

    return "\n".join(lines) + "\n"


# =====================================================================
#  SECTION 1: TRADE OUTCOME PROFILE
# =====================================================================

def analyze_trade_outcomes(trades: List[Dict]) -> Dict:
    """Compute win rate by direction, signal source, confluence count, HC score."""
    results = {}

    # ── By Direction ──
    direction_groups = defaultdict(list)
    for t in trades:
        direction_groups[t["direction"]].append(t)

    results["by_direction"] = {}
    for direction, group in sorted(direction_groups.items()):
        results["by_direction"][direction] = compute_stats(group)

    # ── By Signal Source ──
    source_groups = defaultdict(list)
    for t in trades:
        source_groups[t["signal_source"]].append(t)

    results["by_signal_source"] = {}
    for source, group in sorted(source_groups.items()):
        results["by_signal_source"][source] = compute_stats(group)

    # ── By HC Score Bucket ──
    score_buckets = {
        "0.75-0.80": (0.75, 0.80),
        "0.80-0.85": (0.80, 0.85),
        "0.85-0.90": (0.85, 0.90),
        "0.90+": (0.90, 100.0),
    }
    results["by_hc_score"] = {}
    for label, (lo, hi) in score_buckets.items():
        group = [t for t in trades if lo <= t["signal_score"] < hi]
        results["by_hc_score"][label] = compute_stats(group)

    # ── By Regime ──
    regime_groups = defaultdict(list)
    for t in trades:
        regime_groups[t["regime"]].append(t)

    results["by_regime"] = {}
    for regime, group in sorted(regime_groups.items()):
        results["by_regime"][regime] = compute_stats(group)

    # ── By HTF Bias ──
    htf_groups = defaultdict(list)
    for t in trades:
        htf_groups[t["htf_bias"]].append(t)

    results["by_htf_bias"] = {}
    for bias, group in sorted(htf_groups.items()):
        results["by_htf_bias"][bias] = compute_stats(group)

    return results


# =====================================================================
#  SECTION 2: TIME-BASED ANALYSIS
# =====================================================================

def analyze_time_based(trades: List[Dict]) -> Dict:
    """Win rate by hour, day of week, session."""
    results = {}

    # ── By Hour of Day (ET) ──
    hour_groups = defaultdict(list)
    for t in trades:
        h = get_et_hour(t["entry_timestamp"])
        if h is not None:
            hour_groups[h].append(t)

    results["by_hour"] = {}
    for hour in sorted(hour_groups.keys()):
        results["by_hour"][str(hour)] = compute_stats(hour_groups[hour])

    # ── By Day of Week ──
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    day_groups = defaultdict(list)
    for t in trades:
        et = get_et_datetime(t["entry_timestamp"])
        if et:
            day_groups[et.weekday()].append(t)

    results["by_day_of_week"] = {}
    for day_num in sorted(day_groups.keys()):
        name = day_names[day_num] if day_num < len(day_names) else f"Day_{day_num}"
        results["by_day_of_week"][name] = compute_stats(day_groups[day_num])

    # ── By Session ──
    session_groups = defaultdict(list)
    for t in trades:
        h = get_et_hour(t["entry_timestamp"])
        if h is not None:
            session = get_session(h)
            session_groups[session].append(t)

    results["by_session"] = {}
    for session in ["overnight", "morning", "lunch", "afternoon"]:
        if session in session_groups:
            results["by_session"][session] = compute_stats(session_groups[session])

    return results


# =====================================================================
#  SECTION 3: VOLATILITY REGIME ANALYSIS
# =====================================================================

def analyze_volatility(trades: List[Dict]) -> Dict:
    """Bucket trades by stop distance as ATR proxy."""
    results = {}

    # Stop distance is our best proxy for ATR at entry
    # ATR ~= stop_distance / multiplier, but stop distances directly correlate with vol
    # Bucket by stop distance ranges (structural stops scale with vol)
    stop_buckets = {
        "tight (<5pt)": (0, 5),
        "normal (5-10pt)": (5, 10),
        "moderate (10-15pt)": (10, 15),
        "wide (15-20pt)": (15, 20),
        "very_wide (20-30pt)": (20, 30),
    }

    results["by_stop_distance"] = {}
    for label, (lo, hi) in stop_buckets.items():
        group = [t for t in trades if lo <= t["stop_distance"] < hi]
        stats = compute_stats(group)
        stats["avg_stop_distance"] = (
            round(sum(t["stop_distance"] for t in group) / len(group), 2)
            if group else 0
        )
        results["by_stop_distance"][label] = stats

    # Estimate ATR buckets (stop_distance / 2.0 multiplier = approximate ATR)
    # This is based on the formula: stop = ATR * 2.0
    atr_buckets = {
        "low (<8)": (0, 8),
        "normal (8-12)": (8, 12),
        "elevated (12-18)": (12, 18),
        "high (18-25)": (18, 25),
        "extreme (25+)": (25, 200),
    }

    results["by_estimated_atr"] = {}
    for label, (lo, hi) in atr_buckets.items():
        # Estimate ATR from stop distance (structural stops may not follow exact formula)
        group = [t for t in trades if lo <= t["stop_distance"] / 1.5 < hi]
        stats = compute_stats(group)
        if group:
            avg_stop = sum(t["stop_distance"] for t in group) / len(group)
            stats["avg_stop_distance"] = round(avg_stop, 2)
            stats["estimated_avg_atr"] = round(avg_stop / 1.5, 2)
        results["by_estimated_atr"][label] = stats

    return results


# =====================================================================
#  SECTION 4: DRAWDOWN DEEP DIVE
# =====================================================================

def analyze_drawdown(trades: List[Dict]) -> Dict:
    """Full equity curve, streaks, worst day/week analysis."""
    results = {}

    # ── Equity Curve ──
    curve = compute_equity_curve(trades)
    results["equity_curve_summary"] = {
        "starting_equity": STARTING_EQUITY,
        "final_equity": curve[-1]["equity"] if curve else STARTING_EQUITY,
        "peak_equity": max(p["equity"] for p in curve),
        "trough_equity": min(p["equity"] for p in curve),
        "total_points": len(curve),
    }

    # ── Max Drawdown ──
    results["max_drawdown"] = compute_max_drawdown(trades)

    # ── Consecutive Win/Loss Streaks ──
    pnls = [t["total_pnl"] for t in trades]

    # Track all streaks with details
    streaks = {"wins": [], "losses": []}
    cur_type = None
    cur_count = 0
    cur_pnl = 0
    cur_start_idx = 0

    for i, pnl in enumerate(pnls):
        new_type = "win" if pnl > 0 else "loss"
        if new_type == cur_type:
            cur_count += 1
            cur_pnl += pnl
        else:
            if cur_type == "win" and cur_count > 0:
                streaks["wins"].append({
                    "count": cur_count, "total_pnl": round(cur_pnl, 2),
                    "start_idx": cur_start_idx, "end_idx": i - 1,
                    "start_ts": trades[cur_start_idx]["exit_timestamp"].isoformat()
                    if trades[cur_start_idx]["exit_timestamp"] else "",
                    "end_ts": trades[i - 1]["exit_timestamp"].isoformat()
                    if trades[i - 1]["exit_timestamp"] else "",
                })
            elif cur_type == "loss" and cur_count > 0:
                streaks["losses"].append({
                    "count": cur_count, "total_pnl": round(cur_pnl, 2),
                    "start_idx": cur_start_idx, "end_idx": i - 1,
                    "start_ts": trades[cur_start_idx]["exit_timestamp"].isoformat()
                    if trades[cur_start_idx]["exit_timestamp"] else "",
                    "end_ts": trades[i - 1]["exit_timestamp"].isoformat()
                    if trades[i - 1]["exit_timestamp"] else "",
                })
            cur_type = new_type
            cur_count = 1
            cur_pnl = pnl
            cur_start_idx = i

    # Don't forget the last streak
    if cur_type == "win":
        streaks["wins"].append({
            "count": cur_count, "total_pnl": round(cur_pnl, 2),
            "start_idx": cur_start_idx, "end_idx": len(pnls) - 1,
        })
    elif cur_type == "loss":
        streaks["losses"].append({
            "count": cur_count, "total_pnl": round(cur_pnl, 2),
            "start_idx": cur_start_idx, "end_idx": len(pnls) - 1,
        })

    # Sort by streak length
    max_win_streak = max(streaks["wins"], key=lambda s: s["count"]) if streaks["wins"] else None
    max_loss_streak = max(streaks["losses"], key=lambda s: s["count"]) if streaks["losses"] else None

    results["max_consecutive_wins"] = max_win_streak
    results["max_consecutive_losses"] = max_loss_streak

    # ── Rolling 20-Trade Win Rate ──
    rolling_wr = []
    for i in range(19, len(trades)):
        window = trades[i - 19:i + 1]
        wins = sum(1 for t in window if t["total_pnl"] > 0)
        wr = wins / 20 * 100
        rolling_wr.append({
            "trade_index": i,
            "win_rate_20": round(wr, 1),
            "timestamp": trades[i]["exit_timestamp"].isoformat()
            if trades[i]["exit_timestamp"] else "",
        })

    if rolling_wr:
        results["rolling_20_wr"] = {
            "min_wr": min(r["win_rate_20"] for r in rolling_wr),
            "max_wr": max(r["win_rate_20"] for r in rolling_wr),
            "avg_wr": round(sum(r["win_rate_20"] for r in rolling_wr) / len(rolling_wr), 1),
            "min_trade_idx": min(rolling_wr, key=lambda r: r["win_rate_20"])["trade_index"],
            "max_trade_idx": max(rolling_wr, key=lambda r: r["win_rate_20"])["trade_index"],
            "data_points": len(rolling_wr),
        }
    else:
        results["rolling_20_wr"] = {}

    # ── Worst Single Day ──
    day_pnl = defaultdict(lambda: {"pnl": 0, "trades": 0, "trade_list": []})
    for t in trades:
        et = get_et_datetime(t["exit_timestamp"])
        if et:
            day_key = et.strftime("%Y-%m-%d")
            day_pnl[day_key]["pnl"] += t["total_pnl"]
            day_pnl[day_key]["trades"] += 1
            day_pnl[day_key]["trade_list"].append(t["trade_index"])

    days_sorted = sorted(day_pnl.items(), key=lambda x: x[1]["pnl"])
    results["worst_day"] = {
        "date": days_sorted[0][0] if days_sorted else "",
        "pnl": round(days_sorted[0][1]["pnl"], 2) if days_sorted else 0,
        "num_trades": days_sorted[0][1]["trades"] if days_sorted else 0,
    } if days_sorted else {}

    results["best_day"] = {
        "date": days_sorted[-1][0] if days_sorted else "",
        "pnl": round(days_sorted[-1][1]["pnl"], 2) if days_sorted else 0,
        "num_trades": days_sorted[-1][1]["trades"] if days_sorted else 0,
    } if days_sorted else {}

    # ── Worst Single Week ──
    week_pnl = defaultdict(lambda: {"pnl": 0, "trades": 0})
    for t in trades:
        et = get_et_datetime(t["exit_timestamp"])
        if et:
            week_key = et.strftime("%Y-W%U")
            week_pnl[week_key]["pnl"] += t["total_pnl"]
            week_pnl[week_key]["trades"] += 1

    weeks_sorted = sorted(week_pnl.items(), key=lambda x: x[1]["pnl"])
    results["worst_week"] = {
        "week": weeks_sorted[0][0] if weeks_sorted else "",
        "pnl": round(weeks_sorted[0][1]["pnl"], 2) if weeks_sorted else 0,
        "num_trades": weeks_sorted[0][1]["trades"] if weeks_sorted else 0,
    } if weeks_sorted else {}

    # ── Drawdown Recovery ──
    # Find max DD peak→trough→recovery
    equity = STARTING_EQUITY
    peak = STARTING_EQUITY
    max_dd_pct = 0
    dd_start_idx = 0
    dd_trough_idx = 0
    recovery_idx = None

    in_drawdown = False
    dd_peak_idx = 0

    for i, t in enumerate(trades):
        equity += t["total_pnl"]
        if equity >= peak:
            if in_drawdown:
                recovery_idx = i
                in_drawdown = False
            peak = equity
            dd_peak_idx = i
        else:
            dd = (peak - equity) / peak * 100
            if dd > max_dd_pct:
                max_dd_pct = dd
                dd_start_idx = dd_peak_idx
                dd_trough_idx = i
                in_drawdown = True
                recovery_idx = None

    # Check if recovered after max DD
    if in_drawdown:
        eq2 = STARTING_EQUITY
        for i, t in enumerate(trades):
            eq2 += t["total_pnl"]
            if i > dd_trough_idx and eq2 >= peak:
                recovery_idx = i
                break

    results["drawdown_recovery"] = {
        "dd_start_trade": dd_start_idx,
        "dd_trough_trade": dd_trough_idx,
        "recovery_trade": recovery_idx,
        "trades_to_recover": (recovery_idx - dd_trough_idx) if recovery_idx else None,
        "recovered": recovery_idx is not None,
    }

    # ── Loss Clustering Analysis ──
    loss_trades = [t for t in trades if t["total_pnl"] < 0]
    loss_by_session = defaultdict(int)
    loss_by_direction = defaultdict(int)
    loss_by_regime = defaultdict(int)

    for t in loss_trades:
        h = get_et_hour(t["entry_timestamp"])
        if h is not None:
            loss_by_session[get_session(h)] += 1
        loss_by_direction[t["direction"]] += 1
        loss_by_regime[t["regime"]] += 1

    results["loss_clustering"] = {
        "by_session": dict(loss_by_session),
        "by_direction": dict(loss_by_direction),
        "by_regime": dict(loss_by_regime),
    }

    return results


# =====================================================================
#  SECTION 5: C1 vs C2 PERFORMANCE SPLIT
# =====================================================================

def analyze_c1_c2(trades: List[Dict]) -> Dict:
    """C1 vs C2 performance, runner contribution analysis."""
    results = {}

    c1_pnls = [t["c1_pnl"] for t in trades]
    c2_pnls = [t["c2_pnl"] for t in trades]

    # C1 stats
    c1_winners = [p for p in c1_pnls if p > 0]
    c1_losers = [p for p in c1_pnls if p < 0]
    c1_total = sum(c1_pnls)

    results["c1"] = {
        "total_pnl": round(c1_total, 2),
        "win_rate": round(len(c1_winners) / len(trades) * 100, 1) if trades else 0,
        "avg_pnl": round(c1_total / len(trades), 2) if trades else 0,
        "avg_winner": round(sum(c1_winners) / len(c1_winners), 2) if c1_winners else 0,
        "avg_loser": round(sum(c1_losers) / len(c1_losers), 2) if c1_losers else 0,
        "total_contribution_pct": round(c1_total / sum(t["total_pnl"] for t in trades) * 100, 1)
        if sum(t["total_pnl"] for t in trades) != 0 else 0,
    }

    # C2 stats
    c2_winners = [p for p in c2_pnls if p > 0]
    c2_losers = [p for p in c2_pnls if p < 0]
    c2_total = sum(c2_pnls)
    c2_breakeven = [t for t in trades if t["c2_reason"] == "breakeven"]
    c2_trailing = [t for t in trades if t["c2_reason"] == "trailing"]
    c2_stop = [t for t in trades if t["c2_reason"] == "stop"]
    c2_max_target = [t for t in trades if t["c2_reason"] == "max_target"]
    c2_emergency = [t for t in trades if t["c2_reason"] == "emergency"]

    results["c2"] = {
        "total_pnl": round(c2_total, 2),
        "win_rate": round(len(c2_winners) / len(trades) * 100, 1) if trades else 0,
        "avg_pnl": round(c2_total / len(trades), 2) if trades else 0,
        "avg_winner": round(sum(c2_winners) / len(c2_winners), 2) if c2_winners else 0,
        "avg_loser": round(sum(c2_losers) / len(c2_losers), 2) if c2_losers else 0,
        "total_contribution_pct": round(c2_total / sum(t["total_pnl"] for t in trades) * 100, 1)
        if sum(t["total_pnl"] for t in trades) != 0 else 0,
    }

    # C2 runner analysis
    results["c2_exit_breakdown"] = {
        "breakeven": {"count": len(c2_breakeven),
                      "pct": round(len(c2_breakeven) / len(trades) * 100, 1),
                      "avg_pnl": round(sum(t["c2_pnl"] for t in c2_breakeven) / len(c2_breakeven), 2) if c2_breakeven else 0},
        "trailing": {"count": len(c2_trailing),
                     "pct": round(len(c2_trailing) / len(trades) * 100, 1),
                     "avg_pnl": round(sum(t["c2_pnl"] for t in c2_trailing) / len(c2_trailing), 2) if c2_trailing else 0},
        "stop": {"count": len(c2_stop),
                 "pct": round(len(c2_stop) / len(trades) * 100, 1),
                 "avg_pnl": round(sum(t["c2_pnl"] for t in c2_stop) / len(c2_stop), 2) if c2_stop else 0},
        "max_target": {"count": len(c2_max_target),
                       "pct": round(len(c2_max_target) / len(trades) * 100, 1),
                       "avg_pnl": round(sum(t["c2_pnl"] for t in c2_max_target) / len(c2_max_target), 2) if c2_max_target else 0},
        "emergency": {"count": len(c2_emergency),
                      "pct": round(len(c2_emergency) / len(trades) * 100, 1),
                      "avg_pnl": round(sum(t["c2_pnl"] for t in c2_emergency) / len(c2_emergency), 2) if c2_emergency else 0},
    }

    # C2 runner R-multiple distribution (estimate R from stop_distance)
    c2_r_dist = {"gt_1R": 0, "gt_2R": 0, "gt_3R": 0, "gt_5R": 0}
    for t in trades:
        if t["stop_distance"] > 0 and t["c2_pnl"] > 0:
            r_achieved = t["c2_pnl"] / (t["stop_distance"] * POINT_VALUE)
            if r_achieved > 1:
                c2_r_dist["gt_1R"] += 1
            if r_achieved > 2:
                c2_r_dist["gt_2R"] += 1
            if r_achieved > 3:
                c2_r_dist["gt_3R"] += 1
            if r_achieved > 5:
                c2_r_dist["gt_5R"] += 1

    results["c2_r_distribution"] = {
        k: {"count": v, "pct": round(v / len(trades) * 100, 1)}
        for k, v in c2_r_dist.items()
    }

    # C1 exit breakdown
    from collections import Counter
    c1_reasons = Counter(t["c1_reason"] for t in trades)
    results["c1_exit_breakdown"] = {}
    for reason, count in c1_reasons.most_common():
        group = [t for t in trades if t["c1_reason"] == reason]
        results["c1_exit_breakdown"][reason] = {
            "count": count,
            "pct": round(count / len(trades) * 100, 1),
            "avg_c1_pnl": round(sum(t["c1_pnl"] for t in group) / len(group), 2),
        }

    return results


# =====================================================================
#  SECTION 6: EDGE CONCENTRATION ANALYSIS
# =====================================================================

def analyze_edge_concentration(trades: List[Dict]) -> Dict:
    """Analyze how concentrated the edge is in top trades."""
    results = {}

    pnls = sorted([t["total_pnl"] for t in trades], reverse=True)
    total_pnl = sum(pnls)
    n = len(pnls)

    if total_pnl <= 0 or n == 0:
        return {"warning": "System not profitable, concentration analysis not meaningful"}

    # Top N% contribution
    for pct in [5, 10, 20, 30, 50]:
        top_n = max(1, int(n * pct / 100))
        top_pnl = sum(pnls[:top_n])
        results[f"top_{pct}pct"] = {
            "trade_count": top_n,
            "total_pnl": round(top_pnl, 2),
            "pct_of_total_profit": round(top_pnl / total_pnl * 100, 1) if total_pnl > 0 else 0,
        }

    # Remove top N trades and check profitability
    for remove_count in [5, 10, 20, 30]:
        remaining = pnls[remove_count:]
        remaining_pnl = sum(remaining)
        remaining_winners = [p for p in remaining if p > 0]
        remaining_losers = [p for p in remaining if p < 0]
        gross_wins = sum(remaining_winners)
        gross_losses = abs(sum(remaining_losers))
        pf = gross_wins / gross_losses if gross_losses > 0 else 999.99

        results[f"without_top_{remove_count}"] = {
            "remaining_trades": len(remaining),
            "remaining_pnl": round(remaining_pnl, 2),
            "still_profitable": remaining_pnl > 0,
            "win_rate": round(len(remaining_winners) / len(remaining) * 100, 1) if remaining else 0,
            "profit_factor": round(pf, 2),
        }

    # Bottom analysis - worst trades
    for remove_count in [5, 10, 20]:
        remaining = pnls[:n - remove_count]
        remaining_pnl = sum(remaining)
        results[f"without_bottom_{remove_count}"] = {
            "remaining_trades": len(remaining),
            "remaining_pnl": round(remaining_pnl, 2),
            "improvement": round(remaining_pnl - total_pnl, 2),
        }

    # Edge type classification
    top_10_pct = results.get("top_10pct", {}).get("pct_of_total_profit", 0)
    if top_10_pct > 100:
        edge_type = "FAT_TAIL_DEPENDENT"
    elif top_10_pct > 70:
        edge_type = "CONCENTRATED"
    elif top_10_pct > 40:
        edge_type = "MODERATELY_BROAD"
    else:
        edge_type = "BROADLY_DISTRIBUTED"

    results["edge_classification"] = edge_type

    return results


# =====================================================================
#  SECTION 7: OPTIMAL FILTER IDENTIFICATION
# =====================================================================

def analyze_filters(trades: List[Dict]) -> Dict:
    """Identify conditions with negative expectancy and compute filter impact."""
    results = {}
    total_stats = compute_stats(trades)
    total_pnl = total_stats["total_pnl"]
    total_dd = compute_max_drawdown(trades)

    # Define candidate filters
    filters = []

    # ── Session filters ──
    for session in ["overnight", "morning", "lunch", "afternoon"]:
        group = [t for t in trades
                 if get_et_hour(t["entry_timestamp"]) is not None
                 and get_session(get_et_hour(t["entry_timestamp"])) == session]
        if group:
            stats = compute_stats(group)
            if stats["avg_pnl"] < 0:  # Negative expectancy
                filters.append({
                    "filter": f"No trades during {session} session",
                    "condition": f"session={session}",
                    "removed_trades": group,
                    "removed_stats": stats,
                })

    # ── HC score filters ──
    for threshold in [0.78, 0.80, 0.82, 0.85]:
        group = [t for t in trades if t["signal_score"] < threshold and t["signal_score"] >= 0.75]
        if group:
            stats = compute_stats(group)
            if stats["avg_pnl"] < 0:
                filters.append({
                    "filter": f"No trades with HC < {threshold}",
                    "condition": f"hc_score<{threshold}",
                    "removed_trades": group,
                    "removed_stats": stats,
                })

    # ── Direction + Day filters ──
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    for direction in ["long", "short"]:
        for day_num, day_name in enumerate(day_names):
            group = [t for t in trades
                     if t["direction"] == direction
                     and get_et_datetime(t["entry_timestamp"]) is not None
                     and get_et_datetime(t["entry_timestamp"]).weekday() == day_num]
            if len(group) >= 5:
                stats = compute_stats(group)
                if stats["avg_pnl"] < 0:
                    filters.append({
                        "filter": f"No {direction}s on {day_name}s",
                        "condition": f"direction={direction},day={day_name}",
                        "removed_trades": group,
                        "removed_stats": stats,
                    })

    # ── Stop distance filters ──
    for max_stop in [5, 8, 25]:
        if max_stop <= 5:
            group = [t for t in trades if t["stop_distance"] < max_stop and t["stop_distance"] > 0]
            label = f"No trades with stop < {max_stop}pt"
        else:
            group = [t for t in trades if t["stop_distance"] > max_stop]
            label = f"No trades with stop > {max_stop}pt"
        if len(group) >= 3:
            stats = compute_stats(group)
            if stats["avg_pnl"] < 0:
                filters.append({
                    "filter": label,
                    "condition": f"stop_distance_filter_{max_stop}",
                    "removed_trades": group,
                    "removed_stats": stats,
                })

    # ── Regime filters ──
    regime_groups = defaultdict(list)
    for t in trades:
        regime_groups[t["regime"]].append(t)

    for regime, group in regime_groups.items():
        stats = compute_stats(group)
        if stats["avg_pnl"] < 0 and len(group) >= 5:
            filters.append({
                "filter": f"No trades in {regime} regime",
                "condition": f"regime={regime}",
                "removed_trades": group,
                "removed_stats": stats,
            })

    # ── Signal source filters ──
    source_groups = defaultdict(list)
    for t in trades:
        source_groups[t["signal_source"]].append(t)

    for source, group in source_groups.items():
        stats = compute_stats(group)
        if stats["avg_pnl"] < 0 and len(group) >= 3:
            filters.append({
                "filter": f"No {source} trades",
                "condition": f"source={source}",
                "removed_trades": group,
                "removed_stats": stats,
            })

    # ── HTF bias filters ──
    for bias in ["bearish", "bullish", "neutral", "n/a"]:
        for direction in ["long", "short"]:
            group = [t for t in trades
                     if t["htf_bias"] == bias and t["direction"] == direction]
            if len(group) >= 5:
                stats = compute_stats(group)
                if stats["avg_pnl"] < 0:
                    filters.append({
                        "filter": f"No {direction}s when HTF={bias}",
                        "condition": f"htf={bias},dir={direction}",
                        "removed_trades": group,
                        "removed_stats": stats,
                    })

    # ── Compute filter impact ──
    filter_impacts = []
    for f in filters:
        removed_ids = set(t["trade_index"] for t in f["removed_trades"])
        remaining = [t for t in trades if t["trade_index"] not in removed_ids]

        if not remaining:
            continue

        new_stats = compute_stats(remaining)
        new_dd = compute_max_drawdown(remaining)

        pnl_improvement = new_stats["total_pnl"] - total_pnl
        pnl_per_removed = (
            pnl_improvement / len(f["removed_trades"])
            if f["removed_trades"] else 0
        )

        filter_impacts.append({
            "filter": f["filter"],
            "trades_removed": len(f["removed_trades"]),
            "removed_pnl": f["removed_stats"]["total_pnl"],
            "removed_wr": f["removed_stats"]["win_rate"],
            "removed_avg_pnl": f["removed_stats"]["avg_pnl"],
            "new_total_trades": new_stats["count"],
            "new_win_rate": new_stats["win_rate"],
            "new_profit_factor": new_stats["profit_factor"],
            "new_total_pnl": new_stats["total_pnl"],
            "new_max_dd": new_dd["max_dd_pct"],
            "pnl_improvement": round(pnl_improvement, 2),
            "pnl_per_trade_removed": round(pnl_per_removed, 2),
        })

    # Sort by PnL improvement per trade removed (efficiency)
    filter_impacts.sort(key=lambda x: x["pnl_per_trade_removed"], reverse=True)

    results["filter_impacts"] = filter_impacts
    results["baseline"] = {
        "total_trades": total_stats["count"],
        "win_rate": total_stats["win_rate"],
        "profit_factor": total_stats["profit_factor"],
        "total_pnl": total_stats["total_pnl"],
        "max_dd_pct": total_dd["max_dd_pct"],
    }

    return results


# =====================================================================
#  OUTPUT FORMATTING
# =====================================================================

def generate_markdown(all_results: Dict) -> str:
    """Generate human-readable markdown summary."""
    lines = []
    lines.append("# Trade Profile Analysis — 607-Trade Structural Stop Dataset")
    lines.append("")
    lines.append(f"**Generated:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"**Dataset:** Period 4 (Sep 2023 - Nov 2023) — Structural Stop Placement")
    lines.append(f"**Total Trades:** {all_results['summary']['total_trades']}")
    lines.append(f"**Net PnL:** ${all_results['summary']['total_pnl']:,.2f}")
    lines.append(f"**Win Rate:** {all_results['summary']['win_rate']}%")
    lines.append(f"**Profit Factor:** {all_results['summary']['profit_factor']}")
    lines.append("")

    # ── Section 1: Trade Outcome Profile ──
    lines.append("## 1. Trade Outcome Profile")
    lines.append("")

    lines.append("### By Direction")
    lines.append("```")
    headers = ["Direction", "Count", "WR%", "PF", "Avg PnL", "Total PnL"]
    rows = []
    for d, s in all_results["trade_outcomes"]["by_direction"].items():
        rows.append([d, s["count"], f"{s['win_rate']}%", s["profit_factor"],
                     f"${s['avg_pnl']:.2f}", f"${s['total_pnl']:.2f}"])
    lines.append(format_table(headers, rows))
    lines.append("```")
    lines.append("")

    lines.append("### By Signal Source")
    lines.append("```")
    rows = []
    for src, s in all_results["trade_outcomes"]["by_signal_source"].items():
        rows.append([src, s["count"], f"{s['win_rate']}%", s["profit_factor"],
                     f"${s['avg_pnl']:.2f}", f"${s['total_pnl']:.2f}"])
    lines.append(format_table(headers, rows))
    lines.append("```")
    lines.append("")

    lines.append("### By HC Score Bucket")
    lines.append("```")
    rows = []
    for bucket, s in all_results["trade_outcomes"]["by_hc_score"].items():
        rows.append([bucket, s["count"], f"{s['win_rate']}%", s["profit_factor"],
                     f"${s['avg_pnl']:.2f}", f"${s['total_pnl']:.2f}"])
    lines.append(format_table(headers, rows))
    lines.append("```")
    lines.append("")

    lines.append("### By Regime")
    lines.append("```")
    rows = []
    for regime, s in all_results["trade_outcomes"]["by_regime"].items():
        rows.append([regime, s["count"], f"{s['win_rate']}%", s["profit_factor"],
                     f"${s['avg_pnl']:.2f}", f"${s['total_pnl']:.2f}"])
    lines.append(format_table(headers, rows))
    lines.append("```")
    lines.append("")

    lines.append("### By HTF Bias")
    lines.append("```")
    rows = []
    for bias, s in all_results["trade_outcomes"]["by_htf_bias"].items():
        rows.append([bias, s["count"], f"{s['win_rate']}%", s["profit_factor"],
                     f"${s['avg_pnl']:.2f}", f"${s['total_pnl']:.2f}"])
    lines.append(format_table(headers, rows))
    lines.append("```")
    lines.append("")

    # ── Section 2: Time-Based Analysis ──
    lines.append("## 2. Time-Based Analysis")
    lines.append("")

    lines.append("### By Hour of Day (ET)")
    lines.append("```")
    headers_t = ["Hour", "Count", "WR%", "PF", "Avg PnL", "Total PnL"]
    rows = []
    for hour, s in sorted(all_results["time_analysis"]["by_hour"].items(), key=lambda x: int(x[0])):
        rows.append([f"{int(hour):02d}:00", s["count"], f"{s['win_rate']}%", s["profit_factor"],
                     f"${s['avg_pnl']:.2f}", f"${s['total_pnl']:.2f}"])
    lines.append(format_table(headers_t, rows))
    lines.append("```")
    lines.append("")

    lines.append("### By Day of Week")
    lines.append("```")
    rows = []
    for day, s in all_results["time_analysis"]["by_day_of_week"].items():
        rows.append([day, s["count"], f"{s['win_rate']}%", s["profit_factor"],
                     f"${s['avg_pnl']:.2f}", f"${s['total_pnl']:.2f}"])
    lines.append(format_table(headers_t[:1] + ["Count", "WR%", "PF", "Avg PnL", "Total PnL"], rows))
    lines.append("```")
    lines.append("")

    lines.append("### By Session")
    lines.append("```")
    rows = []
    for session, s in all_results["time_analysis"]["by_session"].items():
        expectancy_flag = " ** NEG **" if s["avg_pnl"] < 0 else ""
        rows.append([session, s["count"], f"{s['win_rate']}%", s["profit_factor"],
                     f"${s['avg_pnl']:.2f}{expectancy_flag}", f"${s['total_pnl']:.2f}"])
    lines.append(format_table(["Session", "Count", "WR%", "PF", "Avg PnL", "Total PnL"], rows))
    lines.append("```")
    lines.append("")

    # ── Section 3: Volatility Regime ──
    lines.append("## 3. Volatility Regime Analysis (by Stop Distance)")
    lines.append("")
    lines.append("```")
    headers_v = ["Stop Bucket", "Count", "WR%", "PF", "Avg PnL", "Total PnL", "Avg Stop"]
    rows = []
    for bucket, s in all_results["volatility_analysis"]["by_stop_distance"].items():
        rows.append([bucket, s["count"], f"{s['win_rate']}%", s["profit_factor"],
                     f"${s['avg_pnl']:.2f}", f"${s['total_pnl']:.2f}",
                     f"{s.get('avg_stop_distance', 0):.1f}pt"])
    lines.append(format_table(headers_v, rows))
    lines.append("```")
    lines.append("")

    # ── Section 4: Drawdown Deep Dive ──
    lines.append("## 4. Drawdown Deep Dive")
    lines.append("")
    dd = all_results["drawdown_analysis"]
    md = dd["max_drawdown"]
    lines.append(f"- **Starting Equity:** ${STARTING_EQUITY:,.2f}")
    lines.append(f"- **Final Equity:** ${md['final_equity']:,.2f}")
    lines.append(f"- **Max Drawdown:** ${md['max_dd_dollars']:,.2f} ({md['max_dd_pct']:.2f}%)")
    lines.append(f"- **Peak Timestamp:** {md.get('peak_timestamp', 'N/A')}")
    lines.append(f"- **Trough Timestamp:** {md.get('trough_timestamp', 'N/A')}")
    lines.append("")

    if dd.get("max_consecutive_wins"):
        mcw = dd["max_consecutive_wins"]
        lines.append(f"- **Max Consecutive Wins:** {mcw['count']} trades (${mcw['total_pnl']:+.2f})")
    if dd.get("max_consecutive_losses"):
        mcl = dd["max_consecutive_losses"]
        lines.append(f"- **Max Consecutive Losses:** {mcl['count']} trades (${mcl['total_pnl']:+.2f})")

    if dd.get("rolling_20_wr"):
        rw = dd["rolling_20_wr"]
        lines.append(f"- **Rolling 20-Trade WR:** min={rw.get('min_wr', 0)}%, max={rw.get('max_wr', 0)}%, avg={rw.get('avg_wr', 0)}%")

    wd = dd.get("worst_day", {})
    if wd:
        lines.append(f"- **Worst Day:** {wd.get('date', 'N/A')} — {wd.get('num_trades', 0)} trades, ${wd.get('pnl', 0):+.2f}")
    ww = dd.get("worst_week", {})
    if ww:
        lines.append(f"- **Worst Week:** {ww.get('week', 'N/A')} — {ww.get('num_trades', 0)} trades, ${ww.get('pnl', 0):+.2f}")

    dr = dd.get("drawdown_recovery", {})
    if dr.get("recovered"):
        lines.append(f"- **Drawdown Recovery:** {dr['trades_to_recover']} trades from trough to new high")
    else:
        lines.append("- **Drawdown Recovery:** Did not fully recover within dataset")
    lines.append("")

    # ── Section 5: C1 vs C2 ──
    lines.append("## 5. C1 vs C2 Performance Split")
    lines.append("")
    c1c2 = all_results["c1_c2_analysis"]
    c1 = c1c2["c1"]
    c2 = c1c2["c2"]

    lines.append("```")
    headers_c = ["Metric", "C1 (Trail)", "C2 (Runner)"]
    rows = [
        ["Total PnL", f"${c1['total_pnl']:,.2f}", f"${c2['total_pnl']:,.2f}"],
        ["Win Rate", f"{c1['win_rate']}%", f"{c2['win_rate']}%"],
        ["Avg PnL/Trade", f"${c1['avg_pnl']:.2f}", f"${c2['avg_pnl']:.2f}"],
        ["Avg Winner", f"${c1['avg_winner']:.2f}", f"${c2['avg_winner']:.2f}"],
        ["Avg Loser", f"${c1['avg_loser']:.2f}", f"${c2['avg_loser']:.2f}"],
        ["% of Total Profit", f"{c1['total_contribution_pct']}%", f"{c2['total_contribution_pct']}%"],
    ]
    lines.append(format_table(headers_c, rows, ["<", ">", ">"]))
    lines.append("```")
    lines.append("")

    lines.append("### C2 Exit Breakdown")
    lines.append("```")
    headers_ce = ["Exit Reason", "Count", "%", "Avg C2 PnL"]
    rows = []
    for reason, data in c1c2["c2_exit_breakdown"].items():
        rows.append([reason, data["count"], f"{data['pct']}%", f"${data['avg_pnl']:.2f}"])
    lines.append(format_table(headers_ce, rows))
    lines.append("```")
    lines.append("")

    lines.append("### C2 Runner R-Multiple Distribution")
    lines.append("```")
    headers_r = ["R-Multiple", "Count", "% of All Trades"]
    rows = []
    for r_level, data in c1c2["c2_r_distribution"].items():
        rows.append([r_level.replace("gt_", ">"), data["count"], f"{data['pct']}%"])
    lines.append(format_table(headers_r, rows))
    lines.append("```")
    lines.append("")

    # ── Section 6: Edge Concentration ──
    lines.append("## 6. Edge Concentration Analysis")
    lines.append("")
    ec = all_results["edge_concentration"]
    lines.append(f"**Edge Classification:** {ec.get('edge_classification', 'N/A')}")
    lines.append("")

    lines.append("### Top-N% Profit Contribution")
    lines.append("```")
    headers_ec = ["Segment", "Trades", "PnL", "% of Total Profit"]
    rows = []
    for key in ["top_5pct", "top_10pct", "top_20pct", "top_30pct", "top_50pct"]:
        if key in ec:
            d = ec[key]
            rows.append([key.replace("_", " ").title(), d["trade_count"],
                        f"${d['total_pnl']:,.2f}", f"{d['pct_of_total_profit']}%"])
    lines.append(format_table(headers_ec, rows))
    lines.append("```")
    lines.append("")

    lines.append("### Robustness: Remove Top-N Trades")
    lines.append("```")
    headers_rob = ["Scenario", "Remaining", "PnL", "Profitable?", "WR%", "PF"]
    rows = []
    for key in ["without_top_5", "without_top_10", "without_top_20", "without_top_30"]:
        if key in ec:
            d = ec[key]
            rows.append([key.replace("_", " ").title(), d["remaining_trades"],
                        f"${d['remaining_pnl']:,.2f}",
                        "YES" if d["still_profitable"] else "NO",
                        f"{d['win_rate']}%", d["profit_factor"]])
    lines.append(format_table(headers_rob, rows))
    lines.append("```")
    lines.append("")

    # ── Section 7: Filter Impact ──
    lines.append("## 7. Optimal Filter Identification")
    lines.append("")

    fi = all_results["filter_analysis"]
    baseline = fi.get("baseline", {})
    lines.append(f"**Baseline:** {baseline.get('total_trades', 0)} trades, "
                 f"WR {baseline.get('win_rate', 0)}%, "
                 f"PF {baseline.get('profit_factor', 0)}, "
                 f"PnL ${baseline.get('total_pnl', 0):,.2f}, "
                 f"MaxDD {baseline.get('max_dd_pct', 0):.2f}%")
    lines.append("")

    impacts = fi.get("filter_impacts", [])
    if impacts:
        lines.append("### FILTER IMPACT TABLE (ranked by efficiency)")
        lines.append("```")
        headers_fi = ["Filter", "Removed", "New WR", "New PF", "New PnL", "New MaxDD", "$/Trade Removed"]
        rows = []
        for imp in impacts:
            rows.append([
                imp["filter"],
                str(imp["trades_removed"]),
                f"{imp['new_win_rate']}%",
                str(imp["new_profit_factor"]),
                f"${imp['new_total_pnl']:,.2f}",
                f"{imp['new_max_dd']:.2f}%",
                f"${imp['pnl_per_trade_removed']:+.2f}",
            ])
        lines.append(format_table(headers_fi, rows, ["<", ">", ">", ">", ">", ">", ">"]))
        lines.append("```")
    else:
        lines.append("No negative-expectancy filters identified.")
    lines.append("")

    # ── Summary ──
    lines.append("## Key Findings")
    lines.append("")

    # Auto-generate key findings
    outcomes = all_results["trade_outcomes"]

    # Best direction
    best_dir = max(outcomes["by_direction"].items(), key=lambda x: x[1]["profit_factor"])
    lines.append(f"1. **Best Direction:** {best_dir[0]} (PF={best_dir[1]['profit_factor']}, "
                 f"WR={best_dir[1]['win_rate']}%, n={best_dir[1]['count']})")

    # Best signal source
    best_src = max(outcomes["by_signal_source"].items(), key=lambda x: x[1]["profit_factor"])
    lines.append(f"2. **Best Signal Source:** {best_src[0]} (PF={best_src[1]['profit_factor']}, "
                 f"WR={best_src[1]['win_rate']}%, n={best_src[1]['count']})")

    # Best HC bucket
    best_hc = max(outcomes["by_hc_score"].items(),
                  key=lambda x: x[1]["profit_factor"] if x[1]["count"] > 0 else 0)
    lines.append(f"3. **Best HC Bucket:** {best_hc[0]} (PF={best_hc[1]['profit_factor']}, "
                 f"WR={best_hc[1]['win_rate']}%, n={best_hc[1]['count']})")

    # Best session
    if all_results["time_analysis"]["by_session"]:
        best_sess = max(all_results["time_analysis"]["by_session"].items(),
                       key=lambda x: x[1]["profit_factor"])
        lines.append(f"4. **Best Session:** {best_sess[0]} (PF={best_sess[1]['profit_factor']}, "
                     f"WR={best_sess[1]['win_rate']}%, n={best_sess[1]['count']})")

    # C2 contribution
    lines.append(f"5. **C2 Runner Contribution:** {c2['total_contribution_pct']}% of total profit "
                 f"(${c2['total_pnl']:,.2f})")

    # Edge concentration
    lines.append(f"6. **Edge Type:** {ec.get('edge_classification', 'N/A')}")
    if "top_10pct" in ec:
        lines.append(f"   Top 10% of trades produce {ec['top_10pct']['pct_of_total_profit']}% of profit")

    # Filter impact
    if impacts:
        best_filter = impacts[0]
        lines.append(f"7. **Best Filter:** \"{best_filter['filter']}\" — "
                     f"removes {best_filter['trades_removed']} trades, "
                     f"improves PnL by ${best_filter['pnl_improvement']:+.2f}")

    lines.append("")
    lines.append("---")
    lines.append(f"*Analysis generated from structural stop backtest results (period_4).*")

    return "\n".join(lines)


# =====================================================================
#  MAIN
# =====================================================================

def main():
    print("=" * 74)
    print("  TRADE PROFILE ANALYSIS — 607-Trade Structural Stop Dataset")
    print("=" * 74)

    # ── Load Data ──
    print("\n  Loading trade data...")
    trades = load_trades()
    print(f"  Loaded {len(trades)} trades")
    print(f"  Date range: {trades[0]['entry_timestamp']} to {trades[-1]['exit_timestamp']}")

    total_pnl = sum(t["total_pnl"] for t in trades)
    winners = [t for t in trades if t["total_pnl"] > 0]
    losers = [t for t in trades if t["total_pnl"] <= 0]
    gross_wins = sum(t["total_pnl"] for t in winners)
    gross_losses = abs(sum(t["total_pnl"] for t in losers))
    pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    print(f"  Total PnL: ${total_pnl:,.2f}")
    print(f"  Win Rate: {len(winners)/len(trades)*100:.1f}%")
    print(f"  Profit Factor: {pf:.2f}")

    # ── Run All Analyses ──
    print("\n  [1/7] Trade Outcome Profile...")
    trade_outcomes = analyze_trade_outcomes(trades)

    print("  [2/7] Time-Based Analysis...")
    time_analysis = analyze_time_based(trades)

    print("  [3/7] Volatility Regime Analysis...")
    volatility_analysis = analyze_volatility(trades)

    print("  [4/7] Drawdown Deep Dive...")
    drawdown_analysis = analyze_drawdown(trades)

    print("  [5/7] C1 vs C2 Performance Split...")
    c1_c2_analysis = analyze_c1_c2(trades)

    print("  [6/7] Edge Concentration Analysis...")
    edge_concentration = analyze_edge_concentration(trades)

    print("  [7/7] Optimal Filter Identification...")
    filter_analysis = analyze_filters(trades)

    # ── Assemble Results ──
    all_results = {
        "generated": datetime.utcnow().isoformat(),
        "dataset": "period_4_structural_stop (Sep-Nov 2023)",
        "summary": {
            "total_trades": len(trades),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": round(len(winners) / len(trades) * 100, 1),
            "profit_factor": round(pf, 2),
            "total_pnl": round(total_pnl, 2),
            "expectancy": round(total_pnl / len(trades), 2),
            "c1_total_pnl": round(sum(t["c1_pnl"] for t in trades), 2),
            "c2_total_pnl": round(sum(t["c2_pnl"] for t in trades), 2),
        },
        "trade_outcomes": trade_outcomes,
        "time_analysis": time_analysis,
        "volatility_analysis": volatility_analysis,
        "drawdown_analysis": drawdown_analysis,
        "c1_c2_analysis": c1_c2_analysis,
        "edge_concentration": edge_concentration,
        "filter_analysis": filter_analysis,
    }

    # ── Save JSON ──
    print(f"\n  Saving JSON to {OUTPUT_JSON}...")
    os.makedirs(str(LOGS_DIR), exist_ok=True)
    with open(str(OUTPUT_JSON), "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"  Saved: {OUTPUT_JSON}")

    # ── Save Markdown ──
    print(f"  Saving markdown to {OUTPUT_MD}...")
    os.makedirs(str(OUTPUT_MD.parent), exist_ok=True)
    md_content = generate_markdown(all_results)
    with open(str(OUTPUT_MD), "w") as f:
        f.write(md_content)
    print(f"  Saved: {OUTPUT_MD}")

    # ── Print Filter Impact Table ──
    print("\n" + "=" * 74)
    print("  FILTER IMPACT TABLE (ranked by efficiency)")
    print("=" * 74)

    fi = filter_analysis
    baseline = fi.get("baseline", {})
    print(f"\n  BASELINE: {baseline.get('total_trades', 0)} trades, "
          f"WR {baseline.get('win_rate', 0)}%, "
          f"PF {baseline.get('profit_factor', 0)}, "
          f"PnL ${baseline.get('total_pnl', 0):,.2f}, "
          f"MaxDD {baseline.get('max_dd_pct', 0):.2f}%\n")

    impacts = fi.get("filter_impacts", [])
    if impacts:
        headers_fi = ["Filter", "Removed", "New WR", "New PF", "New PnL", "New MaxDD", "$/Removed"]
        rows = []
        for imp in impacts:
            rows.append([
                imp["filter"],
                str(imp["trades_removed"]),
                f"{imp['new_win_rate']}%",
                str(imp["new_profit_factor"]),
                f"${imp['new_total_pnl']:,.2f}",
                f"{imp['new_max_dd']:.2f}%",
                f"${imp['pnl_per_trade_removed']:+.2f}",
            ])
        print(format_table(headers_fi, rows, ["<", ">", ">", ">", ">", ">", ">"]))
    else:
        print("  No negative-expectancy filters identified — system edge is broadly distributed.")

    # ── Print Pass/Fail Checklist ──
    print("\n" + "=" * 74)
    print("  DELIVERABLES CHECKLIST")
    print("=" * 74)

    checks = [
        ("Trade outcome profile (direction, source, HC, regime)", True),
        ("Time-based analysis (hour, day, session)", True),
        ("Volatility regime analysis (stop distance buckets)", True),
        ("Drawdown deep dive (equity curve, streaks, worst day/week)", True),
        ("C1 vs C2 split analysis", True),
        ("Edge concentration analysis (top-N dependency)", True),
        ("Optimal filter identification table", True),
        (f"logs/trade_profile_analysis.json saved ({OUTPUT_JSON})",
         os.path.exists(str(OUTPUT_JSON))),
        (f"docs/trade_profile_summary.md saved ({OUTPUT_MD})",
         os.path.exists(str(OUTPUT_MD))),
    ]

    all_pass = True
    for label, passed in checks:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{status}] {label}")

    print(f"\n  {'ALL CHECKS PASSED' if all_pass else 'SOME CHECKS FAILED'}")
    print("=" * 74)

    return all_results


if __name__ == "__main__":
    main()
