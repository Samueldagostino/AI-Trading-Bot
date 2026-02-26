# NQ Futures AI Trading Bot — System Architecture

## Design Philosophy
- **Reality-first**: Every assumption is validated against real market microstructure
- **Survival-first**: The bot's job is to not blow up. Profit is a byproduct of survival.
- **Modularity**: Every layer is independently testable, replaceable, and observable

## System Layers

```
┌─────────────────────────────────────────────────────┐
│                 MONITORING LAYER                     │
│   PnL · Drawdown · Fill Quality · Regime · Decay    │
├──────────────┬──────────────┬───────────────────────┤
│  EXECUTION   │    RISK      │    SIGNAL ENGINE      │
│  Layer       │    Layer     │    (Confidence Score)  │
│  Orders      │  Sizing      │                       │
│  Fills       │  Limits      │  ┌─────────────────┐  │
│  Slippage    │  Kill Switch │  │ Discord Bias     │  │
│  Reconcile   │  Regime Gate │  │ Technical Signals│  │
│              │              │  │ ML Signals       │  │
├──────────────┴──────────────┘  └─────────────────┘  │
├─────────────────────────────────────────────────────┤
│                 FEATURE LAYER                        │
│  Order Flow · Liquidity Sweeps · OB · FVG · IFVG   │
│  VWAP Dev · ATR · Volume Profile · Delta            │
├─────────────────────────────────────────────────────┤
│                  DATA LAYER                          │
│  Discord Messages · NQ Tick/1min · VIX · Calendar   │
│  PostgreSQL · Roll Adjustments · Data Versioning    │
└─────────────────────────────────────────────────────┘
```

## Key Design Decisions
1. **Risk layer is INDEPENDENT** — it can override any signal or execution decision
2. **Kill switch** is hardware-level: separate process, separate logic, cannot be bypassed
3. **No lookahead**: features are computed strictly on past data with proper timestamps
4. **Execution assumes worst case**: 2-tick slippage, partial fills, 50ms latency
5. **Discord bias is ONE input** — never the sole trigger. Confluence required.

## Technology Stack
- Python 3.11+
- PostgreSQL 15+ (TimescaleDB extension recommended for time-series)
- asyncio for concurrent data ingestion
- discord.py for chat monitoring
- broker API (NinjaTrader / Interactive Brokers / Tradovate)
