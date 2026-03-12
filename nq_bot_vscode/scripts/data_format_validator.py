"""
IBKR -> process_bar() Data Format Validator
=============================================
Documents and validates the EXACT format that process_bar() expects.

process_bar() signature:
    async def process_bar(self, bar: Bar) -> Optional[dict]

Bar dataclass (features/engine.py):
    @dataclass
    class Bar:
        timestamp: datetime     # UTC datetime with tzinfo (required)
        open: float             # Opening price (required, > 0, finite)
        high: float             # High price (required, >= open & close, finite)
        low: float              # Low price (required, <= open & close, finite)
        close: float            # Closing price (required, > 0, finite)
        volume: int             # Trade volume (required, > 0)
        bid_volume: int = 0     # Bid-side volume (optional, >= 0)
        ask_volume: int = 0     # Ask-side volume (optional, >= 0)
        delta: int = 0          # ask_volume - bid_volume (optional)
        tick_count: int = 0     # Number of ticks in bar (optional, >= 0)
        vwap: float = 0.0       # Volume-weighted avg price (optional, >= 0)
        session_type: Optional[str] = None  # "RTH" or "ETH" (optional)

Timezone note:
    process_bar() converts bar.timestamp to ET internally for session logic:
        et_time = bar.timestamp.astimezone(ZoneInfo("America/New_York"))
    Therefore timestamp MUST have tzinfo set (UTC recommended).

Data flow paths:
    1. Live: IBKR WS tick -> CandleAggregator -> candle dict -> candle_to_bar() -> Bar
    2. Backfill: get_historical_bars() -> candle dict -> candle_to_bar() -> Bar
    3. Dry-run: DryRunDataGenerator.generate_bar() -> Bar directly

Usage:
    from scripts.data_format_validator import validate_bar, validate_candle_dict

    bar = Bar(timestamp=..., open=..., ...)
    valid, errors = validate_bar(bar)

    candle = {"timestamp": ..., "open": ..., ...}
    valid, errors = validate_candle_dict(candle)
"""

import math
from datetime import datetime, timezone
from typing import List, Tuple, Any, Optional


# ================================================================
# SCHEMA DEFINITION
# ================================================================

REQUIRED_BAR_FIELDS = {
    "timestamp": datetime,
    "open": (int, float),
    "high": (int, float),
    "low": (int, float),
    "close": (int, float),
    "volume": (int, float),
}

OPTIONAL_BAR_FIELDS = {
    "bid_volume": (int, float),
    "ask_volume": (int, float),
    "delta": (int, float),
    "tick_count": (int, float),
    "vwap": (int, float),
    "session_type": (str, type(None)),
}

# All fields with their types
BAR_SCHEMA = {**REQUIRED_BAR_FIELDS, **OPTIONAL_BAR_FIELDS}


# ================================================================
# VALIDATION FUNCTIONS
# ================================================================

def validate_bar(bar: Any) -> Tuple[bool, List[str]]:
    """
    Validate a Bar object against the expected schema for process_bar().

    Args:
        bar: A features.engine.Bar instance (or any object with matching attrs).

    Returns:
        (is_valid, list_of_errors) -- True and empty list if valid.
    """
    errors: List[str] = []

    # Check it's an object with attributes (not a dict)
    if isinstance(bar, dict):
        errors.append("Expected Bar object, got dict. Use validate_candle_dict() for dicts.")
        return False, errors

    # Required fields
    for field_name, expected_type in REQUIRED_BAR_FIELDS.items():
        if not hasattr(bar, field_name):
            errors.append(f"Missing required field: {field_name}")
            continue
        val = getattr(bar, field_name)
        if val is None:
            errors.append(f"Required field '{field_name}' is None")
            continue
        if not isinstance(val, expected_type):
            errors.append(
                f"Field '{field_name}' type mismatch: expected {expected_type}, "
                f"got {type(val).__name__}"
            )

    # Timestamp must have timezone info
    if hasattr(bar, "timestamp") and bar.timestamp is not None:
        if isinstance(bar.timestamp, datetime):
            if bar.timestamp.tzinfo is None:
                errors.append(
                    "timestamp is timezone-naive. Must have tzinfo (UTC recommended) "
                    "for ET session logic in process_bar()"
                )
        else:
            errors.append(f"timestamp must be datetime, got {type(bar.timestamp).__name__}")

    # Price validation
    for price_field in ("open", "high", "low", "close"):
        if hasattr(bar, price_field):
            val = getattr(bar, price_field)
            if isinstance(val, (int, float)):
                if not math.isfinite(val):
                    errors.append(f"'{price_field}' is not finite: {val}")
                elif val <= 0:
                    errors.append(f"'{price_field}' must be positive: {val}")

    # OHLC relationship checks
    if (hasattr(bar, "high") and hasattr(bar, "low")
            and isinstance(getattr(bar, "high", None), (int, float))
            and isinstance(getattr(bar, "low", None), (int, float))):
        if bar.high < bar.low:
            errors.append(f"high ({bar.high}) < low ({bar.low})")

    if (hasattr(bar, "high") and hasattr(bar, "open")
            and isinstance(getattr(bar, "high", None), (int, float))
            and isinstance(getattr(bar, "open", None), (int, float))):
        if bar.high < bar.open:
            errors.append(f"high ({bar.high}) < open ({bar.open})")

    if (hasattr(bar, "high") and hasattr(bar, "close")
            and isinstance(getattr(bar, "high", None), (int, float))
            and isinstance(getattr(bar, "close", None), (int, float))):
        if bar.high < bar.close:
            errors.append(f"high ({bar.high}) < close ({bar.close})")

    if (hasattr(bar, "low") and hasattr(bar, "open")
            and isinstance(getattr(bar, "low", None), (int, float))
            and isinstance(getattr(bar, "open", None), (int, float))):
        if bar.low > bar.open:
            errors.append(f"low ({bar.low}) > open ({bar.open})")

    if (hasattr(bar, "low") and hasattr(bar, "close")
            and isinstance(getattr(bar, "low", None), (int, float))
            and isinstance(getattr(bar, "close", None), (int, float))):
        if bar.low > bar.close:
            errors.append(f"low ({bar.low}) > close ({bar.close})")

    # Volume checks
    if hasattr(bar, "volume") and isinstance(getattr(bar, "volume", None), (int, float)):
        if bar.volume <= 0:
            errors.append(f"volume must be positive: {bar.volume}")

    # Optional field type checks
    for field_name, expected_type in OPTIONAL_BAR_FIELDS.items():
        if hasattr(bar, field_name):
            val = getattr(bar, field_name)
            if val is not None and not isinstance(val, expected_type):
                errors.append(
                    f"Optional field '{field_name}' type mismatch: "
                    f"expected {expected_type}, got {type(val).__name__}"
                )

    # Non-negative checks for optional volume fields
    for field_name in ("bid_volume", "ask_volume", "tick_count"):
        if hasattr(bar, field_name):
            val = getattr(bar, field_name)
            if isinstance(val, (int, float)) and val < 0:
                errors.append(f"'{field_name}' must be non-negative: {val}")

    if hasattr(bar, "vwap"):
        val = getattr(bar, "vwap")
        if isinstance(val, (int, float)) and val < 0:
            errors.append(f"'vwap' must be non-negative: {val}")

    # session_type if set must be "RTH" or "ETH"
    if hasattr(bar, "session_type") and bar.session_type is not None:
        if bar.session_type not in ("RTH", "ETH"):
            errors.append(
                f"session_type must be 'RTH', 'ETH', or None, got '{bar.session_type}'"
            )

    return len(errors) == 0, errors


