"""
Tests for IBKR data format validation and adapter.

Tests:
- validate_bar with valid bar
- validate_bar with missing fields
- validate_bar with wrong types
- validate_bar with invalid values (negative prices, zero volume, etc.)
- validate_candle_dict with valid and invalid data
- adapt_ibkr_bar converts IBKR candle format correctly
- adapt_historical_bar converts raw IBKR API format
- CandleAggregator produces correct OHLCV from ticks
- DryRunDataGenerator produces valid bars
"""

import math
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from features.engine import Bar
from scripts.data_format_validator import (
    validate_bar,
    validate_candle_dict,
    get_bar_schema_doc,
)
from data_pipeline.ibkr_adapter import (
    adapt_ibkr_bar,
    adapt_historical_bar,
)
from Broker.ibkr_client import CandleAggregator


# ================================================================
# FIXTURES
# ================================================================

def make_valid_bar(**overrides) -> Bar:
    """Create a valid Bar for testing."""
    defaults = dict(
        timestamp=datetime(2025, 3, 4, 14, 30, 0, tzinfo=timezone.utc),
        open=20100.50,
        high=20110.25,
        low=20095.00,
        close=20105.75,
        volume=1500,
        bid_volume=700,
        ask_volume=800,
        delta=100,
        tick_count=42,
        vwap=20103.00,
    )
    defaults.update(overrides)
    return Bar(**defaults)


def make_valid_candle(**overrides) -> dict:
    """Create a valid candle dict for testing."""
    defaults = {
        "timestamp": datetime(2025, 3, 4, 14, 30, 0, tzinfo=timezone.utc),
        "open": 20100.50,
        "high": 20110.25,
        "low": 20095.00,
        "close": 20105.75,
        "volume": 1500,
    }
    defaults.update(overrides)
    return defaults


# ================================================================
# validate_bar TESTS
# ================================================================

class TestValidateBar:
    def test_valid_bar(self):
        bar = make_valid_bar()
        valid, errors = validate_bar(bar)
        assert valid is True
        assert errors == []

    def test_valid_bar_minimal(self):
        """Bar with only required fields (defaults for optional)."""
        bar = Bar(
            timestamp=datetime(2025, 3, 4, 14, 30, 0, tzinfo=timezone.utc),
            open=20100.0,
            high=20110.0,
            low=20090.0,
            close=20105.0,
            volume=100,
        )
        valid, errors = validate_bar(bar)
        assert valid is True
        assert errors == []

    def test_dict_rejected(self):
        valid, errors = validate_bar({"open": 100})
        assert valid is False
        assert any("dict" in e.lower() for e in errors)

    def test_missing_timestamp(self):
        bar = make_valid_bar()
        # Simulate missing attr
        bar_dict_like = MagicMock(spec=[])
        valid, errors = validate_bar(bar_dict_like)
        assert valid is False

    def test_naive_timestamp(self):
        bar = make_valid_bar(timestamp=datetime(2025, 3, 4, 14, 30, 0))  # no tzinfo
        valid, errors = validate_bar(bar)
        assert valid is False
        assert any("timezone-naive" in e for e in errors)

    def test_negative_price(self):
        bar = make_valid_bar(open=-100.0, low=-100.0)
        valid, errors = validate_bar(bar)
        assert valid is False
        assert any("positive" in e for e in errors)

    def test_nan_price(self):
        bar = make_valid_bar(close=float("nan"))
        valid, errors = validate_bar(bar)
        assert valid is False
        assert any("finite" in e for e in errors)

    def test_inf_price(self):
        bar = make_valid_bar(high=float("inf"))
        valid, errors = validate_bar(bar)
        assert valid is False
        assert any("finite" in e for e in errors)

    def test_high_less_than_low(self):
        bar = make_valid_bar(high=20090.0, low=20110.0)
        valid, errors = validate_bar(bar)
        assert valid is False
        assert any("high" in e and "low" in e for e in errors)

    def test_zero_volume(self):
        bar = make_valid_bar(volume=0)
        valid, errors = validate_bar(bar)
        assert valid is False
        assert any("volume" in e for e in errors)

    def test_negative_volume(self):
        bar = make_valid_bar(volume=-10)
        valid, errors = validate_bar(bar)
        assert valid is False

    def test_negative_bid_volume(self):
        bar = make_valid_bar(bid_volume=-5)
        valid, errors = validate_bar(bar)
        assert valid is False
        assert any("bid_volume" in e for e in errors)

    def test_invalid_session_type(self):
        bar = make_valid_bar()
        bar.session_type = "INVALID"
        valid, errors = validate_bar(bar)
        assert valid is False
        assert any("session_type" in e for e in errors)

    def test_valid_session_type_rth(self):
        bar = make_valid_bar()
        bar.session_type = "RTH"
        valid, errors = validate_bar(bar)
        assert valid is True

    def test_valid_session_type_eth(self):
        bar = make_valid_bar()
        bar.session_type = "ETH"
        valid, errors = validate_bar(bar)
        assert valid is True

    def test_session_type_none_is_valid(self):
        bar = make_valid_bar()
        bar.session_type = None
        valid, errors = validate_bar(bar)
        assert valid is True


