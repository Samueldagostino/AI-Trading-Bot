"""Tests for FOMC calendar helpers."""

import pytest
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from config.fomc_calendar import (
    ET,
    FOMC_2025_DATES,
    FOMC_2026_DATES,
    ALL_FOMC_DATES,
    hours_until_next_fomc,
    next_fomc_date,
    is_fomc_day,
    get_fomc_window,
)


class TestFOMCDates:
    def test_2025_dates_count(self):
        assert len(FOMC_2025_DATES) == 8

    def test_2026_dates_count(self):
        assert len(FOMC_2026_DATES) == 8

    def test_all_dates_sorted(self):
        for i in range(len(ALL_FOMC_DATES) - 1):
            assert ALL_FOMC_DATES[i] < ALL_FOMC_DATES[i + 1]

    def test_all_at_2pm_et(self):
        for dt in ALL_FOMC_DATES:
            assert dt.hour == 14
            assert dt.minute == 0

    def test_all_have_et_timezone(self):
        for dt in ALL_FOMC_DATES:
            assert dt.tzinfo == ET

    def test_2025_specific_dates(self):
        expected_months_days = [
            (1, 29), (3, 19), (5, 7), (6, 18),
            (7, 30), (9, 17), (10, 29), (12, 17),
        ]
        for dt, (month, day) in zip(FOMC_2025_DATES, expected_months_days):
            assert dt.month == month
            assert dt.day == day

    def test_2026_specific_dates(self):
        expected_months_days = [
            (1, 29), (3, 19), (5, 7), (6, 18),
            (7, 30), (9, 17), (11, 5), (12, 17),
        ]
        for dt, (month, day) in zip(FOMC_2026_DATES, expected_months_days):
            assert dt.month == month
            assert dt.day == day

    def test_combined_count(self):
        assert len(ALL_FOMC_DATES) == 16


class TestHoursUntilNextFOMC:
    def test_before_first_fomc(self):
        t = datetime(2025, 1, 1, 12, 0, tzinfo=ET)
        hours = hours_until_next_fomc(t)
        assert hours is not None
        assert hours > 0

    def test_between_fomc_dates(self):
        # After Jan 29 2025, next is Mar 19 2025
        t = datetime(2025, 2, 15, 12, 0, tzinfo=ET)
        hours = hours_until_next_fomc(t)
        assert hours is not None
        assert hours > 0
        next_dt = next_fomc_date(t)
        assert next_dt.month == 3
        assert next_dt.day == 19

    def test_after_last_fomc(self):
        t = datetime(2027, 1, 1, 12, 0, tzinfo=ET)
        assert hours_until_next_fomc(t) is None

    def test_naive_datetime_gets_et(self):
        t = datetime(2025, 1, 28, 14, 0)  # naive
        hours = hours_until_next_fomc(t)
        assert hours is not None
        # Next is Jan 29 at 14:00 ET, so ~24 hours away
        assert 23 < hours < 25

    def test_exact_fomc_time(self):
        # At exact FOMC time, should return next one
        t = FOMC_2025_DATES[0]  # Jan 29 2025 14:00 ET
        hours = hours_until_next_fomc(t)
        # Should return hours until March 19
        assert hours is not None
        assert hours > 1000  # ~49 days * 24 hours

    def test_cross_year_boundary(self):
        # After Dec 17 2025, next is Jan 29 2026
        t = datetime(2025, 12, 20, 12, 0, tzinfo=ET)
        hours = hours_until_next_fomc(t)
        next_dt = next_fomc_date(t)
        assert next_dt.year == 2026
        assert next_dt.month == 1


class TestNextFOMCDate:
    def test_returns_correct_next(self):
        t = datetime(2025, 5, 10, 12, 0, tzinfo=ET)
        nd = next_fomc_date(t)
        assert nd.month == 6
        assert nd.day == 18

    def test_returns_none_after_all(self):
        t = datetime(2027, 1, 1, 12, 0, tzinfo=ET)
        assert next_fomc_date(t) is None


class TestIsFOMCDay:
    def test_fomc_day_datetime(self):
        dt = datetime(2025, 1, 29, 10, 0, tzinfo=ET)
        assert is_fomc_day(dt) is True

    def test_fomc_day_date_object(self):
        d = date(2025, 1, 29)
        assert is_fomc_day(d) is True

    def test_non_fomc_day(self):
        d = date(2025, 1, 30)
        assert is_fomc_day(d) is False

    def test_all_2025_dates(self):
        for fomc_dt in FOMC_2025_DATES:
            assert is_fomc_day(fomc_dt) is True

    def test_all_2026_dates(self):
        for fomc_dt in FOMC_2026_DATES:
            assert is_fomc_day(fomc_dt) is True

    def test_invalid_input(self):
        assert is_fomc_day("2025-01-29") is False
        assert is_fomc_day(None) is False
        assert is_fomc_day(12345) is False


class TestGetFOMCWindow:
    def test_no_fomc_nearby(self):
        # Far from any FOMC date
        t = datetime(2025, 2, 15, 12, 0, tzinfo=ET)
        assert get_fomc_window(t) == "NONE"

    def test_stand_aside_within_2_hours(self):
        # 1 hour before FOMC announcement
        t = datetime(2025, 1, 29, 13, 0, tzinfo=ET)
        assert get_fomc_window(t) == "STAND_ASIDE"

    def test_stand_aside_at_1_hour(self):
        # Exactly 1 hour before
        fomc = FOMC_2025_DATES[0]  # Jan 29 14:00
        t = fomc - timedelta(hours=1)
        assert get_fomc_window(t) == "STAND_ASIDE"

    def test_drift_strong_4_hours_before(self):
        # 6 hours before — should be DRIFT_STRONG (within 24h)
        fomc = FOMC_2025_DATES[0]
        t = fomc - timedelta(hours=6)
        assert get_fomc_window(t) == "DRIFT_STRONG"

    def test_drift_strong_boundary(self):
        # Exactly 2 hours + 1 second before → DRIFT_STRONG
        fomc = FOMC_2025_DATES[0]
        t = fomc - timedelta(hours=2, seconds=1)
        assert get_fomc_window(t) == "DRIFT_STRONG"

    def test_drift_mild_48_hours_before(self):
        # 48 hours before — should be DRIFT_MILD
        fomc = FOMC_2025_DATES[0]
        t = fomc - timedelta(hours=48)
        assert get_fomc_window(t) == "DRIFT_MILD"

    def test_none_beyond_72_hours(self):
        # 100 hours before
        fomc = FOMC_2025_DATES[0]
        t = fomc - timedelta(hours=100)
        assert get_fomc_window(t) == "NONE"

    def test_after_all_fomc(self):
        t = datetime(2027, 1, 1, 12, 0, tzinfo=ET)
        assert get_fomc_window(t) == "NONE"

    def test_boundary_exactly_2_hours(self):
        # Exactly 2 hours before — should be STAND_ASIDE
        fomc = FOMC_2025_DATES[0]
        t = fomc - timedelta(hours=2)
        assert get_fomc_window(t) == "STAND_ASIDE"

    def test_boundary_exactly_24_hours(self):
        # Exactly 24 hours before — should be DRIFT_STRONG
        fomc = FOMC_2025_DATES[0]
        t = fomc - timedelta(hours=24)
        assert get_fomc_window(t) == "DRIFT_STRONG"

    def test_boundary_exactly_72_hours(self):
        # Exactly 72 hours before — should be DRIFT_MILD
        fomc = FOMC_2025_DATES[0]
        t = fomc - timedelta(hours=72)
        assert get_fomc_window(t) == "DRIFT_MILD"
