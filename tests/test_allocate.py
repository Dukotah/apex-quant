"""
Tests for scripts.allocate pure helpers (return alignment + blending). The full two-strategy
backtest in main() is operator-run, not in CI.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from scripts.allocate import align, blend, equity_from_returns, returns_by_date


def _ts(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def test_returns_by_date_pairs_return_with_following_day():
    eq = [100.0, 110.0, 99.0]  # +10%, -10%
    ts = [_ts(2021, 1, 1), _ts(2021, 1, 2), _ts(2021, 1, 3)]
    out = returns_by_date(eq, ts)
    assert set(out) == {date(2021, 1, 2), date(2021, 1, 3)}
    assert round(out[date(2021, 1, 2)], 4) == 0.1
    assert round(out[date(2021, 1, 3)], 4) == -0.1


def test_align_intersects_on_common_dates():
    a = {date(2021, 1, 1): 0.1, date(2021, 1, 2): 0.2, date(2021, 1, 3): 0.3}
    b = {date(2021, 1, 2): 0.5, date(2021, 1, 3): 0.6, date(2021, 1, 4): 0.7}
    dates, av, bv = align(a, b)
    assert dates == [date(2021, 1, 2), date(2021, 1, 3)]
    assert av == [0.2, 0.3]
    assert bv == [0.5, 0.6]


def test_blend_weights_sum_to_one():
    trend = [0.10, -0.04]
    value = [0.02, 0.06]
    assert blend(trend, value, 0.0) == trend  # all trend
    assert blend(trend, value, 1.0) == value  # all value
    mid = blend(trend, value, 0.5)
    assert round(mid[0], 4) == 0.06 and round(mid[1], 4) == 0.01


def test_equity_from_returns_compounds():
    eq = equity_from_returns([0.1, -0.1])
    assert eq[0] == 1.0
    assert round(eq[1], 4) == 1.1
    assert round(eq[2], 4) == 0.99