# ================================================================
# validate_candle_dict TESTS
# ================================================================

class TestValidateCandleDict:
    def test_valid_candle(self):
        candle = make_valid_candle()
        valid, errors = validate_candle_dict(candle)
        assert valid is True
        assert errors == []

    def test_missing_open(self):
        candle = make_valid_candle()
        del candle["open"]
        valid, errors = validate_candle_dict(candle)
        assert valid is False
        assert any("open" in e for e in errors)

    def test_missing_timestamp(self):
        candle = make_valid_candle()
        del candle["timestamp"]
        valid, errors = validate_candle_dict(candle)
        assert valid is False

    def test_naive_timestamp(self):
        candle = make_valid_candle(
            timestamp=datetime(2025, 3, 4, 14, 30, 0)
        )
        valid, errors = validate_candle_dict(candle)
        assert valid is False
        assert any("timezone-naive" in e for e in errors)

    def test_string_price(self):
        candle = make_valid_candle(open="bad")
        valid, errors = validate_candle_dict(candle)
        assert valid is False
        assert any("numeric" in e for e in errors)

    def test_not_a_dict(self):
        valid, errors = validate_candle_dict("not a dict")
        assert valid is False
        assert any("dict" in e for e in errors)

    def test_zero_volume(self):
        candle = make_valid_candle(volume=0)
        valid, errors = validate_candle_dict(candle)
        assert valid is False


# ================================================================
# adapt_ibkr_bar TESTS
# ================================================================

class TestAdaptIbkrBar:
    def test_valid_candle(self):
        candle = make_valid_candle()
        bar = adapt_ibkr_bar(candle)
        assert bar is not None
        assert isinstance(bar, Bar)
        assert bar.open == 20100.50
        assert bar.high == 20110.25
        assert bar.low == 20095.00
        assert bar.close == 20105.75
        assert bar.volume == 1500
        assert bar.timestamp == candle["timestamp"]

    def test_with_optional_fields(self):
        candle = make_valid_candle()
        candle["bid_volume"] = 600
        candle["ask_volume"] = 900
        candle["delta"] = 300
        candle["tick_count"] = 55
        candle["vwap"] = 20102.0
        bar = adapt_ibkr_bar(candle)
        assert bar is not None
        assert bar.bid_volume == 600
        assert bar.ask_volume == 900
        assert bar.delta == 300
        assert bar.tick_count == 55
        assert bar.vwap == 20102.0

    def test_with_session_type_string(self):
        candle = make_valid_candle()
        candle["session_type"] = "RTH"
        bar = adapt_ibkr_bar(candle)
        assert bar is not None
        assert bar.session_type == "RTH"

    def test_with_session_type_enum(self):
        from Broker.ibkr_client import SessionType
        candle = make_valid_candle()
        candle["session_type"] = SessionType.ETH
        bar = adapt_ibkr_bar(candle)
        assert bar is not None
        assert bar.session_type == "ETH"

    def test_missing_field_returns_none(self):
        candle = make_valid_candle()
        del candle["close"]
        assert adapt_ibkr_bar(candle) is None

    def test_invalid_price_returns_none(self):
        candle = make_valid_candle(open=-100.0)
        assert adapt_ibkr_bar(candle) is None

    def test_nan_price_returns_none(self):
        candle = make_valid_candle(close=float("nan"))
        assert adapt_ibkr_bar(candle) is None

    def test_zero_volume_returns_none(self):
        candle = make_valid_candle(volume=0)
        assert adapt_ibkr_bar(candle) is None

    def test_high_lt_low_returns_none(self):
        candle = make_valid_candle(high=20090.0, low=20110.0)
        assert adapt_ibkr_bar(candle) is None

    def test_defaults_for_missing_optional(self):
        candle = make_valid_candle()
        bar = adapt_ibkr_bar(candle)
        assert bar is not None
        assert bar.bid_volume == 0
        assert bar.ask_volume == 0
        assert bar.delta == 0
        assert bar.tick_count == 0
        assert bar.vwap == 0.0
        assert bar.session_type is None


