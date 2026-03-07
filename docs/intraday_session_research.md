# Intraday Session Microstructure Research
## MNQ/NQ Trade Timing Optimization

**Date:** 2026-03-04
**Scope:** Empirically documented intraday microstructure patterns relevant to MNQ/NQ futures trading
**Data Available:** OHLCV bars (1m–1D), VIX daily, overnight session data via IBKR

---

## Table of Contents

1. [Time-of-Day Effects on Futures](#1-time-of-day-effects-on-futures)
2. [Initial Balance Theory](#2-initial-balance-theory)
3. [Volume Profile & Value Area](#3-volume-profile--value-area)
4. [Overnight Session Structure](#4-overnight-session-structure)
5. [VWAP Reversion in Futures](#5-vwap-reversion-in-futures)
6. [TICK Index Divergence](#6-tick-index-divergence)
7. [Foreign Institutional Flow Patterns](#7-foreign-institutional-flow-patterns)
8. [Volatility Clustering and Regime Persistence](#8-volatility-clustering-and-regime-persistence)
9. [Implementation Summary Matrix](#9-implementation-summary-matrix)

---

## 1. Time-of-Day Effects on Futures

### Mechanism

Intraday returns and volatility in equity index futures exhibit strong, persistent time-of-day patterns driven by institutional order flow concentration. These patterns arise from the interaction of informed traders, market makers, and scheduled liquidity events throughout the trading day.

**Opening Drive (9:30–10:00 ET):**
The first 30 minutes see the highest volatility and volume of the day. Overnight information is incorporated, institutional portfolio rebalancing orders cluster at the open, and market-on-open (MOO) orders execute. The bid-ask spread narrows rapidly as market makers compete, but price impact per unit of volume remains elevated. For MNQ/NQ, this window typically produces 25–35% of the day's total range.

**Initial Balance (9:30–10:30 ET):**
The first 60 minutes establish the day's "initial balance" — a reference range that predicts the character of the trading day. Narrow IB ranges (relative to recent history) signal potential for range expansion (trend days), while wide IB ranges signal range-bound activity.

**European Close Effect (~11:30 ET):**
European equity markets close at 11:30 ET (17:30 CET during winter). This creates a liquidity withdrawal event as European market makers and prop desks square positions. NQ often experiences a brief volatility spike or directional move around this time as cross-market hedging flows hit the book.

**Lunch Lull (12:00–13:30 ET):**
Volume drops 40–60% from morning levels. Spreads widen. Market making becomes less competitive. Price action shifts toward mean-reversion as directional conviction fades. New position initiation during this window has historically lower expected value due to poor fills and false signals.

**MOC/LOC Imbalance Window (15:00–16:00 ET):**
Market-on-close and limit-on-close orders from institutional rebalancing (index funds, ETF creation/redemption) create predictable volume surges. The NYSE publishes MOC imbalance data at 15:50 ET, creating a brief but tradeable information event. NQ futures absorb spillover flow from SPX rebalancing.

**Post-Close (16:00–18:00 ET):**
Liquidity drops dramatically. Spreads widen. This is a dead zone for new position initiation.

### References

- Admati, A. R. & Pfleiderer, P. (1988). "A Theory of Intraday Patterns: Volume and Price Variability." *Review of Financial Studies*, 1(1), 3–40.
  - Establishes the theoretical framework: informed traders cluster in high-volume periods, which attract liquidity traders, creating a self-reinforcing U-shaped volume pattern.
- Heston, S. L., Korajczyk, R. A. & Sadka, R. (2010). "Intraday Patterns in the Cross-Section of Stock Returns." *Journal of Finance*, 65(4), 1369–1407.
  - Documents persistent half-hour return patterns in US equities that repeat across days, driven by institutional trading schedules.
- Wood, R. A., McInish, T. H. & Ord, J. K. (1985). "An Investigation of Transactions Data for NYSE Stocks." *Journal of Finance*, 40(3), 723–739.
  - Early empirical documentation of the U-shaped intraday volume and volatility pattern.

### Implementability with Current Data

**HIGH** — OHLCV bars at 1m or 5m resolution provide full coverage of RTH session phases. Volume data allows direct measurement of time-of-day volume profiles. No additional data sources required.

### Impact Rating: **HIGH**

Position sizing by session phase is a first-order risk management improvement. Avoiding lunch-lull entries and sizing up during high-conviction windows (opening drive, MOC) directly improves expected per-trade PnL and reduces noise trades.

---

## 2. Initial Balance Theory

### Mechanism

The Initial Balance (IB) is the price range established during the first 30–60 minutes of Regular Trading Hours (RTH, 9:30–10:30 ET). Developed by J. Peter Steidlmayer as part of the Market Profile framework, IB serves as the day's "opening auction" that reveals the balance between buyers and sellers.

**IB Range as Day-Type Predictor:**
- **Narrow IB** (range < 20th percentile of recent history): Indicates lack of strong directional conviction at the open. High probability of range expansion — the day will likely be a "trend day" where price breaks out of IB and extends directionally. Look for breakout entries.
- **Normal IB** (20th–80th percentile): No strong day-type signal. Standard approach.
- **Wide IB** (range > 80th percentile): Early aggressive activity has already established a wide range. High probability of range-bound (rotational) day. Look for mean-reversion entries at IB extremes.

**IB Break Follow-Through:**
When price breaks above IB high or below IB low, the break has a 65–70% probability of following through for at least one IB range extension. This is one of the highest-probability setups in intraday futures trading.

**Day Type Classification (Dalton Framework):**
- **Normal Day:** 85% of the day's range established in IB. Activity confined to IB.
- **Normal Variation Day:** Range extends to ~1.25x IB in one direction.
- **Trend Day:** Range extends to 2x+ IB, often in one direction with no meaningful pullback.
- **Double Distribution Day:** Range extends in one direction, then reverses and extends in the opposite direction, creating two distributions.

### References

- Steidlmayer, J. P. & Hawkins, S. B. (1986). *Market Profile: Organizing Financial Data for Decision Making*. Chicago Board of Trade.
  - Original development of Market Profile and Initial Balance concepts.
- Dalton, J. F. (1993). *Mind Over Markets: Power Trading with Market Generated Information*. Traders Press.
  - Extends Steidlmayer's work with practical day-type classification and trading frameworks.
- Dalton, J. F. (2007). *Markets in Profile: Profiting from the Auction Process*. Wiley.
  - Updated treatment of IB theory with modern market microstructure context.

### Implementability with Current Data

**HIGH** — Requires only RTH OHLCV bars at 1m or 5m resolution. IB high/low can be computed from the first 6–12 bars (for 5m data). Rolling percentile calculation of IB ranges is straightforward. No external data needed.

### Impact Rating: **HIGH**

IB classification is one of the most reliable intraday forecasting tools for equity index futures. Knowing whether to expect a trend day (favor breakouts, wider stops, hold winners longer) vs. a range day (favor fades, tighter targets, avoid breakout chasing) is a first-order alpha signal for trade management.

---

## 3. Volume Profile & Value Area

### Mechanism

Volume Profile is a horizontal histogram of volume traded at each price level over a session. Unlike time-based charts that plot volume per bar, Volume Profile shows where the market spent the most time transacting, revealing areas of acceptance (balance) and rejection.

**Key Levels:**
- **Point of Control (POC):** The single price level with the highest traded volume. Acts as a magnet — price tends to revisit the POC. Also acts as the session's "fair value" consensus.
- **Value Area (VA):** The range of prices encompassing approximately 70% of the session's total volume (1 standard deviation). Represents the zone where most transactions occurred — the market's "accepted" range.
- **Value Area High (VAH) / Value Area Low (VAL):** Boundaries of the value area. These act as support/resistance levels. Price acceptance above VAH or below VAL signals distribution shift.

**Trading Implications:**
- Price returning to previous session's POC: high-probability mean-reversion target.
- Price rejecting at VAH/VAL: continuation of prior session's value area.
- Price accepting above prior VAH: bullish shift in value (look for longs on pullbacks to VAH).
- Price accepting below prior VAL: bearish shift in value (look for shorts on rallies to VAL).
- Developing VA relationship to prior day's VA: "inside" (narrowing), "outside" (expanding), or "shifting" (directional migration).

### References

- Chicago Board of Trade (1984–1990). *Market Profile® Manual*. CBOT.
  - Original CBOT documentation on Market Profile construction and interpretation.
- Dalton, J. F. (2007). *Markets in Profile: Profiting from the Auction Process*. Wiley.
  - Practical application of Volume Profile concepts to trading.
- Jones, C. M., Kaul, G. & Lipson, M. L. (1994). "Transactions, Volume, and Volatility." *Review of Financial Studies*, 7(4), 631–651.
  - Empirical analysis of the relationship between volume, price, and volatility at different price levels.

### Implementability with Current Data

**MEDIUM** — Basic POC and Value Area can be approximated from 1m OHLCV bars using typical price × volume aggregation per bar. However, true tick-level volume profile requires tick data or at minimum volume-at-price data from the exchange, which is not available via standard IBKR bar data. The approximation using bar data is sufficient for reference level generation but less precise than tick-based profiles.

### Impact Rating: **MEDIUM**

POC and VA provide useful reference levels but overlap significantly with the overnight levels (prior day high/low, settlement) that we already track. The incremental alpha from approximate VA over prior-day high/low/close is moderate. Full tick-based volume profile would be HIGH impact but is not currently feasible.

---

## 4. Overnight Session Structure

### Mechanism

The CME Globex session for E-mini and Micro E-mini Nasdaq-100 futures runs from 18:00 ET to 17:00 ET the next day (23-hour session with a 60-minute maintenance break). The overnight portion (18:00–09:30 ET) provides price discovery outside of US RTH, incorporating:

- Asian economic data releases (18:00–03:00 ET)
- European market opens and ECB/BOE communications (03:00–09:30 ET)
- Overnight institutional portfolio adjustments

**Key Reference Levels:**
- **Overnight High/Low:** The extremes of the 18:00–09:30 ET session. These represent tested price levels and act as immediate support/resistance at RTH open.
- **Previous Day Close (Settlement):** The 16:00 ET settlement price. The gap between previous close and RTH open price reveals overnight information incorporation.
- **Previous Day High/Low:** Broader reference levels from the full prior RTH session.

**Gap Analysis:**
- **Gap Direction:** RTH open vs. previous close. UP gap = overnight buyers dominant. DOWN gap = overnight sellers dominant.
- **Gap Fill Probability:** Historically, gaps in NQ fill (price returns to previous close) approximately 60–70% of the time. However, unfilled gaps (price does not return to prior close within the first 30 minutes) are strong signals of institutional directional commitment.
- **Gap Size Significance:** Larger gaps (>0.5%) have lower fill probability and stronger trend-day correlation.

### References

- Lou, D., Polk, C. & Skouras, S. (2019). "A Tug of War: Overnight Versus Intraday Expected Returns." *Journal of Financial Economics*, 134(1), 192–213.
  - Documents the concentration of equity premium in the overnight period, with intraday returns near zero on average. Already partially implemented in the overnight bias modifier.
- Berkman, H., Koch, P. D., Tuttle, L. & Zhang, Y. J. (2012). "Paying Attention: Overnight Returns and the Hidden Cost of Buying at the Open." *Journal of Financial and Quantitative Analysis*, 47(4), 715–741.
  - Analyzes the attention-driven premium in overnight returns and its implications for opening-price trading.

### Implementability with Current Data

**HIGH** — Overnight high/low and previous day close are directly available from IBKR historical bar data. Gap calculations are trivial. The 30-minute unfilled gap signal requires only a timer check after RTH open.

### Impact Rating: **HIGH**

Overnight levels are among the most-referenced levels by institutional and algorithmic traders. Gap analysis provides a fast, first-bar signal about the day's likely character. The overnight bias modifier already captures part of this; the overnight level tracker provides the complementary reference-level component.

---

## 5. VWAP Reversion in Futures

### Mechanism

Volume-Weighted Average Price (VWAP) is the benchmark price computed as the cumulative sum of (price × volume) divided by cumulative volume over a session. VWAP is the single most important execution benchmark for institutional traders:

- **Institutional Anchoring:** Large institutional orders (mutual funds, pension funds, ETFs) are typically executed with VWAP as the target benchmark. Portfolio managers judge execution quality by comparing their fill price to session VWAP. This creates a self-reinforcing gravitational pull toward VWAP.
- **Mean-Reversion Signal:** When price deviates significantly from VWAP (>1 ATR), there is a statistical tendency for price to revert back. This occurs because institutions delay execution when price is unfavorable relative to VWAP, creating latent order flow that pushes price back.
- **Momentum Confirmation:** VWAP crossovers (price crossing from below to above VWAP, or vice versa) signal shifts in the intraday balance of supply and demand. A sustained move above VWAP indicates buyers are willing to pay above average — bullish. Below VWAP indicates sellers dominating — bearish.

**VWAP Bands:**
- Price within ±0.5 ATR of VWAP: neutral zone, no signal.
- Price ±0.5–1.0 ATR from VWAP: mildly extended, fade with confirmation.
- Price >1.0 ATR from VWAP: significantly extended, high-probability mean-reversion zone.

**VWAP Reset:**
Session VWAP resets at RTH open (9:30 ET). Some traders also compute developing VWAP (continuous) and anchored VWAP from significant events. For our purposes, session VWAP is the primary calculation.

### References

- Berkowitz, S. A., Logue, D. E. & Noser, E. A. (1988). "The Total Cost of Transactions on the NYSE." *Journal of Finance*, 43(1), 97–112.
  - Early documentation of VWAP as an institutional execution benchmark and its implications for market microstructure.
- Biais, B., Hillion, P. & Spatt, C. (1995). "An Empirical Analysis of the Limit Order Book and the Order Flow in the Paris Bourse." *Journal of Finance*, 50(5), 1655–1689.
  - Empirical analysis of order flow dynamics around volume-weighted price levels.
- Madhavan, A. (2002). "VWAP Strategies." *Trading*, 2002(1), 32–39.
  - Practical analysis of VWAP execution strategies and their market impact.

### Implementability with Current Data

**HIGH** — VWAP calculation requires only price and volume from OHLCV bars. Using typical price ((H+L+C)/3) as the bar's representative price provides a standard approximation. Cumulative calculation is computationally trivial. ATR is already computed in existing modules.

### Impact Rating: **HIGH**

VWAP is the single most-used reference level in institutional futures trading. Distance-from-VWAP as a mean-reversion signal and VWAP crossover as a momentum signal both have strong empirical support. The implementation provides a clean, quantitative signal that complements our existing technical indicators (FVG, order blocks, sweeps).

---

## 6. TICK Index Divergence

### Mechanism

The NYSE TICK Index measures the number of NYSE-listed stocks trading on an uptick minus those trading on a downtick at any given moment. It serves as a real-time gauge of broad market buying/selling pressure:

- **Normal Range:** TICK oscillates between approximately -500 and +500 during normal trading.
- **Extreme Readings:** TICK exceeding +1000 or falling below -1000 indicates broad-based buying or selling exhaustion. These extremes often mark short-term reversal points.
- **TICK Divergence:** When NQ price makes a new high but TICK fails to confirm (does not make a new high), this bearish divergence signals weakening breadth. Conversely, NQ new lows without TICK confirmation signal buying emerging under the surface.

**Why It Matters for NQ:**
NQ is a concentrated index (top 7 stocks are ~50% of weight). TICK captures the broader market's health. When NQ rallies on mega-cap strength alone while the broader market (measured by TICK) deteriorates, the rally is fragile. TICK divergence has historically preceded NQ reversals by 5–15 minutes.

### References

- Hasbrouck, J. (2003). "Intraday Price Formation in U.S. Equity Index Markets." *Journal of Finance*, 58(6), 2375–2400.
  - Documents lead-lag relationships between index futures and underlying stock markets, relevant to understanding why broad-market indicators like TICK inform NQ.
- Harris, L. (1986). "A Transaction Data Study of Weekly and Intradaily Patterns in Stock Returns." *Journal of Financial Economics*, 16(1), 99–117.
  - Early empirical work on intraday patterns that relates to TICK-based analysis.

### Implementability with Current Data

**LOW** — NYSE TICK is not directly available through IBKR's standard market data API for MNQ/NQ futures subscriptions. Accessing TICK requires a separate NYSE market data subscription or an alternative data provider. While conceptually powerful, this signal cannot be implemented without additional data infrastructure.

### Impact Rating: **MEDIUM**

TICK divergence is a well-known signal among futures daytraders and has genuine predictive power for NQ reversals. However, since we cannot currently access the data, the impact is theoretical. If NYSE data subscription is added in the future, this becomes a HIGH-impact signal.

---

## 7. Foreign Institutional Flow Patterns

### Mechanism

Global institutional flow patterns create predictable intraday effects on NQ futures due to the participation of foreign investors during specific time windows:

**Asian Session (18:00–03:00 ET):**
- Japanese Government Pension Investment Fund (GPIF) and other Asian pension/sovereign wealth funds periodically rebalance US equity exposure.
- Bank of Japan monetary policy decisions (typically announced ~00:00–01:00 ET) can trigger sharp NQ moves.
- Chinese economic data releases (typically 21:30 ET or 02:00 ET) affect global risk sentiment.
- Overnight NQ volume during the Asian session is thin (typically 5–15% of RTH volume), making price more susceptible to large institutional orders.

**European Session (03:00–09:30 ET):**
- ECB press conferences (typically 08:45 ET / 14:45 CET) and Bank of England decisions create volatility.
- European sovereign wealth funds (Norges Bank, Swiss National Bank) adjust US equity allocations.
- Volume picks up from ~03:00 ET as London opens, creating the first significant liquidity of the day.
- European institutional orders tend to be VWAP-targeted, creating directional bias in the pre-market.

**London Fix (11:00 ET / 16:00 GMT):**
- The WM/Reuters FX fixing window at 16:00 GMT creates large hedging flows as asset managers adjust FX exposure on international equity portfolios. These FX flows spill into equity futures as dealers hedge cross-asset risk.
- Effect is more pronounced in SPX/ES than NQ, but correlation spillover is measurable.

### References

- Evans, M. D. D. & Lyons, R. K. (2002). "Order Flow and Exchange Rate Dynamics." *Journal of Political Economy*, 110(1), 170–180.
  - Demonstrates that order flow (not macroeconomic fundamentals) drives short-term FX dynamics. Relevant because FX order flow spills into equity futures markets.
- Froot, K. A. & Ramadorai, T. (2005). "Currency Returns, Intrinsic Value, and Institutional-Investor Flows." *Journal of Finance*, 60(3), 1535–1566.
  - Documents the relationship between institutional cross-border flows and asset prices, establishing that flow patterns are persistent and predictable.
- Menkhoff, L. & Schmeling, M. (2010). "Whose Trades Convey Information? Evidence from a Cross-Section of Traders." *Journal of Financial Markets*, 13(2), 234–257.
  - Analyzes which categories of institutional traders produce informative order flow.

### Implementability with Current Data

**LOW–MEDIUM** — Direct institutional flow data is not available. However, proxy signals can be constructed:
- Overnight session volume spikes (detectable from Globex bar data) may indicate large institutional orders.
- Pre-market directional bias (computed from 03:00–09:30 ET bar data) captures European session positioning.
- Calendar-based flags for known events (ECB, BOJ, Chinese data) can be added to a calendar module.

The primary implementable component is the overnight session directional bias, which is already partially captured by the overnight bias modifier. The specific institutional flow decomposition requires proprietary data (EPFR, custody bank data) not available through IBKR.

### Impact Rating: **LOW**

While the theoretical framework is well-established, the lack of direct flow data limits practical implementation. The overnight session bias (already implemented) captures the first-order effect. Incremental improvement from proxy-based flow detection is marginal. Best treated as context for interpreting overnight moves rather than a standalone signal.

---

## 8. Volatility Clustering and Regime Persistence

### Mechanism

Financial market volatility exhibits strong autocorrelation — high-volatility periods are followed by high-volatility periods, and low-volatility periods persist. This "clustering" property was first formally documented by Mandelbrot (1963) and underpins the entire family of GARCH models.

**Key Properties:**
- **Volatility Persistence:** Today's realized volatility is the best single predictor of tomorrow's. This is the foundation of our HAR-RV forecaster.
- **Regime Transitions as Alpha Signals:**
  - **High-Vol → Low-Vol Transition:** The compression following an expansion phase. Price begins to trend as the market establishes a new equilibrium direction. This is a favorable environment for trend-following strategies — IB breakouts have higher follow-through rates.
  - **Low-Vol → High-Vol Transition:** Breakout/breakdown. Compressed ranges snap, often triggered by news events or liquidity vacuums. This is a favorable environment for breakout strategies — position larger, use wider stops.
  - **Sustained High-Vol:** Mean-reversion dominates as price oscillates wildly. Reduce position sizes, tighten stops, favor counter-trend setups.
  - **Sustained Low-Vol:** Low opportunity environment. Reduce trading frequency, wait for regime transition.

**Regime Detection:**
The transition points between volatility regimes are the highest-alpha moments. Our existing HAR-RV forecaster provides the raw volatility estimate. The regime detector (VIX-based) provides a complementary view. Combining both — HAR-RV direction of change + VIX regime — yields a robust regime transition signal.

### References

- Mandelbrot, B. (1963). "The Variation of Certain Speculative Prices." *Journal of Business*, 36(4), 394–419.
  - Foundational paper establishing that financial returns exhibit fat tails and volatility clustering, violating the assumptions of Gaussian models.
- Engle, R. F. (1982). "Autoregressive Conditional Heteroscedasticity with Estimates of the Variance of United Kingdom Inflation." *Econometrica*, 50(4), 987–1007.
  - Introduces the ARCH model, formalizing volatility clustering as a statistical process.
- Ang, A. & Timmermann, A. (2012). "Regime Changes and Financial Markets." *Annual Review of Financial Economics*, 4, 313–337.
  - Comprehensive survey of regime-switching models in finance, documenting that regime transitions are predictable and exploitable.
- Hamilton, J. D. (1989). "A New Approach to the Economic Analysis of Nonstationary Time Series and the Business Cycle." *Econometrica*, 57(2), 357–384.
  - Introduces the Markov regime-switching model, the methodological basis for formal regime detection.

### Implementability with Current Data

**HIGH** — The HAR-RV forecaster is already implemented. The VIX-based regime detector is already implemented. Combining these into a regime transition signal requires only comparison logic (today's forecast vs. yesterday's, VIX regime shift detection). No additional data sources needed.

### Impact Rating: **HIGH**

Volatility regime awareness is arguably the most important contextual modifier for any trading system. The difference between a trend-day strategy in a trending regime vs. a ranging regime is the difference between profit and loss. Our existing modules (HAR-RV, VIX regime) provide the building blocks; the session profiler adds the intraday dimension.

---

## 9. Implementation Summary Matrix

| # | Concept | Implementable Now? | Data Required | Impact | Module |
|---|---------|-------------------|---------------|--------|--------|
| 1 | Time-of-Day Session Phases | **YES** | OHLCV bars, timestamps | **HIGH** | `session_profiler.py` |
| 2 | Initial Balance Theory | **YES** | RTH OHLCV bars (1m/5m) | **HIGH** | `initial_balance.py` |
| 3 | Volume Profile / Value Area | **PARTIAL** | Tick-level volume (unavail.) | **MEDIUM** | Future — approximate from bars |
| 4 | Overnight Session Structure | **YES** | Globex + RTH OHLCV bars | **HIGH** | `overnight_levels.py` |
| 5 | VWAP Reversion | **YES** | OHLCV bars with volume | **HIGH** | `vwap_tracker.py` |
| 6 | TICK Index Divergence | **NO** | NYSE TICK (unavail.) | **MEDIUM** | Not implementable currently |
| 7 | Foreign Institutional Flow | **PARTIAL** | Proprietary flow data (unavail.) | **LOW** | Partially via overnight bias |
| 8 | Volatility Regime Persistence | **YES** | OHLCV + VIX (available) | **HIGH** | Already implemented (HAR-RV + regime detector) |

### Priority Implementation Order

1. **Session Profiler** (HIGH impact, trivial to implement) — immediate position sizing improvement
2. **Initial Balance Tracker** (HIGH impact, straightforward) — day-type classification for strategy selection
3. **VWAP Tracker** (HIGH impact, straightforward) — institutional reference level + mean-reversion signal
4. **Overnight Level Tracker** (HIGH impact, straightforward) — reference levels + gap analysis

All four modules are implemented as standalone components in this session, ready for integration into `process_bar()` in a future session after review.

### Integration Architecture (Future)

```
Bar arrives
  │
  ├─► SessionProfiler.get_session_phase(bar.timestamp)
  │     └─► Phase modifiers (position_size_mult, stop_width_mult)
  │
  ├─► InitialBalanceTracker.update(bar)
  │     └─► Day-type forecast (TREND_DAY / RANGE_DAY)
  │     └─► IB break direction (LONG / SHORT / None)
  │
  ├─► VWAPTracker.update(bar)
  │     └─► VWAP signal (distance, position, crossed_recently, extended)
  │
  ├─► OvernightLevelTracker.update(bar)
  │     └─► Reference levels (overnight H/L, prev close, prev H/L)
  │     └─► Gap analysis (direction, size, fill status)
  │
  └─► Existing pipeline (FVG, OB, sweeps, HTF, aggregator)
        │
        └─► Modified by session phase + day-type + VWAP context
```

### Theoretical Framework Summary

The microstructure patterns documented here arise from a common underlying mechanism: **the interaction of heterogeneous traders with different information sets, time horizons, and execution constraints**. Institutional traders (pension funds, mutual funds, ETF managers) are constrained by VWAP benchmarks, end-of-day NAV calculations, and regulatory reporting windows. This creates predictable patterns in order flow, volume, and volatility that can be exploited by traders who understand the microstructure.

The key insight from Admati & Pfleiderer (1988) is that informed traders strategically time their orders to periods of high liquidity, which attracts more liquidity, creating a self-reinforcing concentration effect. This explains both the U-shaped volume curve and the concentration of price discovery in the first and last hours of trading.

Our implementation captures the most actionable subset of these patterns using only OHLCV + VIX data available through IBKR, prioritizing signals with the highest implementability-to-impact ratio.
