# NQ Futures AI Trading Bot

**Institutional-grade automated trading system for NQ (Nasdaq-100) Futures**

Built on three pillars:
1. **Ruthless Scientific Discipline** — reality-first, bias-free, validated
2. **Risk-First Engineering** — survival is the job, profit is the byproduct
3. **Systems Thinking & Modularity** — testable, upgradeable, observable

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│              MONITORING LAYER                     │
│   PnL · Drawdown · Fill Quality · Regime · Alerts│
├──────────┬──────────┬────────────────────────────┤
│ EXECUTION│   RISK   │      SIGNAL ENGINE         │
│ Orders   │ Sizing   │  Discord  + Technical + ML │
│ Fills    │ Limits   │  Confidence Scoring        │
│ Slippage │ Kill SW  │  Confluence Required       │
├──────────┴──────────┴────────────────────────────┤
│              FEATURE LAYER                        │
│ Order Blocks · FVG · IFVG · Liquidity Sweeps     │
│ VWAP + Bands · Order Flow Delta · ATR · Trend    │
├──────────────────────────────────────────────────┤
│               DATA LAYER                          │
│ Discord Messages · NQ 1-min OHLCV · VIX · Calendar│
│ PostgreSQL · Roll Adjustments · Data Versioning   │
└──────────────────────────────────────────────────┘
```

## Module Reference

| Module | File | Purpose |
|--------|------|---------|
| **Config** | `config/settings.py` | All tunable parameters — single source of truth |
| **Database** | `database/schema.sql`, `database/connection.py` | PostgreSQL schema + async connection pool |
| **Discord** | `discord_ingestion/listener.py` | Channel monitoring, bias parsing, author reliability |
| **Features** | `features/engine.py` | OB, FVG, IFVG, sweeps, VWAP, delta, trend |
| **Signals** | `signals/aggregator.py` | Multi-source confluence scoring |
| **Risk** | `risk/engine.py` | Position sizing, daily limits, kill switch |
| **Regime** | `risk/regime_detector.py` | Market regime classification + adaptive behavior |
| **Execution** | `execution/engine.py` | Paper/live order management |
| **Monitoring** | `monitoring/engine.py` | Real-time metrics, alerts, dashboard |
| **Orchestrator** | `main.py` | Main loop tying all layers together |

---

## Setup

### 1. Prerequisites
- Python 3.11+
- PostgreSQL 15+
- Discord Bot Token (with MESSAGE_CONTENT intent)

### 2. Install
```bash
git clone <repo>
cd nq_trading_bot
pip install -r requirements.txt
cp .env.template .env
# Edit .env with your credentials
```

### 3. Database Setup
```bash
createdb nq_trading
psql nq_trading < database/schema.sql
```

### 4. Discord Bot Setup
1. Go to https://discord.com/developers/applications
2. Create application → Bot → Enable MESSAGE CONTENT intent
3. Copy token to `.env`
4. Invite bot to your server with Read Messages permission
5. Add channel IDs to `.env`

### 5. Run
```bash
python main.py
```

---

## Risk Controls (Hard Limits)

| Control | Default | Description |
|---------|---------|-------------|
| Max risk per trade | 1% | Maximum account risk per position |
| Daily loss limit | 3% | Stop trading after 3% daily loss |
| Weekly loss limit | 5% | Reduced activity after 5% weekly loss |
| Max drawdown | 10% | Kill switch — complete shutdown |
| Max consecutive losses | 5 | Kill switch + 60-min cooldown |
| VIX > 25 | Half size | Reduce exposure in elevated vol |
| VIX > 40 | No trading | Complete shutdown |
| Near FOMC/CPI | No new trades | 15 min before, 10 min after |
| Overnight session | Half size | Reduced liquidity protection |
| Min R:R ratio | 1.5:1 | Reject trades below this ratio |

---

## Signal Confluence

A trade requires **minimum 3 independent signals** agreeing:

**Discord (25% weight):**
- Parsed bias from monitored channels
- Author reliability scoring (Bayesian-updated)
- NEVER sufficient alone — requires technical confirmation

**Technical (50% weight):**
- Order Block proximity (bullish/bearish)
- Fair Value Gap (price inside FVG zone)
- Inverse FVG (flipped polarity confirmation)
- Liquidity sweep (buy-side → short, sell-side → long)
- VWAP deviation (mean-reversion when extended)
- Delta divergence (smart money footprint)
- Trend alignment (EMA crossover confirmation)

**ML Model (25% weight):**
- Placeholder for future implementation
- Designed to accept any model that outputs direction + confidence

---

## What Needs Building Next

### Phase 2 — Data Pipeline (YOU PROVIDE)
1. **Market data feed**: Connect to a live NQ data source
   - Options: Tradovate WebSocket, IB TWS, CQG, Rithmic
   - I need to know which broker/data provider you'll use
2. **Historical data backfill**: Load 2+ years of 1-min NQ bars
   - For walk-forward testing across regimes
3. **VIX data feed**: Real-time VIX for regime detection
4. **Economic calendar API**: For news-event guards

### Phase 3 — Broker Integration
1. Implement `_execute_live()` in `execution/engine.py`
2. Order reconciliation (confirm fills match expected)
3. Account equity sync from broker

### Phase 4 — Backtesting & Validation
1. Walk-forward optimization across regimes
2. Monte Carlo simulation of equity curves
3. Parameter stability testing
4. Slippage stress testing (2x, 4x, 8x normal)

### Phase 5 — ML Layer
1. Feature engineering from computed signals
2. Regime-aware model training
3. Online learning / model decay detection

### Phase 6 — Production Hardening
1. Systemd service for auto-restart
2. Prometheus metrics export
3. Slack/Discord alerting from monitoring engine
4. Database backup automation

---

## Questions I Need From You

1. **Broker**: Which broker will you use? (Tradovate, Interactive Brokers, NinjaTrader?)
2. **Data feed**: Do you have a market data provider, or do we need to set one up?
3. **Discord channels**: Which specific channels should the bot monitor?
4. **Account size**: What's the starting capital? (Currently set to $50K)
5. **MNQ vs NQ**: Micro (MNQ, $0.50/tick) or full NQ ($5.00/tick)?
6. **Historical data**: Do you have historical NQ 1-min bars for backtesting?
7. **Deployment**: Where will this run? (VPS, home server, cloud?)

---

## Safety Notice

**This bot starts in PAPER TRADING mode by default.**
Live trading requires explicitly changing `paper_trading = False` in config.
Always validate with extensive paper trading before risking real capital.
Futures trading carries substantial risk of loss.