# ================================================================
# adapt_historical_bar TESTS
# ================================================================

class TestAdaptHistoricalBar:
    def test_valid_raw_bar(self):
        raw = {
            "t": 1709000000000,
            "o": 20150.25,
            "h": 20155.50,
            "l": 20148.00,
            "c": 20153.75,
            "v": 8234,
        }
        result = adapt_historical_bar(raw)
        assert result is not None
        assert isinstance(result["timestamp"], datetime)
        assert result["timestamp"].tzinfo is not None
        assert result["open"] == 20150.25
        assert result["high"] == 20155.50
        assert result["low"] == 20148.00
        assert result["close"] == 20153.75
        assert result["volume"] == 8234

    def test_zero_timestamp(self):
        raw = {"t": 0, "o": 100, "h": 105, "l": 95, "c": 102, "v": 50}
        assert adapt_historical_bar(raw) is None

    def test_missing_timestamp(self):
        raw = {"o": 100, "h": 105, "l": 95, "c": 102, "v": 50}
        assert adapt_historical_bar(raw) is None

    def test_roundtrip_with_adapt_ibkr_bar(self):
        """Raw IBKR -> adapt_historical_bar -> adapt_ibkr_bar -> Bar."""
        raw = {
            "t": 1709000000000,
            "o": 20150.253,
            "h": 20155.501,
            "l": 20148.004,
            "c": 20153.756,
            "v": 8234,
        }
        candle = adapt_historical_bar(raw)
        assert candle is not None
        bar = adapt_ibkr_bar(candle)
        assert bar is not None
        assert isinstance(bar, Bar)
        assert bar.open == 20150.25  # rounded
        assert bar.volume == 8234


# ================================================================
# CANDLE AGGREGATION TESTS
# ================================================================

