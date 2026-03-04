"""
FOMC Calendar — 2026 Schedule & Helper
========================================
Single source of truth for FOMC meeting dates.
All announcement times are 2:00 PM ET.

Used by FOMCDriftModifier to calculate hours until next FOMC.
"""

from datetime import datetime, time
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# 2026 FOMC announcement dates — all at 2:00 PM ET
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


def hours_until_next_fomc(current_time: datetime) -> Optional[float]:
    """Return hours until the next FOMC announcement, or None if no future FOMC."""
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=ET)

    for fomc_dt in FOMC_2026_DATES:
        if fomc_dt > current_time:
            delta = fomc_dt - current_time
            return delta.total_seconds() / 3600.0

    return None


def next_fomc_date(current_time: datetime) -> Optional[datetime]:
    """Return the datetime of the next FOMC announcement, or None if none remain."""
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=ET)

    for fomc_dt in FOMC_2026_DATES:
        if fomc_dt > current_time:
            return fomc_dt

    return None
