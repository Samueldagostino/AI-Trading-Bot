"""
Replay -> Weekly Report Adapter
================================
Reads logs/replay_feb23-27.json, converts the trade log into the format
expected by ibkr_monitor.py's weekly report pipeline, then runs:

  1. generate_weekly_report()  -> WeeklyReport for the replay week
  2. export_weekly_report()    -> logs/weekly_report_2026-02-27.json
  3. compute_weekly_reports()  -> all weekly reports (just one week here)
  4. compute_4_week_trend()    -> trend analysis
  5. update_viz_data()         -> docs/viz_data.json

Does NOT modify the weekly report or viz pipeline — only adapts the
replay output to match the expected input format (TradeRecord).

Usage:
    python scripts/replay_to_weekly.py
"""

import json
import sys
from pathlib import Path

# ── Project path setup ──
script_dir = Path(__file__).resolve().parent
project_dir = script_dir.parent
sys.path.insert(0, str(project_dir))

from scripts.ibkr_monitor import (
    TradeRecord,
    BacktestBaseline,
    generate_weekly_report,
    compute_weekly_reports,
    compute_4_week_trend,
    export_weekly_report,
    update_viz_data,
    _render_weekly_report,
    BASELINE_PATH,
    VIZ_DATA_PATH,
    LOGS_DIR,
)

REPLAY_JSON = LOGS_DIR / "replay_feb23-27.json"


def load_replay_trades(path: Path) -> list:
    """Load the raw trades array from the replay JSON."""
    with open(path) as f:
        data = json.load(f)
    return data.get("trades", [])


def convert_replay_to_trade_records(raw_trades: list) -> list:
    """
    Convert replay trade log entries to TradeRecord objects.

    Replay format (each entry):
        {
            "timestamp": "2026-02-23T02:36:00+00:00",
            "event": "trade_closed",
            "direction": "long",
            "entry_price": 24914.5,
            "total_pnl": -86.58,
            "c1_pnl": -43.29,
            "c2_pnl": -43.29,
            "signal_source": "sweep",
            ...
        }

    ibkr_monitor.parse_trades() expects:
        event == "fill", pnl (not total_pnl), source (not signal_source)

    We skip parse_trades() entirely and build TradeRecord objects
    directly — this is the correct adaptation layer.
    """
    records = []
    for entry in raw_trades:
        pnl = entry.get("total_pnl")
        if pnl is None:
            continue

        records.append(TradeRecord(
            timestamp=entry.get("timestamp", ""),
            direction=entry.get("direction", ""),
            pnl=float(pnl),
            c1_pnl=float(entry.get("c1_pnl", 0)),
            c2_pnl=float(entry.get("c2_pnl", 0)),
            entry_price=float(entry.get("entry_price", 0)),
            exit_price=0.0,  # Replay doesn't track exit price per-trade
            contracts=2,
            source=entry.get("signal_source", ""),
        ))
    return records


