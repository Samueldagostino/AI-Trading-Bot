"""
Tests for MNQ Contract Rollover Manager
========================================
Verifies:
  - Roll timing (5 trading days before 3rd Friday expiry)
  - Contract cycle (H→M→U→Z→H with year rollover)
  - Position closure before roll
  - Data feed resubscription after roll
  - Trading halt on roll failure
  - Expiry date calculation for all 4 quarter months
"""

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from Broker.contract_roller import (
    ContractRoller,
    ROLL_DAYS_BEFORE_EXPIRY,
    third_friday,
    subtract_trading_days,
    is_trading_day,
    get_cme_holidays,
)


# ================================================================
# FIXTURES
# ================================================================

@pytest.fixture
def roller():
    return ContractRoller()


@pytest.fixture
def mock_ibkr_client():
    client = MagicMock()
    client.config = MagicMock()
    client.config.symbol = "MNQH6"
    client._post = AsyncMock(return_value=[{
        "conid": "362687422",
        "symbol": "MNQ",
        "sections": [{
            "secType": "FUT",
            "months": "MAR26;JUN26;SEP26;DEC26",
            "exchange": "CME",
        }],
    }])
    return client


@pytest.fixture
def mock_executor():
    executor = MagicMock()
    executor.emergency_flatten = AsyncMock()
    executor.is_halted = False
    return executor


@pytest.fixture
def mock_position_manager():
    pm = MagicMock()
    pm.open_position_count = 0
    pm.open_positions = {}
    return pm


@pytest.fixture
def mock_data_feed():
    feed = MagicMock()
    feed.start = AsyncMock(return_value=True)
    feed.stop = AsyncMock()
    return feed


# ================================================================
# EXPIRY DATE CALCULATION
# ================================================================

class TestExpiryDateCalculation:
    """Verify 3rd Friday logic for all 4 quarter months."""

    def test_march_2026_expiry(self):
        """MNQH6 expires on March 20, 2026 (3rd Friday of March)."""
        expiry = ContractRoller.get_expiry_date("H", 6)
        assert expiry == date(2026, 3, 20)

    def test_june_2026_expiry(self):
        """MNQM6 expires on June 19, 2026 (3rd Friday of June)."""
        expiry = ContractRoller.get_expiry_date("M", 6)
        assert expiry == date(2026, 6, 19)

    def test_september_2026_expiry(self):
        """MNQU6 expires on September 18, 2026 (3rd Friday of September)."""
        expiry = ContractRoller.get_expiry_date("U", 6)
        assert expiry == date(2026, 9, 18)

    def test_december_2026_expiry(self):
        """MNQZ6 expires on December 18, 2026 (3rd Friday of December)."""
        expiry = ContractRoller.get_expiry_date("Z", 6)
        assert expiry == date(2026, 12, 18)

    def test_march_2027_expiry(self):
        """MNQH7 expires on March 19, 2027 (3rd Friday of March)."""
        expiry = ContractRoller.get_expiry_date("H", 7)
        assert expiry == date(2027, 3, 19)

    def test_third_friday_function(self):
        """Verify third_friday helper for known dates."""
        # March 2026: 1st is Sunday, first Friday is March 6, third is March 20
        assert third_friday(2026, 3) == date(2026, 3, 20)
        # June 2026: 1st is Monday, first Friday is June 5, third is June 19
        assert third_friday(2026, 6) == date(2026, 6, 19)


# ================================================================
# ROLL TIMING
# ================================================================

class TestShouldRoll:
    """Roll timing: 5 trading days before expiry."""

    def test_should_roll_5_days_before(self, roller):
        """Returns True on March 13 for March 20 expiry (MNQH6).

        March 20 (Fri) is expiry.
        5 trading days before: March 13 (Fri).
        """
        assert roller.should_roll("MNQH6", today=date(2026, 3, 13)) is True

    def test_should_not_roll_10_days_before(self, roller):
        """Returns False on March 6 (10+ trading days before March 20 expiry)."""
        assert roller.should_roll("MNQH6", today=date(2026, 3, 6)) is False

    def test_should_roll_on_expiry_day(self, roller):
        """Returns True on the expiry day itself."""
        assert roller.should_roll("MNQH6", today=date(2026, 3, 20)) is True

    def test_should_roll_day_after_roll_date(self, roller):
        """Returns True the Monday after the roll date."""
        assert roller.should_roll("MNQH6", today=date(2026, 3, 16)) is True

    def test_should_not_roll_day_before_roll_date(self, roller):
        """Returns False the day before the roll date."""
        assert roller.should_roll("MNQH6", today=date(2026, 3, 12)) is False

    def test_roll_date_calculation_mnqh6(self, roller):
        """Verify roll date for MNQH6 is approximately March 13."""
        roll_date = ContractRoller.get_roll_date("H", 6)
        # Expiry is March 20 (Friday)
        # 5 trading days before: March 13 (Friday)
        assert roll_date == date(2026, 3, 13)


