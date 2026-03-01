"""
Tests for IBKR Client Portal API connector.

Tests:
- Contract resolution and rollover detection
- Market data snapshot parsing
- Session keepalive and reconnection logic
- Historical bar fetching
- Price parsing edge cases
- Connection health status
- Candle aggregation from tick/snapshot data
- Session type detection (RTH vs ETH)
- Data quality checks (zero volume, high < low, gap detection)
- WebSocket streaming and HTTP polling fallback
- IBKRDataFeed lifecycle, backfill, and health monitoring
"""

import asyncio
import json
import math
import time
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock, call

from Broker.ibkr_client import (
    IBKRClient,
    IBKRConfig,
    IBKRDataFeed,
    IBKRWebSocket,
    CandleAggregator,
    ContractInfo,
    MarketSnapshot,
    SessionType,
    get_session_type,
    BACKFILL_BAR_SIZE,
    BACKFILL_PERIOD,
    CANDLE_INTERVAL_SECONDS,
    CONSECUTIVE_GAP_ALERT_THRESHOLD,
    SNAPSHOT_FIELDS,
    WS_FALLBACK_THRESHOLD,
)


# ================================================================
# FIXTURES
# ================================================================

@pytest.fixture
def config():
    return IBKRConfig(
        gateway_host="localhost",
        gateway_port=5000,
        account_type="paper",
        symbol="MNQ",
    )


@pytest.fixture
def client(config):
    return IBKRClient(config)


# ================================================================
# CONFIG TESTS
# ================================================================

class TestIBKRConfig:
    def test_default_config(self):
        cfg = IBKRConfig()
        assert cfg.gateway_host == "localhost"
        assert cfg.gateway_port == 5000
        assert cfg.account_type == "paper"
        assert cfg.symbol == "MNQ"

    def test_base_url(self):
        cfg = IBKRConfig(gateway_host="192.168.1.10", gateway_port=5001)
        assert cfg.base_url == "https://192.168.1.10:5001/v1/api"

    def test_is_live(self):
        paper = IBKRConfig(account_type="paper")
        live = IBKRConfig(account_type="live")
        assert not paper.is_live
        assert live.is_live


# ================================================================
# PRICE PARSING TESTS
# ================================================================

class TestPriceParser:
    def test_parse_float(self, client):
        assert client._parse_price(21050.75) == 21050.75

    def test_parse_int(self, client):
        assert client._parse_price(21050) == 21050.0

    def test_parse_string(self, client):
        assert client._parse_price("21050.50") == 21050.50

    def test_parse_string_with_c_prefix(self, client):
        """IBKR prefixes closing prices with 'C'."""
        assert client._parse_price("C21050.25") == 21050.25

    def test_parse_none(self, client):
        assert client._parse_price(None) == 0.0

    def test_parse_invalid_string(self, client):
        assert client._parse_price("N/A") == 0.0

    def test_parse_empty_string(self, client):
        assert client._parse_price("") == 0.0

    def test_rounds_to_2dp(self, client):
        assert client._parse_price(21050.123456) == 21050.12


# ================================================================
# CONTRACT RESOLUTION TESTS
# ================================================================

class TestContractResolution:
    @pytest.mark.asyncio
    async def test_resolve_front_month_success(self, client):
        """Should parse search results and return front-month contract."""
        search_response = [
            {
                "conid": 654321,
                "symbol": "MNQ",
                "secType": "FUT",
                "exchange": "CME",
                "companyHeader": "Micro E-mini Nasdaq-100",
                "sections": [
                    {"secType": "FUT", "months": "MAR2026;JUN2026", "exchange": "CME"}
                ],
            }
        ]

        contract_info_response = {
            "conid": 654321,
            "symbol": "MNQH6",
            "exchange": "CME",
            "maturity_date": "20260320",
            "company_name": "Micro E-mini Nasdaq-100 Mar26",
        }

        client._session = MagicMock()
        client._post = AsyncMock(return_value=search_response)
        client._get = AsyncMock(return_value=contract_info_response)

        result = await client._resolve_front_month("MNQ")

        assert result is not None
        assert result.conid == 654321
        assert result.symbol == "MNQH6"
        assert result.expiry == "20260320"

    @pytest.mark.asyncio
    async def test_resolve_front_month_no_results(self, client):
        """Should return None if search returns empty."""
        client._session = MagicMock()
        client._post = AsyncMock(return_value=[])

        result = await client._resolve_front_month("MNQ")
        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_front_month_search_fails(self, client):
        """Should return None if search request fails."""
        client._session = MagicMock()
        client._post = AsyncMock(return_value=None)

        result = await client._resolve_front_month("MNQ")
        assert result is None

    @pytest.mark.asyncio
    async def test_contract_rollover_detected(self, client):
        """Should detect when front-month conid changes."""
        client._contract = ContractInfo(
            conid=100, symbol="MNQH6", expiry="20260320"
        )

        new_contract = ContractInfo(
            conid=200, symbol="MNQM6", expiry="20260619"
        )

        with patch.object(client, "_resolve_front_month",
                          new_callable=AsyncMock, return_value=new_contract):
            result = await client.check_contract_rollover()

        assert result is not None
        assert result.conid == 200
        assert result.symbol == "MNQM6"
        assert client._contract.conid == 200  # Updated internally

    @pytest.mark.asyncio
    async def test_contract_no_rollover(self, client):
        """Should return None when contract hasn't changed."""
        current = ContractInfo(conid=100, symbol="MNQH6", expiry="20260320")
        client._contract = current

        same_contract = ContractInfo(conid=100, symbol="MNQH6", expiry="20260320")

        with patch.object(client, "_resolve_front_month",
                          new_callable=AsyncMock, return_value=same_contract):
            result = await client.check_contract_rollover()

        assert result is None
        assert client._contract.conid == 100  # Unchanged

    @pytest.mark.asyncio
    async def test_contract_rollover_no_existing(self, client):
        """Should return None if no contract was resolved yet."""
        client._contract = None
        result = await client.check_contract_rollover()
        assert result is None


# ================================================================
# MARKET DATA SNAPSHOT TESTS
# ================================================================

