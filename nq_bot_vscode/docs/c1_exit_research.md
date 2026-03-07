# C1 Exit Strategy Research

**Generated:** 2026-02-27 01:33 UTC
**Data:** FirstRate 1m absolute-adjusted NQ, Sep 2025 – Feb 2026
**Config:** D (HC ON, HTF gate=0.3, 2m exec)
**Baseline C1:** TP1 = 1.5× stop (current production)
**Total Entries Captured:** 751 trades (from full pipeline run)

## Methodology

Split-phase backtest:
1. **Phase 1** — Full Config D pipeline (features + signals + HC gates + risk) run once to capture all trade entries and 2m bar data.
2. **Phase 2** — For each experiment, exit logic replayed on captured trades. Entry signals identical across all experiments. Only C1 exit strategy varies.

> **Note:** C1 exit timing affects C2 breakeven placement, so C2 PnL may vary slightly between experiments. This is expected and correctly modeled.

---

## Master Comparison

| # | Experiment | Trades | WR% | PF | C1 PnL | C2 PnL | Total PnL | Exp/Trade | Max DD |
|---|------------|--------|-----|-----|--------|--------|-----------|-----------|--------|
| 1 | C: Pure Runner | 751 | 54.9 | **1.40** | $+6,253 | $+6,253 | $+12,506 (+$6,728) | $16.65 | 5.9% |
| 2 | E: BE Step | 751 | 38.6 | **1.38** | $+3,657 | $+8,629 | $+12,287 (+$6,508) | $16.36 | 7.1% |
| 3 | B: 10 bars | 751 | 70.4 | **1.59** | $+2,736 | $+8,368 | $+11,104 (+$5,326) | $14.79 | 4.2% |
| 4 | B: 5 bars | 751 | 77.4 | **1.81** | $+2,551 | $+8,454 | $+11,005 (+$5,227) | $14.65 | 3.0% |
| 5 | B: 15 bars | 751 | 64.8 | **1.47** | $+2,530 | $+8,168 | $+10,698 (+$4,920) | $14.25 | 4.9% |
| 6 | B: 20 bars | 751 | 60.1 | **1.37** | $+1,655 | $+7,879 | $+9,534 (+$3,756) | $12.69 | 5.3% |
| 7 | A: 2.5x stop | 751 | 36.0 | **1.21** | $+1,212 | $+8,129 | $+9,341 (+$3,563) | $12.44 | 11.6% |
| 8 | A: 2.0x stop | 751 | 40.6 | **1.21** | $+719 | $+7,693 | $+8,412 (+$2,634) | $11.20 | 8.3% |
| 9 | A: 1.75x stop | 751 | 44.2 | **1.21** | $+1,049 | $+7,019 | $+8,068 (+$2,290) | $10.74 | 9.6% |
| 10 | B: 30 bars | 751 | 54.3 | **1.25** | $+566 | $+6,986 | $+7,552 (+$1,773) | $10.06 | 6.1% |
| 11 | A: 1.5x stop | 751 | 46.6 | **1.16** | $-458 | $+6,216 | $+5,759 | $7.67 | 10.2% |
| 12 | D: 0.5x scalp | 751 | 69.1 | **1.26** | $-2,054 | $+7,638 | $+5,584 | $7.44 | 6.5% |
| 13 | A: 1.0x stop | 751 | 54.9 | **1.13** | $-2,016 | $+6,253 | $+4,237 | $5.64 | 11.5% |
| 14 | A: 1.25x stop | 751 | 49.0 | **1.11** | $-2,229 | $+6,100 | $+3,871 | $5.15 | 12.1% |
| — | *Baseline (current prod)* | 748 | 46.7 | **1.15** | $-904 | $+6,682 | $+5,778 | $7.72 | — |

---

## Experiment A — Vary C1 Target Ratio

C1 TP1 = {ratio} × stop distance. C2 runner unchanged.