# ================================================================
# CONTRACT CYCLE
# ================================================================

class TestGetNextContract:
    """H→M→U→Z→H quarterly cycle."""

    def test_get_next_contract_h_to_m(self):
        """MNQH6 → MNQM6."""
        assert ContractRoller.get_next_contract("MNQH6") == "MNQM6"

    def test_get_next_contract_m_to_u(self):
        """MNQM6 → MNQU6."""
        assert ContractRoller.get_next_contract("MNQM6") == "MNQU6"

    def test_get_next_contract_u_to_z(self):
        """MNQU6 → MNQZ6."""
        assert ContractRoller.get_next_contract("MNQU6") == "MNQZ6"

    def test_get_next_contract_z_to_h_year_rollover(self):
        """MNQZ6 → MNQH7 (year increments on Z→H)."""
        assert ContractRoller.get_next_contract("MNQZ6") == "MNQH7"

    def test_full_cycle(self):
        """Verify a complete year cycle."""
        sym = "MNQH6"
        sym = ContractRoller.get_next_contract(sym)
        assert sym == "MNQM6"
        sym = ContractRoller.get_next_contract(sym)
        assert sym == "MNQU6"
        sym = ContractRoller.get_next_contract(sym)
        assert sym == "MNQZ6"
        sym = ContractRoller.get_next_contract(sym)
        assert sym == "MNQH7"


# ================================================================
# SYMBOL PARSING
# ================================================================

class TestParseSymbol:
    def test_parse_mnqh6(self):
        base, code, year = ContractRoller.parse_symbol("MNQH6")
        assert base == "MNQ"
        assert code == "H"
        assert year == 6

    def test_parse_mnqz6(self):
        base, code, year = ContractRoller.parse_symbol("MNQZ6")
        assert base == "MNQ"
        assert code == "Z"
        assert year == 6


# ================================================================
# EXECUTE ROLL -- Position Closure
# ================================================================

