"""
Tests for TWS API Client (ib_insync) and TWS Bar Adapter
==========================================================
Tests:
- Connection parameters (correct ports)
- Bar conversion to Bar dataclass
- Order submission format
- Auto-reconnect logic (mock disconnect)
- Contract definition for MNQ
"""

import asyncio
import math
import time
import pytest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from zoneinfo import ZoneInfo

from features.engine import Bar
from Broker.tws_adapter import adapt_tws_bar, validate_bar, get_session_type
from Broker.order_manager import OrderManager, MAX_CONTRACTS


# ================================================================
# FIXTURES
# ================================================================

def make_realtime_bar(
    ts=None, open_=21000.0, high=21005.0, low=20995.0,
    close=21002.0, volume=150, wap=21001.0, count=42,
):
    """Create a mock ib_insync RealTimeBar."""
    bar = SimpleNamespace()
    bar.time = ts or datetime(2026, 3, 4, 15, 30, 0, tzinfo=ZoneInfo("America/New_York"))
    bar.open_ = open_
    bar.high = high
    bar.low = low
    bar.close = close
    bar.volume = volume
    bar.wap = wap
    bar.count = count
    return bar


def make_historical_bar(
    date=None, open_=21000.0, high=21005.0, low=20995.0,
    close=21002.0, volume=150, average=21001.0, barCount=42,
):
    """Create a mock ib_insync BarData (historical)."""
    bar = SimpleNamespace()
    bar.date = date or datetime(2026, 3, 4, 15, 30, 0, tzinfo=ZoneInfo("America/New_York"))
    bar.open = open_
    bar.high = high
    bar.low = low
    bar.close = close
    bar.volume = volume
    bar.average = average
    bar.barCount = barCount
    return bar


@pytest.fixture
def mock_ib():
    """Create a mock IB instance."""
    ib = MagicMock()
    ib.isConnected.return_value = True
    ib.connectAsync = AsyncMock()
    ib.disconnect = MagicMock()
    front_contract = MagicMock(
        symbol="MNQ", exchange="CME", conId=12345,
        lastTradeDateOrContractMonth="202603", localSymbol="MNQH6",
    )
    back_contract = MagicMock(
        symbol="MNQ", exchange="CME", conId=12346,
        lastTradeDateOrContractMonth="202606", localSymbol="MNQM6",
    )
    ib.reqContractDetails = MagicMock(return_value=[
        MagicMock(contract=front_contract),
        MagicMock(contract=back_contract),
    ])
    ib.qualifyContracts = MagicMock(return_value=[front_contract])
    ib.reqRealTimeBars = MagicMock()
    ib.placeOrder = MagicMock()
    ib.cancelOrder = MagicMock()
    ib.positions = MagicMock(return_value=[])
    ib.accountSummary = MagicMock(return_value=[])
    ib.openTrades = MagicMock(return_value=[])

    # Event hooks as lists (ib_insync uses += to register)
    ib.errorEvent = MagicMock()
    ib.disconnectedEvent = MagicMock()
    ib.orderStatusEvent = MagicMock()

    return ib


@pytest.fixture
def tws_client(mock_ib):
    """Create an IBKRClient with mocked IB."""
    with patch("Broker.ibkr_client.IB", return_value=mock_ib):
        from Broker.ibkr_client import IBKRClient
        client = IBKRClient(host="127.0.0.1", port=7497, client_id=1)
        client._ib = mock_ib
        return client


# ================================================================
# CONNECTION PARAMETER TESTS
# ================================================================

class TestConnectionParameters:
    def test_default_port_paper(self, tws_client):
        """Default port 7497 for TWS paper trading."""
        assert tws_client._port == 7497

    def test_custom_port_gateway(self):
        """Port 4002 for IB Gateway paper trading."""
        with patch("Broker.ibkr_client.IB"):
            from Broker.ibkr_client import IBKRClient
            client = IBKRClient(port=4002)
            assert client._port == 4002

    def test_custom_host(self):
        """Custom host for remote TWS."""
        with patch("Broker.ibkr_client.IB"):
            from Broker.ibkr_client import IBKRClient
            client = IBKRClient(host="192.168.1.100")
            assert client._host == "192.168.1.100"

    def test_client_id(self):
        """Custom client ID."""
        with patch("Broker.ibkr_client.IB"):
            from Broker.ibkr_client import IBKRClient
            client = IBKRClient(client_id=42)
            assert client._client_id == 42

    @pytest.mark.asyncio
    async def test_connect_success(self, tws_client, mock_ib):
        """Successful connection."""
        mock_ib.connectAsync = AsyncMock()
        result = await tws_client.connect()
        assert result is True
        mock_ib.connectAsync.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_refused(self, tws_client, mock_ib):
        """Connection refused."""
        mock_ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError())
        result = await tws_client.connect()
        assert result is False

    def test_is_connected(self, tws_client, mock_ib):
        """is_connected delegates to IB."""
        mock_ib.isConnected.return_value = True
        assert tws_client.is_connected() is True
        mock_ib.isConnected.return_value = False
        assert tws_client.is_connected() is False

    def test_disconnect(self, tws_client, mock_ib):
        """Disconnect calls IB.disconnect."""
        mock_ib.isConnected.return_value = True
        tws_client.disconnect()
        mock_ib.disconnect.assert_called_once()


