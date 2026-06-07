"""Tests for apex.analytics.drawdown_table — hand-computed values + edges."""
from __future__ import annotations

import math

from apex.analytics.drawdown_table import (
    DrawdownEpisode,
    drawdown_episodes,
    top_drawdowns,
)
from apex.validation.metrics import max_drawdown


def test_empty_and_single_point():
    assert drawdown_episodes([]) == []
    assert drawdown_episodes([100.0]) == []
    assert top_drawdowns([], 5) == []


def test_monotonic_rise_has_no_episodes():
    assert drawdown_episodes([100.0, 101.0, 102.0, 110.0]) == []


def test_flat_curve_has_no_episodes():
    # Equal-to-peak counts as a (matched) new high, never a drawdown.
    assert drawdown_episodes([100.0, 100.0, 100.0]) == []


def test_two_episodes_hand_computed():
    curve = [100.0, 90.0, 80.0, 95.0, 100.0, 110.0, 105.0, 100.0]
    eps = drawdown_episodes(curve)
    assert len(eps) == 2

    e1, e2 = eps
    # Chronological order preserved.
    assert e1.start_index < e2.start_index

    # Episode 1: peak 100 (idx0) -> trough 80 (idx2) -> recover 100 (idx4).
    assert e1.peak_index == 0
    assert e1.start_index == 1
    assert e1.trough_index == 2
    assert e1.end_index == 4
    assert e1.peak_value == 100.0
    assert e1.trough_value == 80.0
    assert math.isclose(e1.depth, 0.20)
    assert e1.length == 4
    assert e1.recovered is True

    # Episode 2: peak 110 (idx5) -> trough 100 (idx7), never recovers.
    assert e2.peak_index == 5
    assert e2.start_index == 6
    assert e2.trough_index == 7
    assert e2.end_index == 7
    assert e2.peak_value == 110.0
    assert e2.trough_value == 100.0
    assert math.isclose(e2.depth, 10.0 / 110.0)
    assert e2.length == 2
    assert e2.recovered is False


def test_deepest_matches_metrics_max_drawdown():
    curve = [100.0, 90.0, 80.0, 95.0, 100.0, 110.0, 105.0, 100.0]
    eps = drawdown_episodes(curve)
    deepest = max(e.depth for e in eps)
    # The deepest episode depth must equal the global max_drawdown metric.
    assert math.isclose(deepest, max_drawdown(curve))


def test_top_drawdowns_ordering_and_limit():
    curve = [100.0, 90.0, 80.0, 95.0, 100.0, 110.0, 105.0, 100.0]
    top1 = top_drawdowns(curve, 1)
    assert len(top1) == 1
    assert math.isclose(top1[0].depth, 0.20)  # the 20% one wins

    top_all = top_drawdowns(curve, 5)
    assert len(top_all) == 2
    # Sorted deepest-first.
    assert top_all[0].depth >= top_all[1].depth
    assert math.isclose(top_all[0].depth, 0.20)


def test_top_drawdowns_zero_or_negative_n():
    curve = [100.0, 90.0, 110.0]
    assert top_drawdowns(curve, 0) == []
    assert top_drawdowns(curve, -3) == []


def test_top_drawdowns_more_than_available():
    curve = [100.0, 95.0, 100.0]  # single 5% episode
    eps = top_drawdowns(curve, 10)
    assert len(eps) == 1
    assert math.isclose(eps[0].depth, 0.05)
    assert eps[0].recovered is True


def test_tie_break_by_start_index():
    # Two identical 10% drawdowns; deeper-first ties broken by earlier start.
    curve = [100.0, 90.0, 100.0, 100.0, 90.0, 100.0]
    eps = top_drawdowns(curve, 5)
    assert len(eps) == 2
    assert math.isclose(eps[0].depth, eps[1].depth)
    assert eps[0].start_index < eps[1].start_index
    assert eps[0].start_index == 1


def test_recovery_exactly_at_peak_counts_as_recovered():
    curve = [100.0, 80.0, 100.0]
    (e,) = drawdown_episodes(curve)
    assert e.recovered is True
    assert e.end_index == 2
    assert math.isclose(e.depth, 0.20)
    assert e.length == 2


def test_non_positive_peak_fails_closed():
    # A peak of zero would make depth undefined; such bars are skipped, not
    # turned into garbage episodes.
    curve = [0.0, -1.0, 0.0, 5.0, 4.0, 5.0]
    eps = drawdown_episodes(curve)
    # Only the real, positive-peak episode (5 -> 4 -> 5) is reported.
    assert len(eps) == 1
    e = eps[0]
    assert e.peak_value == 5.0
    assert e.trough_value == 4.0
    assert math.isclose(e.depth, 0.20)


def test_episode_is_frozen():
    e = DrawdownEpisode(0, 1, 2, 3, 100.0, 80.0, 0.2, 3, True)
    try:
        e.depth = 0.5  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("DrawdownEpisode should be immutable")
