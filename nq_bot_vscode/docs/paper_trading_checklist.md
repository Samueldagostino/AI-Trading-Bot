# Paper Trading Launch Checklist

## System Under Test

| Parameter | Value |
|-----------|-------|
| Config | D + Variant C + Sweep Detector + Calibrated Slippage |
| HC Filter | score >= 0.75, stop <= 30pts |
| C1 Exit | Trail from profit (>=3pts → 2.5pt trailing stop, 12-bar fallback) |
| HTF Gate | strength >= 0.3 |
| Sweep Detector | ENABLED (additive — PDH/PDL, session H/L, PWH/PWL, VWAP, rounds) |
| Execution TF | 2m (aggregated from 1m bars) |
| Contracts | 2 MNQ (C1 trail-from-profit + C2 ATR runner) |
| Daily Loss Limit | $500 |
| Max Position | 2 contracts |
| Connection Timeout | 60s → emergency flatten |

## OOS Baseline (What We Expect)

| Metric | 6-Month OOS (with sweep + slippage) |
|--------|--------------------------------------|
| Trades/month | 254 |
| Win Rate | 61.9% |
| Profit Factor | 1.73 |
| PnL/month | $4,264 |
| Expectancy/trade | $16.79 |
| Max Drawdown | 1.4% |
| C1 PnL (total) | $10,008 |
| C2 PnL (total) | $15,573 |
| Sweep-only trades | 338 (WR 61.8%) |
| Confluence trades | 161 (WR 67.7%) |

---

## Pre-Launch Verification

- [ ] **Credentials configured**: Copy `.env.example` to `.env`, fill in Tradovate DEMO credentials
- [ ] **Demo account funded**: Verify $50,000 demo balance on Tradovate
- [ ] **Connection test**: Run `python scripts/run_paper.py --dry-run` to verify connectivity
- [ ] **Startup banner correct**: Confirm log shows `C1 exit: trail_from_profit`, `HTF gate: 0.3`, `HC thresholds`, `Sweep detector: ENABLED`
- [ ] **HTF gate assertion passes**: Startup should NOT show assertion error about HTF gate drift
- [ ] **Symbol correct**: Confirm MNQ contract month matches current front month (e.g., MNQM6)
- [ ] **Time zone**: Confirm your system clock is accurate (session rules depend on UTC → ET conversion via `ZoneInfo("America/New_York")`)
- [ ] **Disk space**: Logs use JSONL with daily rotation — bounded growth, but ensure space for `logs/` directory
- [ ] **Position state file**: Verify `logs/position_state.json` is writable (crash recovery)

## How to Start

```bash
# Terminal 1 — Paper trading runner
cd nq_bot_vscode
python scripts/run_paper.py

# Terminal 2 — Live monitor (optional)
python scripts/paper_monitor.py

# Dry run (connect but don't trade)
python scripts/run_paper.py --dry-run
```

## First Hour — What to Watch

1. **Connection established**: Log should show `Paper trading connector ready`
2. **Bars arriving**: You should see bars being processed within 2 minutes of session open
3. **HC filter active**: Most bars will produce no signal (this is correct — HC filter is strict)
4. **First signal**: When `combined_score >= 0.75` and `stop <= 30pts`, an entry will be logged
5. **Entry logged**: Check `logs/paper_decisions_YYYY-MM-DD.jsonl` for the entry event with all metadata
6. **C1 exit**: Once unrealized profit >= 3pts, 2.5pt trailing stop activates from HWM. Fallback: market exit at bar 12.
7. **C2 trailing**: After C1 exits, C2 stop moves to BE+1 and trails with ATR-based stop
8. **Sweep signals**: Watch for `entry_source: sweep_only` or `entry_source: confluence` in decision logs

**Red flags to watch for:**
- No bars arriving after 5 minutes → connection issue
- Entries with `stop > 30pts` → HC filter leak (should not happen)
- Entries with `signal_score < 0.75` → HC filter bypass (should not happen)
- `EMERGENCY FLATTEN` in logs → connection lost > 60s
- `DAILY LOSS LIMIT HIT` → $500 loss, trading halted for the day
- HTF staleness warnings → HTF data feed may be disconnected

## Daily Review Process

At end of each trading day (after 4:30 PM ET):

1. **Check daily summary** in terminal output (auto-prints at 4:45 PM ET)
2. **Review trades**: `cat logs/paper_trades_$(date +%Y-%m-%d).jsonl | python -m json.tool | tail -50`
3. **Review decisions**: `cat logs/paper_decisions_$(date +%Y-%m-%d).jsonl | python -m json.tool | tail -50`
4. **Compare to baseline**:
   - Are we getting ~12 trades per day? (254/month ÷ 21 trading days ≈ 12)
   - Is win rate tracking near 62%?
   - Is expectancy near $17/trade?
   - Are sweep-only and confluence trades appearing? (expect ~2-3 sweep trades/day)
5. **Check for anomalies**: Run `python scripts/paper_monitor.py --snapshot`

## Weekly Metrics to Track

| Metric | Target | Red Flag |
|--------|--------|----------|
| Trades taken | 55-65/week | < 35 or > 85 |
| Win rate | > 58% | < 45% |
| Profit factor | > 1.3 | < 0.8 |
| Weekly PnL | > $800 | < -$300 |
| C1 PnL | Net positive | Consistently negative |
| Max single loss | < $150 | > $250 |
| Connection drops | 0 | > 2/week |
| Daily loss limit hits | 0 | > 1/week |
| HTF staleness warnings | 0 | Any persistent warnings |

## Stop Criteria

**STOP and investigate if ANY of these occur:**

- Paper PF < 0.8 after 50 trades
- 3 consecutive losing days with > $100 loss each
- Daily loss limit hit 2+ times in one week
- Connection drops causing emergency flattens
- Trades taken outside session hours (indicates time zone bug)
- C1 exits showing `target` instead of `c1_trail_from_profit` or `time_12bars_fallback`
- Stop distances consistently > 30pts (HC filter not enforced)
- Zero sweep-only or confluence trades after 100+ total trades (sweep detector may be broken)

## Success Criteria

**System validated for live trading if:**

- Paper PF > 1.3 after 100+ trades
- Win rate > 55%
- Max drawdown < 3%
- C1 PnL is net positive (trail-from-profit generating value)
- No emergency flattens from connection issues
- No HC filter or session rule violations
- Trade frequency matches OOS expectations (230-280/month)
- Sweep detector contributing (some sweep-only and confluence trades appearing)

---

*Checklist updated for Config D + Variant C (Trail from Profit) + Sweep Detector + Calibrated Slippage. Based on 6-month OOS validation: 1,524 trades, PF 1.73, $25,581, 1.4% DD, 61.9% WR, 6/6 months profitable.*
