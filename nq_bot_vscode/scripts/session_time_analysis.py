#!/usr/bin/env python3
"""
SESSION TIME ANALYSIS: Research Question R4
============================================
Analyzes MNQ futures backtest trades to answer:
"Should the trailing stop tighten approaching 4pm ET?
Do late-day trailing exits have lower avg PnL?"

Action Plan Item: R4 -- Session Timing Analysis
- Bucket C2 trades by EXIT TIME into trading session windows
- Bucket C2 trades by ENTRY TIME into trading session windows
- Analyze exit PnL by exit reason (trailing, breakeven, stop, max_target, time_stop)
- Compute profit factor, win rate, and avg MFE per time bucket
- Recommend whether to tighten trailing stop in close window (15:30-16:00 ET)
- Recommend whether to filter entries during underperforming sessions (e.g., lunch)

Data Source: logs/paper_trades.json (or --trades argument)
Output: logs/session_time_analysis.json

Session Windows (ET):
  - Pre-market:       06:00 - 09:30 ET
  - Open volatility:  09:30 - 10:30 ET
  - Morning:          10:30 - 12:00 ET
  - Lunch doldrums:   12:00 - 14:00 ET
  - Afternoon:        14:00 - 15:30 ET
  - Close:            15:30 - 16:00 ET
  - After hours:      16:00 - 18:00 ET
"""

import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# Constants
MNQ_POINT_VALUE = 2.0  # $0.50 per tick, 4 ticks per point = $2.00/point

# Session time boundaries (hour, minute) in ET
SESSION_WINDOWS = [
    ("Pre-market", 6, 0, 9, 30),
    ("Open volatility", 9, 30, 10, 30),
    ("Morning", 10, 30, 12, 0),
    ("Lunch doldrums", 12, 0, 14, 0),
    ("Afternoon", 14, 0, 15, 30),
    ("Close", 15, 30, 16, 0),
    ("After hours", 16, 0, 18, 0),
]

EXIT_REASONS = ["trailing", "breakeven", "stop", "max_target", "time_stop"]


def get_session_bucket(hour: int, minute: int) -> Optional[str]:
    """
    Determine which session window a given (hour, minute) falls into.
    Returns session name or None if outside all windows.
    """
    time_in_minutes = hour * 60 + minute
    for session_name, start_h, start_m, end_h, end_m in SESSION_WINDOWS:
        start_time = start_h * 60 + start_m
        end_time = end_h * 60 + end_m
        if start_time <= time_in_minutes < end_time:
            return session_name
    return None


def parse_timestamp(ts_str: str) -> Optional[datetime]:
    """
    Parse ISO 8601 timestamp string to datetime.
    Returns None if parsing fails.
    """
    try:
        # Handle formats like '2023-09-01T08:50:00+00:00'
        if ts_str.endswith('Z'):
            return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        return datetime.fromisoformat(ts_str)
    except (ValueError, AttributeError):
        return None


def convert_to_et(dt: datetime) -> datetime:
    """
    Convert datetime (assumed UTC if naive, or with timezone) to US/Eastern.
    """
    try:
        import pytz
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=pytz.UTC)
        et_tz = pytz.timezone('US/Eastern')
        return dt.astimezone(et_tz)
    except ImportError:
        # Fallback: assume 4 hour difference for EST (no DST handling)
        if dt.tzinfo:
            dt = dt.replace(tzinfo=None)
        return dt - timedelta(hours=4)


def load_trades(trades_path: str) -> List[Dict[str, Any]]:
    """Load trades from JSON file."""
    with open(trades_path) as f:
        return json.load(f)


