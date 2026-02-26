# NQ Trading Bot

Institutional-grade automated trading system for **MNQ (Micro Nasdaq-100 Futures)**. Multi-timeframe structure analysis with a 2-contract scale-out execution strategy, governed by a backtested High-Conviction Filter.

## Architecture

```
HTF Bars (1D/4H/1H/30m/15m/5m) → HTF Bias Engine → Directional Gate
Exec Bars (2m) → Feature Engine → Signal Aggregator → HC Filter → Risk → Executor
```

**High-Conviction Filter (3 hard gates):**
1. Signal score ≥ 0.75
2. Stop distance ≤ 30 points
3. TP1 = stop × 1.5 R:R
