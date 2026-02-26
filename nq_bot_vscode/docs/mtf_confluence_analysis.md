# MTF Confluence Analysis Report
**Generated**: 2026-02-26 17:55 UTC
**Data window**: Feb 1-26, 2026 (2m execution, all TF HTF data)
**HC filter**: ON (score >= 0.75, stop <= 30pts, TP1 = 1.5x stop)

---
## Phase 1: HTF Confluence Value Test

| Metric | Config A (No HTF) | Config B (gate=0.7) | Config C (gate=0.5) | Config D (gate=0.3) |
|---|---|---|---|---|
| **Total Trades** | 88 | 110 | 90 | 84 |
| **HTF Blocked** | 0 | 386 | 1682 | 1838 |
| **Win Rate** | 38.6% | 38.2% | 46.7% | 50.0% |
| **Profit Factor** | 0.75 | 0.79 | 1.13 | 1.29 |
| **Total PnL** | $-1,527.68 | $-1,512.08 | $685.98 | $1,302.36 |
| **Avg Winner** | $134.93 | $136.27 | $138.95 | $138.40 |
| **Avg Loser** | $-113.25 | $-106.40 | $-107.29 | $-107.39 |
| **Largest Win** | $363.08 | $363.08 | $364.08 | $364.08 |
| **Largest Loss** | $-330.58 | $-283.58 | $-283.58 | $-284.58 |
| **Max DD** | 4.1% | 4.8% | 3.1% | 2.8% |
| **C1 PnL** | $-985.42 | $-1,049.22 | $28.62 | $329.90 |
| **C2 PnL** | $-542.26 | $-462.86 | $657.36 | $972.46 |
| **Expectancy/trade** | $-17.36 | $-13.75 | $7.62 | $15.50 |

**Best performing config**: D (PF 1.29)

---
## Phase 2: Kill/Save Matrix

Using Config A's trade universe, retroactively checking what each HTF gate would have done:

### Config B (0.7)

|  | HTF Allows | HTF Blocks |
|---|---|---|
| **Winner** | TP=27 ($3,807.48) | FN=7 ($780.16) |
| **Loser** | FP=44 ($-4,772.52) | TN=10 ($-1,342.80) |

- **Precision** (winners / allowed): 38.0%
- **Recall** (winners kept / all winners): 79.4%
- **F1 Score**: 0.514
- **PnL saved** (blocked losers): $1,342.80
- **PnL sacrificed** (blocked winners): $780.16
- **Net filter value**: $562.64

### Config C (0.5)

|  | HTF Allows | HTF Blocks |
|---|---|---|
| **Winner** | TP=14 ($2,026.02) | FN=20 ($2,561.62) |
| **Loser** | FP=19 ($-2,086.02) | TN=35 ($-4,029.30) |

- **Precision** (winners / allowed): 42.4%
- **Recall** (winners kept / all winners): 41.2%
- **F1 Score**: 0.418
- **PnL saved** (blocked losers): $4,029.30
- **PnL sacrificed** (blocked winners): $2,561.62
- **Net filter value**: $1,467.68

### Config D (0.3)

|  | HTF Allows | HTF Blocks |
|---|---|---|
| **Winner** | TP=13 ($1,953.32) | FN=21 ($2,634.32) |
| **Loser** | FP=18 ($-1,979.44) | TN=36 ($-4,135.88) |

- **Precision** (winners / allowed): 41.9%
- **Recall** (winners kept / all winners): 38.2%
- **F1 Score**: 0.400
- **PnL saved** (blocked losers): $4,135.88
- **PnL sacrificed** (blocked winners): $2,634.32
- **Net filter value**: $1,501.56

---
## Phase 3: Regime + Session + HTF Cross-Analysis

Analysis based on best-performing config (D).

### PnL by Regime

| Regime | Trades | Wins | WR | PnL | Expectancy |
|---|---|---|---|---|---|
| trending_down | 23 | 11 | 47.8% | $685.96 | $29.82 |
| ranging | 11 | 7 | 63.6% | $527.64 | $47.97 |
| trending_up | 31 | 16 | 51.6% | $204.26 | $6.59 |
| unknown | 18 | 8 | 44.4% | $-49.92 | $-2.77 |
| low_liquidity | 1 | 0 | 0.0% | $-65.58 | $-65.58 |

### PnL by Session (ET)

