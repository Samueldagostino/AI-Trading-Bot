# QuantData Integration — Quick Start

## Overview

The QuantData integration logs real-time options market data (gamma exposure, net flow, dark pool prints, dealer positioning) alongside every paper trade. This data does **NOT** affect confluence scoring yet — it is recorded for correlation analysis.

After 100+ paper trades, we analyze whether this data predicts winners, and THEN enable scoring.

## Option A: API Discovery (automated)

1. Run: `python data_feeds/quantdata_discovery.py`
2. Follow the browser instructions to capture API endpoints from v3.quantdata.us
3. If endpoints found: system auto-pulls data every 30 minutes during RTH
4. Auth token is stored in `.env` as `QUANTDATA_AUTH`

## Option B: Manual Input (works immediately)

1. Open v3.quantdata.us → Gamma Exposure page → QQQ
2. Note the gamma regime (positive/negative) and flip level
3. Go to Net Flow page → SPY/QQQ → note call/put premium direction
4. Edit `config/quantdata_manual_input.json` with today's values:

```json
{
  "timestamp": "2026-03-06T09:30:00",
  "symbol": "SPY",
  "gex": {
    "total_gex_millions": -500,
    "gamma_regime": "negative",
    "gamma_flip_level": 603.0,
    "nearest_wall_above": 610.0,
    "nearest_wall_below": 595.0
  },
  "net_flow": {
    "call_premium_billions": 1.7,
    "put_premium_billions": 1.5,
    "flow_direction": "bullish"
  },
  "dark_pool": {
    "dark_bias": "neutral",
    "significant_levels": [675.50, 672.00]
  },
  "vol_skew": {
    "skew_regime": "normal",
    "skew_slope": 0.0
  }
}
```

5. Save the file before starting the trading runner.

## What Gets Logged

Every trade in the paper journal now includes:
- `gamma_regime` — positive, negative, or neutral
- `flow_direction` — bullish, bearish, or neutral
- `dark_bias` — institutional dark pool positioning
- `skew_regime` — volatility skew state (normal, elevated, extreme)
- `flow_aligned_with_trade` — whether flow direction matched trade direction
- `favorable_for_momentum` — whether gamma regime favors momentum (negative = yes)
- `source` — api or manual
- `age_seconds` — how old the context snapshot was at trade time

## SPY/QQQ → NQ Translation

QuantData shows data for SPY and QQQ. Our system trades MNQ (Nasdaq-100 futures). The translation:

- **QQQ gamma exposure** → directly relevant to NQ/MNQ (QQQ tracks NDX, NQ is NDX futures)
- **SPY gamma exposure** → relevant because ES/NQ correlation is 0.85+
- **QQQ net flow** → primary signal for NQ directional bias
- **Price levels**: QQQ price × 40 ≈ NQ price (e.g., QQQ $500 ≈ NQ 20,000)

## When to Enable Scoring

After 100+ paper trades with context data, run:

```bash
python scripts/analyze_quantdata_correlation.py
```

If the analysis shows:
- **p < 0.05** for gamma regime → enable gamma scoring
- **p < 0.05** for flow alignment → enable flow scoring

To enable scoring:
1. Confirm statistical significance from the analysis
2. Set `QUANTDATA_SCORING=true` in `.env`
3. Update `ENABLED = True` in `data_feeds/context_scoring.py`

Both steps are required — the `.env` flag alone is not sufficient (safety measure).

## Architecture

```
QuantData Dashboard (v3.quantdata.us)
        │
        ├── Path A: Discovered API endpoints (preferred)
        ├── Path B: Manual snapshot input (fallback)
        │
        ▼
data_feeds/quantdata_client.py
        │
        ├── Pulls/receives: GEX, DEX, net flow, dark pool, vol skew
        ├── Computes: gamma regime, flow direction, institutional bias
        ├── Stores: MarketContext snapshot
        │
        ▼
data_feeds/market_context.py
        │
        ├── MarketContext dataclass (frozen snapshot of all external data)
        ├── NQContextTranslator (QQQ/SPY → NQ price translation)
        ├── Consumed by: paper_trading_journal.py (LOG per trade)
        ├── Future: context_scoring.py (SCORE boost — NOT YET)
        │
        ▼
logs/paper_journal_YYYY-MM-DD.json
        │
        └── Every trade now includes: gamma_regime, net_flow_direction,
            dark_pool_bias, gex_level, nearest_gamma_wall, vol_skew_slope
```

## File Locations

| File | Purpose |
|------|---------|
| `data_feeds/quantdata_client.py` | Dual-mode client (API + manual) |
| `data_feeds/quantdata_discovery.py` | API endpoint discovery tool |
| `data_feeds/market_context.py` | MarketContext dataclass + NQ translator |
| `data_feeds/context_scoring.py` | Scoring stubs (DISABLED) |
| `config/quantdata_endpoints.json` | API endpoint configuration |
| `config/quantdata_manual_input.json` | Manual data input template |
| `scripts/analyze_quantdata_correlation.py` | Post-hoc correlation analysis |

## Fallback Behavior

If no QuantData is available (no API, no manual file), the system:
- Sets all context fields to neutral/default
- Trades normally — QuantData is never required
- Logs `source: "default"` so you know no data was present