def validate_candle_dict(candle: dict) -> Tuple[bool, List[str]]:
    """
    Validate a candle dict (from CandleAggregator or get_historical_bars())
    against the expected schema for IBKRDataFeed.candle_to_bar().

    The candle dict is an intermediate format that candle_to_bar() converts
    to a Bar object.

    Required keys: timestamp, open, high, low, close, volume
    Optional keys: bid_volume, ask_volume, delta, tick_count, vwap, session_type

    Args:
        candle: dict with OHLCV data.

    Returns:
        (is_valid, list_of_errors) -- True and empty list if valid.
    """
    errors: List[str] = []

    if not isinstance(candle, dict):
        errors.append(f"Expected dict, got {type(candle).__name__}")
        return False, errors

    # Required keys
    for key in ("timestamp", "open", "high", "low", "close", "volume"):
        if key not in candle:
            errors.append(f"Missing required key: '{key}'")

    # Timestamp checks
    if "timestamp" in candle:
        ts = candle["timestamp"]
        if not isinstance(ts, datetime):
            errors.append(f"'timestamp' must be datetime, got {type(ts).__name__}")
        elif ts.tzinfo is None:
            errors.append("'timestamp' is timezone-naive. Must have tzinfo (UTC).")

    # Price checks
    for key in ("open", "high", "low", "close"):
        if key in candle:
            val = candle[key]
            if not isinstance(val, (int, float)):
                errors.append(f"'{key}' must be numeric, got {type(val).__name__}")
            elif not math.isfinite(val):
                errors.append(f"'{key}' is not finite: {val}")
            elif val <= 0:
                errors.append(f"'{key}' must be positive: {val}")

    # OHLC relationship
    if all(k in candle and isinstance(candle[k], (int, float)) for k in ("high", "low")):
        if candle["high"] < candle["low"]:
            errors.append(f"high ({candle['high']}) < low ({candle['low']})")

    # Volume
    if "volume" in candle:
        vol = candle["volume"]
        if not isinstance(vol, (int, float)):
            errors.append(f"'volume' must be numeric, got {type(vol).__name__}")
        elif vol <= 0:
            errors.append(f"'volume' must be positive: {vol}")

    return len(errors) == 0, errors


def get_bar_schema_doc() -> str:
    """Return a human-readable schema description for process_bar() input."""
    return """
process_bar() Expected Input Format (Bar dataclass)
=====================================================
REQUIRED FIELDS:
  timestamp    : datetime (UTC with tzinfo)  -- bar open time
  open         : float (> 0, finite)         -- opening price
  high         : float (>= open & close)     -- high price
  low          : float (<= open & close)     -- low price
  close        : float (> 0, finite)         -- closing price
  volume       : int (> 0)                   -- trade volume

OPTIONAL FIELDS (defaults shown):
  bid_volume   : int = 0                     -- bid-side volume
  ask_volume   : int = 0                     -- ask-side volume
  delta        : int = 0                     -- ask_volume - bid_volume
  tick_count   : int = 0                     -- ticks in this bar
  vwap         : float = 0.0                 -- volume-weighted avg price
  session_type : str | None = None           -- "RTH" or "ETH"

CONSTRAINTS:
  - high >= max(open, close)
  - low  <= min(open, close)
  - high >= low
  - All prices: finite, positive
  - Volume: positive integer
  - Timestamp: timezone-aware (UTC recommended)
  - Bars are 2-minute intervals for execution TF

DATA PATHS:
  1. Live IBKR:    WS tick -> CandleAggregator -> candle dict -> candle_to_bar() -> Bar
  2. Backfill:     get_historical_bars() -> candle dict -> candle_to_bar() -> Bar
  3. Dry-run:      DryRunDataGenerator.generate_bar() -> Bar directly
""".strip()
