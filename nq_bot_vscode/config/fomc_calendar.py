"""
FOMC Calendar -- 2025 + 2026 Schedules & Helpers
=================================================
Single source of truth for FOMC meeting dates.
All announcement times are 2:00 PM ET.

Used by FOMCDriftModifier to calculate hours until next FOMC.
"""

from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# 2025 FOMC announcement dates -- all at 2:00 PM ET
FOMC_2025_DATES: list = [
    datetime(2025, 1, 29, 14, 0, tzinfo=ET),
    datetime(2025, 3, 19, 14, 0, tzinfo=ET),
    datetime(2025, 5, 7, 14, 0, tzinfo=ET),
    datetime(2025, 6, 18, 14, 0, tzinfo=ET),
    datetime(2025, 7, 30, 14, 0, tzinfo=ET),
    datetime(2025, 9, 17, 14, 0, tzinfo=ET),
    datetime(2025, 10, 29, 14, 0, tzinfo=ET),
    datetime(2025, 12, 17, 14, 0, tzinfo=ET),
]

# 2026 FOMC announcement dates -- all at 2:00 PM ET
FOMC_2026_DATES: list = [
    datetime(2026, 1, 29, 14, 0, tzinfo=ET),
    datetime(2026, 3, 19, 14, 0, tzinfo=ET),
    datetime(2026, 5, 7, 14, 0, tzinfo=ET),
    datetime(2026, 6, 18, 14, 0, tzinfo=ET),
    datetime(2026, 7, 30, 14, 0, tzinfo=ET),
    datetime(2026, 9, 17, 14, 0, tzinfo=ET),
    datetime(2026, 11, 5, 14, 0, tzinfo=ET),
    datetime(2026, 12, 17, 14, 0, tzinfo=ET),
]

# Combined sorted list of all FOMC dates
ALL_FOMC_DATES: list = sorted(FOMC_2025_DATES + FOMC_2026_DATES)


def hours_until_next_fomc(current_time: datetime) -> Optional[float]:
    """Return hours until the next FOMC announcement, or None if no future FOMC."""
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=ET)

    for fomc_dt in ALL_FOMC_DATES:
        if fomc_dt > current_time:
            delta = fomc_dt - current_time
            return delta.total_seconds() / 3600.0

    return None


def next_fomc_date(current_time: datetime) -> Optional[datetime]:
    """Return the datetime of the next FOMC announcement, or None if none remain."""
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=ET)

    for fomc_dt in ALL_FOMC_DATES:
        if fomc_dt > current_time:
            return fomc_dt

    return None


def is_fomc_day(d) -> bool:
    """Check if a given date is an FOMC announcement day.

    Args:
        d: A datetime or date object.
    """
    if isinstance(d, datetime):
        check_date = d.date()
    elif isinstance(d, date):
        check_date = d
    else:
        return False

    return any(fomc_dt.date() == check_date for fomc_dt in ALL_FOMC_DATES)


def get_fomc_window(current_datetime: datetime) -> str:
    """Determine the current FOMC proximity window.

    Returns one of:
        "STAND_ASIDE"  -- within 2 hours of announcement (no trading)
        "DRIFT_STRONG" -- 2-24 hours before announcement (strong pre-FOMC drift)
        "DRIFT_MILD"   -- 24-72 hours before announcement (mild positioning)
        "NONE"         -- no FOMC event within 72 hours
    """
    hours = hours_until_next_fomc(current_datetime)

    if hours is None:
        return "NONE"

    if hours <= 2.0:
        return "STAND_ASIDE"
    elif hours <= 24.0:
        return "DRIFT_STRONG"
    elif hours <= 72.0:
        return "DRIFT_MILD"
    else:
        return "NONE"
