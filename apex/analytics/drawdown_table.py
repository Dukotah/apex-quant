"""
apex.analytics.drawdown_table
=============================
Decompose an equity curve into its individual drawdown EPISODES and rank the
worst N. Where ``apex.validation.metrics.max_drawdown`` answers "how bad was the
single worst peak-to-trough decline?", this module answers the richer question:
"what were the N deepest drawdowns, when did each start, where was its trough,
when (if ever) did it recover, how deep did it go, and how long did it last?"

A *drawdown episode* runs from the bar AFTER a peak (the first bar that sits
below that peak) through to the bar where equity first recovers back to (or
above) that peak. An episode that never recovers by the end of the curve is
still reported, marked ``recovered=False`` with ``end_index`` pointing at the
last bar.

This is analytics/metric code, not money-movement code: it lives in the same
statistical layer as ``apex.validation.metrics`` and follows that layer's
convention of using ``float`` (matching the ``Sequence[float]`` equity curves
metrics.py consumes). Exact position/cash bookkeeping lives in
``apex.risk.portfolio`` and uses Decimal — a different layer.

All functions are pure and deterministic given their inputs and degrade
gracefully on insufficient data (empty / flat / monotonically-rising curves
yield an empty episode list, never garbage). Tested in
tests/test_drawdown_table.py against hand-computed values.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence


@dataclass(frozen=True)
class DrawdownEpisode:
    """
    A single peak-to-trough(-to-recovery) drawdown.

    Attributes:
        peak_index:   Index of the prior equity peak the decline fell from.
        start_index:  First bar below that peak (the start of the decline). This
                      is ``peak_index + 1``.
        trough_index: Index of the lowest equity reached within the episode.
        end_index:    Bar where equity first recovered to/above the peak. If the
                      episode never recovered, this is the last index of the
                      curve and ``recovered`` is False.
        peak_value:   Equity at ``peak_index``.
        trough_value: Equity at ``trough_index``.
        depth:        Drawdown depth as a POSITIVE fraction of the peak
                      (0.25 == a 25% decline). ``(peak - trough) / peak``.
        length:       Number of bars from the peak to recovery, inclusive of
                      both endpoints (``end_index - peak_index``). For an
                      unrecovered episode this measures peak-to-end.
        recovered:    True if equity returned to/above the peak within the curve.
    """

    peak_index: int
    start_index: int
    trough_index: int
    end_index: int
    peak_value: float
    trough_value: float
    depth: float
    length: int
    recovered: bool


def drawdown_episodes(equity_curve: Sequence[float]) -> List[DrawdownEpisode]:
    """
    Find every drawdown episode in an equity curve, in chronological order.

    An episode opens on the first bar that dips below a running peak and closes
    on the first bar that recovers to/above that peak (or at the end of the
    curve if recovery never happens). Bars that set new highs are not part of any
    episode. A flat or monotonically non-decreasing curve has no episodes.

    Returns an empty list for curves shorter than two points (insufficient data)
    and for curves that never decline. Peaks with a non-positive value are
    skipped (depth is undefined when dividing by a peak <= 0), failing closed.
    """
    n = len(equity_curve)
    if n < 2:
        return []

    episodes: List[DrawdownEpisode] = []
    peak_value = float(equity_curve[0])
    peak_index = 0
    in_drawdown = False
    start_index = 0
    trough_value = peak_value
    trough_index = 0

    def _close(end_index: int, recovered: bool) -> None:
        # peak_value is guaranteed > 0 here (episodes only open below a positive
        # peak), so the depth division is safe.
        depth = (peak_value - trough_value) / peak_value
        episodes.append(
            DrawdownEpisode(
                peak_index=peak_index,
                start_index=start_index,
                trough_index=trough_index,
                end_index=end_index,
                peak_value=peak_value,
                trough_value=trough_value,
                depth=depth,
                length=end_index - peak_index,
                recovered=recovered,
            )
        )

    for i in range(1, n):
        value = float(equity_curve[i])

        if not in_drawdown:
            if value >= peak_value:
                # New high (or matched) — advance the peak, still no drawdown.
                peak_value = value
                peak_index = i
            elif peak_value > 0.0:
                # First dip below a positive peak — open an episode.
                in_drawdown = True
                start_index = i
                trough_value = value
                trough_index = i
            else:
                # Peak is non-positive: depth would be undefined. Track the new
                # extreme as the peak and wait for a positive one. Fail closed.
                peak_value = value
                peak_index = i
        else:
            if value >= peak_value:
                # Recovered to/above the peak — close the episode here, then this
                # bar becomes the new running peak.
                _close(end_index=i, recovered=True)
                in_drawdown = False
                peak_value = value
                peak_index = i
            elif value < trough_value:
                # New trough within the ongoing episode.
                trough_value = value
                trough_index = i

    if in_drawdown:
        # Episode never recovered before the curve ended.
        _close(end_index=n - 1, recovered=False)

    return episodes


def top_drawdowns(
    equity_curve: Sequence[float],
    n: int = 5,
) -> List[DrawdownEpisode]:
    """
    Return the ``n`` deepest drawdown episodes, ordered by depth (deepest first).

    Ties on depth are broken by earlier ``start_index`` so the ordering is fully
    deterministic. ``n <= 0`` returns an empty list; if there are fewer than
    ``n`` episodes, all of them are returned.
    """
    if n <= 0:
        return []
    episodes = drawdown_episodes(equity_curve)
    episodes.sort(key=lambda e: (-e.depth, e.start_index))
    return episodes[:n]
