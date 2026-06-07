"""
apex.data.returns_aggregator
============================
Compound a daily return series into weekly, monthly, and yearly returns.

A backtest or a live equity curve produces one return per day (a fraction:
``0.01`` = +1%). Reporting and the Gauntlet, however, often want those daily
returns rolled up into calendar periods — a weekly return, a monthly return,
a yearly return — so you can see how a strategy behaved across regimes without
squinting at hundreds of daily numbers.

Rolling up returns is *not* averaging them: returns **compound**. A +1% day
followed by a -1% day is not 0%, it is ``(1.01 * 0.99) - 1 = -0.0001``. This
module does that compounding correctly, grouping each daily return into the
calendar bucket its date falls in, in chronological order.

Design notes:
  - **Statistical layer convention.** Returns here are ``float`` fractions, to
    match ``apex.validation.metrics`` and the rest of the analytics layer. This
    is reporting/statistics, not P&L money math (which stays ``Decimal``).
  - **Calendar buckets.** Weeks are ISO weeks (Monday-start), keyed by
    ``(ISO-year, ISO-week)``; months by ``(year, month)``; years by ``year``.
    The date supplied with each daily return decides its bucket — the function
    never invents or assumes a calendar.
  - **Pure & deterministic.** No I/O, no clock, no randomness. Buckets are
    emitted oldest→newest. Same input → same output, every time.
  - **Fails closed on bad data.** An empty input yields an empty result, never
    a garbage period. A return of exactly ``-1.0`` (a -100% day) compounds to a
    factor of ``0`` legitimately; anything below ``-1`` is rejected as it would
    imply negative equity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, List, Sequence, Tuple

# A single dated daily return: (date-or-datetime, return-fraction).
DatedReturn = Tuple[object, float]


@dataclass(frozen=True)
class PeriodReturn:
    """
    The compounded return of one calendar period.

    ``key`` is the bucket identity used for grouping (e.g. ``(2024, 6)`` for a
    month, ``(2024, 23)`` for an ISO week, ``2024`` for a year). ``label`` is a
    stable human-readable rendering of that key. ``start`` / ``end`` are the
    first and last *observed* dates that fell in the bucket (not the calendar
    bounds — only days that actually had a return). ``ret`` is the compounded
    return as a fraction, and ``count`` is how many daily returns it spans.
    """

    key: object
    label: str
    start: date
    end: date
    ret: float
    count: int


def _as_date(value: object) -> date:
    """Coerce a ``date``/``datetime`` (or ISO string) into a plain ``date``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("date is missing/empty")
        if text[-1] in ("Z", "z"):
            text = text[:-1] + "+00:00"  # 3.11-safe Zulu handling
        try:
            return datetime.fromisoformat(text).date()
        except ValueError as exc:
            raise ValueError(f"unparseable date {value!r}: {exc}") from exc
    raise ValueError(f"unsupported date type {type(value).__name__}: {value!r}")


def _as_return(value: object) -> float:
    """Validate and coerce a single daily return fraction to ``float``."""
    if value is None or value == "":
        raise ValueError("return is missing/empty")
    try:
        r = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"return is not a number: {value!r}") from exc
    if r != r:  # NaN
        raise ValueError("return is NaN")
    if r < -1.0:
        # A return below -100% would imply negative equity — impossible.
        raise ValueError(f"return below -1.0 (would imply negative equity): {r}")
    return r


def compound(returns: Iterable[float]) -> float:
    """
    Compound a flat iterable of return fractions into a single return.

    ``(1 + r0)(1 + r1)... - 1``. An empty iterable compounds to ``0.0`` (no
    movement). Returns are validated (NaN / < -1 rejected).
    """
    factor = 1.0
    any_seen = False
    for value in returns:
        any_seen = True
        factor *= 1.0 + _as_return(value)
    return factor - 1.0 if any_seen else 0.0


# --------------------------------------------------------------- bucket keying


def _week_key(d: date) -> Tuple[int, int]:
    """ISO (year, week) — weeks start Monday, per ISO-8601."""
    iso = d.isocalendar()
    return (iso[0], iso[1])


def _week_label(key: Tuple[int, int]) -> str:
    return f"{key[0]}-W{key[1]:02d}"


def _month_key(d: date) -> Tuple[int, int]:
    return (d.year, d.month)


def _month_label(key: Tuple[int, int]) -> str:
    return f"{key[0]}-{key[1]:02d}"


def _year_key(d: date) -> int:
    return d.year


def _year_label(key: int) -> str:
    return f"{key}"


def _aggregate(
    daily: Sequence[DatedReturn],
    key_fn,
    label_fn,
) -> List[PeriodReturn]:
    """
    Group dated daily returns into calendar buckets and compound each bucket.

    Buckets are emitted in chronological order of their first observed date.
    Order *within* a bucket follows the input order, so a caller that pre-sorts
    chronologically gets chronological compounding (compounding is commutative,
    so the resulting ``ret`` is order-independent, but ``start``/``end`` track
    observed extremes regardless).
    """
    if not daily:
        return []

    # Preserve first-seen order of buckets for deterministic chronological output.
    order: List[object] = []
    factors: dict[object, float] = {}
    counts: dict[object, int] = {}
    starts: dict[object, date] = {}
    ends: dict[object, date] = {}

    for raw_dt, raw_ret in daily:
        d = _as_date(raw_dt)
        r = _as_return(raw_ret)
        key = key_fn(d)
        if key not in factors:
            order.append(key)
            factors[key] = 1.0
            counts[key] = 0
            starts[key] = d
            ends[key] = d
        factors[key] *= 1.0 + r
        counts[key] += 1
        if d < starts[key]:
            starts[key] = d
        if d > ends[key]:
            ends[key] = d

    out = [
        PeriodReturn(
            key=key,
            label=label_fn(key),
            start=starts[key],
            end=ends[key],
            ret=factors[key] - 1.0,
            count=counts[key],
        )
        for key in order
    ]
    # Sort chronologically by first observed date, then by key for stable ties.
    out.sort(key=lambda p: (p.start, p.label))
    return out


# ------------------------------------------------------------------- public API


def to_weekly(daily: Sequence[DatedReturn]) -> List[PeriodReturn]:
    """Compound dated daily returns into ISO-week (Monday-start) buckets."""
    return _aggregate(daily, _week_key, _week_label)


def to_monthly(daily: Sequence[DatedReturn]) -> List[PeriodReturn]:
    """Compound dated daily returns into calendar-month buckets."""
    return _aggregate(daily, _month_key, _month_label)


def to_yearly(daily: Sequence[DatedReturn]) -> List[PeriodReturn]:
    """Compound dated daily returns into calendar-year buckets."""
    return _aggregate(daily, _year_key, _year_label)
