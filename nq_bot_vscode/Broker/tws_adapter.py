"""
TWS Bar Adapter
================
Converts ib_insync bar data to the Bar dataclass format that process_bar() expects.

Handles:
  - Timezone conversion (IBKR sends ET, system expects UTC)
  - Session type detection (RTH vs ETH)
  - Validation of each bar before passing to process_bar()

The Bar dataclass interface is SACRED — we adapt data TO it, never change it.
"""

import logging
import math
from datetime import datetime, timezone, time as dt_time
from typing import Optional
from zoneinfo import ZoneInfo

from features.engine import Bar

logger = logging.getLogger(__name__)

ET_TZ = ZoneInfo("America/New_York")

# RTH = Regular Trading Hours: 9:30 AM - 4:00 PM ET
RTH_START = dt_time(9, 30)
RTH_END = dt_time(16, 0)


def get_session_type(ts: datetime) -> str:
    """Determine if timestamp falls in RTH or ETH."""
    et = ts.astimezone(ET_TZ)
    t = et.time()
    if RTH_START <= t < RTH_END and et.weekday() < 5:
        return "RTH"
    return "ETH"


def adapt_tws_bar(ib_bar) -> Optional[Bar]:
    """
    Convert an ib_insync RealTimeBar or BarData to a Bar dataclass.

    ib_insync RealTimeBar fields:
        time: datetime, open_: float, high: float, low: float,
        close: float, volume: int, wap: float, count: int

    ib_insync BarData fields (historical):
        date: datetime/str, open: float, high: float, low: float,
        close: float, volume: int, average: float, barCount: int

    Returns:
        Bar instance, or None if validation fails.
    """
    try:
        # Extract timestamp — handle both RealTimeBar and BarData
        if hasattr(ib_bar, "time"):
            raw_ts = ib_bar.time
        elif hasattr(ib_bar, "date"):
            raw_ts = ib_bar.date
        else:
            logger.warning("adapt_tws_bar: no timestamp field found")
            return None

        # Convert to UTC datetime
        if isinstance(raw_ts, datetime):
            if raw_ts.tzinfo is None:
                # Assume ET if no timezone
                ts = raw_ts.replace(tzinfo=ET_TZ).astimezone(timezone.utc)
            else:
                ts = raw_ts.astimezone(timezone.utc)
        elif isinstance(raw_ts, (int, float)):
            ts = datetime.fromtimestamp(raw_ts, tz=timezone.utc)
        elif isinstance(raw_ts, str):
            # ib_insync sometimes returns date strings for daily bars
            ts = datetime.fromisoformat(raw_ts).replace(tzinfo=timezone.utc)
        else:
            logger.warning("adapt_tws_bar: unrecognized timestamp type: %s", type(raw_ts))
            return None

        # Extract OHLCV — handle both field name conventions
        open_price = getattr(ib_bar, "open_", None) or getattr(ib_bar, "open", None)
        high_price = getattr(ib_bar, "high", None)
        low_price = getattr(ib_bar, "low", None)
        close_price = getattr(ib_bar, "close", None)
        volume = getattr(ib_bar, "volume", None)

        if any(v is None for v in (open_price, high_price, low_price, close_price, volume)):
            logger.warning("adapt_tws_bar: missing OHLCV field")
            return None

        # Validate OHLC
        for name, val in [("open", open_price), ("high", high_price),
                          ("low", low_price), ("close", close_price)]:
            if not isinstance(val, (int, float)) or not math.isfinite(val) or val <= 0:
                logger.warning("adapt_tws_bar: invalid %s=%s", name, val)
                return None

        # Validate volume
        volume = int(volume)
        if volume <= 0:
            logger.warning("adapt_tws_bar: zero/negative volume")
            return None

        # Validate high >= low
        if high_price < low_price:
            logger.warning("adapt_tws_bar: high < low (%.2f < %.2f)", high_price, low_price)
            return None

        # Extract optional fields
        wap = getattr(ib_bar, "wap", None) or getattr(ib_bar, "average", None)
        count = getattr(ib_bar, "count", None) or getattr(ib_bar, "barCount", None)

        # Determine session type
        session_type = get_session_type(ts)

        bar = Bar(
            timestamp=ts,
            open=round(float(open_price), 2),
            high=round(float(high_price), 2),
            low=round(float(low_price), 2),
            close=round(float(close_price), 2),
            volume=volume,
            vwap=round(float(wap), 2) if wap else 0.0,
            tick_count=int(count) if count else 0,
        )
        bar.session_type = session_type
        return bar

    except Exception as e:
        logger.warning("adapt_tws_bar failed: %s", e)
        return None


def validate_bar(bar: Bar) -> bool:
    """
    Validate a Bar before passing to process_bar().

    Checks:
      - All prices are finite and positive
      - High >= Low
      - Volume > 0
      - Timestamp is timezone-aware
    """
    if bar.timestamp.tzinfo is None:
        logger.warning("validate_bar: timestamp not timezone-aware")
        return False

    for name, val in [("open", bar.open), ("high", bar.high),
                      ("low", bar.low), ("close", bar.close)]:
        if not math.isfinite(val) or val <= 0:
            logger.warning("validate_bar: invalid %s=%s", name, val)
            return False

    if bar.high < bar.low:
        logger.warning("validate_bar: high < low")
        return False

    if bar.volume <= 0:
        logger.warning("validate_bar: zero/negative volume")
        return False

    return True
