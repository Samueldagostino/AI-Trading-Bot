# Trade Profile Analysis — 607-Trade Structural Stop Dataset

**Generated:** 2026-03-03 21:44 UTC
**Dataset:** Period 4 (Sep 2023 - Nov 2023) — Structural Stop Placement
**Total Trades:** 607
**Net PnL:** $5,130.76
**Win Rate:** 50.1%
**Profit Factor:** 1.39

## 1. Trade Outcome Profile

### By Direction
```
  Direction  Count    WR%    PF  Avg PnL  Total PnL
  ---------  -----  -----  ----  -------  ---------
  long         357  50.4%  1.44    $9.67   $3451.40
  short        250  49.6%  1.32    $6.72   $1679.36

```

### By Signal Source
```
  Direction                      Count    WR%    PF  Avg PnL  Total PnL
  -----------------------------  -----  -----  ----  -------  ---------
  confluence                        33  42.4%  0.99   $-0.25     $-8.16
  signal                           471  50.7%  1.34    $7.31   $3443.82
  sweep                             99  48.5%  1.74   $16.70   $1653.20
  ucl_confirmed_wide_stop_sweep      4  75.0%   1.5   $10.48     $41.90

```

### By HC Score Bucket
```
  Direction  Count    WR%    PF  Avg PnL  Total PnL
  ---------  -----  -----  ----  -------  ---------
  0.75-0.80    383  48.0%  1.32    $7.16   $2744.18
  0.80-0.85    202  53.5%  1.62   $12.16   $2456.60
  0.85-0.90     17  52.9%  0.75   $-5.61    $-95.34
  0.90+          5  60.0%  1.25    $5.06     $25.32

```

### By Regime
```
  Direction        Count    WR%    PF  Avg PnL  Total PnL
  ---------------  -----  -----  ----  -------  ---------
  high_volatility     58  48.3%  0.72   $-7.33   $-425.14
  low_liquidity        2   0.0%   0.0  $-21.08    $-42.16
  ranging            102  57.8%  2.28   $21.74   $2217.20
  trending_down      139  46.0%  1.04    $1.00    $138.68
  trending_up        151  47.7%  1.33    $7.18   $1084.92
  unknown            155  52.3%  1.69   $13.92   $2157.26

```

### By HTF Bias
```
  Direction  Count    WR%    PF  Avg PnL  Total PnL
  ---------  -----  -----  ----  -------  ---------
  bearish      214  49.1%  1.21    $4.65    $995.88
  bullish      275  50.5%   1.7   $15.35   $4220.18
  neutral      118  50.8%  0.97   $-0.72    $-85.30

```

## 2. Time-Based Analysis

### By Hour of Day (ET)
```
  Hour   Count    WR%    PF  Avg PnL  Total PnL
  -----  -----  -----  ----  -------  ---------
  00:00     36  55.6%  1.96   $16.91    $608.82
  01:00     19  47.4%  0.59   $-6.46   $-122.72
  02:00     24  41.7%  1.26    $5.42    $130.12
  03:00     46  54.3%  2.58   $22.62   $1040.58
  04:00     50  62.0%  1.35    $6.95    $347.58
  05:00     62  48.4%  0.89   $-3.42   $-211.82
  06:00     60  56.7%  2.09   $30.39   $1823.16
  07:00     36  55.6%  1.68   $16.68    $600.38
  08:00     28  35.7%  1.21    $8.53    $238.84
  09:00     37  45.9%  1.09    $1.79     $66.10
  10:00     24  58.3%  2.53   $34.32    $823.70
  11:00     27  40.7%  0.77   $-5.14   $-138.76
  12:00     18  61.1%  1.38    $7.62    $137.22
  13:00      4  50.0%  4.19   $29.67    $118.68
  14:00     17  52.9%  1.37    $4.64     $78.94
  15:00      5  20.0%  0.03  $-21.16   $-105.82
  16:00      6  33.3%  0.88   $-3.22    $-19.34
  18:00     13  30.8%  0.21  $-13.16   $-171.04
  19:00      5  40.0%  0.29  $-11.58    $-57.90
  20:00      4  25.0%  0.31  $-17.90    $-71.62
  21:00     13  30.8%  1.54    $8.40    $109.26
  22:00     22  36.4%   1.1    $1.41     $30.96
  23:00     51  56.9%  0.83   $-2.44   $-124.56

```

### By Day of Week
```
  Hour       Count    WR%    PF  Avg PnL  Total PnL
  ---------  -----  -----  ----  -------  ---------
  Monday       110  47.3%  1.38    $8.30    $913.46
  Tuesday      120  50.0%  1.55   $10.96   $1314.82
  Wednesday    124  49.2%  1.31    $5.77    $715.18
  Thursday     122  51.6%  1.19    $5.22    $636.70
  Friday       106  51.9%  1.51   $11.77   $1248.10
  Sunday        25  52.0%  1.97   $12.10    $302.50

```

### By Session
```
  Session    Count    WR%    PF           Avg PnL  Total PnL
  ---------  -----  -----  ----  ----------------  ---------
  overnight    469  50.5%   1.4             $8.89   $4170.04
  morning       88  47.7%  1.39             $8.53    $751.04
  lunch         22  59.1%  1.65            $11.63    $255.90
  afternoon     28  42.9%   0.9  $-1.65 ** NEG **    $-46.22

```

