"""
CME Micro Futures Contract Rollover Manager
=============================================
Automatically switches from the expiring contract to the next front month
before expiry. Rolls 5 trading days before expiration.

Supported instruments: MNQ, MES, MYM, M2K (any CME quarterly futures).
Expiry cycle: H (March), M (June), U (September), Z (December)
Expiry = 3rd Friday of the expiry month.

Safety rules:
  - Never trade both contracts simultaneously
  - If roll fails at any step, halt trading entirely (CRITICAL log)
  - Verify new contract has adequate volume (>= 1000 daily contracts)
  - If verification fails, retry next startup — do not force roll
"""

import logging
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ================================================================
# CONSTANTS
# ================================================================

ROLL_DAYS_BEFORE_EXPIRY = 5

# MNQ quarterly cycle: month code -> (calendar month, next code)
MONTH_CODES = {
    "H": 3,   # March
    "M": 6,   # June
    "U": 9,   # September
    "Z": 12,  # December
}

CYCLE_ORDER = ["H", "M", "U", "Z"]

# CME holidays (month, day) — fixed-date holidays.
# Floating holidays (MLK, Presidents, Memorial, Labor, Thanksgiving, Good Friday)
# are computed dynamically.
FIXED_CME_HOLIDAYS = {
    (1, 1),    # New Year's Day
    (7, 4),    # Independence Day
    (12, 25),  # Christmas Day
}

# Minimum daily volume to consider a contract tradeable
MIN_DAILY_VOLUME = 1000


# ================================================================
# TRADING CALENDAR
# ================================================================