def main():
    # ── Load and convert ──
    print(f"\n{'=' * 62}")
    print(f"  REPLAY -> WEEKLY REPORT PIPELINE")
    print(f"{'=' * 62}\n")

    if not REPLAY_JSON.exists():
        print(f"ERROR: Replay log not found: {REPLAY_JSON}")
        print("Run scripts/replay_single_day.py first.")
        sys.exit(1)

    print(f"  Source: {REPLAY_JSON}")
    raw_trades = load_replay_trades(REPLAY_JSON)
    print(f"  Raw trades loaded: {len(raw_trades)}")

    trades = convert_replay_to_trade_records(raw_trades)
    print(f"  TradeRecords created: {len(trades)}")

    if not trades:
        print("ERROR: No trades to process.")
        sys.exit(1)

    # Show date range
    dates = sorted(set(t.timestamp[:10] for t in trades if t.timestamp))
    print(f"  Date range: {dates[0]} -> {dates[-1]}")
    print(f"  Trading days: {len(dates)} ({', '.join(dates)})")

    # ── Load baseline ──
    baseline = BacktestBaseline.from_json(BASELINE_PATH)
    print(f"  Baseline loaded: PF {baseline.profit_factor}, "
          f"WR {baseline.win_rate_pct}%")

    # ── Step 1: Generate weekly report ──
    print(f"\n  Step 1: generate_weekly_report()")
    report = generate_weekly_report(trades, baseline, "2026-02-23")
    print(f"    Week: {report.week_start} -> {report.week_end}")
    print(f"    Trades: {report.trade_count}")
    print(f"    PnL: ${report.net_pnl:+.2f}")
    print(f"    WR: {report.win_rate_pct:.1f}%")
    pf_str = f"{report.profit_factor:.2f}" if report.profit_factor < 100 else "inf"
    print(f"    PF: {pf_str}")

    # ── Step 2: Export weekly report JSON ──
    print(f"\n  Step 2: export_weekly_report()")
    export_path = export_weekly_report(report)
    print(f"    Exported: {export_path}")

    # ── Step 3: Compute all weekly reports (for trend) ──
    print(f"\n  Step 3: compute_weekly_reports()")
    all_reports = compute_weekly_reports(trades, baseline)
    print(f"    Reports generated: {len(all_reports)}")
    for r in all_reports:
        print(f"      {r.week_start} -> {r.week_end}: "
              f"{r.trade_count} trades, ${r.net_pnl:+.2f}")

    # ── Step 4: Compute 4-week trend ──
    print(f"\n  Step 4: compute_4_week_trend()")
    trend = compute_4_week_trend(all_reports)
    print(f"    Status: {trend['status']}")
    print(f"    Weeks available: {trend['weeks_available']}")

    # ── Step 5: Update viz_data.json ──
    print(f"\n  Step 5: update_viz_data()")
    viz_path = update_viz_data(all_reports, trend)
    print(f"    Written: {viz_path}")

    # Verify contents
    with open(viz_path) as f:
        viz_data = json.load(f)
    print(f"    Keys: {list(viz_data.keys())}")
    wr = viz_data.get("weekly_reports", [])
    print(f"    Weekly reports: {len(wr)}")
    if wr:
        print(f"    First report: {wr[0].get('week_start')} -> {wr[0].get('week_end')}")

    # ── Print the full weekly report ──
    print(_render_weekly_report(report, trend, baseline))

    # ── Confirm website rendering ──
    print(f"\n{'─' * 62}")
    print(f"  WEBSITE RENDERING CHECK")
    print(f"{'─' * 62}")

    checks_passed = 0
    checks_total = 0

    # Check 1: viz_data.json exists
    checks_total += 1
    if viz_path.exists():
        checks_passed += 1
        print(f"  [PASS] docs/viz_data.json exists ({viz_path.stat().st_size:,} bytes)")
    else:
        print(f"  [FAIL] docs/viz_data.json missing")

    # Check 2: Has weekly_reports array
    checks_total += 1
    if "weekly_reports" in viz_data and len(viz_data["weekly_reports"]) > 0:
        checks_passed += 1
        print(f"  [PASS] weekly_reports array present ({len(viz_data['weekly_reports'])} reports)")
    else:
        print(f"  [FAIL] weekly_reports array missing or empty")

    # Check 3: Has trend data
    checks_total += 1
    if "trend" in viz_data:
        checks_passed += 1
        print(f"  [PASS] trend data present (status: {viz_data['trend'].get('status')})")
    else:
        print(f"  [FAIL] trend data missing")

    # Check 4: Has last_updated
    checks_total += 1
    if "last_updated" in viz_data:
        checks_passed += 1
        print(f"  [PASS] last_updated: {viz_data['last_updated']}")
    else:
        print(f"  [FAIL] last_updated missing")

    # Check 5: Weekly report has all required fields
    checks_total += 1
    required_fields = [
        "week_start", "week_end", "trade_count", "net_pnl",
        "profit_factor", "win_rate_pct", "c1_pnl", "c2_pnl",
        "wins", "losses", "wr_z_score", "pf_z_score",
    ]
    if wr:
        missing = [f for f in required_fields if f not in wr[0]]
        if not missing:
            checks_passed += 1
            print(f"  [PASS] All {len(required_fields)} required fields present in report")
        else:
            print(f"  [FAIL] Missing fields: {missing}")
    else:
        print(f"  [FAIL] No weekly reports to check")

    # Check 6: Weekly report JSON exported
    checks_total += 1
    if export_path.exists():
        checks_passed += 1
        print(f"  [PASS] Weekly report JSON: {export_path.name}")
    else:
        print(f"  [FAIL] Weekly report JSON not exported")

    print(f"\n  Result: {checks_passed}/{checks_total} checks passed")

    print(f"\n{'=' * 62}")
    print(f"  Output files:")
    print(f"    {export_path}")
    print(f"    {viz_path}")
    print(f"{'=' * 62}")


if __name__ == "__main__":
    main()
