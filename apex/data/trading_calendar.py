"""
apex.data.trading_calendar
==========================
US equity trading-day helper — pure, deterministic calendar arithmetic.

Daily-bar backtests and the scheduled live runner both need to answer three
questions without ever calling a network or wall clock:

  - *Is this calendar date a US-equity trading day?*  (``is_trading_day``)
  - *What is the next trading day on/after a date?*    (``next_trading_day``)
  - *What is the previous trading day on/before it?*   (``prev_trading_day``)

A market is closed on weekends and on the NYSE/Nasdaq holiday schedule. This
module encodes that schedule from **fixed rules** (computed per year), not a
hand-maintained lookup table, so it works for any year without edits and stays
deterministic: the same date always yields the same answer.

Scope and deliberate omissions:
  - This models *full-day* closures only. Early-close half days (e.g. the day
    after Thanksgiving, Christmas Eve) are still *trading days* — the market is
    open — so they are intentionally NOT treated as holidays here.
  - It does not know about one-off historical closures (9/11, hurricane Sandy,
    presidential funerals). Those are rare, ad-hoc, and out of scope for a
    rules-based calendar; pass extra dates via ``extra_holidays`` if a specific
    backtest needs them.

Holiday observance rule (federal/NYSE convention): a holiday that falls on a
Saturday is observed the preceding Friday; one that falls on a Sunday is
observed the following Monday. Good Friday (the one floating, non-federal market
holiday) is computed from Easter via the anonymous Gregorian algorithm.

This is a date layer, not a money layer — it deals in ``datetime.date`` and
plain ints, so (unlike the OHLCV models) there is no ``Decimal`` here. It is
pure: no I/O, no clock, no randomness.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable, Optional

# Weekday() values for Saturday/Sunday (Monday == 0).
_SATURDAY = 5
_SUNDAY = 6


# ----------------------------------------------------------------- observance


def _observed(holiday: date) -> date:
    """
    Shift a fixed-date holiday to the day the market actually observes it.

    Saturday → observed the preceding Friday; Sunday → the following Monday.
    A weekday holiday is observed on its own date.
    """
    if holiday.weekday() == _SATURDAY:
        return holiday - timedelta(days=1)
    if holiday.weekday() == _SUNDAY:
        return holiday + timedelta(days=1)
    return holiday


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (Mon==0) of ``month`` in ``year`` (n is 1-based)."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last ``weekday`` (Mon==0) of ``month`` in ``year``."""
    # Step to the first day of the next month, then walk back to the weekday.
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """
    Gregorian Easter Sunday (anonymous/Meeus algorithm). Deterministic, integer-only.
    """
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
    lo = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lo) // 451
    month = (h + lo - 7 * m + 114) // 31
    day = ((h + lo - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _good_friday(year: int) -> date:
    """Good Friday — two days before Easter Sunday. The market's one floating holiday."""
    return _easter(year) - timedelta(days=2)


# ---------------------------------------------------------------- holiday set


def holidays_for_year(year: int) -> frozenset[date]:
    """
    The set of *observed* full-day US-equity market holidays in ``year``.

    Rules encoded (NYSE/Nasdaq):
      - New Year's Day (Jan 1, observed)
      - Martin Luther King Jr. Day (3rd Monday of January)
      - Washington's Birthday / Presidents' Day (3rd Monday of February)
      - Good Friday (Friday before Easter)
      - Memorial Day (last Monday of May)
      - Juneteenth National Independence Day (Jun 19, observed) — a market
        holiday since 2022; computed unconditionally as a fixed rule, so years
        before then are not its concern (this is a forward-looking engine).
      - Independence Day (Jul 4, observed)
      - Labor Day (1st Monday of September)
      - Thanksgiving Day (4th Thursday of November)
      - Christmas Day (Dec 25, observed)

    New Year's Day observance can land on Dec 31 of the *previous* year (when
    Jan 1 is a Saturday); that edge is handled by also folding in the previous
    year's New Year observance when it spills forward — see ``is_trading_day``.
    """
    days = {
        _observed(date(year, 1, 1)),  # New Year's Day
        _nth_weekday(year, 1, 0, 3),  # MLK Day (3rd Mon Jan)
        _nth_weekday(year, 2, 0, 3),  # Presidents' Day (3rd Mon Feb)
        _good_friday(year),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day (last Mon May)
        _observed(date(year, 6, 19)),  # Juneteenth
        _observed(date(year, 7, 4)),  # Independence Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day (1st Mon Sep)
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving (4th Thu Nov)
        _observed(date(year, 12, 25)),  # Christmas
    }
    return frozenset(days)


def _as_date(value: date | datetime) -> date:
    """Coerce a ``date``/``datetime`` to a ``date`` (datetime is a date subclass)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError(f"expected date or datetime, got {type(value).__name__}")


def _normalize_extra(extra_holidays: Optional[Iterable[date | datetime]]) -> frozenset[date]:
    """Coerce a caller-supplied set of ad-hoc closures into a frozenset of dates."""
    if not extra_holidays:
        return frozenset()
    return frozenset(_as_date(d) for d in extra_holidays)


# ------------------------------------------------------------------- public API


def is_trading_day(
    day: date | datetime,
    *,
    extra_holidays: Optional[Iterable[date | datetime]] = None,
) -> bool:
    """
    True iff ``day`` is a US-equity trading day: a weekday that is not a market
    holiday (nor a caller-supplied ad-hoc closure in ``extra_holidays``).

    Accepts a ``date`` or ``datetime`` (the time-of-day is ignored). Pure and
    deterministic — never consults a clock.
    """
    d = _as_date(day)
    if d.weekday() >= _SATURDAY:  # Saturday(5) or Sunday(6)
        return False
    if d in _normalize_extra(extra_holidays):
        return False
    # Holidays for this year, plus the prior year's New-Year observance that can
    # spill onto Dec 31 (when Jan 1 of *this* year falls on a Saturday).
    if d in holidays_for_year(d.year):
        return False
    if _observed(date(d.year + 1, 1, 1)) == d:  # Dec 31 New Year observance
        return False
    return True


def next_trading_day(
    day: date | datetime,
    *,
    extra_holidays: Optional[Iterable[date | datetime]] = None,
) -> date:
    """
    The first trading day **strictly after** ``day``.

    Always returns a future date even if ``day`` itself is a trading day — use
    this to advance a cursor. Returns a ``date`` regardless of input type.
    """
    d = _as_date(day) + timedelta(days=1)
    # At most a long weekend + stacked holidays; this terminates quickly.
    while not is_trading_day(d, extra_holidays=extra_holidays):
        d += timedelta(days=1)
    return d


def prev_trading_day(
    day: date | datetime,
    *,
    extra_holidays: Optional[Iterable[date | datetime]] = None,
) -> date:
    """
    The first trading day **strictly before** ``day``.

    Always returns a past date even if ``day`` itself is a trading day. Returns
    a ``date`` regardless of input type.
    """
    d = _as_date(day) - timedelta(days=1)
    while not is_trading_day(d, extra_holidays=extra_holidays):
        d -= timedelta(days=1)
    return d


def trading_days_between(
    start: date | datetime,
    end: date | datetime,
    *,
    extra_holidays: Optional[Iterable[date | datetime]] = None,
) -> list[date]:
    """
    All trading days in the **inclusive** ``[start, end]`` range, ascending.

    Returns an empty list if ``start`` is after ``end``. Pure and deterministic;
    handy for building a backtest's expected bar dates.
    """
    lo = _as_date(start)
    hi = _as_date(end)
    if lo > hi:
        return []
    out: list[date] = []
    d = lo
    while d <= hi:
        if is_trading_day(d, extra_holidays=extra_holidays):
            out.append(d)
        d += timedelta(days=1)
    return out
