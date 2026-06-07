"""
apex.data.resample
==================
Aggregate a sorted, single-symbol list of finer ``Bar`` s into coarser bars.

A data feed (or a backtest fixture) often arrives at one granularity — say
one-minute bars — while a strategy reasons in another — say five-minute or
hourly bars. Resampling is the deterministic, ``Decimal``-correct boundary that
rolls the finer bars up into the coarser ones:

  - **open**   = the open of the first finer bar in the bucket
  - **high**   = the max high across the bucket
  - **low**    = the min low across the bucket
  - **close**  = the close of the last finer bar in the bucket
  - **volume** = the sum of volumes across the bucket
  - **timestamp** = the bucket's *close* time (matching ``Bar``'s convention that
    a timestamp is the bar close time, UTC)
  - **timeframe** = the requested ``target_timeframe``

This module is pure: no I/O, no network, no clock, no float. The same input
always produces the same output, so it is fully unit-testable offline and safe
to use identically in backtest and live paths.

Bucketing is *calendar-aligned* to the UTC epoch and respects the fact that a
``Bar``'s timestamp is its **close** time. A finer bar belongs to the bucket
whose close boundary is the first epoch-aligned multiple of ``step`` that is
``>= timestamp`` — i.e. the bar covering ``(close - step, close]`` rolls up into
the coarse bar with the same closing instant. A bar closing exactly on a
boundary (e.g. 00:05) is the *last* finer bar of that bucket (00:00–00:05), not
the first of the next. Two equivalent runs over the same data therefore always
carve the same buckets, regardless of where the slice happens to start.

Input is validated up front (ascending timestamps, single symbol); bad input
fails loud with ``ValueError`` rather than silently producing garbage. An empty
input yields an empty output.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from apex.core.models import Bar, Symbol

# Map a timeframe *unit* spelling → the duration of one of that unit. Amounts are
# parsed off the front of the string (e.g. "5Min" → 5 × Minute). Spellings are
# matched case-insensitively and kept in sync with the rest of the data layer
# ("1Min" / "1Hour" / "1Day").
_UNIT_DURATIONS: dict[str, timedelta] = {
    "min": timedelta(minutes=1),
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "h": timedelta(hours=1),
    "day": timedelta(days=1),
    "d": timedelta(days=1),
    "week": timedelta(weeks=1),
    "w": timedelta(weeks=1),
}

# The epoch anchor that bucket boundaries are aligned to.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_timeframe(timeframe: str) -> timedelta:
    """
    Parse an Apex timeframe string (e.g. ``"1Min"``, ``"5Min"``, ``"1Hour"``,
    ``"1Day"``) into the ``timedelta`` it represents.

    The leading integer is the amount and the trailing letters are the unit;
    either may be omitted-of-amount (``"Day"`` means ``1Day``). Raises
    ``ValueError`` on an unparseable spelling or a non-positive amount.
    """
    text = timeframe.strip()
    if not text:
        raise ValueError("timeframe is missing/empty")

    # Split the leading digits (amount) from the trailing unit letters.
    i = 0
    while i < len(text) and text[i].isdigit():
        i += 1
    amount_text, unit_text = text[:i], text[i:].strip().lower()

    amount = int(amount_text) if amount_text else 1
    if amount <= 0:
        raise ValueError(f"timeframe amount must be positive: {timeframe!r}")

    if unit_text not in _UNIT_DURATIONS:
        raise ValueError(
            f"unrecognized timeframe unit {unit_text!r} in {timeframe!r} "
            f"(known: {sorted(_UNIT_DURATIONS)})"
        )
    return amount * _UNIT_DURATIONS[unit_text]


def _bucket_close(ts: datetime, step: timedelta) -> datetime:
    """
    Snap a bar's *close* timestamp ``ts`` up to its bucket's close boundary: the
    first epoch-aligned multiple of ``step`` that is ``>= ts``. A timestamp that
    already sits on a boundary is its own bucket close (it is that bucket's last
    finer bar), so this is a ceiling, not a strict round-up.
    """
    elapsed = ts - _EPOCH
    # Ceiling division on the timedeltas: exact and deterministic.
    floored = (elapsed // step) * step
    if floored < elapsed:
        floored += step
    return _EPOCH + floored


def resample_bars(
    bars: list[Bar],
    target_timeframe: str,
    *,
    keep_partial: bool = False,
) -> list[Bar]:
    """
    Aggregate a sorted, single-symbol list of finer ``bars`` into coarser bars at
    ``target_timeframe``.

    Aggregation per bucket: open = first open, high = max high, low = min low,
    close = last close, volume = sum, timestamp = bucket close time, timeframe =
    ``target_timeframe``. All money math stays in ``Decimal``.

    A *partial trailing bucket* is one whose time window extends past the last
    finer bar — i.e. the most recent bucket may still be filling. By default
    (``keep_partial=False``) the trailing bucket is dropped unless it ends
    exactly on the last bar's boundary, so only fully-formed bars are emitted.
    Set ``keep_partial=True`` to emit the in-progress bucket too.

    Args:
        bars: finer bars, strictly ascending by timestamp, all the same symbol.
        target_timeframe: the coarser timeframe to roll up to (e.g. ``"5Min"``).
        keep_partial: keep the final, possibly-incomplete bucket.

    Returns:
        Coarser bars in ascending order. Empty input → empty output.

    Raises:
        ValueError: if ``bars`` are not strictly ascending, mix symbols, or
            ``target_timeframe`` is unparseable.
    """
    if not bars:
        return []

    step = parse_timeframe(target_timeframe)

    symbol: Symbol = bars[0].symbol
    prev_ts: datetime | None = None
    for bar in bars:
        if bar.symbol != symbol:
            raise ValueError(
                f"resample_bars requires a single symbol; got {symbol} and {bar.symbol}"
            )
        if prev_ts is not None and bar.timestamp <= prev_ts:
            raise ValueError(
                f"bars must be strictly ascending by timestamp; "
                f"{bar.timestamp} follows {prev_ts}"
            )
        prev_ts = bar.timestamp

    # Group consecutive bars by their bucket close boundary, preserving order.
    buckets: list[tuple[datetime, list[Bar]]] = []
    for bar in bars:
        close_time = _bucket_close(bar.timestamp, step)
        if buckets and buckets[-1][0] == close_time:
            buckets[-1][1].append(bar)
        else:
            buckets.append((close_time, [bar]))

    last_ts = bars[-1].timestamp

    out: list[Bar] = []
    for close_time, group in buckets:
        # The trailing bucket is "complete" only once a finer bar closes on its
        # boundary. Equivalently: this bucket is partial when it is the last one
        # AND no observed bar reaches its close time.
        is_last = close_time == buckets[-1][0]
        is_partial = is_last and last_ts < close_time
        if is_partial and not keep_partial:
            continue

        high = max(b.high for b in group)
        low = min(b.low for b in group)
        volume = sum((b.volume for b in group), Decimal("0"))

        out.append(
            Bar(
                symbol=symbol,
                timestamp=close_time,
                open=group[0].open,
                high=high,
                low=low,
                close=group[-1].close,
                volume=volume,
                timeframe=target_timeframe,
            )
        )

    return out
