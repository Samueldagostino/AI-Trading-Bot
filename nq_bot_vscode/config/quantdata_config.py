"""
Quant Data API Configuration
==============================
Config for Quant Data GEX (Gamma Exposure) data integration.
"""

# API base URL
QUANTDATA_API_BASE = "https://core-lb-prod.quantdata.us"

# API endpoints
QUANTDATA_ENDPOINTS = {
    "heat_map": "/api/options/heat-map/",
    "exposure_strike": "/api/options/exposure/strike/",
    "expirations": "/api/options/expirations",
}

# Auth token file (placeholder — never commit the real token)
TOKEN_FILE = "config/quantdata_token.txt"

# Instance ID file
INSTANCE_ID_FILE = "config/quantdata_instance_id.txt"

# Tiered refresh intervals based on trading state (respect rate limits)
REFRESH_INTERVAL_IDLE = 300       # 5 min — no position, low confluence
REFRESH_INTERVAL_ACTIVE = 120     # 2 min — position open
REFRESH_INTERVAL_PREFLIGHT = 60   # 1 min — high confluence, entry imminent

# Tickers to monitor
TICKERS = ["SPY", "QQQ"]

# Strike range: +/- 5% of spot price
STRIKE_RANGE_PCT = 0.05

# Number of nearest expirations to include
NEAREST_EXPIRATIONS = 4