class TestMarketSnapshot:
    @pytest.mark.asyncio
    async def test_parse_snapshot(self, client):
        """Should parse IBKR snapshot response into MarketSnapshot."""
        client._contract = ContractInfo(conid=654321, symbol="MNQ")

        api_response = [
            {
                "conid": 654321,
                "31": "21050.25",
                "84": "21050.00",
                "85": "21050.50",
                "86": "21100.00",
                "88": "20900.00",
            }
        ]

        client._get = AsyncMock(return_value=api_response)

        snapshot = await client.get_market_snapshot()

        assert snapshot is not None
        assert snapshot.conid == 654321
        assert snapshot.last_price == 21050.25
        assert snapshot.bid == 21050.00
        assert snapshot.ask == 21050.50
        assert snapshot.high == 21100.00
        assert snapshot.low == 20900.00
        assert snapshot.timestamp is not None

    @pytest.mark.asyncio
    async def test_snapshot_no_contract(self, client):
        """Should return None if no contract resolved."""
        client._contract = None
        result = await client.get_market_snapshot()
        assert result is None

    @pytest.mark.asyncio
    async def test_snapshot_api_failure(self, client):
        """Should return None on API error."""
        client._contract = ContractInfo(conid=123, symbol="MNQ")
        client._get = AsyncMock(return_value=None)

        result = await client.get_market_snapshot()
        assert result is None

    @pytest.mark.asyncio
    async def test_snapshot_dict_response(self, client):
        """Should handle dict response (single conid)."""
        client._contract = ContractInfo(conid=123, symbol="MNQ")

        api_response = {
            "conid": 123,
            "31": 21000.0,
            "84": 20999.75,
            "85": 21000.25,
            "86": 21050.0,
            "88": 20950.0,
        }

        client._get = AsyncMock(return_value=api_response)

        snapshot = await client.get_market_snapshot()
        assert snapshot is not None
        assert snapshot.last_price == 21000.0


# ================================================================
# SESSION & KEEPALIVE TESTS
# ================================================================

class TestSessionKeepalive:
    @pytest.mark.asyncio
    async def test_tickle_success(self, client):
        """Tickle should update last_keepalive time."""
        client._session = MagicMock()
        client._post = AsyncMock(return_value={"session": "abc123"})

        result = await client._tickle()

        assert result is True
        assert client._session_valid is True
        assert client._last_keepalive > 0

    @pytest.mark.asyncio
    async def test_tickle_failure(self, client):
        """Tickle failure should return False."""
        client._session = MagicMock()
        client._post = AsyncMock(return_value=None)

        result = await client._tickle()

        assert result is False

    @pytest.mark.asyncio
    async def test_auth_status_authenticated(self, client):
        """Should detect authenticated session."""
        client._session = MagicMock()
        client._get = AsyncMock(return_value={"authenticated": True, "competing": False})

        result = await client._check_auth_status()

        assert result is True
        assert client._session_valid is True

    @pytest.mark.asyncio
    async def test_auth_status_not_authenticated(self, client):
        """Should detect unauthenticated session."""
        client._session = MagicMock()
        client._get = AsyncMock(return_value={"authenticated": False, "competing": False})

        result = await client._check_auth_status()

        assert result is False

    @pytest.mark.asyncio
    async def test_auth_status_competing(self, client):
        """Should handle competing session by calling /compete."""
        call_count = 0

        async def mock_get(endpoint, params=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"authenticated": False, "competing": True}
            return {"authenticated": True, "competing": False}

        client._session = MagicMock()
        client._get = mock_get
        client._post = AsyncMock(return_value={})

        result = await client._check_auth_status()

        assert result is True
        client._post.assert_called_once()  # /compete was called

    @pytest.mark.asyncio
    async def test_fetch_account_paper(self, client):
        """Should select paper account (DU prefix)."""
        client._session = MagicMock()
        client._get = AsyncMock(return_value=[
            {"accountId": "DU12345", "type": "INDIVIDUAL"},
            {"accountId": "U67890", "type": "INDIVIDUAL"},
        ])

        result = await client._fetch_account_id()

        assert result is True
        assert client._account_id == "DU12345"

    @pytest.mark.asyncio
    async def test_fetch_account_live(self, client):
        """Should select live account (non-DU)."""
        client.config.account_type = "live"
        client._session = MagicMock()
        client._get = AsyncMock(return_value=[
            {"accountId": "DU12345", "type": "INDIVIDUAL"},
            {"accountId": "U67890", "type": "INDIVIDUAL"},
        ])

        result = await client._fetch_account_id()

        assert result is True
        assert client._account_id == "U67890"


# ================================================================
# HISTORICAL DATA TESTS
# ================================================================

class TestHistoricalData:
    @pytest.mark.asyncio
    async def test_fetch_historical_bars(self, client):
        """Should parse historical bar response into list of dicts."""
        client._contract = ContractInfo(conid=123, symbol="MNQ")

        api_response = {
            "data": [
                {"t": 1709000000000, "o": 21000.0, "h": 21050.0,
                 "l": 20990.0, "c": 21030.0, "v": 150},
                {"t": 1709000120000, "o": 21030.0, "h": 21060.0,
                 "l": 21020.0, "c": 21045.0, "v": 200},
            ]
        }

        client._get = AsyncMock(return_value=api_response)

        bars = await client.get_historical_bars(period="2h", bar_size="2min")

        assert len(bars) == 2
        assert bars[0]["open"] == 21000.0
        assert bars[0]["close"] == 21030.0
        assert bars[0]["volume"] == 150
        assert isinstance(bars[0]["timestamp"], datetime)

    @pytest.mark.asyncio
    async def test_fetch_historical_no_contract(self, client):
        """Should return empty list if no contract resolved."""
        client._contract = None
        bars = await client.get_historical_bars()
        assert bars == []

    @pytest.mark.asyncio
    async def test_fetch_historical_no_data(self, client):
        """Should return empty list on API failure."""
        client._contract = ContractInfo(conid=123, symbol="MNQ")
        client._get = AsyncMock(return_value=None)

        bars = await client.get_historical_bars()
        assert bars == []


# ================================================================
# STATUS & HEALTH TESTS
# ================================================================

class TestStatus:
    def test_is_connected(self, client):
        assert not client.is_connected

        client._connected = True
        client._session_valid = True
        assert client.is_connected

        client._session_valid = False
        assert not client.is_connected

    def test_get_current_price_none(self, client):
        assert client.get_current_price() is None

    def test_get_current_price(self, client):
        client._last_snapshot = MarketSnapshot(
            conid=123, last_price=21050.0, bid=21049.75, ask=21050.25,
        )
        prices = client.get_current_price()
        assert prices["bid"] == 21049.75
        assert prices["ask"] == 21050.25
        assert prices["last"] == 21050.0

    def test_get_status(self, client):
        status = client.get_status()
        assert status["connected"] is False
        assert status["account_type"] == "paper"
        assert status["symbol"] == "MNQ"
        assert status["conid"] == 0

    def test_get_status_with_contract(self, client):
        client._connected = True
        client._session_valid = True
        client._account_id = "DU12345"
        client._contract = ContractInfo(
            conid=654321, symbol="MNQH6", expiry="20260320"
        )
        client._last_keepalive = time.time() - 30

        status = client.get_status()
        assert status["connected"] is True
        assert status["session_valid"] is True
        assert status["conid"] == 654321
        assert status["contract_expiry"] == "20260320"
        assert status["account_id"] == "DU12345"
        assert status["last_keepalive_age_s"] is not None


