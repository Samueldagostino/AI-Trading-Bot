"""
Backtest Runner
================
Run the 2-contract scale-out strategy against historical NQ data.

Usage:
    python scripts/run_backtest.py --sample              # Sample data (pipeline test)
    python scripts/run_backtest.py --tv                   # TradingView CSV exports
    python scripts/run_backtest.py --file data/nq.csv     # Specific file
"""

import asyncio
import argparse
import logging
import random
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import CONFIG
from features.engine import Bar
from data_pipeline.pipeline import DataPipeline
from main import TradingOrchestrator


def generate_sample_bars(num_bars: int = 3000, start_price: float = 20000.0) -> list:
    """
    Generate NQ-like 1-minute bars for pipeline testing.
    NOT for strategy validation — patterns are random.
    """
    bars = []
    price = start_price
    base_time = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
    trend = 0

    for i in range(num_bars):
        if i % 300 == 0:
            trend = random.choice([-1, 0, 0, 1])

        volatility = random.uniform(1.0, 6.0)
        direction = random.gauss(trend * 0.3, 1.0)
        move = direction * volatility

        if random.random() < 0.005:
            move *= random.uniform(3, 8)

        open_price = price
        close_price = price + move
        high = max(open_price, close_price) + random.uniform(0, volatility * 0.5)
        low = min(open_price, close_price) - random.uniform(0, volatility * 0.5)
        volume = max(int(random.gauss(8000, 3000)), 500)

        if close_price > open_price:
            ask_vol = int(volume * random.uniform(0.5, 0.7))
        else:
            ask_vol = int(volume * random.uniform(0.3, 0.5))
        bid_vol = volume - ask_vol

        bars.append(Bar(
            timestamp=base_time + timedelta(minutes=i),
            open=round(open_price, 2),
            high=round(high, 2),
            low=round(low, 2),
            close=round(close_price, 2),
            volume=volume,
            bid_volume=bid_vol,
            ask_volume=ask_vol,
            delta=ask_vol - bid_vol,
        ))
        price = close_price

    return bars


async def run_backtest(bars, label: str = ""):
    """Execute backtest through the orchestrator."""
    CONFIG.execution.paper_trading = True

    bot = TradingOrchestrator(CONFIG)
    await bot.initialize(skip_db=True)

    print(f"\n{'='*60}")
    print(f"  BACKTEST: {label}")
    print(f"  Bars: {len(bars)}")
    if bars:
        print(f"  Range: {bars[0].timestamp} → {bars[-1].timestamp}")
        print(f"  Strategy: 2x MNQ Scale-Out")
        print(f"  C1 Target: {CONFIG.scale_out.c1_target_min_points}-{CONFIG.scale_out.c1_target_max_points} pts")
        print(f"  Account: ${CONFIG.risk.account_size:,.2f}")
    print(f"{'='*60}\n")

    results = await bot.run_backtest(bars)

    # Print C1 vs C2 breakdown
    if results.get("total_trades", 0) > 0:
        print(f"\n  SCALE-OUT BREAKDOWN:")
        print(f"  C1 (fixed target) total PnL:  ${results.get('c1_total_pnl', 0):,.2f}")
        print(f"  C2 (runner) total PnL:         ${results.get('c2_total_pnl', 0):,.2f}")
        print(f"  C2 outperformed C1:            {results.get('c2_outperformed_c1_pct', 0):.1f}% of trades")
        print(f"{'='*60}\n")

    return results


def main():
    parser = argparse.ArgumentParser(description="NQ Bot — Backtest Runner")
    parser.add_argument("--sample", action="store_true", help="Use generated sample data")
    parser.add_argument("--tv", action="store_true", help="Use TradingView CSV exports from data/tradingview/")
    parser.add_argument("--file", type=str, help="Path to specific CSV file")
    parser.add_argument("--bars", type=int, default=3000, help="Number of sample bars")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.file:
        # Import specific TradingView CSV
        pipeline = DataPipeline(CONFIG)
        bar_data = pipeline.tv_importer.import_file(args.file)
        if not bar_data:
            print(f"ERROR: No data loaded from {args.file}")
            sys.exit(1)
        summary = pipeline.get_data_summary(bar_data)
        print(f"Data summary: {summary}")
        bars = pipeline.convert_to_feature_bars(bar_data)
        asyncio.run(run_backtest(bars, label=f"TradingView: {args.file}"))

    elif args.tv:
        # Import all TradingView CSVs from directory
        pipeline = DataPipeline(CONFIG)
        bar_data = pipeline.tv_importer.import_directory()
        if not bar_data:
            print("ERROR: No CSV files found in data/tradingview/")
            print("Export data from TradingView and place CSVs in that directory.")
            sys.exit(1)
        summary = pipeline.get_data_summary(bar_data)
        print(f"Data summary: {summary}")
        bars = pipeline.convert_to_feature_bars(bar_data)
        asyncio.run(run_backtest(bars, label="TradingView Directory Import"))

    elif args.sample or True:  # Default to sample
        print("Generating sample data (for pipeline testing only)...")
        bars = generate_sample_bars(args.bars)
        asyncio.run(run_backtest(bars, label=f"Sample Data ({args.bars} bars)"))


if __name__ == "__main__":
    main()
