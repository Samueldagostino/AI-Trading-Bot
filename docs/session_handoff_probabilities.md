# Session Handoff Conditional Probability Analysis

> **OBSERVATION ONLY** — Not a trading strategy. No trading decisions
> should be based on this until we have 6+ months of observations AND
> statistically significant, reproducible results.

## Data Coverage

- **Period**: 2021-08-31 to 2025-08-31
- **Duration**: 48 months
- **Trading days**: 1249
- **Total sessions**: 5130
- **Status**: USABLE — but verify with live observation

## Methodology

### Session Definitions (all times ET)
| Session | Start | End |
|---------|-------|-----|
| Asia | 18:00 | 02:00 |
| London | 02:00 | 08:00 |
| NY Open | 08:00 | 10:30 |
| NY Core | 10:30 | 15:00 |
| NY Close | 15:00 | 16:00 |

### Session Behavior Classification
| Behavior | Criteria |
|----------|----------|
| STRONG_TREND_UP | Return > +0.3%, close in top 20% of range |
| STRONG_TREND_DOWN | Return < -0.3%, close in bottom 20% of range |
| WEAK_TREND_UP | Return +0.1% to +0.3% |
| WEAK_TREND_DOWN | Return -0.3% to -0.1% |
| RANGE_BOUND | Return -0.1% to +0.1%, range < median |
| SPIKE_REVERSAL | Range > 0.4%, close near open (< 0.1% net) |
| EXPANSION | Range > 1.5x median (high volatility) |

### Handoff Outcome Classification
| Outcome | Criteria |
|---------|----------|
| CONTINUATION | Next session moves same direction |
| REVERSAL | Next session moves opposite > 0.15% |
| RANGE | Next session stays within 0.1% of prev close |

## ASIA → LONDON

| Behavior | CONTINUATION | REVERSAL | RANGE | N | p-value | Significant? |
|----------|-------------|----------|-------|---|---------|-------------|
| STRONG_TREND_UP | 36.5% | 30.8% | 32.7% | 52 | 0.8741 | no |
| STRONG_TREND_DOWN | 33.3% | 36.1% | 30.6% | 36 | 0.9200 | no |
| WEAK_TREND_UP | 31.8% | 26.5% | 41.7% | 223 | 0.0183 | **YES** |
| WEAK_TREND_DOWN | 34.5% | 36.3% | 29.2% | 168 | 0.4984 | no |
| RANGE_BOUND | 33.6% | 26.2% | 40.2% | 229 | 0.0348 | **YES** |
| SPIKE_REVERSAL | 38.9% | 35.2% | 25.9% | 54 | 0.4857 | no |
| EXPANSION | 42.1% | 38.4% | 19.6% | 271 | 0.0000 | **YES** |

<details><summary>95% Confidence Intervals</summary>

| Behavior | CONT CI | REV CI | RANGE CI |
|----------|---------|--------|----------|
| STRONG_TREND_UP | [24.8%, 50.1%] | [19.9%, 44.3%] | [21.5%, 46.2%] |
| STRONG_TREND_DOWN | [20.2%, 49.7%] | [22.5%, 52.4%] | [18.0%, 46.9%] |
| WEAK_TREND_UP | [26.1%, 38.2%] | [21.1%, 32.6%] | [35.4%, 48.3%] |
| WEAK_TREND_DOWN | [27.8%, 42.0%] | [29.4%, 43.8%] | [22.8%, 36.4%] |
| RANGE_BOUND | [27.8%, 40.0%] | [20.9%, 32.3%] | [34.0%, 46.6%] |
| SPIKE_REVERSAL | [27.0%, 52.2%] | [23.8%, 48.5%] | [16.1%, 38.9%] |
| EXPANSION | [36.3%, 48.0%] | [32.8%, 44.3%] | [15.3%, 24.7%] |

</details>

### Survivorship Bias Test
- Split date: 2023-08-29
- First half: N=517, p=0.0027
- Second half: N=516, p=0.0865
- **Consistent edge**: NO

### Regime Test (High-Vol vs Low-Vol)
- High-vol: N=508, p=0.0001
- Low-vol: N=525, p=0.0069
- **Consistent edge**: YES

## LONDON → NY_OPEN

| Behavior | CONTINUATION | REVERSAL | RANGE | N | p-value | Significant? |
|----------|-------------|----------|-------|---|---------|-------------|
| STRONG_TREND_UP | 42.7% | 35.4% | 22.0% | 82 | 0.0659 | no |
| STRONG_TREND_DOWN | 31.4% | 47.1% | 21.6% | 51 | 0.0797 | no |
| WEAK_TREND_UP | 42.9% | 34.5% | 22.7% | 238 | 0.0007 | **YES** |
| WEAK_TREND_DOWN | 43.5% | 33.9% | 22.6% | 168 | 0.0042 | **YES** |
| RANGE_BOUND | 36.0% | 31.3% | 32.7% | 150 | 0.7711 | no |
| SPIKE_REVERSAL | 53.5% | 32.3% | 14.1% | 99 | 0.0000 | **YES** |
| EXPANSION | 46.1% | 42.9% | 11.0% | 245 | 0.0000 | **YES** |