def analyze_trades_by_exit_time(
    trades: List[Dict[str, Any]],
    timezone_name: str = "US/Eastern"
) -> Dict[str, Dict[str, Any]]:
    """
    Bucket all C2 trades by EXIT TIME and compute statistics.

    Returns dict mapping session name -> statistics dict.
    """
    results = defaultdict(lambda: {
        "exits": [],
        "pnls": [],
        "exit_reasons": defaultdict(list),
        "mfes": [],
    })

    for trade in trades:
        if trade.get("event") != "trade_closed":
            continue

        # Extract exit time
        ts_str = trade.get("timestamp")
        if not ts_str:
            continue
        dt = parse_timestamp(ts_str)
        if not dt:
            continue

        # Convert to ET
        dt_et = convert_to_et(dt)
        hour, minute = dt_et.hour, dt_et.minute

        # Bucket into session
        session = get_session_bucket(hour, minute)
        if not session:
            continue

        # Extract C2 exit reason and PnL
        c2_pnl = trade.get("c2_pnl", 0.0)
        c2_reason = trade.get("c2_reason", "unknown")

        # Collect data
        results[session]["exits"].append({
            "timestamp": ts_str,
            "time_et": f"{hour:02d}:{minute:02d}",
            "c2_pnl": c2_pnl,
            "c2_reason": c2_reason,
            "direction": trade.get("direction"),
            "daily_pnl": trade.get("daily_pnl"),
        })
        results[session]["pnls"].append(c2_pnl)
        results[session]["exit_reasons"][c2_reason].append(c2_pnl)

        # MFE if available (placeholder for now)
        if "mfe" in trade:
            results[session]["mfes"].append(trade["mfe"])

    # Compute aggregate statistics
    output = {}
    for session_name in [s[0] for s in SESSION_WINDOWS]:
        if session_name not in results:
            output[session_name] = {
                "exit_count": 0,
                "exit_breakdown": {},
                "avg_pnl": 0.0,
                "median_pnl": 0.0,
                "stdev_pnl": 0.0,
                "win_count": 0,
                "loss_count": 0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "avg_mfe": None,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
                "sample_exits": [],
            }
            continue

        data = results[session_name]
        pnls = data["pnls"]

        # Basic stats
        exit_count = len(pnls)
        avg_pnl = statistics.mean(pnls) if pnls else 0.0
        median_pnl = statistics.median(pnls) if pnls else 0.0
        stdev_pnl = statistics.stdev(pnls) if len(pnls) > 1 else 0.0

        # Win/loss
        win_count = sum(1 for p in pnls if p > 0)
        loss_count = sum(1 for p in pnls if p < 0)
        win_rate = win_count / exit_count if exit_count > 0 else 0.0

        # Profit factor
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # MFE
        avg_mfe = statistics.mean(data["mfes"]) if data["mfes"] else None

        # Exit breakdown by reason
        exit_breakdown = {}
        for reason in EXIT_REASONS:
            if reason in data["exit_reasons"]:
                reason_pnls = data["exit_reasons"][reason]
                exit_breakdown[reason] = {
                    "count": len(reason_pnls),
                    "avg_pnl": round(statistics.mean(reason_pnls), 2),
                    "median_pnl": round(statistics.median(reason_pnls), 2),
                    "win_count": sum(1 for p in reason_pnls if p > 0),
                    "loss_count": sum(1 for p in reason_pnls if p < 0),
                }

        output[session_name] = {
            "exit_count": exit_count,
            "exit_breakdown": exit_breakdown,
            "avg_pnl": round(avg_pnl, 2),
            "median_pnl": round(median_pnl, 2),
            "stdev_pnl": round(stdev_pnl, 2),
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else "inf",
            "avg_mfe": round(avg_mfe, 2) if avg_mfe else None,
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "sample_exits": data["exits"][:5],
        }

    return output


def analyze_trades_by_entry_time(
    trades: List[Dict[str, Any]],
    timezone_name: str = "US/Eastern"
) -> Dict[str, Dict[str, Any]]:
    """
    Bucket all trades by ENTRY TIME (inferred from trade timestamp as a proxy).

    Note: This is a simplification since we may not have explicit entry timestamps.
    We'll use the trade timestamp and estimate entry ~1-5 bars before exit.
    """
    results = defaultdict(lambda: {
        "trades": [],
        "pnls": [],
    })

    for trade in trades:
        if trade.get("event") != "trade_closed":
            continue

        # For now, use the exit timestamp and estimate entry was earlier in the day
        ts_str = trade.get("timestamp")
        if not ts_str:
            continue
        dt = parse_timestamp(ts_str)
        if not dt:
            continue

        dt_et = convert_to_et(dt)
        hour, minute = dt_et.hour, dt_et.minute

        # Assume entry was 10-60 minutes earlier (rough heuristic)
        # For more accuracy, would need explicit entry timestamps
        entry_hour = hour
        entry_minute = max(0, minute - 30)  # 30-minute proxy
        if entry_minute < 0:
            entry_hour -= 1
            entry_minute += 60

        session = get_session_bucket(entry_hour, entry_minute)
        if not session:
            continue

        # Use daily PnL as proxy for trade success
        daily_pnl = trade.get("daily_pnl", 0.0)
        results[session]["trades"].append({
            "timestamp": ts_str,
            "estimated_entry_time_et": f"{entry_hour:02d}:{entry_minute:02d}",
            "daily_pnl": daily_pnl,
            "direction": trade.get("direction"),
        })
        results[session]["pnls"].append(daily_pnl)

    # Compute statistics
    output = {}
    for session_name in [s[0] for s in SESSION_WINDOWS]:
        if session_name not in results:
            output[session_name] = {
                "entry_count": 0,
                "avg_pnl": 0.0,
                "median_pnl": 0.0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "gross_profit": 0.0,
                "gross_loss": 0.0,
            }
            continue

        data = results[session_name]
        pnls = data["pnls"]

        entry_count = len(pnls)
        avg_pnl = statistics.mean(pnls) if pnls else 0.0
        median_pnl = statistics.median(pnls) if pnls else 0.0

        win_count = sum(1 for p in pnls if p > 0)
        win_rate = win_count / entry_count if entry_count > 0 else 0.0

        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        output[session_name] = {
            "entry_count": entry_count,
            "avg_pnl": round(avg_pnl, 2),
            "median_pnl": round(median_pnl, 2),
            "win_count": win_count,
            "loss_count": entry_count - win_count,
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else "inf",
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "sample_trades": data["trades"][:5],
        }

    return output


