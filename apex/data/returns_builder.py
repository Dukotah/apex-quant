"""
apex.data.returns_builder
=========================
Turn a price history — a list of ``Bar`` objects (or raw closes) — into the
**simple** and **log** return series the validation layer consumes.

A "return" is a statistical/metric quantity, not money: it is the input to
``apex.validation.metrics`` (Sharpe, Sortino, drawdown, …), all of which operate
on ``Sequence[float]``. So this module deliberately follows the *metrics* layer's
``float`` convention rather than the ``Decimal`` convention of the price models —
it is the single, tested boundary where ``Decimal`` closes are converted into the
``float`` returns the rest of the analytics stack expects.

Two return flavours, both standard:
  - **Simple return**:  rₜ = Pₜ / Pₜ₋₁ − 1
  - **Log return**:     rₜ = ln(Pₜ / Pₜ₋₁)

Both series have length ``N − 1`` for ``N`` prices (the first price has no prior
to compare against). With fewer than two prices there is no return to compute, so
an empty list is returned — never garbage.

Conversion from ``Decimal`` close to ``float`` goes through ``float(Decimal)``
(exact, not via ``str``→``float``) — the loss of precision is intentional and
correct here: returns are float statistics, and the price models remain the
authoritative ``Decimal`` source.

This module is pure: no I/O, no clock, no randomness. It is fully unit-testable
offline against hand-computed values.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import List, Sequence, Union

from apex.core.models import Bar

# A price may arrive as a Decimal (from a Bar), an int/float (raw close), or a
# numeric string. All are coerced to float for the float-valued return series.
PriceLike = Union[Bar, Decimal, int, float, str]


def _to_price(value: PriceLike) -> float:
    """
    Coerce one element into a positive ``float`` close.

    Accepts a ``Bar`` (uses its ``close``), a ``Decimal``/``int``/``float``, or a
    numeric string. Raises ``ValueError`` on a non-positive or unparseable price,
    because a return computed against a zero/negative base is meaningless.
    """
    if isinstance(value, Bar):
        price = float(value.close)
    elif isinstance(value, bool):
        # bool is an int subclass; a boolean is never a valid price.
        raise ValueError(f"invalid price (bool): {value!r}")
    elif isinstance(value, (Decimal, int, float)):
        price = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("price is missing/empty")
        try:
            price = float(text)
        except ValueError as exc:
            raise ValueError(f"price is not a number: {value!r}") from exc
    else:
        raise ValueError(f"unsupported price type: {type(value).__name__}")

    if not math.isfinite(price):
        raise ValueError(f"price is not finite: {value!r}")
    if price <= 0.0:
        raise ValueError(f"price must be positive (got {price!r})")
    return price


def to_closes(prices: Sequence[PriceLike]) -> List[float]:
    """
    Normalize a sequence of ``Bar``/numeric prices into a list of ``float`` closes.

    This is the front door for both return builders: it accepts the same loose
    input shapes they do, so callers can validate/inspect the close series
    independently. Raises ``ValueError`` naming the first bad element.
    """
    out: List[float] = []
    for idx, value in enumerate(prices):
        try:
            out.append(_to_price(value))
        except ValueError as exc:
            raise ValueError(f"price at index {idx}: {exc}") from exc
    return out


def simple_returns(prices: Sequence[PriceLike]) -> List[float]:
    """
    Period-over-period **simple** returns: rₜ = Pₜ / Pₜ₋₁ − 1.

    Length is ``len(prices) - 1`` (one fewer than the price series). Fewer than
    two prices yields ``[]`` — there is nothing to compare. Accepts ``Bar``s,
    ``Decimal``/numeric closes, or numeric strings; raises ``ValueError`` on a
    non-positive/unparseable price.
    """
    closes = to_closes(prices)
    if len(closes) < 2:
        return []
    return [curr / prev - 1.0 for prev, curr in zip(closes, closes[1:])]


def log_returns(prices: Sequence[PriceLike]) -> List[float]:
    """
    Period-over-period **log** returns: rₜ = ln(Pₜ / Pₜ₋₁).

    Length is ``len(prices) - 1``. Fewer than two prices yields ``[]``. Log
    returns are additive across time (their sum equals the log of the total
    growth factor), which is why annualization and many statistical tests prefer
    them. Accepts the same loose input shapes as :func:`simple_returns`.
    """
    closes = to_closes(prices)
    if len(closes) < 2:
        return []
    return [math.log(curr / prev) for prev, curr in zip(closes, closes[1:])]


def returns_from_bars(bars: Sequence[Bar], *, log: bool = False) -> List[float]:
    """
    Convenience wrapper: build a return series directly from ``Bar`` objects.

    Bars are used **in the order given** — no sorting — so the caller controls
    chronology (e.g. ``HistoricalDataFeed`` already yields oldest→newest). Set
    ``log=True`` for log returns; the default is simple returns.
    """
    return log_returns(bars) if log else simple_returns(bars)
