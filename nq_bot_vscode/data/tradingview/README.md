# TradingView Data Exports

Place your TradingView CSV exports here for backtesting.

## How to Export from TradingView

1. Open TradingView → Chart → NQ or MNQ
2. Set timeframe to **1 minute**
3. Scroll back to load as much history as possible
4. Click the **"..."** menu (top-right of chart) → **Export chart data**
5. Save the CSV file into this directory
6. Run backtest: `python scripts/run_backtest.py --file data/tradingview/your_export.csv`

## Expected CSV Format

```
time,open,high,low,close,Volume
2025-01-02T14:30:00Z,20150.25,20155.50,20148.00,20153.75,8234
2025-01-02T14:31:00Z,20153.75,20158.00,20152.25,20156.50,6891
```

## Tips

- Export the **maximum** date range available on your TradingView plan
- TradingView free tier has limited history; Pro/Premium has more
- For best backtesting, export at least 3-6 months of 1-min data
- Multiple CSV files can be placed here — they'll be merged automatically
- The importer handles various date formats and column name variations