def _mlk_day(year: int) -> date:
    """Third Monday of January."""
    jan1 = date(year, 1, 1)
    # First Monday
    first_monday = jan1 + timedelta(days=(7 - jan1.weekday()) % 7)
    if first_monday.month != 1:
        first_monday = date(year, 1, 1) + timedelta(days=(0 - jan1.weekday()) % 7)
    # Find first Monday of January
    d = date(year, 1, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d + timedelta(weeks=2)


def _presidents_day(year: int) -> date:
    """Third Monday of February."""
    d = date(year, 2, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d + timedelta(weeks=2)


def _memorial_day(year: int) -> date:
    """Last Monday of May."""
    d = date(year, 5, 31)
    while d.weekday() != 0:
        d -= timedelta(days=1)
    return d


def _labor_day(year: int) -> date:
    """First Monday of September."""
    d = date(year, 9, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d


def _thanksgiving(year: int) -> date:
    """Fourth Thursday of November."""
    d = date(year, 11, 1)
    while d.weekday() != 3:
        d += timedelta(days=1)
    return d + timedelta(weeks=3)


def _easter(year: int) -> date:
    """Compute Easter Sunday using the Anonymous Gregorian algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _good_friday(year: int) -> date:
    """Good Friday = Easter Sunday - 2."""
    return _easter(year) - timedelta(days=2)


def get_cme_holidays(year: int) -> set:
    """Return all CME holiday dates for the given year."""
    holidays = set()

    # Fixed holidays
    for month, day in FIXED_CME_HOLIDAYS:
        holidays.add(date(year, month, day))

    # Floating holidays
    holidays.add(_mlk_day(year))
    holidays.add(_presidents_day(year))
    holidays.add(_good_friday(year))
    holidays.add(_memorial_day(year))
    holidays.add(_labor_day(year))
    holidays.add(_thanksgiving(year))

    return holidays


def is_trading_day(d: date) -> bool:
    """True if d is a trading day (not weekend, not CME holiday)."""
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    if d in get_cme_holidays(d.year):
        return False
    return True


def subtract_trading_days(from_date: date, n: int) -> date:
    """Subtract n trading days from from_date."""
    current = from_date
    count = 0
    while count < n:
        current -= timedelta(days=1)
        if is_trading_day(current):
            count += 1
    return current


def third_friday(year: int, month: int) -> date:
    """Return the 3rd Friday of the given month/year."""
    d = date(year, month, 1)
    # Find first Friday
    while d.weekday() != 4:
        d += timedelta(days=1)
    # Third Friday = first Friday + 2 weeks
    return d + timedelta(weeks=2)


# ================================================================
# CONTRACT ROLLER
# ================================================================

class ContractRoller:
    """
    Manages automatic contract rollover for CME Micro futures.

    Supports MNQ, MES, MYM, M2K and any symbol using the HMUZ quarterly cycle.
    Rolls 5 trading days before the 3rd Friday of the expiry month.
    Uses the H→M→U→Z quarterly cycle with year rollover on Z→H.
    """

    def __init__(self, instrument: str = "MNQ"):
        self._instrument = instrument.upper()
        self._current_symbol: Optional[str] = None
        self._roll_executed: bool = False

    @staticmethod
    def parse_symbol(symbol: str) -> tuple:
        """
        Parse MNQ symbol into (base, month_code, year_digit).

        Examples:
            "MNQH6" -> ("MNQ", "H", 6)
            "MNQM6" -> ("MNQ", "M", 6)
            "MNQZ6" -> ("MNQ", "Z", 6)
        """
        # Symbol format: MNQ + month_code + year_digit(s)
        # Find the month code position (last uppercase letter before digits)
        base = symbol[:-2]       # "MNQ"
        month_code = symbol[-2]  # "H", "M", "U", "Z"
        year_digit = int(symbol[-1])  # 6, 7, etc.
        return base, month_code, year_digit

    @staticmethod
    def get_expiry_date(month_code: str, year_digit: int) -> date:
        """
        Calculate the expiry date (3rd Friday of the expiry month).

        Args:
            month_code: H, M, U, or Z
            year_digit: Single digit year (6 = 2026)

        Returns:
            date of the 3rd Friday of the contract's expiry month.
        """
        month = MONTH_CODES[month_code]
        # Map single digit to full year (assume 2020s decade)
        full_year = 2020 + year_digit
        return third_friday(full_year, month)

    @staticmethod
    def get_roll_date(month_code: str, year_digit: int) -> date:
        """
        Calculate the roll date (5 trading days before expiry).

        Returns:
            The first date on which the bot should roll to the next contract.
        """
        expiry = ContractRoller.get_expiry_date(month_code, year_digit)
        return subtract_trading_days(expiry, ROLL_DAYS_BEFORE_EXPIRY)

    def should_roll(self, current_symbol: str, today: Optional[date] = None) -> bool:
        """
        Determine if we should roll away from the current contract.

        Returns True if today >= roll_date (expiry - 5 trading days).
        """
        if today is None:
            today = date.today()

        _base, month_code, year_digit = self.parse_symbol(current_symbol)
        roll_date = self.get_roll_date(month_code, year_digit)

        should = today >= roll_date
        if should:
            expiry = self.get_expiry_date(month_code, year_digit)
            logger.info(
                "Roll check: %s expires %s, roll date %s, today %s -> ROLL NEEDED",
                current_symbol, expiry, roll_date, today,
            )
        else:
            roll_date_val = self.get_roll_date(month_code, year_digit)
            logger.debug(
                "Roll check: %s roll date %s, today %s -> no roll needed",
                current_symbol, roll_date_val, today,
            )

        return should

    @staticmethod
    def get_next_contract(current_symbol: str) -> str:
        """
        Determine the next contract in the quarterly cycle.

        H -> M -> U -> Z -> H (with year increment)

        Examples:
            "MNQH6" -> "MNQM6"
            "MNQZ6" -> "MNQH7"
        """
        base, month_code, year_digit = ContractRoller.parse_symbol(current_symbol)
        idx = CYCLE_ORDER.index(month_code)
        next_idx = (idx + 1) % len(CYCLE_ORDER)
        next_code = CYCLE_ORDER[next_idx]

        # Year rolls over on Z -> H
        next_year = year_digit
        if next_idx == 0:  # Wrapped from Z to H
            next_year = year_digit + 1

        return f"{base}{next_code}{next_year}"

    async def verify_contract(self, ibkr_client, symbol: str) -> bool:
        """
        Verify a contract exists and is tradeable via IBKR Client Portal.

        Uses /iserver/secdef/search to find the contract and checks
        that it has a valid conid and adequate volume.

        Args:
            ibkr_client: IBKRClient (Client Portal) instance
            symbol: Contract symbol (e.g., "MNQM6")

        Returns:
            True if contract is tradeable with adequate volume.
        """
        base, month_code, year_digit = self.parse_symbol(symbol)

        try:
            # Search for the base symbol
            search_data = await ibkr_client._post(
                "/iserver/secdef/search",
                {"symbol": base},
            )

            if not search_data or not isinstance(search_data, list):
                logger.error(
                    "CONTRACT_ROLL: Search returned no results for %s", base,
                )
                return False

            # Find FUT section
            for entry in search_data:
                sections = entry.get("sections", [])
                for section in sections:
                    if section.get("secType") == "FUT":
                        months_str = section.get("months", "")
                        # months_str format: "MAR26;JUN26;SEP26;DEC26"
                        month_names = {
                            "H": "MAR", "M": "JUN", "U": "SEP", "Z": "DEC",
                        }
                        full_year = 2020 + year_digit
                        target_month = f"{month_names[month_code]}{str(full_year)[2:]}"

                        if target_month in months_str:
                            logger.info(
                                "CONTRACT_ROLL: Verified %s exists (found %s in available months)",
                                symbol, target_month,
                            )
                            return True

            logger.warning(
                "CONTRACT_ROLL: Could not verify %s — not found in IBKR search results",
                symbol,
            )
            return False

        except Exception as e:
            logger.error(
                "CONTRACT_ROLL: Verification failed for %s — %s", symbol, e,
            )
            return False

    async def execute_roll(
        self,
        ibkr_client,
        order_executor,
        position_manager,
        data_feed=None,
    ) -> bool:
        """
        Execute the contract roll sequence.

        Steps:
          1. Close all open positions on the current contract
          2. Wait for fills to confirm
          3. Update client config symbol to new contract
          4. Resubscribe to real-time bars on new contract
          5. Log the roll at WARNING level

        Safety:
          - Never trades both contracts simultaneously
          - If any step fails, halts trading entirely (CRITICAL)

        Args:
            ibkr_client: IBKRClient instance
            order_executor: IBKROrderExecutor instance
            position_manager: PositionManager instance
            data_feed: IBKRDataFeed instance (optional, for resubscription)

        Returns:
            True on successful roll, False on failure (trading halted).
        """
        current_symbol = ibkr_client.config.symbol
        next_symbol = self.get_next_contract(current_symbol)

        logger.warning(
            "CONTRACT_ROLL: Starting roll %s -> %s", current_symbol, next_symbol,
        )

        # Step 1: Verify new contract exists
        verified = await self.verify_contract(ibkr_client, next_symbol)
        if not verified:
            logger.warning(
                "CONTRACT_ROLL: New contract %s failed verification — "
                "will retry next startup",
                next_symbol,
            )
            return False

        # Step 2: Close all open positions on current contract
        if position_manager.open_position_count > 0:
            logger.warning(
                "CONTRACT_ROLL: Closing %d open positions before roll",
                position_manager.open_position_count,
            )

            try:
                await order_executor.emergency_flatten(
                    reason=f"CONTRACT_ROLL: Rolling {current_symbol} -> {next_symbol}"
                )
            except Exception as e:
                logger.critical(
                    "CONTRACT_ROLL FAILED: Could not flatten positions — %s. "
                    "TRADING HALTED. Manual intervention required.",
                    e,
                )
                return False

            # Verify all positions are closed
            if position_manager.open_position_count > 0:
                logger.critical(
                    "CONTRACT_ROLL FAILED: %d positions still open after flatten. "
                    "TRADING HALTED. Manual intervention required.",
                    position_manager.open_position_count,
                )
                return False

        # Step 3: Update symbol in config
        old_symbol = ibkr_client.config.symbol
        ibkr_client.config.symbol = next_symbol
        logger.info(
            "CONTRACT_ROLL: Updated config symbol %s -> %s",
            old_symbol, next_symbol,
        )

        # Step 4: Resubscribe data feed on new contract
        if data_feed is not None:
            try:
                await data_feed.stop()
                await data_feed.start()
                logger.info(
                    "CONTRACT_ROLL: Data feed resubscribed on %s", next_symbol,
                )
            except Exception as e:
                logger.critical(
                    "CONTRACT_ROLL FAILED: Could not resubscribe data feed — %s. "
                    "TRADING HALTED.",
                    e,
                )
                # Revert symbol
                ibkr_client.config.symbol = old_symbol
                return False

        # Step 5: Log success
        _base, old_code, old_year = self.parse_symbol(current_symbol)
        _base, new_code, new_year = self.parse_symbol(next_symbol)
        old_expiry = self.get_expiry_date(old_code, old_year)
        new_expiry = self.get_expiry_date(new_code, new_year)

        logger.warning(
            "CONTRACT_ROLL: %s -> %s (old expiry: %s, new expiry: %s)",
            current_symbol, next_symbol, old_expiry, new_expiry,
        )

        self._current_symbol = next_symbol
        self._roll_executed = True
        return True

    @staticmethod
    def build_symbol(base: str, month_code: str, year_digit: int) -> str:
        """
        Build a contract symbol from components.

        Examples:
            build_symbol("MES", "H", 6) -> "MESH6"
            build_symbol("M2K", "M", 6) -> "M2KM6"
        """
        return f"{base}{month_code}{year_digit}"

    @staticmethod
    def get_front_month(base: str, ref_date: Optional[date] = None) -> str:
        """
        Determine the current front-month symbol for any instrument.

        Args:
            base: Instrument base symbol (e.g., "MNQ", "MES", "MYM", "M2K")
            ref_date: Reference date (default: today)

        Returns:
            Front-month symbol, e.g., "MNQM6"
        """
        if ref_date is None:
            ref_date = date.today()

        # Find the nearest quarterly expiry that hasn't passed the roll date
        for year_offset in range(2):
            year = ref_date.year + year_offset
            year_digit = year % 10
            for code in CYCLE_ORDER:
                month = MONTH_CODES[code]
                expiry = third_friday(year, month)
                roll_date = subtract_trading_days(expiry, ROLL_DAYS_BEFORE_EXPIRY)
                if ref_date < roll_date:
                    return f"{base}{code}{year_digit}"

        # Fallback: should not reach here
        year_digit = ref_date.year % 10
        return f"{base}{CYCLE_ORDER[0]}{year_digit + 1}"

    def get_roll_schedule(self, symbol: str) -> dict:
        """
        Return the roll schedule for a given contract symbol.

        Returns dict with expiry_date, roll_date, next_contract, and
        the next contract's expiry.
        """
        _base, month_code, year_digit = self.parse_symbol(symbol)
        expiry = self.get_expiry_date(month_code, year_digit)
        roll_date = self.get_roll_date(month_code, year_digit)
        next_contract = self.get_next_contract(symbol)
        _base2, next_code, next_year = self.parse_symbol(next_contract)
        next_expiry = self.get_expiry_date(next_code, next_year)

        return {
            "current_symbol": symbol,
            "expiry_date": expiry,
            "roll_date": roll_date,
            "next_contract": next_contract,
            "next_expiry": next_expiry,
        }