# ================================================================
# DISCONNECT TESTS
# ================================================================

class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_cleans_up(self, client):
        """Disconnect should cancel tasks and close session."""
        client._connected = True
        client._authenticated = True
        client._session_valid = True

        mock_session = AsyncMock()
        client._session = mock_session

        # Simulate running tasks
        client._keepalive_task = asyncio.create_task(asyncio.sleep(999))
        client._poll_task = asyncio.create_task(asyncio.sleep(999))

        await client.disconnect()

        assert client._connected is False
        assert client._authenticated is False
        assert client._session_valid is False
        assert client._session is None
        mock_session.close.assert_called_once()


# ================================================================
# SESSION TYPE DETECTION TESTS
# ================================================================

class TestSessionType:
    """Test RTH vs ETH detection — must match main.py process_bar() logic."""

    def _utc(self, hour: int, minute: int = 0) -> datetime:
        """Helper: create a UTC datetime for 2026-02-28 at given H:M."""
        return datetime(2026, 2, 28, hour, minute, tzinfo=timezone.utc)

    def test_rth_open(self):
        """9:30 ET = 14:30 UTC → RTH."""
        assert get_session_type(self._utc(14, 30)) == SessionType.RTH

    def test_rth_mid_morning(self):
        """11:00 ET = 16:00 UTC → RTH."""
        assert get_session_type(self._utc(16, 0)) == SessionType.RTH

    def test_rth_close_boundary(self):
        """16:00 ET = 21:00 UTC → ETH (RTH is exclusive of 16:00)."""
        assert get_session_type(self._utc(21, 0)) == SessionType.ETH

    def test_rth_just_before_close(self):
        """15:59 ET = 20:59 UTC → RTH."""
        assert get_session_type(self._utc(20, 59)) == SessionType.RTH

    def test_eth_premarket(self):
        """8:00 ET = 13:00 UTC → ETH."""
        assert get_session_type(self._utc(13, 0)) == SessionType.ETH

    def test_eth_overnight(self):
        """2:00 ET = 07:00 UTC → ETH."""
        assert get_session_type(self._utc(7, 0)) == SessionType.ETH

    def test_eth_evening(self):
        """20:00 ET = 01:00 UTC next day → ETH."""
        ts = datetime(2026, 3, 1, 1, 0, tzinfo=timezone.utc)
        assert get_session_type(ts) == SessionType.ETH

    def test_rth_929_is_eth(self):
        """9:29 ET = 14:29 UTC → ETH (one minute before RTH open)."""
        assert get_session_type(self._utc(14, 29)) == SessionType.ETH

    def test_client_get_session_type(self, client):
        """IBKRClient.get_session_type() returns a SessionType."""
        result = client.get_session_type()
        assert isinstance(result, SessionType)


# ================================================================
# CANDLE AGGREGATOR FIXTURES
# ================================================================

@pytest.fixture
def aggregator():
    return CandleAggregator()


def _ts(minute: int, second: int = 0) -> datetime:
    """Helper: create UTC datetime at 2026-02-28 14:MM:SS (RTH)."""
    return datetime(2026, 2, 28, 14, minute, second, tzinfo=timezone.utc)


def _ts_eth(minute: int, second: int = 0) -> datetime:
    """Helper: create UTC datetime at 2026-02-28 07:MM:SS (ETH: 2:00 ET)."""
    return datetime(2026, 2, 28, 7, minute, second, tzinfo=timezone.utc)


# ================================================================
# CANDLE AGGREGATION TESTS
# ================================================================

