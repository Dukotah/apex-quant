"""
apex.data.winsorize
===================
Winsorize (clip) a numeric series at given lower/upper percentiles.

Winsorizing replaces extreme values with the nearest "acceptable" value at a
chosen percentile, rather than discarding them. It is the standard defensive
pre-processing step before feeding a series into a statistic that is sensitive
to outliers (mean, standard deviation, covariance, regression slope): a single
fat-fingered print, a split-adjustment glitch, or a flash-crash tick can
otherwise dominate the result. Unlike trimming, winsorizing keeps the series
length unchanged — every observation stays, the extremes are just capped.

This module is **type-preserving and money-safe**. The clip thresholds are
chosen *from the values already present in the series* using the nearest-rank
percentile method, so no float arithmetic is ever performed on the data itself.
Feed it ``Decimal`` prices and you get ``Decimal`` back, bit-for-bit equal to
values that were already in the input — never a binary-float artifact. Feed it
``float`` returns (as the indicator/metric layer does) and you get ``float``
back. The only constraint is that the elements are mutually comparable.

It is pure (no I/O, no clock, no randomness) and deterministic, so it is fully
unit-testable offline. Insufficient/degenerate input is handled gracefully:
an empty series returns an empty list; ``None`` elements are rejected loudly
(``ValueError``) rather than silently producing garbage bounds.

Nearest-rank convention (matches numpy's ``interpolation="lower"`` /
``"higher"`` at the endpoints we use): for a sorted series of ``n`` values and
a percentile ``p`` in ``[0, 1]``, the lower bound is the value at sorted index
``floor(p * (n - 1))`` and the upper bound is the value at sorted index
``ceil(p * (n - 1))``. Because the bound is always an actual element, the output
contains only values that already existed in the input.
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple, TypeVar

# The series element type. Anything mutually comparable works (Decimal, float,
# int); the same type flows out that flowed in because bounds are chosen from
# the series itself rather than computed arithmetically.
T = TypeVar("T")


def _percentile_bounds(values: Sequence[T], lower: float, upper: float) -> Tuple[T, T]:
    """
    Return ``(low_bound, high_bound)`` selected from ``values`` by nearest rank.

    ``values`` must be non-empty. ``lower``/``upper`` are fractions in ``[0, 1]``
    with ``lower <= upper``. Both bounds are actual elements of ``values`` (so the
    element type — including ``Decimal`` — is preserved exactly).
    """
    ordered = sorted(values)
    n = len(ordered)
    last = n - 1
    # floor for the lower percentile, ceil for the upper: this brackets the
    # requested percentile with real elements and is symmetric for lower==upper.
    low_idx = int(math.floor(lower * last))
    high_idx = int(math.ceil(upper * last))
    return ordered[low_idx], ordered[high_idx]


def winsorize(
    series: Sequence[T],
    lower_percentile: float = 0.05,
    upper_percentile: float = 0.95,
) -> List[T]:
    """
    Clip ``series`` so values below the ``lower_percentile`` are raised to the
    lower-percentile value and values above the ``upper_percentile`` are lowered
    to the upper-percentile value. Order and length are preserved.

    Parameters
    ----------
    series:
        The numeric series. Elements must be mutually comparable; ``Decimal``,
        ``float`` and ``int`` are all fine and the element type is preserved
        (the returned bounds are values taken from ``series`` itself).
    lower_percentile, upper_percentile:
        Fractions in ``[0, 1]`` with ``lower_percentile <= upper_percentile``.
        Defaults clip the bottom and top 5% (a 5%/95% winsorization).
        ``lower_percentile == 0.0`` and ``upper_percentile == 1.0`` together are
        a no-op (clip to the actual min/max).

    Returns
    -------
    A new list, same length and order as ``series``, with extremes clipped.
    An empty ``series`` returns an empty list.

    Raises
    ------
    ValueError
        If the percentiles are out of range, mis-ordered, or any element is
        ``None`` (which would corrupt the bound selection). Fails closed: a
        bad request never silently returns an unwinsorized or garbage series.
    """
    if not (0.0 <= lower_percentile <= 1.0):
        raise ValueError(f"lower_percentile must be in [0, 1]: {lower_percentile!r}")
    if not (0.0 <= upper_percentile <= 1.0):
        raise ValueError(f"upper_percentile must be in [0, 1]: {upper_percentile!r}")
    if lower_percentile > upper_percentile:
        raise ValueError(
            f"lower_percentile ({lower_percentile!r}) must be <= "
            f"upper_percentile ({upper_percentile!r})"
        )

    values = list(series)
    if not values:
        return []
    if any(v is None for v in values):
        raise ValueError("series contains None; cannot winsorize a missing value")

    low_bound, high_bound = _percentile_bounds(values, lower_percentile, upper_percentile)
    return [low_bound if v < low_bound else high_bound if v > high_bound else v for v in values]


def clip(series: Sequence[T], low: T, high: T) -> List[T]:
    """
    Clip ``series`` to the explicit ``[low, high]`` range (the building block
    winsorize uses, exposed for callers who already know their bounds — e.g. a
    fixed price collar). Order and length are preserved; element type is
    preserved since clipped values are the supplied bounds.

    Raises ``ValueError`` if ``low > high``. An empty series returns ``[]``.
    """
    if low > high:
        raise ValueError(f"clip low ({low!r}) must be <= high ({high!r})")
    return [low if v < low else high if v > high else v for v in series]
