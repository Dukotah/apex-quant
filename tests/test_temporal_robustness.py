"""
Tests for scripts.temporal_robustness pure helpers (per-calendar-year equity slicing).
The full backtest in main() is operator-run, not in CI.
"""

from __future__ import annotations

from datetime import datetime, timezone

from scripts.temporal_robustness import is_active, slice_curve_by_year, year_sharpe


def _ts(year, day=1):
    return datetime(year, 1, day, tzinfo=timezone.utc)


def test_slice_curve_by_year_groups_and_orders():
    equity = [100.0, 101.0, 102.0, 103.0]
    stamps = [_ts(2021), _ts(2021, 2), _ts(2022), _ts(2023)]
    out = slice_curve_by_year(equity, stamps)
    assert [y for y, _ in out] == [2021, 2022, 2023]
    assert out[0] == (2021, [100.0, 101.0])
    assert out[1] == (2022, [102.0])


def test_slice_curve_by_year_zips_to_shorter():
    out = slice_curve_by_year([100.0, 101.0, 102.0], [_ts(2021), _ts(2021, 2)])
    assert out == [(2021, [100.0, 101.0])]


def test_slice_curve_by_year_empty():
    assert slice_curve_by_year([], []) == []


def test_year_sharpe_short_segment_is_zero():
    assert year_sharpe([100.0]) == 0.0
    assert year_sharpe([]) == 0.0


def test_year_sharpe_sign_tracks_trend():
    up = [100.0 * (1.01**i) for i in range(40)]
    down = [100.0 * (0.99**i) for i in range(40)]
    assert year_sharpe(up) > 0
    assert year_sharpe(down) < 0


def test_is_active_distinguishes_flat_warmup_from_traded():
    assert is_active([100.0, 100.0, 100.0]) is False  # flat warmup year
    assert is_active([100.0, 101.0, 100.5]) is True  # the book moved
    assert is_active([100.0]) is False  # too short