class TestCandleAggregation:
    """Test building 2-minute OHLCV candles from tick data."""

    def test_first_tick_starts_window(self, aggregator):
        """First tick should start a window but not emit a candle."""
        result = aggregator.process_tick(21000.0, 1, _ts(0, 0))
        assert result is None
        assert aggregator._current_open == 21000.0

    def test_ticks_in_same_window(self, aggregator):
        """Multiple ticks in same 2-min window update OHLCV."""
        aggregator.process_tick(21000.0, 1, _ts(0, 0))
        aggregator.process_tick(21010.0, 1, _ts(0, 30))
        aggregator.process_tick(20990.0, 1, _ts(0, 45))
        aggregator.process_tick(21005.0, 1, _ts(1, 30))

        # Still in [14:00, 14:02) window — no candle yet
        assert aggregator._current_open == 21000.0
        assert aggregator._current_high == 21010.0
        assert aggregator._current_low == 20990.0
        assert aggregator._current_close == 21005.0
        assert aggregator._current_volume == 4
        assert aggregator._current_tick_count == 4

    def test_window_boundary_emits_candle(self, aggregator):
        """Tick in a new 2-min window should close the previous candle."""
        # Window 1: [14:00, 14:02)
        aggregator.process_tick(21000.0, 1, _ts(0, 0))
        aggregator.process_tick(21010.0, 2, _ts(0, 30))
        aggregator.process_tick(20990.0, 1, _ts(1, 0))
        aggregator.process_tick(21005.0, 1, _ts(1, 59))

        # Window 2 starts: first tick at 14:02 closes window 1
        candle = aggregator.process_tick(21008.0, 1, _ts(2, 0))

        assert candle is not None
        assert candle["timestamp"] == _ts(0, 0)
        assert candle["open"] == 21000.0
        assert candle["high"] == 21010.0
        assert candle["low"] == 20990.0
        assert candle["close"] == 21005.0
        assert candle["volume"] == 5   # 1+2+1+1
        assert candle["tick_count"] == 4  # 4 ticks fed

    def test_candle_prices_rounded_to_2dp(self, aggregator):
        """All prices in emitted candles must be rounded to 2 decimal places."""
        aggregator.process_tick(21000.126, 1, _ts(0, 0))
        aggregator.process_tick(21010.999, 1, _ts(0, 30))
        aggregator.process_tick(20989.501, 1, _ts(1, 0))
        aggregator.process_tick(21005.557, 1, _ts(1, 30))

        candle = aggregator.process_tick(21008.0, 1, _ts(2, 0))

        assert candle["open"] == 21000.13   # rounded
        assert candle["high"] == 21011.0    # rounded
        assert candle["low"] == 20989.5     # rounded
        assert candle["close"] == 21005.56  # rounded

    def test_candle_has_session_type_rth(self, aggregator):
        """Candles during RTH should be tagged RTH."""
        # 14:30 UTC = 9:30 ET = RTH
        aggregator.process_tick(21000.0, 1, _ts(30, 0))
        candle = aggregator.process_tick(21005.0, 1, _ts(32, 0))

        assert candle is not None
        assert candle["session_type"] == SessionType.RTH

    def test_candle_has_session_type_eth(self, aggregator):
        """Candles during ETH should be tagged ETH."""
        # 07:00 UTC = 2:00 ET = ETH
        aggregator.process_tick(21000.0, 1, _ts_eth(0, 0))
        candle = aggregator.process_tick(21005.0, 1, _ts_eth(2, 0))

        assert candle is not None
        assert candle["session_type"] == SessionType.ETH

    def test_multiple_candles(self, aggregator):
        """Should emit multiple sequential candles."""
        # Window 1: [14:00, 14:02)
        aggregator.process_tick(21000.0, 1, _ts(0, 0))
        aggregator.process_tick(21010.0, 1, _ts(1, 0))

        # Window 2: [14:02, 14:04) — emits candle 1
        c1 = aggregator.process_tick(21020.0, 1, _ts(2, 0))
        aggregator.process_tick(21015.0, 1, _ts(3, 0))

        # Window 3: [14:04, 14:06) — emits candle 2
        c2 = aggregator.process_tick(21030.0, 1, _ts(4, 0))

        assert c1 is not None
        assert c1["timestamp"] == _ts(0, 0)
        assert c1["open"] == 21000.0
        assert c1["close"] == 21010.0

        assert c2 is not None
        assert c2["timestamp"] == _ts(2, 0)
        assert c2["open"] == 21020.0
        assert c2["close"] == 21015.0

    def test_window_alignment_odd_minute(self, aggregator):
        """Tick at odd minute should floor to even minute boundary."""
        aggregator.process_tick(21000.0, 1, _ts(3, 15))
        # Window starts at 14:02 (3 floors to 2)
        assert aggregator._current_window_start == _ts(2, 0)

    def test_flush_emits_partial_candle(self, aggregator):
        """flush() should emit partial candle with whatever ticks exist."""
        aggregator.process_tick(21000.0, 1, _ts(0, 0))
        aggregator.process_tick(21010.0, 1, _ts(0, 30))

        candle = aggregator.flush()

        assert candle is not None
        assert candle["open"] == 21000.0
        assert candle["high"] == 21010.0
        assert candle["volume"] == 2

    def test_flush_empty_returns_none(self, aggregator):
        """flush() with no data returns None."""
        assert aggregator.flush() is None

    def test_callback_fires(self):
        """on_candle callback should be called when candle is emitted."""
        received = []
        agg = CandleAggregator(on_candle=lambda c: received.append(c))

        agg.process_tick(21000.0, 1, _ts(0, 0))
        agg.process_tick(21010.0, 1, _ts(2, 0))

        assert len(received) == 1
        assert received[0]["open"] == 21000.0

    def test_callback_via_setter(self):
        """Callback registered via on_candle() method should work."""
        received = []
        agg = CandleAggregator()
        agg.on_candle(lambda c: received.append(c))

        agg.process_tick(21000.0, 1, _ts(0, 0))
        agg.process_tick(21010.0, 1, _ts(2, 0))

        assert len(received) == 1

    def test_zero_price_ignored(self, aggregator):
        """Ticks with price <= 0 should be silently ignored."""
        result = aggregator.process_tick(0.0, 1, _ts(0, 0))
        assert result is None
        assert aggregator._current_window_start is None

        result = aggregator.process_tick(-5.0, 1, _ts(0, 0))
        assert result is None

    def test_reset_clears_state(self, aggregator):
        """reset() should clear all in-progress candle state."""
        aggregator.process_tick(21000.0, 1, _ts(0, 0))
        aggregator.process_tick(21010.0, 1, _ts(0, 30))

        aggregator.reset()

        assert aggregator._current_window_start is None
        assert aggregator._current_volume == 0
        assert aggregator._current_high == -math.inf
        assert aggregator._current_low == math.inf

    def test_get_stats(self, aggregator):
        """get_stats() should reflect aggregator activity."""
        aggregator.process_tick(21000.0, 1, _ts(0, 0))
        aggregator.process_tick(21010.0, 1, _ts(2, 0))

        stats = aggregator.get_stats()
        assert stats["candles_emitted"] == 1
        assert stats["ticks_processed"] == 2
        assert stats["candles_rejected"] == 0


# ================================================================
# DATA QUALITY CHECK TESTS
# ================================================================