<details><summary>95% Confidence Intervals</summary>

| Behavior | CONT CI | REV CI | RANGE CI |
|----------|---------|--------|----------|
| STRONG_TREND_UP | [32.5%, 53.5%] | [25.9%, 46.2%] | [14.4%, 32.1%] |
| STRONG_TREND_DOWN | [20.3%, 45.0%] | [34.1%, 60.5%] | [12.5%, 34.6%] |
| WEAK_TREND_UP | [36.7%, 49.2%] | [28.7%, 40.7%] | [17.8%, 28.4%] |
| WEAK_TREND_DOWN | [36.2%, 51.0%] | [27.2%, 41.4%] | [16.9%, 29.5%] |
| RANGE_BOUND | [28.8%, 43.9%] | [24.5%, 39.1%] | [25.7%, 40.5%] |
| SPIKE_REVERSAL | [43.8%, 63.0%] | [23.9%, 42.0%] | [8.6%, 22.3%] |
| EXPANSION | [40.0%, 52.4%] | [36.8%, 49.1%] | [7.7%, 15.6%] |

</details>

### Survivorship Bias Test
- Split date: 2023-08-30
- First half: N=517, p=0.0000
- Second half: N=516, p=0.0000
- **Consistent edge**: YES

### Regime Test (High-Vol vs Low-Vol)
- High-vol: N=623, p=0.0000
- Low-vol: N=410, p=0.0105
- **Consistent edge**: YES

## NY_OPEN → NY_CORE

| Behavior | CONTINUATION | REVERSAL | RANGE | N | p-value | Significant? |
|----------|-------------|----------|-------|---|---------|-------------|
| STRONG_TREND_UP | 51.7% | 27.6% | 20.7% | 145 | 0.0000 | **YES** |
| STRONG_TREND_DOWN | 46.3% | 33.3% | 20.3% | 123 | 0.0019 | **YES** |
| WEAK_TREND_UP | 49.2% | 34.3% | 16.6% | 181 | 0.0000 | **YES** |
| WEAK_TREND_DOWN | 37.2% | 40.9% | 21.9% | 215 | 0.0014 | **YES** |
| RANGE_BOUND | 40.9% | 15.9% | 43.2% | 44 | 0.0487 | **YES** |
| SPIKE_REVERSAL | 41.2% | 34.2% | 24.6% | 114 | 0.0912 | no |
| EXPANSION | 51.2% | 37.3% | 11.5% | 209 | 0.0000 | **YES** |

<details><summary>95% Confidence Intervals</summary>

| Behavior | CONT CI | REV CI | RANGE CI |
|----------|---------|--------|----------|
| STRONG_TREND_UP | [43.7%, 59.7%] | [21.0%, 35.4%] | [14.9%, 28.0%] |
| STRONG_TREND_DOWN | [37.8%, 55.1%] | [25.6%, 42.1%] | [14.2%, 28.3%] |
| WEAK_TREND_UP | [42.0%, 56.4%] | [27.7%, 41.4%] | [11.9%, 22.7%] |
| WEAK_TREND_DOWN | [31.0%, 43.8%] | [34.6%, 47.6%] | [16.9%, 27.9%] |
| RANGE_BOUND | [27.7%, 55.6%] | [7.9%, 29.4%] | [29.7%, 57.8%] |
| SPIKE_REVERSAL | [32.6%, 50.4%] | [26.1%, 43.3%] | [17.6%, 33.2%] |
| EXPANSION | [44.5%, 57.9%] | [31.0%, 44.1%] | [7.8%, 16.5%] |

</details>

### Survivorship Bias Test
- Split date: 2023-08-30
- First half: N=516, p=0.0000
- Second half: N=515, p=0.0000
- **Consistent edge**: YES

### Regime Test (High-Vol vs Low-Vol)
- High-vol: N=623, p=0.0000
- Low-vol: N=408, p=0.0039
- **Consistent edge**: YES

## NY_CORE → NY_CLOSE

| Behavior | CONTINUATION | REVERSAL | RANGE | N | p-value | Significant? |
|----------|-------------|----------|-------|---|---------|-------------|
| STRONG_TREND_UP | 36.4% | 17.0% | 46.7% | 165 | 0.0000 | **YES** |
| STRONG_TREND_DOWN | 46.1% | 21.1% | 32.9% | 76 | 0.0283 | **YES** |
| WEAK_TREND_UP | 34.0% | 20.4% | 45.5% | 191 | 0.0001 | **YES** |
| WEAK_TREND_DOWN | 36.4% | 25.5% | 38.0% | 184 | 0.0782 | no |
| RANGE_BOUND | 40.0% | 20.0% | 40.0% | 20 *low-N* | 0.4493 | no |
| SPIKE_REVERSAL | 29.9% | 18.7% | 51.4% | 107 | 0.0001 | **YES** |
| EXPANSION | 44.0% | 33.7% | 22.2% | 252 | 0.0001 | **YES** |

