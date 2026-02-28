"""
Tests for IBKR Client Portal API connector.

Tests:
- Contract resolution and rollover detection
- Market data snapshot parsing
- Session keepalive and reconnection logic
- Historical bar fetching
- Price parsing edge cases
- Connection health status
"""

import asyncio
import time
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from Broker.ibkr_client import (
    IBKRClient,
    IBKRConfig,
    ContractInfo,
    MarketSnapshot,
    SNAPSHOT_FIELDS,
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