class TestDataQualityChecks:
    """Test candle rejection and gap detection logic."""

    def test_reject_zero_volume(self):
        """Candles with zero volume should be rejected."""
        agg = CandleAggregator()

        # Manually set up a candle with 0 volume to test validation
        agg._current_window_start = _ts(0, 0)
        agg._current_open = 21000.0
        agg._current_high = 21010.0
        agg._current_low = 20990.0
        agg._current_close = 21005.0
        agg._current_volume = 0
        agg._current_tick_count = 0

        candle = agg._close_current_candle()
        assert candle is None
        assert agg._candles_rejected == 1

    def test_reject_high_lt_low(self):
        """Candles where high < low should be rejected (safety net)."""
        agg = CandleAggregator()

        agg._current_window_start = _ts(0, 0)
        agg._current_open = 21000.0
        agg._current_high = 20980.0  # impossibly lower than low
        agg._current_low = 21010.0
        agg._current_close = 21000.0
        agg._current_volume = 5
        agg._current_tick_count = 5

        candle = agg._close_current_candle()
        assert candle is None
        assert agg._candles_rejected == 1

    def test_valid_candle_not_rejected(self, aggregator):
        """Normal candle with valid data should pass quality checks."""
        aggregator.process_tick(21000.0, 1, _ts(0, 0))
        aggregator.process_tick(21010.0, 1, _ts(0, 30))
        aggregator.process_tick(20995.0, 1, _ts(1, 0))

        candle = aggregator.process_tick(21020.0, 1, _ts(2, 0))
        assert candle is not None
        assert aggregator._candles_rejected == 0

    def test_gap_detection_warns(self, aggregator):
        """Gap > 2 minutes between candles should increment consecutive_gaps."""
        # Candle 1 at :00
        aggregator.process_tick(21000.0, 1, _ts(0, 0))
        aggregator.process_tick(21005.0, 1, _ts(2, 0))  # emits candle :00, starts :02

        # Candle 2 at :02 — skip ahead so gap shows on candle 3
        aggregator.process_tick(21010.0, 1, _ts(8, 0))  # emits candle :02, starts :08

        # Candle 3 at :08 — gap detected: last=:02, expected=:04, current=:08
        # gap = (08-04) = 240s, missed = 240/120 = 2
        aggregator.process_tick(21015.0, 1, _ts(10, 0))  # emits candle :08

        assert aggregator._consecutive_gaps == 2

    def test_consecutive_gap_alert_threshold(self, aggregator):
        """3+ consecutive gaps should trigger alert (logged)."""
        # Candle 1 at :00
        aggregator.process_tick(21000.0, 1, _ts(0, 0))
        aggregator.process_tick(21005.0, 1, _ts(2, 0))  # emits candle :00, starts :02

        # Candle 2 at :02 — skip far ahead
        aggregator.process_tick(21010.0, 1, _ts(12, 0))  # emits candle :02, starts :12

        # Candle 3 at :12 — gap: last=:02, expected=:04, current=:12
        # gap = (12-04) = 480s, missed = 480/120 = 4
        aggregator.process_tick(21015.0, 1, _ts(14, 0))  # emits candle :12

        assert aggregator._consecutive_gaps >= CONSECUTIVE_GAP_ALERT_THRESHOLD

    def test_no_gap_resets_counter(self, aggregator):
        """Consecutive candles without gaps should reset gap counter."""
        # Candle 1
        aggregator.process_tick(21000.0, 1, _ts(0, 0))
        aggregator.process_tick(21005.0, 1, _ts(2, 0))

        # Candle 2 — no gap
        aggregator.process_tick(21010.0, 1, _ts(4, 0))

        assert aggregator._consecutive_gaps == 0

    def test_candle_format_matches_bar_constructor(self, aggregator):
        """
        Emitted candle must have all fields needed by features.engine.Bar:
        timestamp, open, high, low, close, volume.
        tick_count and session_type are extras.
        """
        aggregator.process_tick(21000.0, 1, _ts(0, 0))
        aggregator.process_tick(21010.0, 2, _ts(0, 30))
        aggregator.process_tick(20995.0, 1, _ts(1, 0))
        aggregator.process_tick(21005.0, 1, _ts(1, 30))

        candle = aggregator.process_tick(21015.0, 1, _ts(2, 0))

        # Required fields for Bar()
        assert "timestamp" in candle
        assert "open" in candle
        assert "high" in candle
        assert "low" in candle
        assert "close" in candle
        assert "volume" in candle

        # Verify types
        assert isinstance(candle["timestamp"], datetime)
        assert isinstance(candle["open"], float)
        assert isinstance(candle["high"], float)
        assert isinstance(candle["low"], float)
        assert isinstance(candle["close"], float)
        assert isinstance(candle["volume"], int)

        # Extra fields
        assert "tick_count" in candle
        assert "session_type" in candle
        assert isinstance(candle["session_type"], SessionType)

    def test_validate_candle_static_method(self):
        """Direct test of _validate_candle static method."""
        # Valid candle
        assert CandleAggregator._validate_candle({
            "volume": 10, "high": 100.0, "low": 90.0,
        }) is None

        # Zero volume
        assert CandleAggregator._validate_candle({
            "volume": 0, "high": 100.0, "low": 90.0,
        }) == "zero_volume"

        # high < low
        assert CandleAggregator._validate_candle({
            "volume": 10, "high": 80.0, "low": 90.0,
        }) == "high_lt_low"


# ================================================================
# WEBSOCKET TESTS
# ================================================================

@pytest.fixture
def ws(config):
    return IBKRWebSocket(config, conid=654321)


class TestIBKRWebSocket:
    """Tests for WebSocket streaming client."""

    def test_ws_url(self, ws):
        assert ws.ws_url == "wss://localhost:5000/v1/api/ws"

    def test_initial_state(self, ws):
        assert ws.is_connected is False
        assert ws._consecutive_failures == 0

    def test_on_tick_registration(self, ws):
        """on_tick() should register a callback."""
        callback = MagicMock()
        ws.on_tick(callback)
        assert ws._on_tick is callback

    def test_handle_message_conid_key(self, ws):
        """Should parse message where conid is the top-level key."""
        received = []
        ws.on_tick(lambda p, v, t: received.append(p))

        msg = json.dumps({"654321": {"31": "21050.25"}})
        ws._handle_message(msg)

        assert len(received) == 1
        assert received[0] == 21050.25

    def test_handle_message_conid_field(self, ws):
        """Should parse message with conid as a field."""
        received = []
        ws.on_tick(lambda p, v, t: received.append(p))

        msg = json.dumps({"conid": 654321, "31": 21000.0})
        ws._handle_message(msg)

        assert len(received) == 1
        assert received[0] == 21000.0

    def test_handle_message_list_format(self, ws):
        """Should parse list-of-updates format."""
        received = []
        ws.on_tick(lambda p, v, t: received.append(p))

        msg = json.dumps([
            {"conid": 999, "31": 10000.0},
            {"conid": 654321, "31": "21075.50"},
        ])
        ws._handle_message(msg)

        assert len(received) == 1
        assert received[0] == 21075.50

    def test_handle_message_no_price(self, ws):
        """Should ignore messages without field 31."""
        received = []
        ws.on_tick(lambda p, v, t: received.append(p))

        msg = json.dumps({"654321": {"84": "21050.0"}})
        ws._handle_message(msg)

        assert len(received) == 0

    def test_handle_message_wrong_conid(self, ws):
        """Should ignore messages for other conids."""
        received = []
        ws.on_tick(lambda p, v, t: received.append(p))

        msg = json.dumps({"999999": {"31": "21050.25"}})
        ws._handle_message(msg)

        assert len(received) == 0

    def test_handle_message_invalid_json(self, ws):
        """Should silently handle invalid JSON."""
        received = []
        ws.on_tick(lambda p, v, t: received.append(p))

        ws._handle_message("not json")
        ws._handle_message("")
        ws._handle_message("{bad")

        assert len(received) == 0

    def test_handle_message_zero_price(self, ws):
        """Should ignore zero price ticks."""
        received = []
        ws.on_tick(lambda p, v, t: received.append(p))

        msg = json.dumps({"654321": {"31": "0.0"}})
        ws._handle_message(msg)

        assert len(received) == 0

    def test_handle_message_c_prefix(self, ws):
        """Should handle IBKR 'C' prefix on closing prices."""
        received = []
        ws.on_tick(lambda p, v, t: received.append(p))

        msg = json.dumps({"654321": {"31": "C21050.25"}})
        ws._handle_message(msg)

        assert len(received) == 1
        assert received[0] == 21050.25

    @pytest.mark.asyncio
    async def test_connect_failure_increments_counter(self, ws):
        """Failed connect should increment consecutive_failures."""
        # No real gateway → connection will fail
        ws.config = IBKRConfig(gateway_host="127.0.0.1", gateway_port=59999)

        result = await ws.connect()

        assert result is False
        assert ws._consecutive_failures == 1
        assert ws.is_connected is False

    @pytest.mark.asyncio
    async def test_disconnect_cleans_up(self, ws):
        """Disconnect should reset state even if not connected."""
        ws._connected = False
        await ws.disconnect()
        assert ws.is_connected is False
        assert ws._ws is None