## 3. Volatility Regime Analysis (by Stop Distance)

```
  Stop Bucket          Count     WR%      PF  Avg PnL  Total PnL  Avg Stop
  -------------------  -----  ------  ------  -------  ---------  --------
  tight (<5pt)           106   37.7%    1.15    $2.63    $279.16     3.0pt
  normal (5-10pt)        374   50.5%    1.34    $7.27   $2719.50     7.0pt
  moderate (10-15pt)      91   51.6%    1.28    $8.12    $739.32    11.8pt
  wide (15-20pt)          33   75.8%    2.35   $29.92    $987.52    16.5pt
  very_wide (20-30pt)      3  100.0%  999.99  $135.09    $405.26    22.4pt

```

## 4. Drawdown Deep Dive

- **Starting Equity:** $25,000.00
- **Final Equity:** $30,130.76
- **Max Drawdown:** $1,392.04 (5.57%)
- **Peak Timestamp:** 2023-11-14T08:34:00+00:00
- **Trough Timestamp:** 2023-09-22T10:26:00+00:00

- **Max Consecutive Wins:** 7 trades ($+494.84)
- **Max Consecutive Losses:** 9 trades ($-194.22)
- **Rolling 20-Trade WR:** min=20.0%, max=75.0%, avg=50.2%
- **Worst Day:** 2023-10-19 — 9 trades, $-557.16
- **Worst Week:** 2023-W38 — 52 trades, $-766.74
- **Drawdown Recovery:** 41 trades from trough to new high

## 5. C1 vs C2 Performance Split

```
  Metric             C1 (Trail)  C2 (Runner)
  -----------------  ----------  -----------
  Total PnL           $2,022.47    $3,108.29
  Win Rate                53.0%        22.6%
  Avg PnL/Trade           $3.33        $5.12
  Avg Winner             $26.39       $72.98
  Avg Loser             $-22.73      $-14.66
  % of Total Profit       39.4%        60.6%

```

### C2 Exit Breakdown
```
  Exit Reason  Count      %  Avg C2 PnL
  -----------  -----  -----  ----------
  breakeven      219  36.1%      $-1.12
  trailing       124  20.4%      $56.93
  stop           246  40.5%     $-26.17
  max_target       8   1.3%     $314.77
  emergency       10   1.6%      $21.31

```

### C2 Runner R-Multiple Distribution
```
  R-Multiple  Count  % of All Trades
  ----------  -----  ---------------
  >1R           105            17.3%
  >2R            87            14.3%
  >3R            65            10.7%
  >5R            41             6.8%

```

## 6. Edge Concentration Analysis

**Edge Classification:** FAT_TAIL_DEPENDENT

### Top-N% Profit Contribution
```
  Segment    Trades         PnL  % of Total Profit
  ---------  ------  ----------  -----------------
  Top 5Pct       30   $8,990.98             175.2%
  Top 10Pct      60  $12,351.48             240.7%
  Top 20Pct     121  $15,895.14             309.8%
  Top 30Pct     182  $17,506.06             341.2%
  Top 50Pct     303  $18,296.58             356.6%

```

### Robustness: Remove Top-N Trades
```
  Scenario        Remaining         PnL  Profitable?    WR%    PF
  --------------  ---------  ----------  -----------  -----  ----
  Without Top 5         602   $2,504.24          YES  49.7%  1.19
  Without Top 10        597     $776.58          YES  49.2%  1.06
  Without Top 20        587  $-1,862.02           NO  48.4%  0.86
  Without Top 30        577  $-3,860.22           NO  47.5%  0.71

```

## 7. Optimal Filter Identification

**Baseline:** 607 trades, WR 50.1%, PF 1.39, PnL $5,130.76, MaxDD 5.57%

### FILTER IMPACT TABLE (ranked by efficiency)
```
  Filter                               Removed  New WR  New PF    New PnL  New MaxDD  $/Trade Removed
  -----------------------------------  -------  ------  ------  ---------  ---------  ---------------
  No longs when HTF=bearish                 10   50.6%    1.42  $5,402.06      5.05%          $+27.13
  No trades in high_volatility regime       58   50.3%    1.48  $5,555.90      4.53%           $+7.33
  No trades during afternoon session        28   50.4%    1.41  $5,176.98      5.36%           $+1.65
  No longs when HTF=neutral                 82   49.9%    1.47  $5,255.54      5.14%           $+1.52
  No confluence trades                      33   50.5%    1.42  $5,138.92      5.54%           $+0.25

```

## Key Findings

1. **Best Direction:** long (PF=1.44, WR=50.4%, n=357)
2. **Best Signal Source:** sweep (PF=1.74, WR=48.5%, n=99)
3. **Best HC Bucket:** 0.80-0.85 (PF=1.62, WR=53.5%, n=202)
4. **Best Session:** lunch (PF=1.65, WR=59.1%, n=22)
5. **C2 Runner Contribution:** 60.6% of total profit ($3,108.29)
6. **Edge Type:** FAT_TAIL_DEPENDENT
   Top 10% of trades produce 240.7% of profit
7. **Best Filter:** "No longs when HTF=bearish" — removes 10 trades, improves PnL by $+271.30

---
*Analysis generated from structural stop backtest results (period_4).*