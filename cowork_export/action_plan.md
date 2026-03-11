# Action Plan — Scale-Out Exit Optimization for MNQ Futures Bot

**Generated:** 2026-03-07
**Companion to:** `docs/algo_trading_research.md`
**Baseline:** Config D, PF 1.73, 61.9% WR, 254 trades/month, $4,264/month gross

---

## Ranked Changes: (Expected Impact × Evidence Quality) / (Implementation Complexity × Overfitting Risk)

### #1 — Switch C1 Exit to B:5 Bars (Time-Based)
| Dimension | Assessment |
|-----------|------------|
| **What to change** | Replace C1 TP1 = 1.5× stop with "exit C1 at market after 5 bars if profitable, else fallback to 1.5× target or stop" |
| **Where** | `execution/scale_out_executor.py` → `_manage_phase_1()` |
| **Expected PF impact** | PF 1.15 → 1.81 on C1 alone; +$870/month total PnL improvement |
| **Evidence source** | Your own c1_exit_research.md (751 trades, 6 months, 14 configs tested) |
| **Test methodology** | Already backtested. Verify on most recent month's data as final confirmation. |
| **Implementation time** | < 1 day |
| **Overfitting risk** | Low — tested across 6 months, consistent 5/6 profitable months |
| **Score** | ★★★★★ (highest priority) |

### #2 — Widen C2 Trailing Stop to 2.5× ATR
| Dimension | Assessment |
|-----------|------------|
| **What to change** | `c2_trailing_atr_multiplier` from 2.0 to 2.5 in `config/settings.py` |
| **Where** | `config/settings.py` → `ScaleOutConfig` |
| **Expected PF impact** | +$500–1,200/month (wider trail lets the 358 trailing winners run further) |
| **Evidence source** | Lo & Remorov 2017 (wider stops outperform after costs); Clenow 2013 (3× ATR for trend-following) |
| **Test methodology** | Backtest identical pipeline with 2.5× multiplier. Compare: avg trailing exit PnL, total C2 PnL, max DD, number of trailing winners vs BE exits. Also test 3.0× as upper bound. |
| **Implementation time** | < 1 day (single constant) |
| **Overfitting risk** | Low — testing 2 values of existing parameter, no new parameters |
| **Score** | ★★★★★ |

### #3 — Enable MFE Tracking for All Exits
| Dimension | Assessment |
|-----------|------------|
| **What to change** | Track maximum favorable excursion (MFE) for every trade from entry to exit, for all exit types (stop, BE, trailing, max target) |
| **Where** | `execution/scale_out_executor.py` — add MFE tracking to trade lifecycle |
| **Expected PF impact** | Informational — enables all subsequent optimizations |
| **Evidence source** | Forensic output shows `mfe_computed_count: 0` for stopped trades — critical data gap |
| **Test methodology** | N/A — data collection only |
| **Implementation time** | 1 day |
| **Overfitting risk** | Zero |
| **Score** | ★★★★★ (prerequisite for most other improvements) |

### #4 — Add BE Buffer (Entry + 2–3pts Instead of Entry + 1pt)
| Dimension | Assessment |
|-----------|------------|
| **What to change** | When moving C2 to breakeven, set stop at entry + 2pts (longs) or entry − 2pts (shorts) instead of entry + 1pt |
| **Where** | `execution/scale_out_executor.py` → `_close_c1_to_runner()` |
| **Expected PF impact** | +$200–400/month (reduces stop-hunting on exact entry levels) |
| **Evidence source** | Osler 2005 (stop-hunting at round/obvious price levels); Davey 2014 |
| **Test methodology** | Backtest with buffer = 2pts and 3pts. Compare BE exit rate and total PnL. |
| **Implementation time** | < 1 day |
| **Overfitting risk** | Very low (single parameter with strong theoretical justification) |
| **Score** | ★★★★☆ |

### #5 — Calculate Deflated Sharpe Ratio
| Dimension | Assessment |
|-----------|------------|
| **What to change** | Add a diagnostic script to compute DSR based on number of configurations tested |
| **Where** | New script: `scripts/deflated_sharpe.py` |
| **Expected PF impact** | Informational — may prevent catastrophic live capital loss |
| **Evidence source** | Lopez de Prado 2018; Bailey et al. 2014 |
| **Test methodology** | Count all configs tested → apply DSR formula → if DSR < 1.0, system is not statistically distinguishable from random after multiple testing |
| **Implementation time** | 1–2 days |
| **Overfitting risk** | Zero (diagnostic only) |
| **Score** | ★★★★☆ |