# ================================================================
# BAR CONVERSION TESTS
# ================================================================

class TestBarConversion:
    def test_realtime_bar_to_bar(self):
        """Convert ib_insync RealTimeBar to Bar dataclass."""
        ib_bar = make_realtime_bar()
        bar = adapt_tws_bar(ib_bar)
        assert bar is not None
        assert isinstance(bar, Bar)
        assert bar.open == 21000.0
        assert bar.high == 21005.0
        assert bar.low == 20995.0
        assert bar.close == 21002.0
        assert bar.volume == 150
        assert bar.timestamp.tzinfo is not None

    def test_historical_bar_to_bar(self):
        """Convert ib_insync BarData (historical) to Bar."""
        ib_bar = make_historical_bar()
        bar = adapt_tws_bar(ib_bar)
        assert bar is not None
        assert bar.open == 21000.0
        assert bar.high == 21005.0
        assert bar.low == 20995.0
        assert bar.close == 21002.0
        assert bar.volume == 150

    def test_utc_conversion(self):
        """Timestamp is converted to UTC."""
        et_time = datetime(2026, 3, 4, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        ib_bar = make_realtime_bar(ts=et_time)
        bar = adapt_tws_bar(ib_bar)
        assert bar is not None
        assert bar.timestamp.tzinfo == timezone.utc
        assert bar.timestamp.hour == 15  # 10 AM ET = 3 PM UTC

    def test_naive_timestamp_assumed_et(self):
        """Naive timestamps are assumed ET."""
        naive = datetime(2026, 3, 4, 10, 0, 0)  # no tzinfo
        ib_bar = make_realtime_bar(ts=naive)
        bar = adapt_tws_bar(ib_bar)
        assert bar is not None
        assert bar.timestamp.tzinfo == timezone.utc

    def test_session_type_rth(self):
        """Session type is RTH during market hours."""
        rth_time = datetime(2026, 3, 4, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        ib_bar = make_realtime_bar(ts=rth_time)
        bar = adapt_tws_bar(ib_bar)
        assert bar is not None
        assert bar.session_type == "RTH"

    def test_session_type_eth(self):
        """Session type is ETH outside market hours."""
        eth_time = datetime(2026, 3, 4, 20, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        ib_bar = make_realtime_bar(ts=eth_time)
        bar = adapt_tws_bar(ib_bar)
        assert bar is not None
        assert bar.session_type == "ETH"

    def test_vwap_extracted(self):
        """VWAP is extracted from wap field."""
        ib_bar = make_realtime_bar(wap=21001.5)
        bar = adapt_tws_bar(ib_bar)
        assert bar is not None
        assert bar.vwap == 21001.5

    def test_tick_count_extracted(self):
        """Tick count is extracted from count field."""
        ib_bar = make_realtime_bar(count=99)
        bar = adapt_tws_bar(ib_bar)
        assert bar is not None
        assert bar.tick_count == 99

    def test_reject_zero_volume(self):
        """Bars with zero volume are rejected."""
        ib_bar = make_realtime_bar(volume=0)
        bar = adapt_tws_bar(ib_bar)
        assert bar is None

    def test_reject_negative_price(self):
        """Bars with negative price are rejected."""
        ib_bar = make_realtime_bar(open_=-100.0)
        bar = adapt_tws_bar(ib_bar)
        assert bar is None

    def test_reject_high_less_than_low(self):
        """Bars with high < low are rejected."""
        ib_bar = make_realtime_bar(high=20990.0, low=21000.0)
        bar = adapt_tws_bar(ib_bar)
        assert bar is None

    def test_reject_nan_price(self):
        """Bars with NaN price are rejected."""
        ib_bar = make_realtime_bar(close=float("nan"))
        bar = adapt_tws_bar(ib_bar)
        assert bar is None

    def test_reject_inf_price(self):
        """Bars with infinite price are rejected."""
        ib_bar = make_realtime_bar(high=float("inf"))
        bar = adapt_tws_bar(ib_bar)
        assert bar is None


# ================================================================
# BAR VALIDATION TESTS
# ================================================================

class TestBarValidation:
    def test_valid_bar(self):
        bar = Bar(
            timestamp=datetime.now(timezone.utc),
            open=21000.0, high=21005.0, low=20995.0,
            close=21002.0, volume=100,
        )
        assert validate_bar(bar) is True

    def test_naive_timestamp_rejected(self):
        bar = Bar(
            timestamp=datetime.now(),  # no tz
            open=21000.0, high=21005.0, low=20995.0,
            close=21002.0, volume=100,
        )
        assert validate_bar(bar) is False

    def test_zero_volume_rejected(self):
        bar = Bar(
            timestamp=datetime.now(timezone.utc),
            open=21000.0, high=21005.0, low=20995.0,
            close=21002.0, volume=0,
        )
        assert validate_bar(bar) is False

    def test_high_less_than_low_self_heals(self):
        """Bar.__post_init__ auto-corrects high < low by swapping."""
        bar = Bar(
            timestamp=datetime.now(timezone.utc),
            open=21000.0, high=20990.0, low=21000.0,
            close=21002.0, volume=100,
        )
        # Bar self-heals: high/low swapped, then adjusted to cover open/close
        assert bar.high >= bar.low
        assert validate_bar(bar) is True


# ================================================================
# SESSION TYPE TESTS
# ================================================================

class TestSessionType:
    def test_rth_open(self):
        ts = datetime(2026, 3, 4, 9, 30, 0, tzinfo=ZoneInfo("America/New_York"))
        assert get_session_type(ts) == "RTH"

    def test_rth_close(self):
        ts = datetime(2026, 3, 4, 15, 59, 0, tzinfo=ZoneInfo("America/New_York"))
        assert get_session_type(ts) == "RTH"

    def test_eth_premarket(self):
        ts = datetime(2026, 3, 4, 8, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        assert get_session_type(ts) == "ETH"

    def test_eth_afterhours(self):
        ts = datetime(2026, 3, 4, 16, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        assert get_session_type(ts) == "ETH"

    def test_eth_weekend(self):
        # March 7, 2026 is Saturday
        ts = datetime(2026, 3, 7, 12, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        assert get_session_type(ts) == "ETH"


# ================================================================
# ORDER SUBMISSION TESTS
# ================================================================

class TestOrderSubmission:
    @pytest.mark.asyncio
    async def test_market_order(self, tws_client, mock_ib):
        """Market order submission."""
        mock_trade = MagicMock()
        mock_trade.order.orderId = 100
        mock_ib.placeOrder.return_value = mock_trade

        # Qualify contract first
        tws_client.get_contract("MNQ", "CME")

        oid = await tws_client.place_order("BUY", 1, "MKT")
        assert oid == 100
        mock_ib.placeOrder.assert_called_once()

    @pytest.mark.asyncio
    async def test_limit_order(self, tws_client, mock_ib):
        """Limit order submission."""
        mock_trade = MagicMock()
        mock_trade.order.orderId = 101
        mock_ib.placeOrder.return_value = mock_trade

        tws_client.get_contract("MNQ", "CME")
        oid = await tws_client.place_order("SELL", 1, "LMT", limit_price=21050.0)
        assert oid == 101

    @pytest.mark.asyncio
    async def test_stop_order(self, tws_client, mock_ib):
        """Stop order submission."""
        mock_trade = MagicMock()
        mock_trade.order.orderId = 102
        mock_ib.placeOrder.return_value = mock_trade

        tws_client.get_contract("MNQ", "CME")
        oid = await tws_client.place_order("SELL", 1, "STP", stop_price=20900.0)
        assert oid == 102

    @pytest.mark.asyncio
    async def test_limit_order_requires_price(self, tws_client, mock_ib):
        """Limit order without price returns None."""
        tws_client.get_contract("MNQ", "CME")
        oid = await tws_client.place_order("BUY", 1, "LMT")
        assert oid is None

    @pytest.mark.asyncio
    async def test_order_without_contract(self, tws_client):
        """Order without qualified contract returns None."""
        oid = await tws_client.place_order("BUY", 1, "MKT")
        assert oid is None


# ================================================================
# CONTRACT TESTS
# ================================================================

class TestContract:
    def test_qualify_mnq(self, tws_client, mock_ib):
        """Contract qualification for MNQ picks front month."""
        contract = tws_client.get_contract("MNQ", "CME")
        assert contract.symbol == "MNQ"
        assert contract.exchange == "CME"
        assert contract.lastTradeDateOrContractMonth == "202603"
        mock_ib.reqContractDetails.assert_called_once()
        mock_ib.qualifyContracts.assert_called_once()

    def test_picks_nearest_expiry(self, tws_client, mock_ib):
        """Front month is selected even when details arrive out of order."""
        front = MagicMock(
            symbol="MNQ", exchange="CME", conId=12345,
            lastTradeDateOrContractMonth="202603",
        )
        back = MagicMock(
            symbol="MNQ", exchange="CME", conId=12346,
            lastTradeDateOrContractMonth="202606",
        )
        # Return in reverse order -- back month first
        mock_ib.reqContractDetails.return_value = [
            MagicMock(contract=back),
            MagicMock(contract=front),
        ]
        mock_ib.qualifyContracts.return_value = [front]

        contract = tws_client.get_contract("MNQ", "CME")
        assert contract.lastTradeDateOrContractMonth == "202603"

    def test_no_details_raises(self, tws_client, mock_ib):
        """Raises ValueError if no contract details found."""
        mock_ib.reqContractDetails.return_value = []
        with pytest.raises(ValueError, match="Could not find contract details"):
            tws_client.get_contract("INVALID", "CME")

    def test_qualify_fails(self, tws_client, mock_ib):
        """Raises ValueError if qualification fails."""
        mock_ib.qualifyContracts.return_value = []
        with pytest.raises(ValueError, match="Could not qualify"):
            tws_client.get_contract("MNQ", "CME")


# ================================================================
# AUTO-RECONNECT TESTS
# ================================================================

class TestAutoReconnect:
    @pytest.mark.asyncio
    async def test_reconnect_on_disconnect(self, tws_client, mock_ib):
        """Reconnect is attempted on disconnect."""
        mock_ib.connectAsync = AsyncMock()
        mock_ib.isConnected.return_value = True

        await tws_client._reconnect()
        mock_ib.connectAsync.assert_awaited()

    @pytest.mark.asyncio
    async def test_reconnect_retries(self, tws_client, mock_ib):
        """Reconnect retries with exponential backoff."""
        call_count = 0

        async def fail_then_succeed(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionRefusedError("refused")

        mock_ib.connectAsync = AsyncMock(side_effect=fail_then_succeed)
        mock_ib.isConnected.return_value = True

        await tws_client._reconnect()
        assert call_count == 3  # Failed twice, succeeded on third

    @pytest.mark.asyncio
    async def test_reconnect_gives_up(self, tws_client, mock_ib):
        """Reconnect gives up after max retries."""
        mock_ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("refused"))
        mock_ib.isConnected.return_value = False

        await tws_client._reconnect()
        assert mock_ib.connectAsync.await_count == 3  # RECONNECT_MAX_RETRIES


# ================================================================
# CALLBACK TESTS
# ================================================================

class TestCallbacks:
    def test_on_bar_update_callback(self, tws_client):
        """Register and fire bar update callback."""
        received = []
        tws_client.on_bar_update(lambda bar: received.append(bar))

        assert len(tws_client._on_bar_update) == 1

    def test_on_order_filled_callback(self, tws_client):
        """Register order filled callback."""
        tws_client.on_order_filled(lambda info: None)
        assert len(tws_client._on_order_filled) == 1

    def test_on_error_callback(self, tws_client):
        """Register error callback."""
        tws_client.on_error(lambda *args: None)
        assert len(tws_client._on_error) == 1

    def test_order_filled_fires_callback(self, tws_client):
        """Order fill fires on_order_filled callbacks."""
        fills = []
        tws_client.on_order_filled(lambda info: fills.append(info))

        mock_trade = MagicMock()
        mock_trade.orderStatus.status = "Filled"
        mock_trade.orderStatus.filled = 1
        mock_trade.orderStatus.avgFillPrice = 21050.0
        mock_trade.order.orderId = 200
        mock_trade.order.action = "BUY"

        tws_client._handle_order_status(mock_trade)

        assert len(fills) == 1
        assert fills[0]["order_id"] == 200
        assert fills[0]["avg_fill_price"] == 21050.0
