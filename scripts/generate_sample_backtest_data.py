"""
Generate Sample TradingView Data for CI Testing
================================================

Creates minimal sample OHLCV data files for backtest validation in CI.
Generates realistic MNQ bar data with proper volume profile.

Output: data/tradingview/*.csv (standard TradingView format)

This allows backtest-validation.yml to run without requiring
actual historical data files in the repo.
"""

import csv
from datetime import datetime, timedelta
from pathlib import Path
import random

# Configuration
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "nq_bot_vscode" / "data" / "tradingview"
NUM_BARS = 1000  # 1000 bars @ 1m = ~7 hours of trading

def generate_realistic_bar_data(num_bars: int = NUM_BARS):
    """Generate synthetic MNQ data with realistic OHLC/volume."""
    bars = []

    # Start from 2 weeks ago
    start_time = datetime.utcnow() - timedelta(days=14)
    close = 18500.0  # MNQ-ish price

    for i in range(num_bars):
        # Timestamp (1-minute bars, excluding weekends)
        bar_time = start_time + timedelta(minutes=i)

        # Skip weekends
        if bar_time.weekday() >= 5:
            continue

        # Realistic price movement
        drift = 0.1 if random.random() > 0.5 else -0.1
        volatility = random.gauss(0, 5.0)  # 5pt std dev
        open_p = close
        high = max(open_p, close + volatility + abs(random.gauss(0, 2.0)))
        low = min(open_p, close - volatility - abs(random.gauss(0, 2.0)))
        close = close + drift + volatility

        # Volume (realistic: 100-1000 contracts per bar)
        volume = max(50, int(random.gauss(500, 150)))

        bars.append({
            'time': bar_time.isoformat() + 'Z',
            'open': round(open_p, 2),
            'high': round(high, 2),
            'low': round(low, 2),
            'close': round(close, 2),
            'volume': volume,
        })

    return bars

def write_tradingview_csv(filename: str, bars: list):
    """Write bars in TradingView CSV format."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filepath = OUTPUT_DIR / filename

    with open(filepath, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['time', 'open', 'high', 'low', 'close', 'volume'])
        writer.writeheader()
        writer.writerows(bars)

    print(f"Generated: {filepath} ({len(bars)} bars)")

if __name__ == "__main__":
    bars = generate_realistic_bar_data()
    write_tradingview_csv("mnq_sample_1m.csv", bars)
    print(f"\nSample data ready for backtest: {OUTPUT_DIR}/mnq_sample_1m.csv")