| Session | Trades | Wins | WR | PnL | Expectancy |
|---|---|---|---|---|---|
| overnight/extended | 55 | 28 | 50.9% | $1,018.70 | $18.52 |
| morning (9:30-11:30) | 3 | 2 | 66.7% | $254.52 | $84.84 |
| pre-market (6-9:30) | 18 | 9 | 50.0% | $109.88 | $6.10 |
| midday (11:30-14:00) | 2 | 1 | 50.0% | $-3.08 | $-1.54 |
| afternoon (14-16:00) | 6 | 2 | 33.3% | $-77.66 | $-12.94 |

### PnL by HTF Consensus Direction at Entry

| HTF Direction | Trades | Wins | WR | PnL | Expectancy |
|---|---|---|---|---|---|
| bullish | 36 | 19 | 52.8% | $1,000.02 | $27.78 |
| bearish | 34 | 16 | 47.1% | $291.64 | $8.58 |
| neutral | 14 | 7 | 50.0% | $10.70 | $0.76 |

### Combined Filter Search (min 3 trades)

All filter combinations sorted by expectancy. **Positive expectancy** combos are highlighted.

| Filter | Trades | WR | PnL | Expectancy/trade |
|---|---|---|---|---|
| **regime=trending_up + session=overnight/extended + htf=neutral** | 4 | 75.0% | $352.38 | $88.10 |
| **session=overnight/extended + htf=neutral** | 9 | 77.8% | $774.60 | $86.07 |
| **session=morning (9:30-11:30)** | 3 | 66.7% | $254.52 | $84.84 |
| **regime=trending_up + session=pre-market (6-9:30) + htf=bullish** | 4 | 75.0% | $297.56 | $74.39 |
| **regime=ranging + htf=bullish** | 4 | 75.0% | $276.90 | $69.22 |
| **regime=ranging + session=overnight/extended + htf=bullish** | 4 | 75.0% | $276.90 | $69.22 |
| **regime=trending_down + session=overnight/extended + htf=bullish** | 3 | 33.3% | $200.34 | $66.78 |
| **regime=trending_up + session=pre-market (6-9:30) + htf=bearish** | 3 | 66.7% | $176.06 | $58.69 |
| **regime=ranging + htf=bearish** | 6 | 66.7% | $327.32 | $54.55 |
| **regime=trending_down + session=overnight/extended** | 12 | 58.3% | $591.22 | $49.27 |
| **regime=ranging** | 11 | 63.6% | $527.64 | $47.97 |
| **regime=trending_down + htf=bearish** | 12 | 58.3% | $558.78 | $46.56 |
| **regime=unknown + session=overnight/extended + htf=bullish** | 6 | 50.0% | $254.48 | $42.41 |
| **regime=trending_up + session=pre-market (6-9:30)** | 8 | 62.5% | $329.04 | $41.13 |
| **session=overnight/extended + htf=bullish** | 25 | 56.0% | $989.54 | $39.58 |
| **regime=ranging + session=overnight/extended** | 10 | 60.0% | $394.64 | $39.46 |
| **regime=ranging + session=overnight/extended + htf=bearish** | 5 | 60.0% | $194.32 | $38.86 |
| **regime=trending_down** | 23 | 47.8% | $685.96 | $29.82 |
| **regime=trending_up + session=overnight/extended + htf=bullish** | 11 | 63.6% | $323.40 | $29.40 |
| **regime=trending_up + htf=bullish** | 18 | 61.1% | $517.30 | $28.74 |
| **session=pre-market (6-9:30) + htf=bearish** | 10 | 60.0% | $280.64 | $28.06 |
| **htf=bullish** | 36 | 52.8% | $1,000.02 | $27.78 |
| **regime=unknown + htf=bullish** | 7 | 42.9% | $151.90 | $21.70 |
| **regime=trending_down + htf=bullish** | 6 | 33.3% | $119.50 | $19.92 |
| **session=overnight/extended** | 55 | 50.9% | $1,018.70 | $18.52 |
| **regime=unknown + session=pre-market (6-9:30) + htf=bearish** | 3 | 66.7% | $42.68 | $14.23 |
| **regime=trending_up + session=overnight/extended** | 19 | 52.6% | $263.46 | $13.87 |
| **htf=bearish** | 34 | 47.1% | $291.64 | $8.58 |
| **regime=trending_down + session=overnight/extended + htf=bearish** | 7 | 57.1% | $48.46 | $6.92 |
| **regime=trending_up** | 31 | 51.6% | $204.26 | $6.59 |
| **session=pre-market (6-9:30)** | 18 | 50.0% | $109.88 | $6.10 |
| **regime=trending_down + session=afternoon (14-16:00)** | 5 | 40.0% | $22.92 | $4.58 |
| **regime=trending_down + htf=neutral** | 5 | 40.0% | $7.68 | $1.54 |
| **htf=neutral** | 14 | 50.0% | $10.70 | $0.76 |
| regime=unknown | 18 | 44.4% | $-49.92 | $-2.77 |
| session=pre-market (6-9:30) + htf=bullish | 7 | 42.9% | $-26.18 | $-3.74 |
| regime=unknown + session=overnight/extended | 13 | 38.5% | $-165.04 | $-12.70 |
| regime=trending_up + htf=neutral | 6 | 50.0% | $-76.78 | $-12.80 |
| session=afternoon (14-16:00) | 6 | 33.3% | $-77.66 | $-12.94 |
| regime=unknown + session=pre-market (6-9:30) | 4 | 50.0% | $-59.90 | $-14.97 |
| regime=trending_down + session=pre-market (6-9:30) + htf=bearish | 3 | 33.3% | $-71.10 | $-23.70 |
| regime=trending_up + htf=bearish | 7 | 28.6% | $-236.26 | $-33.75 |
| session=overnight/extended + htf=bearish | 21 | 33.3% | $-745.44 | $-35.50 |
| regime=unknown + htf=bearish | 9 | 33.3% | $-358.20 | $-39.80 |
| regime=trending_down + session=pre-market (6-9:30) | 5 | 20.0% | $-292.26 | $-58.45 |
| regime=trending_up + session=overnight/extended + htf=bearish | 4 | 0.0% | $-412.32 | $-103.08 |
| session=afternoon (14-16:00) + htf=neutral | 3 | 0.0% | $-334.74 | $-111.58 |
| regime=trending_down + session=afternoon (14-16:00) + htf=neutral | 3 | 0.0% | $-334.74 | $-111.58 |
| regime=unknown + session=overnight/extended + htf=bearish | 5 | 0.0% | $-575.90 | $-115.18 |