# ================================================================
# IBKR DATA FEED FIXTURES
# ================================================================

def _make_connected_client(config=None):
    """Create a mock IBKRClient that appears connected."""
    client = IBKRClient(config or IBKRConfig())
    client._connected = True
    client._session_valid = True
    client._contract = ContractInfo(conid=654321, symbol="MNQH6", expiry="20260320")
    client._last_snapshot = MarketSnapshot(
        conid=654321, last_price=21050.0, bid=21049.75, ask=21050.25,
        timestamp=datetime.now(timezone.utc),
    )
    return client


def _make_backfill_bars(count: int = 5) -> list:
    """Generate synthetic backfill bar dicts."""
    base_ts = datetime(2026, 2, 28, 14, 0, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(count):
        ts = base_ts + timedelta(minutes=i * 2)
        bars.append({
            "timestamp": ts,
            "open": round(21000.0 + i * 5, 2),
            "high": round(21010.0 + i * 5, 2),
            "low": round(20990.0 + i * 5, 2),
            "close": round(21005.0 + i * 5, 2),
            "volume": 100 + i * 10,
        })
    return bars


@pytest.fixture
def mock_client():
    return _make_connected_client()


@pytest.fixture
def feed(mock_client):
    return IBKRDataFeed(mock_client)


# ================================================================
# IBKR DATA FEED TESTS
# ================================================================

class TestIBKRDataFeed:
    """Tests for the high-level data feed orchestrator."""

    def test_initial_state(self, feed):
        assert feed._running is False
        assert feed._data_mode == "none"
        assert feed.is_connected() is False

    def test_on_bar_registration(self, feed):
        callback = MagicMock()
        feed.on_bar(callback)
        assert feed._on_bar is callback

    def test_get_current_price(self, feed):
        """get_current_price delegates to the underlying client."""
        prices = feed.get_current_price()
        assert prices is not None
        assert prices["last"] == 21050.0
        assert prices["bid"] == 21049.75
        assert prices["ask"] == 21050.25

    def test_get_current_price_no_snapshot(self, feed):
        """Should return None if client has no snapshot."""
        feed._client._last_snapshot = None
        assert feed.get_current_price() is None

    def test_is_connected_not_running(self, feed):
        """Should be False when feed is not running."""
        assert feed.is_connected() is False

    def test_is_connected_websocket_mode(self, feed):
        """Should reflect WebSocket state when in ws mode."""
        feed._running = True
        feed._data_mode = "websocket"
        feed._ws = MagicMock()
        feed._ws.is_connected = True
        assert feed.is_connected() is True

        feed._ws.is_connected = False
        assert feed.is_connected() is False

    def test_is_connected_polling_mode(self, feed):
        """Should reflect client connection in polling mode."""
        feed._running = True
        feed._data_mode = "polling"
        assert feed.is_connected() is True  # mock_client is connected

        feed._client._session_valid = False
        assert feed.is_connected() is False

    def test_get_status(self, feed):
        status = feed.get_status()
        assert status["running"] is False
        assert status["data_mode"] == "none"
        assert status["ws_connected"] is False
        assert "aggregator" in status
        assert "client" in status

    def test_dispatch_bar(self, feed):
        """_dispatch_bar should convert candle dict to Bar and forward."""
        received = []
        feed.on_bar(lambda c: received.append(c))

        candle = {
            "timestamp": _ts(0, 0), "open": 21000.0, "high": 21010.0,
            "low": 20990.0, "close": 21005.0, "volume": 100,
            "tick_count": 5, "session_type": SessionType.RTH,
        }
        feed._dispatch_bar(candle)

        assert len(received) == 1
        from features.engine import Bar
        assert isinstance(received[0], Bar)
        assert received[0].open == 21000.0
        assert received[0].session_type == "RTH"

    def test_dispatch_bar_no_callback(self, feed):
        """_dispatch_bar should not fail with no callback."""
        feed._on_bar = None
        feed._dispatch_bar({
            "timestamp": _ts(0, 0), "open": 21000.0, "high": 21010.0,
            "low": 20990.0, "close": 21005.0, "volume": 100,
        })  # Should not raise


# ================================================================
# BACKFILL TESTS
# ================================================================

class TestBackfill:
    """Tests for historical data backfill."""

    @pytest.mark.asyncio
    async def test_backfill_dispatches_bars(self, feed):
        """Backfill should fetch history and dispatch each bar."""
        bars = _make_backfill_bars(5)
        feed._client.get_historical_bars = AsyncMock(return_value=bars)

        received = []
        feed.on_bar(lambda c: received.append(c))

        await feed._run_backfill()

        assert len(received) == 5
        assert feed._backfill_bars == bars
        # Each bar should be a Bar with session_type set
        from features.engine import Bar
        for bar in received:
            assert isinstance(bar, Bar)
            assert bar.session_type in ("RTH", "ETH")
            assert bar.tick_count >= 0

    @pytest.mark.asyncio
    async def test_backfill_uses_correct_params(self, feed):
        """Backfill should request 2h of 2min bars."""
        feed._client.get_historical_bars = AsyncMock(return_value=[])

        await feed._run_backfill()

        feed._client.get_historical_bars.assert_called_once_with(
            period=BACKFILL_PERIOD,
            bar_size=BACKFILL_BAR_SIZE,
        )

    @pytest.mark.asyncio
    async def test_backfill_empty_data(self, feed):
        """Backfill should handle empty response gracefully."""
        feed._client.get_historical_bars = AsyncMock(return_value=[])

        received = []
        feed.on_bar(lambda c: received.append(c))

        await feed._run_backfill()

        assert len(received) == 0
        assert feed._backfill_bars == []

    @pytest.mark.asyncio
    async def test_backfill_preserves_existing_fields(self, feed):
        """Backfill should not overwrite existing bar fields."""
        bars = _make_backfill_bars(1)
        bars[0]["tick_count"] = 42  # Pre-existing
        feed._client.get_historical_bars = AsyncMock(return_value=bars)

        received = []
        feed.on_bar(lambda c: received.append(c))

        await feed._run_backfill()

        # tick_count should not be overwritten by setdefault
        assert received[0].tick_count == 42

    @pytest.mark.asyncio
    async def test_backfill_no_callback(self, feed):
        """Backfill with no on_bar callback should not fail."""
        bars = _make_backfill_bars(3)
        feed._client.get_historical_bars = AsyncMock(return_value=bars)
        feed._on_bar = None

        await feed._run_backfill()  # Should not raise

        assert feed._backfill_bars == bars


# ================================================================
# DATA FEED START/STOP TESTS
# ================================================================

class TestDataFeedLifecycle:
    """Tests for IBKRDataFeed start/stop flow."""

    @pytest.mark.asyncio
    async def test_start_fails_if_client_not_connected(self, feed):
        """start() should fail if client is not connected."""
        feed._client._connected = False
        feed._client._session_valid = False

        result = await feed.start()
        assert result is False
        assert feed._running is False

    @pytest.mark.asyncio
    async def test_start_fails_if_no_contract(self, feed):
        """start() should fail if no contract resolved."""
        feed._client._contract = None

        result = await feed.start()
        assert result is False

    @pytest.mark.asyncio
    async def test_start_websocket_success(self, feed):
        """start() should use WebSocket when it connects."""
        feed._client.get_historical_bars = AsyncMock(return_value=[])

        mock_ws = MagicMock(spec=IBKRWebSocket)
        mock_ws.connect = AsyncMock(return_value=True)
        mock_ws.is_connected = True
        mock_ws._consecutive_failures = 0

        with patch.object(feed, '_start_websocket', new_callable=AsyncMock, return_value=True):
            result = await feed.start()

        assert result is True
        assert feed._running is True
        assert feed._data_mode == "websocket"

        # Cleanup
        await feed.stop()

    @pytest.mark.asyncio
    async def test_start_falls_back_to_polling(self, feed):
        """start() should fall back to polling if WebSocket fails."""
        feed._client.get_historical_bars = AsyncMock(return_value=[])
        feed._client.start_polling = AsyncMock()
        feed._client.stop_polling = AsyncMock()

        with patch.object(feed, '_start_websocket', new_callable=AsyncMock, return_value=False):
            result = await feed.start()

        assert result is True
        assert feed._data_mode == "polling"
        feed._client.start_polling.assert_called_once()

        await feed.stop()

    @pytest.mark.asyncio
    async def test_stop_cleans_up(self, feed):
        """stop() should cancel tasks and reset state."""
        feed._running = True
        feed._data_mode = "websocket"

        mock_ws = MagicMock(spec=IBKRWebSocket)
        mock_ws.disconnect = AsyncMock()
        feed._ws = mock_ws

        feed._client.stop_polling = AsyncMock()

        await feed.stop()

        assert feed._running is False
        assert feed._data_mode == "none"
        mock_ws.disconnect.assert_called_once()
        feed._client.stop_polling.assert_called_once()


# ================================================================
# POLLING FALLBACK TESTS
# ================================================================

class TestPollingFallback:
    """Tests for HTTP polling fallback data path."""

    @pytest.mark.asyncio
    async def test_on_poll_snapshot_feeds_aggregator(self, feed):
        """Snapshots from polling should be fed to the aggregator."""
        received = []
        feed.on_bar(lambda c: received.append(c))

        # Feed snapshots that span two 2-min windows
        snap1 = MarketSnapshot(conid=654321, last_price=21000.0,
                               timestamp=_ts(0, 0))
        snap2 = MarketSnapshot(conid=654321, last_price=21010.0,
                               timestamp=_ts(0, 30))
        snap3 = MarketSnapshot(conid=654321, last_price=21005.0,
                               timestamp=_ts(2, 0))  # new window → emit

        await feed._on_poll_snapshot(snap1)
        await feed._on_poll_snapshot(snap2)
        await feed._on_poll_snapshot(snap3)

        assert len(received) == 1
        assert received[0].open == 21000.0
        assert received[0].high == 21010.0

    @pytest.mark.asyncio
    async def test_on_poll_snapshot_ignores_zero_price(self, feed):
        """Snapshots with zero price should be ignored."""
        snap = MarketSnapshot(conid=654321, last_price=0.0,
                              timestamp=_ts(0, 0))

        await feed._on_poll_snapshot(snap)

        assert feed._aggregator._ticks_processed == 0

    @pytest.mark.asyncio
    async def test_on_poll_snapshot_uses_now_if_no_timestamp(self, feed):
        """Should use current time if snapshot has no timestamp."""
        snap = MarketSnapshot(conid=654321, last_price=21000.0,
                              timestamp=None)

        await feed._on_poll_snapshot(snap)

        assert feed._aggregator._ticks_processed == 1


# ================================================================
# HEALTH MONITOR TESTS
# ================================================================

class TestHealthMonitor:
    """Tests for the background health monitoring logic."""

    def test_ws_fallback_threshold_constant(self):
        """WS_FALLBACK_THRESHOLD should be 3."""
        assert WS_FALLBACK_THRESHOLD == 3

    def test_backfill_constants(self):
        """Backfill should request 2h of 2min bars."""
        assert BACKFILL_PERIOD == "2h"
        assert BACKFILL_BAR_SIZE == "2min"

    def test_feed_aggregator_wired(self, feed):
        """CandleAggregator should dispatch Bar to feed's on_bar callback."""
        received = []
        feed.on_bar(lambda c: received.append(c))

        # Manually push ticks through the aggregator
        feed._aggregator.process_tick(21000.0, 1, _ts(0, 0))
        feed._aggregator.process_tick(21010.0, 1, _ts(2, 0))

        assert len(received) == 1
        from features.engine import Bar
        assert isinstance(received[0], Bar)
        assert received[0].open == 21000.0


# ================================================================
# INTEGRATION-STYLE TESTS
# ================================================================

class TestDataFeedIntegration:
    """End-to-end tests for tick→candle→callback pipeline."""

    def test_ws_tick_to_bar_pipeline(self):
        """WebSocket tick → aggregator → on_bar callback."""
        client = _make_connected_client()
        feed = IBKRDataFeed(client)

        received = []
        feed.on_bar(lambda c: received.append(c))

        # Simulate WebSocket ticks flowing through the aggregator
        agg = feed._aggregator

        # Window 1: [14:30, 14:32) — 14:30 UTC = 9:30 ET = RTH
        agg.process_tick(21000.0, 1, _ts(30, 0))
        agg.process_tick(21010.0, 1, _ts(30, 30))
        agg.process_tick(20990.0, 1, _ts(31, 0))
        agg.process_tick(21005.0, 1, _ts(31, 30))

        # Window 2 starts → emits candle 1
        agg.process_tick(21020.0, 1, _ts(32, 0))

        assert len(received) == 1
        bar = received[0]
        from features.engine import Bar
        assert isinstance(bar, Bar)
        assert bar.open == 21000.0
        assert bar.high == 21010.0
        assert bar.low == 20990.0
        assert bar.close == 21005.0
        assert bar.volume == 4
        assert bar.session_type == "RTH"

    @pytest.mark.asyncio
    async def test_backfill_then_live_ticks(self):
        """Backfill bars followed by live ticks should all dispatch."""
        client = _make_connected_client()
        backfill_bars = _make_backfill_bars(3)
        client.get_historical_bars = AsyncMock(return_value=backfill_bars)

        feed = IBKRDataFeed(client)
        received = []
        feed.on_bar(lambda c: received.append(c))

        # Step 1: Backfill
        await feed._run_backfill()
        assert len(received) == 3  # 3 backfill bars

        # Step 2: Live ticks
        feed._aggregator.process_tick(21100.0, 1, _ts(30, 0))
        feed._aggregator.process_tick(21110.0, 1, _ts(32, 0))  # new window → emit

        assert len(received) == 4  # 3 backfill + 1 live candle
        live_bar = received[3]
        assert live_bar.open == 21100.0
        assert live_bar.close == 21100.0


# ================================================================
# CANDLE-TO-BAR ADAPTER TESTS
# ================================================================

class TestCandleToBar:
    """Tests for the dict→Bar adapter in IBKRDataFeed."""

    def test_basic_conversion(self):
        """candle_to_bar should convert dict to Bar with all fields."""
        candle = {
            "timestamp": _ts(30, 0),
            "open": 21000.0,
            "high": 21010.0,
            "low": 20990.0,
            "close": 21005.0,
            "volume": 150,
            "tick_count": 12,
            "session_type": SessionType.RTH,
        }
        bar = IBKRDataFeed.candle_to_bar(candle)

        from features.engine import Bar
        assert isinstance(bar, Bar)
        assert bar.timestamp == _ts(30, 0)
        assert bar.open == 21000.0
        assert bar.high == 21010.0
        assert bar.low == 20990.0
        assert bar.close == 21005.0
        assert bar.volume == 150
        assert bar.tick_count == 12

    def test_session_type_enum_to_string(self):
        """SessionType enum should be stored as string on Bar."""
        candle = {
            "timestamp": _ts(30, 0), "open": 21000.0, "high": 21010.0,
            "low": 20990.0, "close": 21005.0, "volume": 100,
            "session_type": SessionType.RTH,
        }
        bar = IBKRDataFeed.candle_to_bar(candle)
        assert bar.session_type == "RTH"

        candle["session_type"] = SessionType.ETH
        bar = IBKRDataFeed.candle_to_bar(candle)
        assert bar.session_type == "ETH"

    def test_session_type_string_passthrough(self):
        """String session_type should pass through unchanged."""
        candle = {
            "timestamp": _ts(30, 0), "open": 21000.0, "high": 21010.0,
            "low": 20990.0, "close": 21005.0, "volume": 100,
            "session_type": "RTH",
        }
        bar = IBKRDataFeed.candle_to_bar(candle)
        assert bar.session_type == "RTH"

    def test_session_type_none(self):
        """Missing session_type should result in None on Bar."""
        candle = {
            "timestamp": _ts(30, 0), "open": 21000.0, "high": 21010.0,
            "low": 20990.0, "close": 21005.0, "volume": 100,
        }
        bar = IBKRDataFeed.candle_to_bar(candle)
        assert bar.session_type is None

    def test_missing_required_field_returns_none(self):
        """Missing required field should return None and log warning."""
        candle = {
            "timestamp": _ts(30, 0), "open": 21000.0,
            # Missing high, low, close, volume
        }
        bar = IBKRDataFeed.candle_to_bar(candle)
        assert bar is None

    def test_optional_fields_default(self):
        """Missing optional fields should use defaults."""
        candle = {
            "timestamp": _ts(30, 0), "open": 21000.0, "high": 21010.0,
            "low": 20990.0, "close": 21005.0, "volume": 100,
        }
        bar = IBKRDataFeed.candle_to_bar(candle)
        assert bar.bid_volume == 0
        assert bar.ask_volume == 0
        assert bar.delta == 0
        assert bar.tick_count == 0
        assert bar.vwap == 0.0

    def test_explicit_field_mapping_no_kwargs(self):
        """Extra keys in candle dict should NOT appear on Bar."""
        candle = {
            "timestamp": _ts(30, 0), "open": 21000.0, "high": 21010.0,
            "low": 20990.0, "close": 21005.0, "volume": 100,
            "session_type": SessionType.RTH,
            "extra_key": "should_not_appear",
        }
        bar = IBKRDataFeed.candle_to_bar(candle)
        assert not hasattr(bar, "extra_key")

    def test_round_trip_candle_to_bar_accepted_by_process_bar(self):
        """
        Round-trip: raw candle dict → candle_to_bar → Bar → process_bar()
        accepts it without AttributeError.

        Verifies that every attribute access process_bar() makes on the bar
        object succeeds on the adapter output.
        """
        from features.engine import Bar

        # Simulate a CandleAggregator output dict
        candle = {
            "timestamp": _ts(30, 0),
            "open": 21000.0,
            "high": 21010.0,
            "low": 20990.0,
            "close": 21005.0,
            "volume": 150,
            "tick_count": 12,
            "session_type": SessionType.RTH,
        }

        bar = IBKRDataFeed.candle_to_bar(candle)
        assert isinstance(bar, Bar)

        # Verify all attribute accesses that process_bar() makes:
        # bar.timestamp (line 254: bar.timestamp.astimezone)
        assert isinstance(bar.timestamp, datetime)
        _ = bar.timestamp.astimezone(timezone(timedelta(hours=-5)))

        # bar.volume (line 234, 241)
        assert isinstance(bar.volume, int)
        assert bar.volume > 0

        # bar.close (line 268)
        assert isinstance(bar.close, float)

        # bar.open, bar.high, bar.low (used by feature_engine.update)
        assert isinstance(bar.open, float)
        assert isinstance(bar.high, float)
        assert isinstance(bar.low, float)

        # bar.session_type (stored for risk engine RTH/ETH slippage modeling)
        assert bar.session_type == "RTH"

        # Verify Bar.range and Bar.body properties work
        assert bar.range == bar.high - bar.low
        assert bar.body == abs(bar.close - bar.open)
