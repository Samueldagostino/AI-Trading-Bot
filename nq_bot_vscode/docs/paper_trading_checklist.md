# Paper Trading Launch Checklist

## System Under Test

| Parameter | Value |
|-----------|-------|
| Config | D |
| HC Filter | score >= 0.75, stop <= 30pts |
| C1 Exit | Time-based (10 bars, if profitable) |
| HTF Gate | strength >= 0.3 |
| Execution TF | 2m (aggregated from 1m Tradovate bars) |
| Contracts | 2 MNQ (C1 time exit + C2 runner) |
| Daily Loss Limit | $500 |
| Max Position | 2 contracts |
| Connection Timeout | 60s → emergency flatten |

## OOS Baseline (What We Expect)

| Metric | 6-Month OOS |
|--------|-------------|
| Trades/month | 158 |
| Win Rate | 68.1% |
| Profit Factor | 1.59 |
| PnL/month | $2,424 |
| Expectancy/trade | $15.34 |
| Max Drawdown | 1.7% |

---

## Pre-Launch Verification

- [ ] **Credentials configured**: Copy `.env.example` to `.env`, fill in Tradovate DEMO credentials
- [ ] **Demo account funded**: Verify $50,000 demo balance on Tradovate
- [ ] **Connection test**: Run `python scripts/run_paper.py --dry-run` to verify connectivity
- [ ] **Startup banner correct**: Confirm log shows `C1 exit: time-based (10 bars)`, `HTF gate: 0.3`, `HC thresholds`
- [ ] **HTF gate assertion passes**: Startup should NOT show assertion error about HTF gate drift
- [ ] **Symbol correct**: Confirm MNQ contract month matches current front month (e.g., MNQM5)
- [ ] **Time zone**: Confirm your system clock is accurate (session rules depend on UTC → ET conversion)
- [ ] **Disk space**: Logs will grow — ensure sufficient space in `logs/` directory

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
5. **Entry logged**: Check `logs/paper_decisions.json` for the entry event with all metadata
6. **C1 exit**: After 10 bars (20 minutes on 2m), if C1 is profitable, it will exit at market
7. **C2 trailing**: After C1 exits, C2 stop moves to BE+1 and trails with ATR-based stop

**Red flags to watch for:**
- No bars arriving after 5 minutes → connection issue
- Entries with `stop > 30pts` → HC filter leak (should not happen)
- Entries with `signal_score < 0.75` → HC filter bypass (should not happen)
- `EMERGENCY FLATTEN` in logs → connection lost > 60s
- `DAILY LOSS LIMIT HIT` → $500 loss, trading halted for the day

## Daily Review Process

At end of each trading day (after 4:30 PM ET):

1. **Check daily summary** in terminal output (auto-prints at 4:45 PM ET)
2. **Review trades**: `cat logs/paper_trades.json | python -m json.tool | tail -50`
3. **Review decisions**: `cat logs/paper_decisions.json | python -m json.tool | tail -50`
4. **Compare to baseline**:
   - Are we getting ~7-8 trades per day? (158/month ÷ 21 trading days ≈ 7.5)
   - Is win rate tracking near 68%?
   - Is expectancy near $15/trade?
5. **Check for anomalies**: Run `python scripts/paper_monitor.py --snapshot`

## Weekly Metrics to Track

| Metric | Target | Red Flag |
|--------|--------|----------|
| Trades taken | 35-40/week | < 20 or > 60 |
| Win rate | > 60% | < 45% |
| Profit factor | > 1.2 | < 0.8 |
| Weekly PnL | > $400 | < -$200 |
| C1 PnL | Net positive | Consistently negative |
| Max single loss | < $150 | > $250 |
| Connection drops | 0 | > 2/week |
| Daily loss limit hits | 0 | > 1/week |

## Stop Criteria

**STOP and investigate if ANY of these occur:**

- Paper PF < 0.8 after 50 trades
- 3 consecutive losing days with > $100 loss each
- Daily loss limit hit 2+ times in one week
- Connection drops causing emergency flattens
- Trades taken outside session hours (indicates time zone bug)
- C1 exits showing `target` instead of `time_10bars` (indicates wrong executor code)
- Stop distances consistently > 30pts (HC filter not enforced)

## Success Criteria

**System validated for live trading if:**

- Paper PF > 1.2 after 100+ trades
- Win rate > 55%
- Max drawdown < 3%
- C1 PnL is net positive
- No emergency flattens from connection issues
- No HC filter or session rule violations
- Trade frequency matches OOS expectations (150-165/month)

---

*Checklist created for Config D + C1 Time Exit. Based on 6-month OOS validation: 948 trades, PF 1.59, $14,544, 1.7% DD, 68.1% WR, 6/6 months profitable.*
