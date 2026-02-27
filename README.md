# NQ Trading Bot

Institutional-grade automated trading system for **MNQ (Micro Nasdaq-100 Futures)**. Multi-timeframe structure analysis with a 2-contract scale-out execution strategy, governed by a backtested High-Conviction Filter.

**Config D** — 6/6 months profitable | PF 1.59 | 68.1% WR | $14,544 total PnL | 1.7% max DD

## Live Dashboard

Browse the interactive visualizations at **[GitHub Pages](https://samueldagostino.github.io/AI-Trading-Bot/)**:

- **[Forensic Trade Dashboard](https://samueldagostino.github.io/AI-Trading-Bot/dashboard.html)** — Canvas-based chart with trade overlays, stop/target levels, regime and HTF subpanes, sortable trade table with filters
- **[OOS Validation Report](https://samueldagostino.github.io/AI-Trading-Bot/report.html)** — 6-month out-of-sample validation with monthly equity curves, rolling PF/WR, regime distribution, drawdown analysis

## Architecture

```
HTF Bars (1D/4H/1H/30m/15m/5m) → HTF Bias Engine → Directional Gate
Exec Bars (2m) → Feature Engine → Signal Aggregator → HC Filter → Risk → Executor
```

**High-Conviction Filter (3 hard gates):**
1. Signal score ≥ 0.75
2. Stop distance ≤ 30 points
3. TP1 = stop × 1.5 R:R