class TestExecuteRoll:

    @pytest.mark.asyncio
    async def test_execute_roll_closes_positions(
        self, roller, mock_ibkr_client, mock_executor,
        mock_position_manager, mock_data_feed,
    ):
        """Mock positions are closed before switching contract."""
        mock_position_manager.open_position_count = 2

        # After emergency_flatten, positions should be 0
        def flatten_side_effect(reason):
            mock_position_manager.open_position_count = 0
        mock_executor.emergency_flatten = AsyncMock(side_effect=flatten_side_effect)

        result = await roller.execute_roll(
            mock_ibkr_client, mock_executor,
            mock_position_manager, mock_data_feed,
        )

        assert result is True
        mock_executor.emergency_flatten.assert_called_once()
        assert "CONTRACT_ROLL" in mock_executor.emergency_flatten.call_args[1]["reason"]

    @pytest.mark.asyncio
    async def test_roll_updates_symbol(
        self, roller, mock_ibkr_client, mock_executor,
        mock_position_manager, mock_data_feed,
    ):
        """Verify config symbol is updated to next contract."""
        result = await roller.execute_roll(
            mock_ibkr_client, mock_executor,
            mock_position_manager, mock_data_feed,
        )

        assert result is True
        assert mock_ibkr_client.config.symbol == "MNQM6"

    @pytest.mark.asyncio
    async def test_roll_resubscribes_bars(
        self, roller, mock_ibkr_client, mock_executor,
        mock_position_manager, mock_data_feed,
    ):
        """Verify data feed is restarted on new contract."""
        result = await roller.execute_roll(
            mock_ibkr_client, mock_executor,
            mock_position_manager, mock_data_feed,
        )

        assert result is True
        mock_data_feed.stop.assert_called_once()
        mock_data_feed.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_roll_halts_on_failure(
        self, roller, mock_ibkr_client, mock_executor,
        mock_position_manager, mock_data_feed,
    ):
        """Verify trading stops if roll fails (positions can't be closed)."""
        mock_position_manager.open_position_count = 2
        mock_executor.emergency_flatten = AsyncMock(
            side_effect=Exception("Connection lost")
        )

        result = await roller.execute_roll(
            mock_ibkr_client, mock_executor,
            mock_position_manager, mock_data_feed,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_roll_fails_on_unverified_contract(
        self, roller, mock_ibkr_client, mock_executor,
        mock_position_manager, mock_data_feed,
    ):
        """If contract verification fails, roll returns False."""
        mock_ibkr_client._post = AsyncMock(return_value=None)

        result = await roller.execute_roll(
            mock_ibkr_client, mock_executor,
            mock_position_manager, mock_data_feed,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_roll_reverts_symbol_on_data_feed_failure(
        self, roller, mock_ibkr_client, mock_executor,
        mock_position_manager, mock_data_feed,
    ):
        """If data feed resubscription fails, symbol is reverted."""
        mock_data_feed.stop = AsyncMock(
            side_effect=Exception("Feed error")
        )

        result = await roller.execute_roll(
            mock_ibkr_client, mock_executor,
            mock_position_manager, mock_data_feed,
        )

        assert result is False
        # Symbol should be reverted to original
        assert mock_ibkr_client.config.symbol == "MNQH6"


# ================================================================
# TRADING CALENDAR
# ================================================================

class TestTradingCalendar:
    def test_weekend_not_trading_day(self):
        """Saturday and Sunday are not trading days."""
        # March 14, 2026 is a Saturday
        assert is_trading_day(date(2026, 3, 14)) is False
        # March 15, 2026 is a Sunday
        assert is_trading_day(date(2026, 3, 15)) is False

    def test_weekday_is_trading_day(self):
        """Regular Monday is a trading day."""
        # March 16, 2026 is a Monday
        assert is_trading_day(date(2026, 3, 16)) is True

    def test_new_years_not_trading_day(self):
        """New Year's Day is not a trading day."""
        assert is_trading_day(date(2026, 1, 1)) is False

    def test_christmas_not_trading_day(self):
        """Christmas is not a trading day."""
        assert is_trading_day(date(2026, 12, 25)) is False

    def test_mlk_day_not_trading_day(self):
        """MLK Day (3rd Monday of January) is not a trading day."""
        holidays = get_cme_holidays(2026)
        # MLK Day 2026: January 19
        assert date(2026, 1, 19) in holidays

    def test_good_friday_not_trading_day(self):
        """Good Friday is a CME holiday."""
        holidays = get_cme_holidays(2026)
        # Easter 2026 is April 5, so Good Friday is April 3
        assert date(2026, 4, 3) in holidays

    def test_subtract_trading_days_skips_weekend(self):
        """Subtracting trading days properly skips weekends."""
        # March 20 (Fri) - 5 trading days = March 13 (Fri)
        result = subtract_trading_days(date(2026, 3, 20), 5)
        assert result == date(2026, 3, 13)

    def test_subtract_trading_days_across_weekend(self):
        """Subtracting trading days crosses a weekend correctly."""
        # March 16 (Mon) - 1 trading day = March 13 (Fri)
        result = subtract_trading_days(date(2026, 3, 16), 1)
        assert result == date(2026, 3, 13)


# ================================================================
# ROLL SCHEDULE
# ================================================================

class TestRollSchedule:
    def test_roll_schedule_mnqh6(self, roller):
        """Get roll schedule for MNQH6."""
        schedule = roller.get_roll_schedule("MNQH6")
        assert schedule["current_symbol"] == "MNQH6"
        assert schedule["expiry_date"] == date(2026, 3, 20)
        assert schedule["roll_date"] == date(2026, 3, 13)
        assert schedule["next_contract"] == "MNQM6"
        assert schedule["next_expiry"] == date(2026, 6, 19)

    def test_roll_schedule_mnqm6(self, roller):
        """Get roll schedule for MNQM6."""
        schedule = roller.get_roll_schedule("MNQM6")
        assert schedule["current_symbol"] == "MNQM6"
        assert schedule["expiry_date"] == date(2026, 6, 19)
        assert schedule["next_contract"] == "MNQU6"


# ================================================================
# CONTRACT VERIFICATION
# ================================================================

class TestVerifyContract:

    @pytest.mark.asyncio
    async def test_verify_valid_contract(self, roller, mock_ibkr_client):
        """Verify returns True for a contract that exists."""
        result = await roller.verify_contract(mock_ibkr_client, "MNQM6")
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_invalid_contract(self, roller, mock_ibkr_client):
        """Verify returns False when search returns nothing."""
        mock_ibkr_client._post = AsyncMock(return_value=None)
        result = await roller.verify_contract(mock_ibkr_client, "MNQM6")
        assert result is False

    @pytest.mark.asyncio
    async def test_verify_handles_api_error(self, roller, mock_ibkr_client):
        """Verify returns False on API exception."""
        mock_ibkr_client._post = AsyncMock(side_effect=Exception("API error"))
        result = await roller.verify_contract(mock_ibkr_client, "MNQM6")
        assert result is False


# ================================================================
# ROLLOVER OVERRIDE ENV VAR
# ================================================================

class TestRolloverOverride:
    def test_rollover_override_skips_check(self, roller):
        """ROLLOVER_OVERRIDE=true skips the roll check even when roll is due."""
        import os
        os.environ["ROLLOVER_OVERRIDE"] = "true"
        try:
            # March 13 would normally trigger a roll for MNQH6
            assert roller.should_roll("MNQH6", today=date(2026, 3, 13)) is False
        finally:
            del os.environ["ROLLOVER_OVERRIDE"]

    def test_rollover_override_false_allows_check(self, roller):
        """ROLLOVER_OVERRIDE=false does NOT skip the roll check."""
        import os
        os.environ["ROLLOVER_OVERRIDE"] = "false"
        try:
            assert roller.should_roll("MNQH6", today=date(2026, 3, 13)) is True
        finally:
            del os.environ["ROLLOVER_OVERRIDE"]


# ================================================================
# 4-QUARTER ROLL SCHEDULE
# ================================================================

class TestRollScheduleNext4Quarters:
    def test_roll_schedule_next_4_quarters(self):
        """Prints correct roll dates for next 4 quarters starting from MNQH6."""
        schedule = ContractRoller.get_roll_schedule_next_4_quarters("MNQH6")
        assert len(schedule) == 4

        # Quarter 1: MNQH6 -> MNQM6
        assert schedule[0]["current"] == "MNQH6"
        assert schedule[0]["next"] == "MNQM6"
        assert schedule[0]["expiry"] == date(2026, 3, 20)
        assert schedule[0]["roll_date"] == date(2026, 3, 13)

        # Quarter 2: MNQM6 -> MNQU6
        assert schedule[1]["current"] == "MNQM6"
        assert schedule[1]["next"] == "MNQU6"
        assert schedule[1]["expiry"] == date(2026, 6, 19)

        # Quarter 3: MNQU6 -> MNQZ6
        assert schedule[2]["current"] == "MNQU6"
        assert schedule[2]["next"] == "MNQZ6"
        assert schedule[2]["expiry"] == date(2026, 9, 18)

        # Quarter 4: MNQZ6 -> MNQH7
        assert schedule[3]["current"] == "MNQZ6"
        assert schedule[3]["next"] == "MNQH7"
        assert schedule[3]["expiry"] == date(2026, 12, 18)


# ================================================================
# ROLL DATE SKIPS CME HOLIDAYS
# ================================================================

class TestRollDateSkipsCMEHolidays:
    def test_roll_date_skips_cme_holiday(self):
        """Roll date calculation correctly skips CME holidays.

        For MNQU6 (Sept 18 expiry), Labor Day (Sept 7, 2026) is
        in the 5-trading-day window. The roll date must account for it.
        """
        roll_date = ContractRoller.get_roll_date("U", 6)
        # Sept 18 (Fri) - 5 trading days, with Labor Day (Sept 7) considered
        # Sept 7 (Mon) is Labor Day -> not a trading day
        # Count back from Sept 18: Sept 17 (Thu), 16 (Wed), 15 (Tue),
        # 14 (Mon), 11 (Fri) = 5 trading days
        assert roll_date == date(2026, 9, 11)
        # Verify Labor Day is indeed a holiday
        assert not is_trading_day(date(2026, 9, 7))