| Ratio | Trades | WR% | PF | C1 PnL | C2 PnL | Total PnL | Exp | vs Baseline |
|-------|--------|-----|-----|--------|--------|-----------|-----|-------------|
| A: 1.0x stop | 751 | 54.9 | 1.13 | $-2,016 | $+6,253 | $+4,237 | $5.64 | $-1,541 |
| A: 1.25x stop | 751 | 49.0 | 1.11 | $-2,229 | $+6,100 | $+3,871 | $5.15 | $-1,907 |
| A: 1.5x stop **(current)** | 751 | 46.6 | 1.16 | $-458 | $+6,216 | $+5,759 | $7.67 | $-20 |
| A: 1.75x stop | 751 | 44.2 | 1.21 | $+1,049 | $+7,019 | $+8,068 | $10.74 | $+2,290 |
| A: 2.0x stop | 751 | 40.6 | 1.21 | $+719 | $+7,693 | $+8,412 | $11.20 | $+2,634 |
| A: 2.5x stop | 751 | 36.0 | 1.21 | $+1,212 | $+8,129 | $+9,341 | $12.44 | $+3,563 |

## Experiment B — Time-Based C1 Exit

Exit C1 at market after N bars if profitable. Fallback: 1.5× target or stop.

| Bars | Trades | WR% | PF | C1 PnL | C2 PnL | Total PnL | Exp | vs Baseline |
|------|--------|-----|-----|--------|--------|-----------|-----|-------------|
| B: 5 bars | 751 | 77.4 | 1.81 | $+2,551 | $+8,454 | $+11,005 | $14.65 | $+5,227 |
| B: 10 bars | 751 | 70.4 | 1.59 | $+2,736 | $+8,368 | $+11,104 | $14.79 | $+5,326 |
| B: 15 bars | 751 | 64.8 | 1.47 | $+2,530 | $+8,168 | $+10,698 | $14.25 | $+4,920 |
| B: 20 bars | 751 | 60.1 | 1.37 | $+1,655 | $+7,879 | $+9,534 | $12.69 | $+3,756 |
| B: 30 bars | 751 | 54.3 | 1.25 | $+566 | $+6,986 | $+7,552 | $10.06 | $+1,773 |

## Experiment C — No C1 Target (Pure Runner)

Both contracts trail with ATR-based trailing stop. Move to BE at 1× stop profit.
Both legs close on the same trailing stop.

| Metric | Value |
|--------|-------|
| Trades | 751 |
| Win Rate | 54.9% |
| Profit Factor | **1.40** |
| C1 PnL | $+6,253 |
| C2 PnL | $+6,253 |
| Total PnL | $+12,506 |
| Expectancy | $16.65 / trade |
| vs Baseline | $+6,728 |
| Max DD | 5.9% |

## Experiment D — Aggressive C1 Scalp (0.5× stop)

C1 target = 0.5× stop. Quick lock-in, then C2 trails.

| Metric | Value |
|--------|-------|
| Trades | 751 |
| Win Rate | 69.1% |
| Profit Factor | **1.26** |
| C1 PnL | $-2,054 |
| C2 PnL | $+7,638 |
| Total PnL | $+5,584 |
| Expectancy | $7.44 / trade |
| vs Baseline | $-194 |
| Max DD | 6.5% |

## Experiment E — Breakeven C1 (Step Exit)

At 1.0× stop in profit: move C1 to BE. At 2.0× stop in profit: exit C1 at market.

| Metric | Value |
|--------|-------|
| Trades | 751 |
| Win Rate | 38.6% |
| Profit Factor | **1.38** |
| C1 PnL | $+3,657 |
| C2 PnL | $+8,629 |
| Total PnL | $+12,287 |
| Expectancy | $16.36 / trade |
| vs Baseline | $+6,508 |
| Max DD | 7.1% |

---

## Monthly Breakdown — Top 3 Configurations

Verifying consistency across all market regimes.

### C: Pure Runner

| Month | Trades | WR% | PF | C1 PnL | C2 PnL | Total PnL |
|-------|--------|-----|-----|--------|--------|-----------|
| 2025-09 | 135 | 45.9 | 0.87 | $-334 | $-334 | $-669 |
| 2025-10 | 140 | 52.9 | 1.14 | $+495 | $+495 | $+990 |
| 2025-11 | 96 | 59.4 | 1.60 | $+1,285 | $+1,285 | $+2,570 |
| 2025-12 | 149 | 53.0 | 1.40 | $+1,213 | $+1,213 | $+2,427 |
| 2026-01 | 129 | 59.7 | 1.96 | $+2,466 | $+2,466 | $+4,933 |
| 2026-02 | 102 | 61.8 | 1.56 | $+1,128 | $+1,128 | $+2,256 |
| **Total** | | | | | | **$+12,506** |

