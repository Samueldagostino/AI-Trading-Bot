# Backtest V2.3.1 — Hardened Causal Replay

## Date Range
- **Period**: December 1, 2025 — February 28, 2026 (3 months)
- **Data Source**: FirstRate 1-minute NQ futures (absolute-adjusted)
- **Execution TF**: 2m (aggregated from 1m)
- **HTF Data**: Built causally from 1m (5m, 15m, 30m, 1H, 4H, 1D)

## Strategy Configuration
- **Contracts**: 5 total (C1=1, C2=1, C3=3)
- **C1 Exit**: 5-bar time exit (canary)
- **C2 Exit**: Structural target + delayed BE (2.0pt buffer)
- **C3 Exit**: ATR trail runner (DELAYED ENTRY — only fills when C1 profits)
- **HC Filter**: score >= 0.75, stop <= 30pts
- **HTF Gate**: strength >= 0.3 (Config D)

## Integrity Hardening (V2.3.1 Changes)
All changes designed to make backtest results MORE conservative than reality:

### Intra-Bar Stop/Target Evaluation
- Stops checked against bar LOW (longs) and bar HIGH (shorts) — not just close
- Stop fills at exact stop price (no improvement)
- Targets checked against bar HIGH (longs) and bar LOW (shorts)

### Punishing Slippage
- **RTH**: 1.25pt base + random [0, 0.25, 0.50, 0.75], minimum 0.50pt
- **ETH**: 2.00pt base + random [0, 0.25, 0.50, 0.75], minimum 0.50pt
- Applied on BOTH entry AND exit (adverse direction)
- Real-world average is ~0.50pt RTH / ~1.00pt ETH — we punish 2-3x harder

### Conservative Commission
- **$1.50 per contract per side** (round-trip = $3.00/contract)
- Real broker rate is $1.29 — we charge 16% more
- C3 with 3 contracts pays 3x commission (not flat)

### Realistic C3 Delayed Entry
- C3 stays PENDING until C1 exits profitably
- If C1 profits: C3 fills at CURRENT market price (not retroactive entry)
- If C1 loses: C3 never fills — zero exposure, zero PnL
- No retroactive accounting (old bug gave best entry + zero loss — impossible IRL)

### Accurate Max Drawdown
- Bar-by-bar equity tracking including unrealized PnL during open positions
- Not just trade-close snapshots

### Anti-Cheat System
- Timestamp monotonicity checks (CausalityViolationError)
- HTF bars only feed after period completion
- Future-leak detection on HTF data
- Causality report in results: CLEAN or CHEATING

## How to Reproduce
```bash
cd nq_bot_vscode
python scripts/full_backtest.py --run \
    --data backtests/V2.3.1/combined_1min.csv \
    --output backtests/V2.3.1/trades.json \
    --summary backtests/V2.3.1/summary.txt \
    --log-level WARNING \
    --resume
```

## Results
See `summary.txt` and `trades.json` in this directory after backtest completion.
