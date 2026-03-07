# Sharpened Research Prompt — Scale-Out Exit Optimization for MNQ Futures Bot

## Context for Claude Code

You are researching exit management and trade lifecycle optimization for an MNQ (Micro Nasdaq-100 futures) algorithmic trading system. This is NOT a general survey — it is a targeted investigation into specific problems identified by forensic analysis of 3,159+ backtested trades.

### System Architecture (what already exists)
- **Entry**: Liquidity sweep-based entries gated by HTF directional bias (6 timeframes) and a High-Conviction Filter (min score 0.75, max stop 30pts)
- **Execution**: 2-contract scale-out on 2-minute bars
  - **C1**: Trail-from-profit exit (activate trailing after +3pts, trail 2.5pts from HWM, 12-bar fallback)
  - **C2**: Runner — stop moves to BE+1 after C1 exits, then ATR-based trailing (2× ATR)
- **Risk**: ATR-based stops (2× ATR multiplier), 1% account risk per trade, structural stop integration
- **Performance**: Config D baseline: PF 1.73, 61.9% WR, 254 trades/month, 1.4% max DD over 6 months OOS

### The Specific Problem (from forensic analysis of C2 exits)

**CRITICAL: The C2 exit distribution shifts dramatically between market periods.** This is the strongest argument for regime-adaptive exit management.

**Later period (OOS, ~1,126 C2 trades):**
- **47.6% (536) exit at breakeven** — avg PnL $0.71. These are "stolen runners" that got clipped before they could trend.
- **31.8% (358) exit via trailing stop** — avg PnL $51.97, total $18,604. This is where ALL the C2 edge lives.
- **19.7% (222) hit initial stop** — avg PnL -$41.28, total -$9,164. Full losses on both contracts.
- **0.9% (10) hit max target** — avg PnL $325.46. Rare but massive.

**Period 4 (Sep 2023 – Feb 2024, 360 trades, PF 1.66, 52.2% WR):**
- **36.1% (130) exit at breakeven** — lower BE kill rate
- **17.8% (64) exit via trailing stop** — fewer runners catch trends
- **40.8% (147) hit initial stop** — dominant loss bucket in this period
- **1.9% (7) hit max target**
- Total PnL: +$5,377.78 | C1: +$1,909.10 | C2: +$3,468.68 | Max DD: 1.2%

**The pattern**: In ranging/choppy markets, the initial stop is the killer (40.8%). In trending markets, the BE stop steals runners (47.6%). A static exit strategy is suboptimal in BOTH regimes — it just fails differently.

**The core tension**: Moving to BE too early kills runners in trends. Moving to BE too late increases losses in chop. The current Variant B (delayed BE: wait for MFE >= 1.5× stop distance) was designed to address this, but the problem persists because it's a static rule applied to a dynamic problem.

---

## RESEARCH AREAS

### 1. THE BREAKEVEN STOP PROBLEM — Academic & Practitioner Evidence
**This is the #1 priority research area.**

Investigate thoroughly:
- What does the academic literature say about moving stops to breakeven? Is there empirical evidence that it helps or hurts systematic strategies?
- The specific tradeoff: BE stops reduce loss magnitude but increase loss frequency (by converting would-be winners into scratched trades). What is the optimal balance point?
- Research on "stop-and-reverse" vs "stop-and-wait" — when a runner gets stopped at BE, what is the probability the original direction resumes?
- Are there regime-dependent optimal BE trigger points? (e.g., in trending markets, delay BE further; in ranging markets, tighten faster)
- What do CTAs (Winton, Man AHL, Aspect Capital) and prop firms actually do about breakeven stops on partial positions?
- The concept of "trailing to protect profits without killing the trend" — what mechanisms exist beyond simple price-based trailing?
- Research from: Kaufman (adaptive trailing), Clenow (trend-following exit design), Covel (turtle system exits), Seykota principles

**Specific questions for our system:**
- Given our data (47.6% BE exits at $0.71 avg), how much PnL are we losing to premature BE triggers?
- What would happen if we never moved to BE (Variant A: keep initial stop)? Our backtest shows this data exists.
- Is there an MFE-based optimal trigger that balances protection vs. opportunity cost?
- Should the BE trigger be volatility-adaptive (wider in trending, tighter in ranging)?