Profitable months: **5/6**

### E: BE Step

| Month | Trades | WR% | PF | C1 PnL | C2 PnL | Total PnL |
|-------|--------|-----|-----|--------|--------|-----------|
| 2025-09 | 135 | 32.6 | 0.87 | $-619 | $-34 | $-654 |
| 2025-10 | 140 | 41.4 | 1.11 | $+80 | $+692 | $+772 |
| 2025-11 | 96 | 37.5 | 1.69 | $+886 | $+2,184 | $+3,070 |
| 2025-12 | 149 | 38.9 | 1.44 | $+912 | $+1,795 | $+2,707 |
| 2026-01 | 129 | 42.6 | 1.90 | $+1,711 | $+3,110 | $+4,821 |
| 2026-02 | 102 | 38.2 | 1.38 | $+687 | $+883 | $+1,570 |
| **Total** | | | | | | **$+12,287** |

Profitable months: **5/6**

### B: 10 bars

| Month | Trades | WR% | PF | C1 PnL | C2 PnL | Total PnL |
|-------|--------|-----|-----|--------|--------|-----------|
| 2025-09 | 135 | 61.5 | 0.80 | $-439 | $-259 | $-698 |
| 2025-10 | 140 | 70.7 | 1.42 | $+130 | $+1,461 | $+1,591 |
| 2025-11 | 96 | 78.1 | 2.43 | $+1,048 | $+1,827 | $+2,875 |
| 2025-12 | 149 | 64.4 | 1.28 | $-152 | $+1,176 | $+1,024 |
| 2026-01 | 129 | 76.0 | 2.18 | $+1,109 | $+2,762 | $+3,871 |
| 2026-02 | 102 | 76.5 | 2.02 | $+1,040 | $+1,401 | $+2,441 |
| **Total** | | | | | | **$+11,104** |

Profitable months: **5/6**

---

## Recommendation

### Best by Total PnL
**C: Pure Runner** — $+12,506 (PF 1.40, 54.9% WR)

### Best by Profit Factor
**B: 5 bars** — PF 1.81 ($+11,005)

### Configurations Beating 6-Month Baseline ($5,778)

- **C: Pure Runner**: $+12,506 (+$6,728, PF 1.40)
- **E: BE Step**: $+12,287 (+$6,508, PF 1.38)
- **B: 10 bars**: $+11,104 (+$5,326, PF 1.59)
- **B: 5 bars**: $+11,005 (+$5,227, PF 1.81)
- **B: 15 bars**: $+10,698 (+$4,920, PF 1.47)
- **B: 20 bars**: $+9,534 (+$3,756, PF 1.37)
- **A: 2.5x stop**: $+9,341 (+$3,563, PF 1.21)
- **A: 2.0x stop**: $+8,412 (+$2,634, PF 1.21)
- **A: 1.75x stop**: $+8,068 (+$2,290, PF 1.21)
- **B: 30 bars**: $+7,552 (+$1,773, PF 1.25)

### Key Insights

**C1 strategies that turn profitable:**
- C: Pure Runner: C1 PnL $+6,253
- E: BE Step: C1 PnL $+3,657
- B: 10 bars: C1 PnL $+2,736
- B: 5 bars: C1 PnL $+2,551
- B: 15 bars: C1 PnL $+2,530
- B: 20 bars: C1 PnL $+1,655
- A: 2.5x stop: C1 PnL $+1,212
- A: 1.75x stop: C1 PnL $+1,049
- A: 2.0x stop: C1 PnL $+719
- B: 30 bars: C1 PnL $+566

**Minimum C1 drag:** A: 1.5x stop (C1 PnL $-458)

---

*Generated by `scripts/c1_exit_experiments.py` (split-phase optimized) — 2026-02-27 01:33 UTC*