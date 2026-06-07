"""
apex.analytics.underwater_curve
===============================
Turn an equity curve into its *underwater curve* — the drawdown at every point
in time. Where ``apex.validation.metrics.max_drawdown`` collapses the whole
history into the single worst number, this exposes the full shape: how deep the
strategy was below its running peak at each step, how long it stayed there, and
how the drawdowns are distributed.

This is analytics/metric code, not money-movement code: it lives in the same
statistical layer as ``apex.validation.metrics`` and follows that layer's
convention of using ``float`` (matching the ``Sequence[float]`` equity curves
metrics.py expects). Exact position/cash bookkeeping lives in
``apex.risk.portfolio`` and uses Decimal — a different layer.

By convention here a drawdown is a NON-NEGATIVE fraction (0.0 = at a new peak,
0.25 = 25% below the running peak), matching ``max_drawdown``. A peak value of
zero (or below) yields a drawdown of 0.0 for that point rather than dividing by
zero (fail closed).

All functions are pure and deterministic given their inputs. They degrade
gracefully on insufficient data — an empty curve yields an empty series, never
garbage. Tested in tests/test_underwater_curve.py against hand-computed values.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


def running_peak(equity_curve: Sequence[float]) -> list[float]:
    """
    The cumulative running maximum of the equity curve — the high-water mark at
    each point. ``out[i]`` is the largest equity value seen in
    ``equity_curve[:i + 1]``.

    Returns an empty list for an empty input.
    """
    peak = float("-inf")
    out: list[float] = []
    for value in equity_curve:
        v = float(value)
        if v > peak:
            peak = v
        out.append(peak)
    return out


def underwater_curve(equity_curve: Sequence[float]) -> list[float]:
    """
    The drawdown at every point: for each equity value, how far it sits below the
    running peak, as a non-negative fraction.

    ``out[i] = (peak_i - equity_i) / peak_i`` where ``peak_i`` is the running
    high-water mark through point ``i``. A fresh peak gives 0.0. If the running
    peak is zero or below, the point's drawdown is 0.0 (fail closed).

    The result has the same length as the input; an empty curve yields an empty
    list.
    """
    out: list[float] = []
    peak = float("-inf")
    for value in equity_curve:
        v = float(value)
        if v > peak:
            peak = v
        if peak > 0.0:
            dd = (peak - v) / peak
            out.append(dd if dd > 0.0 else 0.0)
        else:
            out.append(0.0)
    return out


def underwater_curve_absolute(equity_curve: Sequence[float]) -> list[float]:
    """
    The drawdown at every point in *currency* terms: ``peak_i - equity_i``, a
    non-negative amount below the running high-water mark.

    Unlike the fractional :func:`underwater_curve`, this is well-defined even
    when the peak is zero or negative, so no special-casing is needed beyond
    clamping tiny negatives (which cannot occur given the running-peak logic) to
    zero. Same length as the input; empty in, empty out.
    """
    out: list[float] = []
    peak = float("-inf")
    for value in equity_curve:
        v = float(value)
        if v > peak:
            peak = v
        amt = peak - v
        out.append(amt if amt > 0.0 else 0.0)
    return out


def time_underwater(equity_curve: Sequence[float]) -> list[int]:
    """
    How many consecutive points each step has been underwater (below its running
    peak). ``out[i]`` is 0 whenever equity is at a new high-water mark, otherwise
    it increments by 1 for each consecutive point spent below the peak.

    This is the raw input for measuring drawdown *duration* (recovery time).
    Same length as the input; empty in, empty out.
    """
    out: list[int] = []
    peak = float("-inf")
    streak = 0
    for value in equity_curve:
        v = float(value)
        if v >= peak:
            peak = v
            streak = 0
        else:
            streak += 1
        out.append(streak)
    return out


def max_time_underwater(equity_curve: Sequence[float]) -> int:
    """
    The longest run (in number of points) the curve spent continuously below a
    high-water mark before making a new high — the worst recovery time.

    Returns 0 for a curve that is never underwater (or is empty / a single
    point).
    """
    longest = 0
    for streak in time_underwater(equity_curve):
        if streak > longest:
            longest = streak
    return longest


@dataclass(frozen=True)
class DrawdownEpisode:
    """
    One peak-to-recovery drawdown episode, expressed as index positions into the
    source equity curve.

    Attributes:
        start_index:   Index of the peak the drawdown began from.
        trough_index:  Index of the lowest equity within the episode.
        end_index:     Index where equity first recovered back to (or above) the
                       peak. Equal to ``trough_index`` if the episode never
                       recovered by the end of the curve (still underwater).
        max_drawdown:  Deepest fractional drawdown within the episode
                       (non-negative).
        recovered:     True if equity returned to the prior peak before the curve
                       ended; False if the curve ended still underwater.
    """

    start_index: int
    trough_index: int
    end_index: int
    max_drawdown: float
    recovered: bool

    @property
    def length(self) -> int:
        """Number of points from peak to recovery (or to curve end), inclusive
        of both endpoints minus one — i.e. the span ``end_index - start_index``."""
        return self.end_index - self.start_index


def drawdown_episodes(equity_curve: Sequence[float]) -> list[DrawdownEpisode]:
    """
    Decompose an equity curve into its distinct underwater episodes.

    An episode opens the first point equity dips below a high-water mark, tracks
    its trough, and closes the point equity first recovers to (or exceeds) the
    peak it started from. A final, still-underwater episode is returned with
    ``recovered=False`` and ``end_index`` set to its trough.

    Curves that are flat or monotonically rising produce no episodes. Empty or
    single-point curves produce an empty list.
    """
    episodes: list[DrawdownEpisode] = []
    n = len(equity_curve)
    if n < 2:
        return episodes

    peak = float(equity_curve[0])
    peak_index = 0
    in_drawdown = False
    trough_value = peak
    trough_index = 0

    for i in range(1, n):
        v = float(equity_curve[i])
        if not in_drawdown:
            if v < peak:
                # Open a new episode from the last peak.
                in_drawdown = True
                trough_value = v
                trough_index = i
            else:
                # Still at/above the high-water mark — advance the peak.
                peak = v
                peak_index = i
        else:
            if v < trough_value:
                trough_value = v
                trough_index = i
            if v >= peak:
                # Recovered back to the prior peak — close the episode.
                episodes.append(
                    DrawdownEpisode(
                        start_index=peak_index,
                        trough_index=trough_index,
                        end_index=i,
                        max_drawdown=_dd(peak, trough_value),
                        recovered=True,
                    )
                )
                in_drawdown = False
                peak = v
                peak_index = i

    if in_drawdown:
        # Curve ended still underwater — report the open episode.
        episodes.append(
            DrawdownEpisode(
                start_index=peak_index,
                trough_index=trough_index,
                end_index=trough_index,
                max_drawdown=_dd(peak, trough_value),
                recovered=False,
            )
        )
    return episodes


def _dd(peak: float, value: float) -> float:
    """Fractional drawdown of ``value`` below ``peak`` (fail closed at peak<=0)."""
    if peak <= 0.0:
        return 0.0
    dd = (peak - value) / peak
    return dd if dd > 0.0 else 0.0