### #6 — Regime-Adaptive BE Trigger
| Dimension | Assessment |
|-----------|------------|
| **What to change** | BE trigger varies by detected regime: trending → MFE >= 2.0× stop; ranging → MFE >= 1.0× stop |
| **Where** | `execution/scale_out_executor.py` → `_manage_runner()`, integrate with `risk/regime_detector.py` |
| **Expected PF impact** | +$800–1,500/month (recovers 15–25% of stolen runners in trending markets) |
| **Evidence source** | Kaminski & Lo 2014; your cross-period data showing BE exit rate shifts from 47.6% to 36.1% |
| **Test methodology** | Walk-forward validation mandatory. Use ADX(14) > 25 as trending proxy. Test on 3+ non-overlapping periods. Must improve OOS PF by ≥ 0.10 to adopt. |
| **Implementation time** | 2–3 days |
| **Overfitting risk** | Moderate (adds regime condition to existing parameter) |
| **Score** | ★★★★☆ |

### #7 — Walk-Forward Validation Framework
| Dimension | Assessment |
|-----------|------------|
| **What to change** | Build walk-forward validation pipeline for the entire system |
| **Where** | New script: `scripts/walk_forward_validation.py` |
| **Expected PF impact** | Informational — validates robustness of all changes |
| **Evidence source** | Standard quantitative finance practice; Lopez de Prado 2018 |
| **Test methodology** | Divide available data into 3+ non-overlapping periods. Train/test sequentially. Report OOS PF for each fold. |
| **Implementation time** | 3–5 days |
| **Overfitting risk** | Zero (validation framework) |
| **Score** | ★★★★☆ |

### #8 — Log C1 Exit Metrics for C2 Optimization
| Dimension | Assessment |
|-----------|------------|
| **What to change** | After C1 exits, log: exit price, bars held, profit captured, C1 MFE, price velocity |
| **Where** | `execution/scale_out_executor.py` → `_close_c1_to_runner()` |
| **Expected PF impact** | Informational — builds dataset for conditional C2 management |
| **Evidence source** | No published precedent (potential proprietary edge) |
| **Test methodology** | N/A — data collection. After 500+ trades with logged data, correlate C1 metrics with C2 outcomes. |
| **Implementation time** | < 1 day |
| **Overfitting risk** | Zero |
| **Score** | ★★★☆☆ |

### #9 — Multi-Stage Trailing Stop
| Dimension | Assessment |
|-----------|------------|
| **What to change** | Replace single ATR trail with tiered trail: +20pts→trail 10pts, +50pts→trail 20pts, +100pts→trail 30pts |
| **Where** | `execution/scale_out_executor.py` → `_compute_trailing_stop()` |
| **Expected PF impact** | +$300–800/month |
| **Evidence source** | Practitioner consensus; no rigorous academic study |
| **Test methodology** | Backtest with 3 tier configurations. Compare avg exit PnL per tier, total PnL, max DD. Watch for overfitting — 3 new thresholds + 3 new trail distances = 6 parameters. |
| **Implementation time** | 2–3 days |
| **Overfitting risk** | Moderate to high (6 new parameters) |
| **Score** | ★★★☆☆ |

### #10 — Test 1-Minute Execution Bars
| Dimension | Assessment |
|-----------|------------|
| **What to change** | Run entry signals on 1-minute bars instead of 2-minute |
| **Where** | Data pipeline and `config/settings.py` execution timeframe |
| **Expected PF impact** | Unknown — depends on sweep signal decay rate |
| **Evidence source** | Cont et al. 2014 (OFI signal decays in seconds); Hasbrouck 2007 |
| **Test methodology** | Full pipeline backtest on 1m data. Compare win rate, avg slippage, PnL, trade count. |
| **Implementation time** | 2–3 days (requires data pipeline adaptation) |
| **Overfitting risk** | Low (single parameter, no curve fitting) |
| **Score** | ★★★☆☆ |

---

## QUICK WINS (Implementable in < 1 Day, Strong Evidence)

These four changes can be implemented immediately with high confidence:

1. **Switch C1 to B:5 bars** — Already proven in your backtest. PF jumps from 1.15 to 1.81 for C1.
2. **Test C2 trail at 2.5× ATR** — Single constant change, strong academic backing for wider trails.
3. **Add BE buffer of +2pts** — Minimal code change, reduces stop-hunting exposure.
4. **Enable MFE tracking** — Pure data collection, zero risk, enables all future optimization.

Combined expected impact of quick wins: **+$1,570–2,470/month** (conservative).

**Implementation order:** Do #4 (MFE tracking) and #8 (C1 logging) first so you capture data from the very first trades with new parameters. Then implement #1 (C1 B:5) and #2 (trail 2.5×) simultaneously and backtest together.

---

## RESEARCH NEEDED (Requires Your Own Backtesting Before Implementation)

These changes have theoretical support but need empirical validation with your specific data:

### R1: Stolen Runner Quantification
**Question:** Of the 536 BE exits, how many continued >20pts in the original direction?
**Method:** Track price for 50 bars after every BE exit. Bucket by continuation distance.
**Prerequisite:** MFE tracking (Quick Win #4)
**Decision gate:** If >30% of BE exits are stolen runners (continued >20pts), regime-adaptive BE is very high priority. If <10%, current BE is less problematic than thought.

### R2: C1 Exit Quality → C2 Outcome Correlation
**Question:** Does C1 exit level predict C2 success?
**Method:** After logging C1 metrics (Quick Win #8), correlate C1 profit/bars/velocity with C2 PnL.
**Decision gate:** If correlation > 0.3, implement conditional C2 management. If < 0.1, keep static.

### R3: Optimal ATR Lookback Period
**Question:** Is ATR(10) better than ATR(14) for your 2-minute bars?
**Method:** Compute both, backtest trailing stop with each.
**Decision gate:** If PnL difference > $200/month, switch.

### R4: Session-Specific Trail Adjustment
**Question:** Should the trail tighten approaching 4pm ET?
**Method:** Bucket trailing exits by time of day. Check if late-day exits have lower avg PnL (indicating the trail should have been tighter) or higher avg PnL (trail was appropriately wide).
**Decision gate:** If late-day trailing exits average <$20 (vs $52 overall), add a session-close tightening rule.

### R5: 2-State Regime Model Validation
**Question:** Does a simple trending/ranging classifier improve exit PnL out-of-sample?
**Method:** Walk-forward test with ADX(14) regime classifier. Adapt only BE trigger + trail multiplier.
**Decision gate:** Must improve OOS PF by ≥ 0.10 across all walk-forward folds. If improvement is <0.05, the complexity isn't worth it.

---

## IMPLEMENTATION TIMELINE

### Week 1: Data Infrastructure + Quick Wins
- Day 1: Enable MFE tracking + C1 exit logging (data infrastructure)
- Day 2: Switch C1 to B:5 bars + add BE buffer (+2pts)
- Day 3: Test C2 trail at 2.5× ATR (backtest)
- Day 4: If 2.5× backtest positive, implement. Start deflated Sharpe calculation.
- Day 5: Review all Week 1 backtest results. Lock in changes that improve OOS PF.

### Week 2: Validation + Research
- Days 1–3: Build walk-forward validation framework
- Days 4–5: Run walk-forward on current Config D + Week 1 changes

### Week 3: Regime Adaptation (if validated)
- Days 1–2: Implement regime-adaptive BE trigger
- Days 3–5: Walk-forward validate the adaptive component

### Week 4: Paper Trading Launch
- Begin paper trading with all validated changes
- Target: 500 live-data paper trades before considering real capital

---

## DO NOT DO (Changes That Sound Good But Are Likely Harmful)

1. **Don't adapt more than 2 exit parameters to regime.** Bailey et al. (2014) shows exponential overfitting risk with parameter count. Two is the safe limit.

2. **Don't add ICT-specific entry filters without academic validation.** Order blocks and FVG concepts lack peer-reviewed support. Your current sweep + HTF bias entry already works (61.9% WR).

3. **Don't reduce trade frequency to improve PF.** Higher frequency = faster statistical convergence in live. You'll know in 2 months if the system works vs. 8 months at lower frequency.

4. **Don't chase max-target optimization.** At 0.9% of trades, max target hits are too rare to optimize meaningfully. Statistical noise will dominate any "improvement."

5. **Don't go live until paper trading confirms PF > 1.3 over 500+ trades.** The system's cost sensitivity means PF 1.3 is approximately break-even in live conditions.

---

*Generated by multi-agent research synthesis — 2026-03-07*