class TestCandleAggregation:
    def test_basic_aggregation(self):
        """Feed multiple ticks into same window, verify OHLCV."""
        emitted = []
        agg = CandleAggregator(on_candle=lambda c: emitted.append(c))

        base = datetime(2025, 3, 4, 14, 30, 0, tzinfo=timezone.utc)

        # Window 1: 14:30:00 - 14:31:59
        agg.process_tick(20100.0, 100, base + timedelta(seconds=0))
        agg.process_tick(20110.0, 50, base + timedelta(seconds=30))   # high
        agg.process_tick(20090.0, 75, base + timedelta(seconds=60))   # low
        agg.process_tick(20105.0, 200, base + timedelta(seconds=90))  # close

        # No candle yet (still in window 1)
        assert len(emitted) == 0

        # First tick in window 2 (14:32:00) closes window 1
        agg.process_tick(20106.0, 150, base + timedelta(seconds=120))
        assert len(emitted) == 1

        candle = emitted[0]
        assert candle["open"] == 20100.0
        assert candle["high"] == 20110.0
        assert candle["low"] == 20090.0
        assert candle["close"] == 20105.0
        assert candle["volume"] == 425  # 100 + 50 + 75 + 200
        assert candle["tick_count"] == 4
        assert candle["timestamp"] == base

    def test_flush_emits_partial(self):
        """flush() emits current partial candle."""
        emitted = []
        agg = CandleAggregator(on_candle=lambda c: emitted.append(c))

        base = datetime(2025, 3, 4, 14, 30, 0, tzinfo=timezone.utc)
        agg.process_tick(20100.0, 100, base)
        agg.process_tick(20105.0, 50, base + timedelta(seconds=30))

        candle = agg.flush()
        assert candle is not None
        assert candle["open"] == 20100.0
        assert candle["close"] == 20105.0
        assert candle["volume"] == 150

    def test_zero_volume_rejected(self):
        """Candle with zero volume is rejected."""
        emitted = []
        agg = CandleAggregator(on_candle=lambda c: emitted.append(c))

        # Can't easily make zero volume with process_tick since each tick
        # has volume >= 1. But we can check the validation directly.
        assert CandleAggregator._validate_candle({
            "open": 100, "high": 105, "low": 95, "close": 102, "volume": 0
        }) == "zero_volume"

    def test_invalid_price_rejected(self):
        """Non-finite price tick is dropped."""
        agg = CandleAggregator()
        result = agg.process_tick(float("nan"), 100,
                                  datetime(2025, 3, 4, 14, 30, tzinfo=timezone.utc))
        assert result is None
        assert agg._ticks_processed == 0  # nan tick not counted

    def test_negative_price_rejected(self):
        """Negative price tick is dropped."""
        agg = CandleAggregator()
        result = agg.process_tick(-100.0, 100,
                                  datetime(2025, 3, 4, 14, 30, tzinfo=timezone.utc))
        assert result is None
        assert agg._ticks_processed == 0

    def test_out_of_order_tick_dropped(self):
        """Ticks older than last candle are dropped."""
        emitted = []
        agg = CandleAggregator(on_candle=lambda c: emitted.append(c))

        base = datetime(2025, 3, 4, 14, 30, 0, tzinfo=timezone.utc)

        # Fill window 1
        agg.process_tick(20100.0, 100, base)
        # Close window 1 by starting window 2
        agg.process_tick(20105.0, 50, base + timedelta(seconds=120))
        assert len(emitted) == 1

        # Send out-of-order tick from before window 1
        old_result = agg.process_tick(20095.0, 10, base - timedelta(seconds=60))
        assert old_result is None
        assert agg._ticks_out_of_order == 1

    def test_two_minute_window_boundaries(self):
        """Verify 2-minute window calculation."""
        # Even minutes map to themselves
        ts = datetime(2025, 3, 4, 14, 30, 45, tzinfo=timezone.utc)
        assert CandleAggregator._get_window_start(ts).minute == 30

        # Odd minutes map to previous even minute
        ts = datetime(2025, 3, 4, 14, 31, 45, tzinfo=timezone.utc)
        assert CandleAggregator._get_window_start(ts).minute == 30

        ts = datetime(2025, 3, 4, 14, 33, 0, tzinfo=timezone.utc)
        assert CandleAggregator._get_window_start(ts).minute == 32

    def test_multiple_windows(self):
        """Process ticks across 3 windows and verify all candles."""
        emitted = []
        agg = CandleAggregator(on_candle=lambda c: emitted.append(c))

        base = datetime(2025, 3, 4, 14, 30, 0, tzinfo=timezone.utc)

        # Window 1: 14:30
        agg.process_tick(100.0, 10, base)
        agg.process_tick(105.0, 10, base + timedelta(seconds=60))

        # Window 2: 14:32 (closes window 1)
        agg.process_tick(110.0, 10, base + timedelta(seconds=120))
        agg.process_tick(108.0, 10, base + timedelta(seconds=180))

        # Window 3: 14:34 (closes window 2)
        agg.process_tick(112.0, 10, base + timedelta(seconds=240))

        assert len(emitted) == 2

        # Window 1
        assert emitted[0]["open"] == 100.0
        assert emitted[0]["close"] == 105.0
        assert emitted[0]["volume"] == 20

        # Window 2
        assert emitted[1]["open"] == 110.0
        assert emitted[1]["close"] == 108.0
        assert emitted[1]["volume"] == 20


# ================================================================
# DRY-RUN GENERATOR TESTS
# ================================================================

class TestDryRunDataGenerator:
    def test_generates_valid_bars(self):
        """DryRunDataGenerator produces bars that pass validation."""
        from scripts.run_paper_live import DryRunDataGenerator

        gen = DryRunDataGenerator(base_price=20000.0)
        for _ in range(50):
            bar = gen.generate_bar()
            valid, errors = validate_bar(bar)
            assert valid, f"DryRun bar validation failed: {errors}"

    def test_bar_fields_present(self):
        from scripts.run_paper_live import DryRunDataGenerator

        gen = DryRunDataGenerator()
        bar = gen.generate_bar()
        assert hasattr(bar, "timestamp")
        assert hasattr(bar, "open")
        assert hasattr(bar, "high")
        assert hasattr(bar, "low")
        assert hasattr(bar, "close")
        assert hasattr(bar, "volume")

    def test_ohlc_relationships(self):
        from scripts.run_paper_live import DryRunDataGenerator

        gen = DryRunDataGenerator()
        for _ in range(100):
            bar = gen.generate_bar()
            assert bar.high >= bar.low
            assert bar.high >= bar.open
            assert bar.high >= bar.close
            assert bar.low <= bar.open
            assert bar.low <= bar.close

    def test_positive_volume(self):
        from scripts.run_paper_live import DryRunDataGenerator

        gen = DryRunDataGenerator()
        for _ in range(100):
            bar = gen.generate_bar()
            assert bar.volume > 0


# ================================================================
# SCHEMA DOC TEST
# ================================================================

class TestSchemaDoc:
    def test_schema_doc_not_empty(self):
        doc = get_bar_schema_doc()
        assert len(doc) > 100
        assert "timestamp" in doc
        assert "open" in doc
        assert "process_bar()" in doc
