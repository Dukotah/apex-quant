"""Tests for apex.data.trading_calendar — hand-computed known values + edges."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from apex.data.trading_calendar import (
    holidays_for_year,
    is_trading_day,
    next_trading_day,
    prev_trading_day,
    trading_days_between,
)

# --------------------------------------------------------------------- weekends


def test_saturday_and_sunday_are_not_trading_days():
    # 2026-06-06 is a Saturday, 2026-06-07 a Sunday.
    assert is_trading_day(date(2026, 6, 6)) is False
    assert is_trading_day(date(2026, 6, 7)) is False


def test_ordinary_weekday_is_a_trading_day():
    # 2026-06-08 is a Monday, no holiday.
    assert is_trading_day(date(2026, 6, 8)) is True


# --------------------------------------------------------------------- holidays


def test_fixed_and_floating_holidays_2026():
    # Hand-verified 2026 NYSE full-day closures.
    expected = {
        date(2026, 1, 1),  # New Year's Day (Thu)
        date(2026, 1, 19),  # MLK Day (3rd Mon Jan)
        date(2026, 2, 16),  # Presidents' Day (3rd Mon Feb)
        date(2026, 4, 3),  # Good Friday (Easter is 2026-04-05)
        date(2026, 5, 25),  # Memorial Day (last Mon May)
        date(2026, 6, 19),  # Juneteenth (Fri)
        date(2026, 7, 3),  # Independence Day observed (Jul 4 is Sat → Fri)
        date(2026, 9, 7),  # Labor Day (1st Mon Sep)
        date(2026, 11, 26),  # Thanksgiving (4th Thu Nov)
        date(2026, 12, 25),  # Christmas (Fri)
    }
    assert holidays_for_year(2026) == expected
    for h in expected:
        assert is_trading_day(h) is False


def test_good_friday_2025_and_2024():
    # Easter 2025 = Apr 20 → Good Friday Apr 18; Easter 2024 = Mar 31 → Mar 29.
    assert date(2025, 4, 18) in holidays_for_year(2025)
    assert date(2024, 3, 29) in holidays_for_year(2024)


def test_independence_day_sunday_observed_monday():
    # 2021-07-04 is a Sunday → observed Monday 2021-07-05.
    hs = holidays_for_year(2021)
    assert date(2021, 7, 5) in hs
    assert is_trading_day(date(2021, 7, 5)) is False
    # The actual Sunday is a weekend closure, not in the observed-holiday set.
    assert date(2021, 7, 4) not in hs


def test_christmas_saturday_observed_friday():
    # 2021-12-25 is a Saturday → observed Friday 2021-12-24.
    assert date(2021, 12, 24) in holidays_for_year(2021)
    assert is_trading_day(date(2021, 12, 24)) is False


def test_new_year_falling_on_saturday_observed_prev_dec_31():
    # 2022-01-01 is a Saturday → observed Friday 2021-12-31.
    assert is_trading_day(date(2021, 12, 31)) is False
    # And Jan 1 2022 (the Saturday) is just a weekend.
    assert is_trading_day(date(2022, 1, 1)) is False


def test_new_year_falling_on_sunday_observed_monday():
    # 2023-01-01 is a Sunday → observed Monday 2023-01-02.
    assert date(2023, 1, 2) in holidays_for_year(2023)
    assert is_trading_day(date(2023, 1, 2)) is False


# --------------------------------------------------------- next / prev trading day


def test_next_trading_day_skips_weekend():
    # Friday 2026-06-12 → Monday 2026-06-15.
    assert next_trading_day(date(2026, 6, 12)) == date(2026, 6, 15)


def test_next_trading_day_is_strict():
    # From a Monday returns the *following* trading day, not the same Monday.
    assert next_trading_day(date(2026, 6, 8)) == date(2026, 6, 9)


def test_next_trading_day_skips_holiday_and_weekend():
    # Thu 2026-12-24 → Fri is Christmas (12-25) → Sat/Sun → Mon 2026-12-28.
    assert next_trading_day(date(2026, 12, 24)) == date(2026, 12, 28)


def test_prev_trading_day_skips_weekend():
    # Monday 2026-06-15 → Friday 2026-06-12.
    assert prev_trading_day(date(2026, 6, 15)) == date(2026, 6, 12)


def test_prev_trading_day_skips_holiday():
    # Mon 2026-12-28 → back over weekend + Christmas → Thu 2026-12-24.
    assert prev_trading_day(date(2026, 12, 28)) == date(2026, 12, 24)


def test_long_weekend_around_memorial_day_2026():
    # Memorial Day 2026 = Mon 2026-05-25. Fri 05-22 → next is Tue 05-26.
    assert next_trading_day(date(2026, 5, 22)) == date(2026, 5, 26)
    assert prev_trading_day(date(2026, 5, 26)) == date(2026, 5, 22)


# --------------------------------------------------------------------- extras


def test_extra_holidays_make_a_weekday_closed():
    d = date(2026, 6, 8)  # an ordinary Monday
    assert is_trading_day(d) is True
    assert is_trading_day(d, extra_holidays=[d]) is False
    # next_trading_day respects the extra closure too.
    assert next_trading_day(date(2026, 6, 5), extra_holidays=[date(2026, 6, 8)]) == date(2026, 6, 9)


# ---------------------------------------------------------- datetime acceptance


def test_accepts_datetime_and_ignores_time_and_tz():
    dt = datetime(2026, 6, 6, 23, 59, tzinfo=timezone.utc)  # a Saturday
    assert is_trading_day(dt) is False
    dt2 = datetime(2026, 6, 8, 9, 30, tzinfo=timezone.utc)  # a Monday
    assert is_trading_day(dt2) is True
    assert next_trading_day(dt2) == date(2026, 6, 9)
    assert isinstance(next_trading_day(dt2), date)


def test_rejects_bad_type():
    with pytest.raises(TypeError):
        is_trading_day("2026-06-08")  # type: ignore[arg-type]


# ----------------------------------------------------------- trading_days_between


def test_trading_days_between_one_week():
    # Mon 2026-06-08 .. Sun 2026-06-14 → Mon-Fri only.
    got = trading_days_between(date(2026, 6, 8), date(2026, 6, 14))
    assert got == [
        date(2026, 6, 8),
        date(2026, 6, 9),
        date(2026, 6, 10),
        date(2026, 6, 11),
        date(2026, 6, 12),
    ]


def test_trading_days_between_skips_holiday():
    # Week containing Thanksgiving 2026 (Thu 11-26).
    got = trading_days_between(date(2026, 11, 23), date(2026, 11, 27))
    assert got == [
        date(2026, 11, 23),
        date(2026, 11, 24),
        date(2026, 11, 25),
        date(2026, 11, 27),
    ]


def test_trading_days_between_inclusive_endpoints():
    got = trading_days_between(date(2026, 6, 8), date(2026, 6, 8))
    assert got == [date(2026, 6, 8)]


def test_trading_days_between_reversed_range_is_empty():
    assert trading_days_between(date(2026, 6, 10), date(2026, 6, 8)) == []


def test_determinism_same_input_same_output():
    a = trading_days_between(date(2026, 1, 1), date(2026, 3, 31))
    b = trading_days_between(date(2026, 1, 1), date(2026, 3, 31))
    assert a == b