def generate_recommendations(
    exit_time_analysis: Dict[str, Dict[str, Any]],
    entry_time_analysis: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Generate actionable recommendations based on analysis.

    Research Question R4:
    - Should trailing stop tighten approaching 4pm ET?
    - Do late-day trailing exits have lower avg PnL?
    """
    recommendations = {
        "r4_trailing_stop_timing": None,
        "r4_close_window_analysis": None,
        "r4_entry_timing_optimization": None,
        "diagnostics": {},
    }

    # Analyze close window (15:30-16:00 ET)
    close_analysis = exit_time_analysis.get("Close", {})
    overall_avg_pnl = statistics.mean([
        a["avg_pnl"] for a in exit_time_analysis.values()
        if a.get("exit_count", 0) > 0 and isinstance(a.get("avg_pnl"), (int, float))
    ])

    if close_analysis.get("exit_count", 0) > 5:
        close_avg = close_analysis.get("avg_pnl", 0.0)
        close_trailing = (
            close_analysis.get("exit_breakdown", {}).get("trailing", {}).get("avg_pnl", 0.0)
        )
        pct_diff = ((overall_avg_pnl - close_avg) / abs(overall_avg_pnl) * 100) if overall_avg_pnl != 0 else 0

        recommendations["diagnostics"]["close_window"] = {
            "exit_count": close_analysis["exit_count"],
            "avg_pnl": close_avg,
            "trailing_avg_pnl": close_trailing,
            "overall_avg_pnl": round(overall_avg_pnl, 2),
            "underperformance_pct": round(pct_diff, 2),
        }

        if close_avg < overall_avg_pnl * 0.8:  # Close window 20% worse than average
            recommendations["r4_trailing_stop_timing"] = {
                "recommendation": "TIGHTEN trailing stop in close window (15:30-16:00 ET)",
                "rationale": (
                    f"Close window exits average ${close_avg:.2f}, "
                    f"which is {pct_diff:.1f}% worse than overall average ${overall_avg_pnl:.2f}. "
                    f"Trailing exits in close window avg ${close_trailing:.2f}. "
                    "Tightening trail width in final 30 minutes could lock in profits faster."
                ),
                "action": "Consider using 1.5x ATR (vs 2.0x) for C2 trail in 15:30-16:00 window",
            }
        else:
            recommendations["r4_trailing_stop_timing"] = {
                "recommendation": "MAINTAIN current trailing stop width",
                "rationale": (
                    f"Close window exits (avg ${close_avg:.2f}) are performing "
                    f"within acceptable range vs overall average (${overall_avg_pnl:.2f}). "
                    "Current trail width appears adequate."
                ),
                "action": "No change to trailing stop configuration",
            }

    # Analyze entry timing
    lunch_analysis = entry_time_analysis.get("Lunch doldrums", {})
    morning_analysis = entry_time_analysis.get("Morning", {})

    if lunch_analysis.get("entry_count", 0) > 5 and morning_analysis.get("entry_count", 0) > 5:
        lunch_avg = lunch_analysis.get("avg_pnl", 0.0)
        morning_avg = morning_analysis.get("avg_pnl", 0.0)

        recommendations["diagnostics"]["entry_timing"] = {
            "lunch_avg_pnl": lunch_avg,
            "lunch_entry_count": lunch_analysis["entry_count"],
            "morning_avg_pnl": morning_avg,
            "morning_entry_count": morning_analysis["entry_count"],
        }

        if lunch_avg < morning_avg * 0.75:  # Lunch 25% worse
            recommendations["r4_entry_timing_optimization"] = {
                "recommendation": "FILTER OUT entries during lunch window (12:00-14:00 ET)",
                "rationale": (
                    f"Lunch window entries avg ${lunch_avg:.2f} PnL, "
                    f"vs morning entries avg ${morning_avg:.2f}. "
                    "This 25%+ underperformance suggests reduced volatility or false signals."
                ),
                "action": "Add gate: skip C1/C2 entry signals between 12:00-14:00 ET",
            }
        else:
            recommendations["r4_entry_timing_optimization"] = {
                "recommendation": "MAINTAIN current entry filtering",
                "rationale": (
                    f"Lunch entries (avg ${lunch_avg:.2f}) are not significantly worse "
                    f"than morning entries (avg ${morning_avg:.2f})."
                ),
                "action": "No change to entry gates",
            }

    return recommendations


def generate_demo_data() -> List[Dict[str, Any]]:
    """
    Generate synthetic trade data for --demo mode.
    Shows the expected output format.
    """
    import random

    trades = []
    base_date = datetime(2025, 9, 1, tzinfo=None)

    # Generate ~100 synthetic trades across different times
    sessions = [
        ("Pre-market", 6, 30, 9, 0),
        ("Open volatility", 9, 30, 10, 30),
        ("Morning", 10, 30, 12, 0),
        ("Lunch doldrums", 12, 0, 14, 0),
        ("Afternoon", 14, 0, 15, 30),
        ("Close", 15, 30, 16, 0),
    ]

    trade_id = 0
    for day_offset in range(20):  # 20 trading days
        for session_name, start_h, start_m, end_h, end_m in sessions:
            # 3-5 trades per session per day
            for _ in range(random.randint(3, 5)):
                hour = random.randint(start_h, end_h)
                minute = random.randint(
                    start_m if hour == start_h else 0,
                    end_m if hour == end_h else 59
                )

                # Base PnL varies by session
                if session_name == "Lunch doldrums":
                    base_pnl = random.gauss(35, 45)  # Worse performance
                elif session_name == "Close":
                    base_pnl = random.gauss(48, 50)  # Slightly worse
                elif session_name == "Morning":
                    base_pnl = random.gauss(60, 40)  # Better morning
                else:
                    base_pnl = random.gauss(52, 40)  # Average

                timestamp = (
                    base_date + timedelta(days=day_offset, hours=hour, minutes=minute)
                ).isoformat() + "Z"

                # Determine exit reason (weighted)
                reason = random.choices(
                    ["trailing", "breakeven", "stop", "max_target", "time_stop"],
                    weights=[0.5, 0.15, 0.15, 0.1, 0.1]
                )[0]

                trades.append({
                    "event": "trade_closed",
                    "timestamp": timestamp,
                    "direction": random.choice(["long", "short"]),
                    "entry_price": 18000.0 + random.gauss(0, 100),
                    "c1_pnl": base_pnl * 0.4 + random.gauss(0, 20),
                    "c1_reason": reason,
                    "c2_pnl": base_pnl * 0.6 + random.gauss(0, 20),
                    "c2_reason": reason,
                    "daily_pnl": base_pnl + random.gauss(0, 25),
                    "total_pnl": base_pnl + random.gauss(0, 25),
                    "signal_source": random.choice(["sweep", "setup", "confluence"]),
                    "entry_slippage_pts": random.uniform(0.5, 2.0),
                    "exit_slippage_pts": random.uniform(0.5, 2.0),
                })
                trade_id += 1

    return trades


def print_report(
    exit_analysis: Dict[str, Dict[str, Any]],
    entry_analysis: Dict[str, Dict[str, Any]],
    recommendations: Dict[str, Any],
):
    """Print formatted analysis report to console."""
    print("\n" + "=" * 80)
    print("SESSION TIME ANALYSIS -- Research Question R4")
    print("=" * 80)

    print("\n" + "-" * 80)
    print("EXIT TIME ANALYSIS (C2 Trades by Exit Time)")
    print("-" * 80)
    print(f"{'Session':<20s}  {'Exits':>6s}  {'Avg PnL':>10s}  {'WR':>6s}  {'PF':>6s}  {'Trailing Avg':>12s}")
    print("-" * 80)

    for session_name in [s[0] for s in SESSION_WINDOWS]:
        data = exit_analysis.get(session_name, {})
        if data.get("exit_count", 0) == 0:
            continue

        trailing_avg = data.get("exit_breakdown", {}).get("trailing", {}).get("avg_pnl", "N/A")
        if isinstance(trailing_avg, (int, float)):
            trailing_str = f"${trailing_avg:>10.2f}"
        else:
            trailing_str = f"{str(trailing_avg):>12s}"

        pf = data.get("profit_factor", "inf")
        if pf == "inf":
            pf_str = "   inf"
        else:
            pf_str = f"{pf:>6.3f}"

        print(
            f"{session_name:<20s}  {data['exit_count']:>6d}  "
            f"${data['avg_pnl']:>9.2f}  {data['win_rate']:>5.1%}  "
            f"{pf_str}  {trailing_str}"
        )

    print("\n" + "-" * 80)
    print("ENTRY TIME ANALYSIS (Trades by Estimated Entry Time)")
    print("-" * 80)
    print(f"{'Session':<20s}  {'Entries':>8s}  {'Avg PnL':>10s}  {'WR':>6s}  {'PF':>6s}")
    print("-" * 80)

    for session_name in [s[0] for s in SESSION_WINDOWS]:
        data = entry_analysis.get(session_name, {})
        if data.get("entry_count", 0) == 0:
            continue

        pf = data.get("profit_factor", "inf")
        if pf == "inf":
            pf_str = "   inf"
        else:
            pf_str = f"{pf:>6.3f}"

        print(
            f"{session_name:<20s}  {data['entry_count']:>8d}  "
            f"${data['avg_pnl']:>9.2f}  {data['win_rate']:>5.1%}  {pf_str}"
        )

    print("\n" + "=" * 80)
    print("RECOMMENDATIONS")
    print("=" * 80)

    if recommendations.get("r4_trailing_stop_timing"):
        rec = recommendations["r4_trailing_stop_timing"]
        print(f"\nTrailing Stop Timing (R4):")
        print(f"  → {rec['recommendation']}")
        print(f"  Rationale: {rec['rationale']}")
        print(f"  Action: {rec['action']}")

    if recommendations.get("r4_entry_timing_optimization"):
        rec = recommendations["r4_entry_timing_optimization"]
        print(f"\nEntry Timing Optimization (R4):")
        print(f"  → {rec['recommendation']}")
        print(f"  Rationale: {rec['rationale']}")
        print(f"  Action: {rec['action']}")


def main():
    parser = argparse.ArgumentParser(
        description="Session Time Analysis for MNQ futures trading (R4)"
    )
    parser.add_argument(
        "--trades",
        type=str,
        default=None,
        help="Path to trade results JSON file (default: logs/paper_trades.json)",
    )
    parser.add_argument(
        "--timezone",
        type=str,
        default="US/Eastern",
        help="Timezone for bucketing (default: US/Eastern)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run in demo mode with synthetic data",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output file path (default: logs/session_time_analysis.json)",
    )

    args = parser.parse_args()

    # Determine input file
    if args.demo:
        print("[DEMO MODE] Generating synthetic trade data...")
        trades = generate_demo_data()
        print(f"Generated {len(trades)} synthetic trades")
    else:
        trades_path = args.trades or (
            Path(__file__).resolve().parent.parent / "logs" / "paper_trades.json"
        )
        if not Path(trades_path).exists():
            print(f"Error: Trade file not found: {trades_path}")
            print("Use --trades to specify path, or --demo for synthetic data")
            return 1

        print(f"Loading trades from: {trades_path}")
        trades = load_trades(str(trades_path))
        print(f"Loaded {len(trades)} trades")

    # Run analysis
    print("\nAnalyzing by exit time...")
    exit_analysis = analyze_trades_by_exit_time(trades, args.timezone)

    print("Analyzing by entry time...")
    entry_analysis = analyze_trades_by_entry_time(trades, args.timezone)

    print("Generating recommendations...")
    recommendations = generate_recommendations(exit_analysis, entry_analysis)

    # Print report
    print_report(exit_analysis, entry_analysis, recommendations)

    # Write output
    output_path = args.output or (
        Path(__file__).resolve().parent.parent / "logs" / "session_time_analysis.json"
    )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    output_data = {
        "analysis_type": "session_time_analysis_r4",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "data_source": "paper_trades.json" if not args.demo else "synthetic_demo_data",
        "timezone": args.timezone,
        "total_trades_analyzed": len(trades),
        "exit_time_analysis": exit_analysis,
        "entry_time_analysis": entry_analysis,
        "recommendations": recommendations,
    }

    with open(output_path, "w") as f:
        json.dump(output_data, f, indent=2)

    print(f"\nResults written to: {output_path}")
    return 0


if __name__ == "__main__":
    exit(main())