<details><summary>95% Confidence Intervals</summary>

| Behavior | CONT CI | REV CI | RANGE CI |
|----------|---------|--------|----------|
| STRONG_TREND_UP | [29.4%, 43.9%] | [12.0%, 23.4%] | [39.2%, 54.3%] |
| STRONG_TREND_DOWN | [35.3%, 57.2%] | [13.4%, 31.5%] | [23.4%, 44.1%] |
| WEAK_TREND_UP | [27.7%, 41.0%] | [15.3%, 26.7%] | [38.6%, 52.6%] |
| WEAK_TREND_DOWN | [29.8%, 43.6%] | [19.8%, 32.3%] | [31.3%, 45.2%] |
| RANGE_BOUND | [21.9%, 61.3%] | [8.1%, 41.6%] | [21.9%, 61.3%] |
| SPIKE_REVERSAL | [22.1%, 39.2%] | [12.4%, 27.1%] | [42.0%, 60.7%] |
| EXPANSION | [38.1%, 50.2%] | [28.2%, 39.8%] | [17.5%, 27.8%] |

</details>

### Survivorship Bias Test
- Split date: 2023-08-29
- First half: N=498, p=0.0000
- Second half: N=497, p=0.0000
- **Consistent edge**: YES

### Regime Test (High-Vol vs Low-Vol)
- High-vol: N=620, p=0.0000
- Low-vol: N=375, p=0.0000
- **Consistent edge**: YES

## ASIA → NY_OPEN

| Behavior | CONTINUATION | REVERSAL | RANGE | N | p-value | Significant? |
|----------|-------------|----------|-------|---|---------|-------------|
| STRONG_TREND_UP | 34.6% | 50.0% | 15.4% | 52 | 0.0092 | **YES** |
| STRONG_TREND_DOWN | 47.2% | 38.9% | 13.9% | 36 | 0.0388 | **YES** |
| WEAK_TREND_UP | 39.0% | 33.6% | 27.4% | 223 | 0.1025 | no |
| WEAK_TREND_DOWN | 47.6% | 39.3% | 13.1% | 168 | 0.0000 | **YES** |
| RANGE_BOUND | 37.6% | 30.6% | 31.9% | 229 | 0.3877 | no |
| SPIKE_REVERSAL | 55.6% | 31.5% | 13.0% | 54 | 0.0006 | **YES** |
| EXPANSION | 48.3% | 36.9% | 14.8% | 271 | 0.0000 | **YES** |

<details><summary>95% Confidence Intervals</summary>

| Behavior | CONT CI | REV CI | RANGE CI |
|----------|---------|--------|----------|
| STRONG_TREND_UP | [23.2%, 48.2%] | [36.9%, 63.1%] | [8.0%, 27.5%] |
| STRONG_TREND_DOWN | [32.0%, 63.0%] | [24.8%, 55.1%] | [6.1%, 28.7%] |
| WEAK_TREND_UP | [32.8%, 45.5%] | [27.8%, 40.1%] | [21.9%, 33.6%] |
| WEAK_TREND_DOWN | [40.2%, 55.1%] | [32.2%, 46.8%] | [8.8%, 19.0%] |
| RANGE_BOUND | [31.5%, 44.0%] | [25.0%, 36.8%] | [26.2%, 38.2%] |
| SPIKE_REVERSAL | [42.4%, 68.0%] | [20.7%, 44.7%] | [6.4%, 24.4%] |
| EXPANSION | [42.5%, 54.3%] | [31.4%, 42.8%] | [11.0%, 19.5%] |

</details>

### Survivorship Bias Test
- Split date: 2023-08-29
- First half: N=517, p=0.0000
- Second half: N=516, p=0.0000
- **Consistent edge**: YES

### Regime Test (High-Vol vs Low-Vol)
- High-vol: N=508, p=0.0000
- Low-vol: N=525, p=0.0000
- **Consistent edge**: YES

## Selection Bias Test

Compares volatility in first 30 minutes of session opens vs random 30-minute windows.

- Session open mean range: 0.00272
- Random window mean range: 0.00208
- Ratio: 1.31x
- Mann-Whitney U p-value: 0.000000
- **Verdict**: SESSION OPENS HAVE SIGNIFICANTLY HIGHER VOLATILITY

## Transaction Cost Analysis

- Round-trip cost: $1.24 per contract (MNQ)
- MNQ point value: $2.00
- Minimum edge needed: 0.62 points (~0.003% at NQ 20000)

## Verdict

**STATISTICALLY SIGNIFICANT CELLS FOUND (p < 0.05)**

However, statistical significance does NOT equal a trading edge.
Before acting on any finding:
1. Check effect sizes (Cramér's V) — are deviations large enough to trade?
2. Check survivorship test — does the edge persist in both halves?
3. Check regime test — does the edge persist in both vol regimes?
4. After $1.24 round-trip costs, is there still positive expectancy?
5. Collect 6+ months of live observation before any implementation.

---
*Generated by SessionHandoffAnalyzer — research/observation only.*