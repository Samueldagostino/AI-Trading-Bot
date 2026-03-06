"""
Calibrate Session Volatility Scale Factors
============================================
Reads FirstRate 1-minute NQ data and computes realized volatility
per intraday session period.  Compares against the default scale
factors in SessionVolatilityScaler and outputs recommended calibrated
values.

Usage (local machine only — needs FirstRate data files):
    python scripts/calibrate_session_volatility.py \\
        --input data/firstrate/NQ_1m_absolute.csv

Method:
    1. Parse 1-minute bars with timestamps
    2. Classify each bar into session (opening/midday/closing/eth)
    3. Compute 5-minute returns within each session
    4. Calculate realized variance = sum(r^2) per session per day
    5. Average across all days → session realized vol
    6. Normalize to full-session average → scale factors
"""

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, time
from math import sqrt
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SESSIONS = {
    "opening": (time(9, 30), time(10, 30)),
    "midday":  (time(10, 30), time(14, 0)),
    "closing": (time(14, 0), time(16, 0)),
    "eth_pre": (time(4, 0), time(9, 30)),      # Pre-market ETH
    "eth_post": (time(18, 0), time(23, 59)),    # Post-market ETH
}

DEFAULT_FACTORS = {
    "opening": 1.3,
    "midday":  0.75,
    "closing": 1.1,
    "eth":     0.6,
}


def classify_session(et_time: time) -> str:
    """Classify an ET time into a session bucket."""
    if time(9, 30) <= et_time < time(10, 30):
        return "opening"
    elif time(10, 30) <= et_time < time(14, 0):
        return "midday"
    elif time(14, 0) <= et_time < time(16, 0):
        return "closing"
    else:
        return "eth"


def parse_csv(filepath: str):
    """Parse FirstRate 1-minute CSV.

    Expected columns: DateTime, Open, High, Low, Close, Volume
    DateTime format: YYYY-MM-DD HH:MM:SS (Eastern Time)
    """
    bars = []
    with open(filepath, "r") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            print("ERROR: Empty CSV file")
            sys.exit(1)

        for row in reader:
            if len(row) < 5:
                continue
            try:
                dt = datetime.strptime(row[0].strip(), "%Y-%m-%d %H:%M:%S")
                dt = dt.replace(tzinfo=ET)
                close = float(row[4])
                bars.append((dt, close))
            except (ValueError, IndexError):
                continue

    return bars


def compute_session_volatility(bars):
    """Compute realized volatility per session using 5-min squared returns."""
    # Group bars by date+session
    session_returns = defaultdict(list)

    # Build 5-minute sampled prices
    prev_price = None
    prev_session = None
    bar_count = 0

    for dt, close in bars:
        session = classify_session(dt.time())
        bar_count += 1

        # Sample every 5 bars (5 minutes)
        if bar_count % 5 == 0 and prev_price is not None:
            if session == prev_session:
                ret = (close - prev_price) / prev_price
                date_key = dt.date()
                session_returns[(date_key, session)].append(ret)
            prev_price = close
            prev_session = session
        elif bar_count % 5 == 0:
            prev_price = close
            prev_session = session

    # Compute realized variance per session per day
    session_daily_rv = defaultdict(list)
    for (date, session), returns in session_returns.items():
        if len(returns) < 3:
            continue
        rv = sum(r ** 2 for r in returns)
        session_daily_rv[session].append(rv)

    # Average across days → realized vol
    session_vol = {}
    for session, rvs in session_daily_rv.items():
        avg_rv = sum(rvs) / len(rvs)
        session_vol[session] = sqrt(avg_rv)

    return session_vol, session_daily_rv


def main():
    parser = argparse.ArgumentParser(
        description="Calibrate session volatility scale factors from FirstRate 1m data"
    )
    parser.add_argument(
        "--input", required=True,
        help="Path to FirstRate 1-minute CSV (NQ_1m_absolute.csv)"
    )
    args = parser.parse_args()

    filepath = Path(args.input)
    if not filepath.exists():
        print(f"ERROR: File not found: {filepath}")
        sys.exit(1)

    print(f"Reading: {filepath}")
    bars = parse_csv(str(filepath))
    print(f"Parsed {len(bars):,} bars")

    if len(bars) < 1000:
        print("ERROR: Not enough data for calibration (need at least 1000 bars)")
        sys.exit(1)

    session_vol, session_daily_rv = compute_session_volatility(bars)

    if not session_vol:
        print("ERROR: Could not compute session volatility")
        sys.exit(1)

    # Compute full-session average vol for normalization
    rth_sessions = ["opening", "midday", "closing"]
    rth_vols = [session_vol[s] for s in rth_sessions if s in session_vol]
    if not rth_vols:
        print("ERROR: No RTH session data found")
        sys.exit(1)

    avg_rth_vol = sum(rth_vols) / len(rth_vols)

    print("\n" + "=" * 60)
    print("SESSION VOLATILITY CALIBRATION RESULTS")
    print("=" * 60)

    print(f"\n{'Session':<12} {'Realized Vol':>14} {'Scale Factor':>14} {'Default':>10} {'Delta':>10} {'Days':>8}")
    print("-" * 68)

    calibrated = {}
    for session in ["opening", "midday", "closing", "eth"]:
        vol = session_vol.get(session, 0)
        days = len(session_daily_rv.get(session, []))
        if avg_rth_vol > 0 and vol > 0:
            factor = round(vol / avg_rth_vol, 2)
        else:
            factor = DEFAULT_FACTORS.get(session, 1.0)

        default = DEFAULT_FACTORS.get(session, 1.0)
        delta = factor - default
        calibrated[session] = factor

        print(f"{session:<12} {vol:>14.6f} {factor:>14.2f} {default:>10.2f} {delta:>+10.2f} {days:>8}")

    print("\n" + "-" * 68)
    print("\nCalibrated scale factors for SessionVolatilityScaler:")
    print(f"  opening:  {calibrated.get('opening', 1.3)}")
    print(f"  midday:   {calibrated.get('midday', 0.75)}")
    print(f"  closing:  {calibrated.get('closing', 1.1)}")
    print(f"  eth:      {calibrated.get('eth', 0.6)}")

    print("\nTo use these calibrated values:")
    print("  scaler = SessionVolatilityScaler(scale_factors={")
    for s in ["opening", "midday", "closing", "eth"]:
        print(f'      "{s}": {calibrated.get(s, 1.0)},')
    print("  })")


if __name__ == "__main__":
    main()
