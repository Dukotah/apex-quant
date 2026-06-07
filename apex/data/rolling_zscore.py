"""
apex.data.rolling_zscore
========================
Rolling z-score of a numeric series over a fixed lookback window.

A z-score answers "how unusual is the latest value, relative to its own recent
history?" — ``z = (x - mean) / std`` over the trailing ``window`` observations.
It is the canonical normalizer for mean-reversion logic and for turning a raw
indicator (spread, volume, RSI, return) into a stationary, comparable signal:
``z > 2`` means "two standard deviations above its recent norm," regardless of
the series' absolute scale.

This is statistical/indicator math, not money math, so it follows the convention
of ``apex.validation.metrics`` and ``apex.strategy.indicators`` and works in
``float`` (stdlib ``statistics``), deliberately dependency-light so it runs on
the free GitHub Actions runner with no installs.

Determinism & safety (the house rules):
  - Pure: no I/O, no clock, no randomness. Same input → same output, always.
  - Insufficient-data windows degrade gracefully: positions without a full
    ``window`` of history are ``None`` (never a garbage z-score). A window whose
    standard deviation is zero (a flat run) is also ``None`` — dividing by zero
    is undefined, so we fail closed rather than emit ``inf``/``nan``.

Two standard-deviation conventions are supported, matching the rest of the
codebase's vocabulary:
  - ``population`` (default): divides by ``N`` (``statistics.pstdev``). This is
    what indicator-style code such as Bollinger Bands uses.
  - ``sample``: divides by ``N - 1`` (``statistics.stdev``), the unbiased
    estimator; requires at least two points in the window.
"""

from __future__ import annotations

import statistics
from typing import List, Optional, Sequence


def rolling_zscore(
    series: Sequence[float],
    window: int,
    *,
    ddof: int = 0,
) -> List[Optional[float]]:
    """
    Rolling z-score of ``series`` over a trailing ``window``.

    For each position ``i``, computes ``(series[i] - mean) / std`` over the
    ``window`` values ending at ``i`` (inclusive). The result has the same length
    as ``series``; element ``i`` is ``None`` when the window cannot produce a
    meaningful value:
      - fewer than ``window`` observations are available yet (``i < window - 1``),
      - or the window's standard deviation is zero (a flat run — undefined z).

    Args:
        series: the numeric input series, oldest → newest.
        window: trailing lookback length; must be a positive int.
        ddof: delta degrees of freedom for the standard deviation.
            ``0`` (default) → population std (divide by ``N``), matching
            indicator-style code. ``1`` → sample std (divide by ``N - 1``),
            the unbiased estimator. Other values are rejected.

    Returns:
        A list of ``Optional[float]`` aligned with ``series``.

    Raises:
        ValueError: if ``window`` is not a positive int, ``ddof`` is not 0 or 1,
            or ``window`` is too small for the requested ``ddof`` (sample std
            needs at least two points).
    """
    if not isinstance(window, int) or isinstance(window, bool) or window < 1:
        raise ValueError(f"window must be a positive int, got {window!r}")
    if ddof not in (0, 1):
        raise ValueError(f"ddof must be 0 (population) or 1 (sample), got {ddof!r}")
    if window - ddof < 1:
        raise ValueError(
            f"window={window} too small for ddof={ddof} (need at least {ddof + 1} points)"
        )

    out: List[Optional[float]] = []
    for i in range(len(series)):
        if i < window - 1:
            out.append(None)
            continue
        out.append(_zscore_window(series[i - window + 1 : i + 1], ddof))
    return out


def latest_zscore(
    series: Sequence[float],
    window: int,
    *,
    ddof: int = 0,
) -> Optional[float]:
    """
    Z-score of only the most recent value of ``series`` over the trailing
    ``window`` — the common case for an indicator that just needs "where are we
    now?" without materializing the whole rolling series.

    Returns ``None`` if there is not yet a full ``window`` of data or the window
    is flat (zero std). Raises the same ``ValueError``\\ s as :func:`rolling_zscore`.
    """
    if not isinstance(window, int) or isinstance(window, bool) or window < 1:
        raise ValueError(f"window must be a positive int, got {window!r}")
    if ddof not in (0, 1):
        raise ValueError(f"ddof must be 0 (population) or 1 (sample), got {ddof!r}")
    if window - ddof < 1:
        raise ValueError(
            f"window={window} too small for ddof={ddof} (need at least {ddof + 1} points)"
        )

    if len(series) < window:
        return None
    return _zscore_window(series[len(series) - window :], ddof)


def _zscore_window(values: Sequence[float], ddof: int) -> Optional[float]:
    """
    Z-score of the LAST element of ``values`` relative to the whole window.

    ``values`` must already be exactly one window long. Returns ``None`` when the
    standard deviation is zero (undefined z — fail closed, never ``inf``/``nan``).
    """
    floats = [float(v) for v in values]
    mean = statistics.fmean(floats)
    sd = statistics.pstdev(floats) if ddof == 0 else statistics.stdev(floats)
    if sd == 0:
        return None
    return (floats[-1] - mean) / sd