---
## Recommendation

### HTF engine flips February from negative to positive

| Config | Trades | PF | PnL | Max DD | Expectancy |
|---|---|---|---|---|---|
| A (no HTF) | 88 | 0.75 | -$1,528 | 4.1% | -$17.36 |
| B (gate=0.7) | 110 | 0.79 | -$1,512 | 4.8% | -$13.75 |
| **C (gate=0.5)** | **90** | **1.13** | **+$686** | **3.1%** | **+$7.62** |
| **D (gate=0.3)** | **84** | **1.29** | **+$1,302** | **2.8%** | **+$15.50** |

**Adopt Config D (gate=0.3)** as the production HTF configuration.

Config D delivers:
- $2,830 PnL improvement over the no-HTF baseline (-$1,528 → +$1,302)
- Kill/save matrix: saves $4,136 in blocked losers, sacrifices $2,634 in blocked winners = +$1,502 net filter value
- Max DD drops from 4.1% to 2.8%
- Win rate climbs from 38.6% to 50.0%

### Additional filters to consider

**Toxic combos to block** (all have expectancy < -$30/trade with 3+ trades):
- `regime=trending_up + htf=bearish`: 7 trades, 28.6% WR, -$236 (-$33.75/trade)
- `session=overnight/extended + htf=bearish`: 21 trades, 33.3% WR, -$745 (-$35.50/trade)
- `regime=unknown + htf=bearish`: 9 trades, 33.3% WR, -$358 (-$39.80/trade)
- `session=afternoon + htf=neutral`: 3 trades, 0% WR, -$335 (-$111.58/trade)

**High-edge combos** (expectancy > $40/trade with 3+ trades):
- `session=overnight + htf=neutral`: 9 trades, 77.8% WR, $775 ($86/trade)
- `regime=ranging + htf=bullish`: 4 trades, 75.0% WR, $277 ($69/trade)
- `regime=ranging + htf=bearish`: 6 trades, 66.7% WR, $327 ($55/trade)
- `regime=trending_down + session=overnight`: 12 trades, 58.3% WR, $591 ($49/trade)

---
*Analysis completed 2026-02-26 17:55 UTC*