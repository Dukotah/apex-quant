"""
apex.data.anchored_vwap
=======================
Volume-Weighted Average Price (VWAP) over a series of ``Bar`` models.

VWAP is the cumulative ``sum(price * volume) / sum(volume)`` — the average price
a participant would have paid if they spread an order across the period in
proportion to traded volume. It is the institutional benchmark for execution
quality and a widely-watched dynamic support/resistance level. Two flavours
matter to a strategy:

  - **Anchored VWAP** — accumulation begins at a chosen *anchor* bar (a swing
    low, an earnings gap, a session open) and runs forward, never resetting. It
    answers "what is the average cost of everyone who traded since that event?"
  - **Rolling VWAP** — a fixed-width window (e.g. last 20 bars) that slides
    forward, a volume-weighted cousin of the moving average.

This module lives in the data layer alongside ``normalizer`` and
``historical_feed``, so it follows that layer's convention: prices and volumes
are ``Decimal`` (they come straight off frozen ``Bar`` models) and every result
is ``Decimal``, so a VWAP value can flow into P&L / risk math without ever
touching binary float. The "typical price" of a bar is ``(high+low+close)/3``,
the standard VWAP input; a ``price_fn`` hook lets a caller pick ``close`` or any
other rule instead.

Pure and deterministic: no I/O, no clock, no randomness. Insufficient data is
handled gracefully — a window with zero total volume (or an empty/short series)
yields ``None`` rather than dividing by zero or inventing a number.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Callable, List, Optional, Sequence

from apex.core.models import Bar

# A price-extraction hook: Bar -> Decimal. Defaults to the typical price.
PriceFn = Callable[[Bar], Decimal]

_THREE = Decimal("3")


# --------------------------------------------------------------------- prices

def typical_price(bar: Bar) -> Decimal:
    """The standard VWAP input price: ``(high + low + close) / 3``."""
    return (bar.high + bar.low + bar.close) / _THREE


def close_price(bar: Bar) -> Decimal:
    """Alternative price hook: use the bar's close."""
    return bar.close


# ------------------------------------------------------------------- helpers

def _vwap(bars: Sequence[Bar], price_fn: PriceFn) -> Optional[Decimal]:
    """
    Volume-weighted average price over ``bars``, or ``None`` if there is no
    volume to weight by (empty series or every bar has zero volume).

    Bars with zero volume contribute nothing — they neither move the average
    nor count toward the denominator — which matches how VWAP treats a bar in
    which nothing traded.
    """
    pv_total = Decimal("0")
    vol_total = Decimal("0")
    for bar in bars:
        vol = bar.volume
        if vol == 0:
            continue
        pv_total += price_fn(bar) * vol
        vol_total += vol
    if vol_total == 0:
        return None  # nothing traded → VWAP is undefined; fail closed.
    return pv_total / vol_total


# ------------------------------------------------------------------- public

def anchored_vwap(
    bars: Sequence[Bar],
    anchor_index: int = 0,
    *,
    price_fn: PriceFn = typical_price,
) -> Optional[Decimal]:
    """
    VWAP accumulated from ``anchor_index`` through the end of ``bars``.

    ``anchor_index`` is the position of the anchor bar (inclusive); negative
    indices count from the end, like normal Python slicing. Returns ``None`` if
    the anchor is out of range, the slice is empty, or the slice has zero total
    volume.

    The caller is responsible for passing bars in chronological order (oldest
    first), exactly as ``HistoricalDataFeed`` yields them.
    """
    n = len(bars)
    if n == 0:
        return None
    if anchor_index < 0:
        anchor_index += n
    if anchor_index < 0 or anchor_index >= n:
        return None
    return _vwap(bars[anchor_index:], price_fn)


def rolling_vwap(
    bars: Sequence[Bar],
    window: int,
    *,
    price_fn: PriceFn = typical_price,
) -> Optional[Decimal]:
    """
    VWAP over the most recent ``window`` bars.

    Returns ``None`` if ``window`` is not positive, there are fewer than
    ``window`` bars available (insufficient data — never compute on a partial
    window), or the window has zero total volume.
    """
    if window <= 0:
        return None
    if len(bars) < window:
        return None
    return _vwap(bars[-window:], price_fn)


def rolling_vwap_series(
    bars: Sequence[Bar],
    window: int,
    *,
    price_fn: PriceFn = typical_price,
) -> List[Optional[Decimal]]:
    """
    Rolling VWAP aligned to ``bars``: element ``i`` is the VWAP of the window
    ending at bar ``i`` (i.e. ``bars[i-window+1 .. i]``).

    The first ``window - 1`` entries are ``None`` (not enough history yet), so
    the returned list is the same length as ``bars`` and index-aligned to it —
    convenient for plotting or joining against an indicator column. An entry is
    also ``None`` where its window has zero total volume. A non-positive
    ``window`` yields an all-``None`` list of matching length.
    """
    n = len(bars)
    if window <= 0:
        return [None] * n
    out: List[Optional[Decimal]] = [None] * n
    for i in range(window - 1, n):
        out[i] = _vwap(bars[i - window + 1 : i + 1], price_fn)
    return out


def anchored_vwap_series(
    bars: Sequence[Bar],
    anchor_index: int = 0,
    *,
    price_fn: PriceFn = typical_price,
) -> List[Optional[Decimal]]:
    """
    Running anchored VWAP aligned to ``bars``: element ``i`` is the VWAP
    accumulated from ``anchor_index`` through bar ``i`` (inclusive).

    Entries before the anchor are ``None``. Entries from the anchor onward are
    the cumulative anchored VWAP up to that bar, or ``None`` while no volume has
    yet accumulated. The returned list is the same length as ``bars``. An
    out-of-range anchor yields an all-``None`` list.

    This is the cheap, single-pass form: it carries the running price*volume and
    volume totals forward instead of re-summing the slice for every bar.
    """
    n = len(bars)
    if n == 0:
        return []
    start = anchor_index + n if anchor_index < 0 else anchor_index
    if start < 0 or start >= n:
        return [None] * n

    out: List[Optional[Decimal]] = [None] * n
    pv_total = Decimal("0")
    vol_total = Decimal("0")
    for i in range(start, n):
        vol = bars[i].volume
        if vol != 0:
            pv_total += price_fn(bars[i]) * vol
            vol_total += vol
        out[i] = (pv_total / vol_total) if vol_total != 0 else None
    return out