### 2. TRAILING STOP OPTIMIZATION FOR INDEX FUTURES
**Second priority — directly affects the 31.8% of trades that produce all C2 profit.**

Investigate:
- ATR-based vs. fixed-point vs. structure-based vs. chandelier vs. Keltner channel trailing stops — what does the research say for equity index futures specifically?
- Optimal ATR multiplier for trailing stops: our system uses 2× ATR. Is there research on the optimal multiplier for intraday equity index futures?
- The "trail tightening" problem: as a trade moves further in profit, should the trail tighten (lock in more) or stay wide (let it breathe)?
- Parabolic SAR-style acceleration factors applied to trailing stops — evidence for/against?
- Time-based trail adjustment: should trails tighten as the trade approaches session boundaries (e.g., approaching 4pm ET close)?
- Session-aware trailing: does the optimal trail width change between London open, NY AM, NY PM, overnight?
- Multi-stage trailing: instead of one continuous trail, use stepped trail distances based on profit tiers (e.g., at +20pts trail 8pts, at +50pts trail 15pts, at +100pts trail 25pts)
- Research from: Wilder (parabolic SAR), Kaufman (adaptive moving average exits), Kase (dynamic volatility stops), Perry Kaufman's efficiency ratio applied to exit timing

**Specific questions:**
- What's the expected PnL impact of switching from 2× ATR to other multipliers, based on literature?
- Is there evidence that trailing stop type matters more in trending vs. mean-reverting regimes?
- For our C2 max target of 150pts — is a fixed ceiling optimal or should it be volatility-scaled?

### 3. SCALE-OUT ARCHITECTURE — C1/C2 INTERACTION EFFECTS
**Third priority — the interplay between C1 exit timing and C2 performance.**

Investigate:
- Academic and practitioner literature on partial exit (scale-out) strategies vs. all-in/all-out
- The "scaling out kills your edge" argument (Tharp, Faith) vs. the "psychological sustainability" argument — what does the data say?
- How does C1 exit timing affect C2 optimal management? Our C1 exit research showed PF ranges from 1.11 to 1.81 depending on C1 strategy — but we haven't studied how different C1 exits change C2's optimal trail/BE parameters.
- The information content of C1's exit: if C1 trails 10pts before exiting, does that tell us something about C2's probability of success vs. if C1 exits at 3pts?
- Should C2's BE trigger and trail parameters be CONDITIONAL on C1's exit metrics (exit price, bars held, profit captured)?
- Research on "adaptive exit management" — adjusting exit parameters mid-trade based on unfolding price action
- The concept of "trade quality scoring at time of C1 exit" to dynamically choose C2 management strategy

**Specific questions:**
- In our system, C1 trail-from-profit activates at +3pts with 2.5pt trail. If C1 trails to +8pts before getting stopped, should C2 be managed differently than if C1 trails to +3.5pts?
- Is there evidence that the C1 exit level is predictive of C2's ultimate outcome?

### 4. ENTRY TIMING AND STOP PLACEMENT FOR SWEEP-BASED ENTRIES
**Fourth priority — the 19.7% initial stop losses.**

Investigate:
- Literature on liquidity sweep / stop hunt entry strategies for index futures
- Optimal stop placement relative to the sweep level — behind the sweep, fixed ATR, or structural?
- The "confirmation bar" concept — waiting 1-2 bars after sweep detection before entering. Impact on win rate vs. missed entries?
- Entry execution on 2-minute bars: is this the right timeframe? Evidence for 1m, 2m, 3m, 5m entry bars
- The relationship between entry bar timeframe and optimal stop distance
- "Sweep + FVG + OB confluence" as an entry filter — ICT-style institutional concepts and any academic validation
- Research from: Cont/Kukanov/Stoikov (microstructure), Cartea/Jaimungal (algorithmic execution), ICT concepts and their measurability

**Specific questions:**
- Our 30pt max stop cap comes from the HC filter. Is this the right ceiling for MNQ?
- Of the 222 C2 initial stop losses, how many had the trade move >15pts in favor first? (This would indicate stop-too-tight rather than bad-entry problems)

