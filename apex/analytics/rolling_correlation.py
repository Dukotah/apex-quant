"""
apex.analytics.rolling_correlation
==================================
Rolling (windowed) Pearson correlation between two aligned return series.

Where ``apex.validation.metrics.correlation`` gives a single number over an
entire history, this module slides a fixed-width window across two aligned
series and reports how the correlation *evolves* over time. That matters
because diversification is not static: two strategies (or a strategy and SPY)
can be uncorrelated for months and then snap to +1 in a crisis exactly when
you most need them apart. A rolling view surfaces that regime change.

Design / conventions:
  - This is statistical/metric code, so it follows the float convention of
    apex/validation/metrics.py (NOT Decimal). Money never touches this module.
  - Pure and deterministic: same inputs -> same outputs, no I/O, no clock,
    no randomness.
  - Insufficient-data windows are handled gracefully: a window with fewer than
    two points, or with zero variance in either leg, yields ``None`` rather
    than a garbage number. Fail closed.

All functions are tested in tests/test_rolling_correlation.py against
hand-computed values.
"""

from __future__ import annotations

import math
import statistics
from typing import List, Optional, Sequence


def pearson_correlation(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    """
    Pearson correlation coefficient between two equal-length-ish series.

    The two series are truncated to their common length (min). Returns a value
    in [-1, 1], or ``None`` when the correlation is undefined: fewer than two
    overlapping points, or zero variance in either series (a flat line has no
    correlation with anything).

    Unlike ``metrics.correlation`` (which returns 0.0 for the undefined case),
    this returns ``None`` so a rolling caller can distinguish "genuinely
    uncorrelated" (0.0) from "not computable" (None).
    """
    n = min(len(a), len(b))
    if n < 2:
        return None
    a, b = a[:n], b[:n]
    mean_a = statistics.fmean(a)
    mean_b = statistics.fmean(b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    denom = math.sqrt(var_a * var_b)
    if denom == 0:
        return None
    corr = cov / denom
    # Guard against tiny floating-point overshoot beyond [-1, 1].
    if corr > 1.0:
        return 1.0
    if corr < -1.0:
        return -1.0
    return corr


def rolling_correlation(
    a: Sequence[float],
    b: Sequence[float],
    window: int,
) -> List[Optional[float]]:
    """
    Sliding-window Pearson correlation between two aligned return series.

    The two series are first truncated to their common length ``n``. For each
    window-aligned position the correlation of the trailing ``window`` points
    is computed. The result has length ``n - window + 1``; the i-th entry is the
    correlation of ``a[i : i + window]`` against ``b[i : i + window]``.

    Any individual window that is undefined (zero variance in either leg)
    produces ``None`` in that slot rather than corrupting the whole series.

    Args:
        a: First aligned return series.
        b: Second aligned return series (same alignment as ``a``).
        window: Number of points per window. Must be >= 2.

    Returns:
        A list of correlations (each in [-1, 1] or ``None``). Empty list if
        ``window`` exceeds the common length or is otherwise unsatisfiable.

    Raises:
        ValueError: if ``window`` < 2.
    """
    if window < 2:
        raise ValueError("window must be >= 2")

    n = min(len(a), len(b))
    if n < window:
        return []

    a, b = a[:n], b[:n]
    out: List[Optional[float]] = []
    for start in range(0, n - window + 1):
        win_a = a[start : start + window]
        win_b = b[start : start + window]
        out.append(pearson_correlation(win_a, win_b))
    return out


def latest_rolling_correlation(
    a: Sequence[float],
    b: Sequence[float],
    window: int,
) -> Optional[float]:
    """
    Correlation of only the most recent ``window`` overlapping points.

    Convenience for the common online case where you only care about the
    current regime, not the full history. Returns ``None`` if there is
    insufficient data or the window is undefined.

    Raises:
        ValueError: if ``window`` < 2.
    """
    if window < 2:
        raise ValueError("window must be >= 2")
    n = min(len(a), len(b))
    if n < window:
        return None
    return pearson_correlation(a[n - window : n], b[n - window : n])


def average_rolling_correlation(
    a: Sequence[float],
    b: Sequence[float],
    window: int,
) -> Optional[float]:
    """
    Mean of the defined rolling-correlation values across the whole history.

    A single scalar summarising the *typical* co-movement at the chosen
    window size. ``None`` windows are skipped; returns ``None`` if no window
    was computable.

    Raises:
        ValueError: if ``window`` < 2.
    """
    series = rolling_correlation(a, b, window)
    defined = [c for c in series if c is not None]
    if not defined:
        return None
    return statistics.fmean(defined)


def max_rolling_correlation(
    a: Sequence[float],
    b: Sequence[float],
    window: int,
) -> Optional[float]:
    """
    Highest rolling correlation observed across the history.

    This is the diversification stress number: the worst-case moment when two
    return streams moved most in lockstep. ``None`` windows are skipped;
    returns ``None`` if no window was computable.

    Raises:
        ValueError: if ``window`` < 2.
    """
    series = rolling_correlation(a, b, window)
    defined = [c for c in series if c is not None]
    if not defined:
        return None
    return max(defined)
