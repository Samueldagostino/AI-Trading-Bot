"""
IBKR Data Adapter
==================
Adapts raw IBKR data formats to the Bar dataclass expected by process_bar().

Data flow verification (2026-03-04):
  The IBKR -> process_bar() data path was audited and found to be CORRECT.
  IBKRDataFeed.candle_to_bar() already handles the conversion properly.

  This module provides:
  1. adapt_ibkr_bar() — explicit adapter for IBKR candle dicts -> Bar
     (thin wrapper around candle_to_bar for standalone usage outside IBKRDataFeed)
  2. adapt_historical_bar() — converts raw IBKR API historical bar format
  3. Field mapping documentation

IBKR Raw Formats:
  Historical bars (from /iserver/marketdata/history):
    {"t": 1709000000000, "o": 20150.25, "h": 20155.50, "l": 20148.00, "c": 20153.75, "v": 8234}
    Already converted by get_historical_bars() to:
    {"timestamp": datetime, "open": float, "high": float, "low": float, "close": float, "volume": int}

  CandleAggregator output:
    {"timestamp": datetime, "open": float, "high": float, "low": float, "close": float,
     "volume": int, "tick_count": int, "session_type": SessionType}

  Both are converted to Bar by IBKRDataFeed.candle_to_bar() — VERIFIED CORRECT.
"""

import math
import logging
from datetime import datetime, timezone
from typing import Optional

from features.engine import Bar

logger = logging.getLogger(__name__)


def adapt_ibkr_bar(candle: dict) -> Optional[Bar]:
    """
    Convert an IBKR candle dict to a Bar for process_bar().

    This is equivalent to IBKRDataFeed.candle_to_bar() but usable
    standalone (e.g. in tests or alternate pipelines).

    Args:
        candle: Dict with keys: timestamp, open, high, low, close, volume.
                Optional: bid_volume, ask_volume, delta, tick_count, vwap, session_type.

    Returns:
        Bar instance, or None if validation fails.
    """
    # Required fields
    for field in ("timestamp", "open", "high", "low", "close", "volume"):
        if field not in candle:
            logger.warning("adapt_ibkr_bar: missing required field '%s'", field)
            return None

    # Validate OHLC prices
    for field in ("open", "high", "low", "close"):
        val = candle[field]
        if not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
            logger.warning("adapt_ibkr_bar: invalid %s=%s", field, val)
            return None

    # Validate volume
    if candle["volume"] <= 0:
        logger.warning("adapt_ibkr_bar: zero/negative volume")
        return None

    # High/Low sanity
    if candle["high"] < candle["low"]:
        logger.warning("adapt_ibkr_bar: high < low")
        return None

    # Extract session_type (may be enum or string)
    raw_session = candle.get("session_type")
    if raw_session is not None:
        session_str = raw_session.value if hasattr(raw_session, "value") else str(raw_session)
    else:
        session_str = None

    bar = Bar(
        timestamp=candle["timestamp"],
        open=candle["open"],
        high=candle["high"],
        low=candle["low"],
        close=candle["close"],
        volume=candle["volume"],
        bid_volume=candle.get("bid_volume", 0),
        ask_volume=candle.get("ask_volume", 0),
        delta=candle.get("delta", 0),
        tick_count=candle.get("tick_count", 0),
        vwap=candle.get("vwap", 0.0),
    )
    bar.session_type = session_str
    return bar


def adapt_historical_bar(raw: dict) -> Optional[dict]:
    """
    Convert a raw IBKR historical bar API response item to the standard
    candle dict format.

    Raw IBKR format:
        {"t": 1709000000000, "o": 20150.25, "h": 20155.50, "l": 20148.00,
         "c": 20153.75, "v": 8234}

    Output (standard candle dict):
        {"timestamp": datetime(UTC), "open": float, "high": float,
         "low": float, "close": float, "volume": int}

    Note: This conversion is already performed by IBKRClient.get_historical_bars().
    This function exists for explicit documentation and standalone use.
    """
    try:
        ts_ms = raw.get("t", 0)
        if ts_ms <= 0:
            return None

        return {
            "timestamp": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
            "open": round(raw.get("o", 0.0), 2),
            "high": round(raw.get("h", 0.0), 2),
            "low": round(raw.get("l", 0.0), 2),
            "close": round(raw.get("c", 0.0), 2),
            "volume": raw.get("v", 0),
        }
    except (TypeError, ValueError, OSError) as e:
        logger.warning("adapt_historical_bar failed: %s", e)
        return None


# ================================================================
# FIELD MAPPING DOCUMENTATION
# ================================================================

IBKR_TO_BAR_FIELD_MAP = {
    # IBKR historical API field -> Bar field
    "t": "timestamp",   # Unix millis -> datetime(UTC)
    "o": "open",        # float -> float (rounded 2dp)
    "h": "high",        # float -> float (rounded 2dp)
    "l": "low",         # float -> float (rounded 2dp)
    "c": "close",       # float -> float (rounded 2dp)
    "v": "volume",      # int -> int
}

IBKR_SNAPSHOT_TO_BAR_FIELD_MAP = {
    # IBKR snapshot field IDs -> meaning
    "31": "last_price",   # Used as tick price in CandleAggregator
    "84": "bid",          # Not directly in Bar
    "85": "ask",          # Not directly in Bar
    "86": "high",         # Session high
    "88": "low",          # Session low
}


# ================================================================
# TWS ADAPTER (ib_insync)
# ================================================================

def adapt_tws_bar(ib_bar) -> Optional[Bar]:
    """
    Convert an ib_insync RealTimeBar or BarData to a Bar dataclass.

    This is the TWS-specific counterpart to adapt_ibkr_bar() above.
    Delegates to Broker.tws_adapter for the actual conversion.

    Args:
        ib_bar: An ib_insync RealTimeBar or BarData object.

    Returns:
        Bar instance, or None if validation fails.
    """
    try:
        from Broker.tws_adapter import adapt_tws_bar as _adapt
        return _adapt(ib_bar)
    except ImportError:
        logger.warning("adapt_tws_bar: Broker.tws_adapter not available")
        return None