### 5. THE PROFIT FACTOR VIABILITY QUESTION
**Fifth priority — honest assessment of where we stand.**

Investigate:
- With PF 1.73 OOS on Config D (1,524 trades, 6 months), where does this rank in systematic futures literature?
- What is the typical degradation from backtest PF to live PF? Specific numbers from published research.
- Transaction cost sensitivity: at PF 1.73 with $1.29/contract commissions, how much room do we have before the edge disappears?
- The relationship between trade frequency (254/month) and strategy robustness — is high frequency good (more data, faster convergence) or bad (more execution risk, more noise)?
- Lopez de Prado's deflated Sharpe ratio: how should we apply this to validate our backtest isn't overfit?
- Bailey & Lopez de Prado's "probability of backtest overfitting" framework — how to calculate for our system
- Minimum backtest length for statistical significance: with 1,524 trades at 61.9% WR, what is the p-value that our edge is real?

**Specific questions:**
- Is PF 1.73 (254 trades/month) better or worse than PF 2.0 (50 trades/month) for live robustness?
- What PF degradation should we budget for the paper-to-live transition?

### 6. REGIME-ADAPTIVE EXIT MANAGEMENT
**Sixth priority — advanced optimization.**

Investigate:
- Should the entire exit strategy (C1 method, BE trigger, C2 trail type, trail multiplier) adapt to detected market regime?
- Research on regime detection methods: HMM (Hamilton), Markov-switching models, volatility regime clustering
- Evidence that regime-adaptive systems actually outperform static systems OOS (not just in-sample)
- The "complexity tax" — every adaptive parameter is another overfitting risk. What is the evidence on regime-adaptive exits vs. robust static parameters?
- Our system has a regime detector (risk/regime_detector.py). How should regime state flow into exit management?

---

## OUTPUT FORMAT

Write a comprehensive research document to `docs/algo_trading_research.md` structured as:

### EXECUTIVE SUMMARY
The 5 most critical findings for our specific C1/C2 exit management problem. Be direct — "you should do X because Y study with Z sample size showed W result."

### For each research area:
1. **Key findings** with specific citations (Author, Year, Journal/Source)
2. **Direct implications** for our MNQ system — reference our specific numbers (47.6% BE exits, 31.8% trailing winners, etc.)
3. **Actionable recommendations** ranked by:
   - Expected PnL impact (estimate in $/month based on our trade frequency)
   - Implementation complexity (hours/days to code and test)
   - Evidence quality (peer-reviewed > practitioner book > blog post)
   - Overfitting risk (new parameter = higher risk)

### SYNTHESIS
A ranked action plan: "Do this first, then this, then this" with expected cumulative PnL impact.

### BIBLIOGRAPHY
Every source cited, with:
- Full citation
- Where to access it (DOI, SSRN link, book ISBN)
- Relevance rating (1-5) for our specific system

---

## QUALITY REQUIREMENTS
- **Futures-specific** research preferred over equities-only
- **Intraday** research preferred over daily/weekly holding periods
- **Recent (2015-2025)** preferred but include foundational work (Kaufman, Wilder, Tharp, etc.)
- **Peer-reviewed** preferred, but acknowledge that practitioner literature (Clenow, Faith, Covel) contains unique empirical insights not found in academia
- **Contradictory findings**: if research disagrees, present BOTH sides and explain which applies to our specific case (intraday MNQ, 2-min bars, 254 trades/month)
- **Honest uncertainty**: say "the evidence is unclear" when it is. Don't fabricate confidence.
- Do NOT hallucinate citations. If you cannot find a specific paper, say so. A real "I don't know" is worth more than a made-up reference.

## ADDITIONAL DELIVERABLE
Create `docs/action_plan.md` with:
- Ranked list of changes ordered by (expected impact × evidence quality) / (implementation complexity × overfitting risk)
- For each item: what to change, expected PF impact, evidence source, test methodology
- A "quick wins" section (changes implementable in <1 day with strong evidence)
- A "research needed" section (changes that require our own backtesting before implementation)

## ON COMPLETION
Print:
1. Executive summary (5 key findings)
2. Top 10 ranked action items
3. Any research areas where evidence was insufficient
