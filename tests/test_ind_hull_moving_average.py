"""Tests for apex.strategy.ind_hull_moving_average."""

from __future__ import annotations

import math

import pytest

from apex.strategy.ind_hull_moving_average import hull_moving_average, wma


# --------------------------------------------------------------------------- #
# WMA
# --------------------------------------------------------------------------- #
def test_wma_basic_hand_computed():
    # WMA(3) on [1,2,3]: (1*1 + 2*2 + 3*3) / (1+2+3) = 14/6
    out = wma([1, 2, 3], 3)
    assert out[0] is None
    assert out[1] is None
    assert out[2] == pytest.approx(14 / 6)


def test_wma_slides_window():
    # [1,2,3,4], WMA(3):
    #  idx2: (1*1+2*2+3*3)/6 = 14/6
    #  idx3: (1*2+2*3+3*4)/6 = (2+6+12)/6 = 20/6
    out = wma([1, 2, 3, 4], 3)
    assert out == [None, None, pytest.approx(14 / 6), pytest.approx(20 / 6)]


def test_wma_period_one_is_identity():
    # WMA(1): weight 1, denom 1 -> the raw value.
    out = wma([5.0, 7.0, 9.0], 1)
    assert out == [5.0, 7.0, 9.0]


def test_wma_constant_series_equals_constant():
    out = wma([4.0] * 6, 4)
    for i in range(3, 6):
        assert out[i] == pytest.approx(4.0)
    assert out[:3] == [None, None, None]


def test_wma_insufficient_data_all_none():
    assert wma([1, 2], 5) == [None, None]


def test_wma_invalid_period_raises():
    with pytest.raises(ValueError):
        wma([1, 2, 3], 0)
    with pytest.raises(ValueError):
        wma([1, 2, 3], -2)


def test_wma_accepts_decimal_like_inputs():
    from decimal import Decimal

    out = wma([Decimal("1"), Decimal("2"), Decimal("3")], 2)
    # idx1: (1*1 + 2*2)/3 = 5/3 ; idx2: (1*2 + 2*3)/3 = 8/3
    assert out == [None, pytest.approx(5 / 3), pytest.approx(8 / 3)]


# --------------------------------------------------------------------------- #
# Hull Moving Average — brute-force reference
# --------------------------------------------------------------------------- #
def _wma_ref(values, period):
    """Independent, simple WMA over a full list (raises on short window)."""
    denom = period * (period + 1) / 2.0
    n = len(values)
    out = [None] * n
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        out[i] = sum((w + 1) * x for w, x in enumerate(window)) / denom
    return out


def _hma_ref(values, period):
    half = max(1, round(period / 2))
    sq = max(1, math.isqrt(period))
    wh = _wma_ref(values, half)
    wf = _wma_ref(values, period)
    raw = [None] * len(values)
    for i in range(len(values)):
        if wh[i] is not None and wf[i] is not None:
            raw[i] = 2.0 * wh[i] - wf[i]
    valid = [(i, v) for i, v in enumerate(raw) if v is not None]
    out = [None] * len(values)
    if len(valid) >= sq:
        sm = _wma_ref([v for _, v in valid], sq)
        for (oi, _), s in zip(valid, sm):
            out[oi] = s
    return out


def test_hma_matches_reference_period4():
    data = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0]
    got = hull_moving_average(data, 4)
    ref = _hma_ref(data, 4)
    for g, r in zip(got, ref):
        if r is None:
            assert g is None
        else:
            assert g == pytest.approx(r)


def test_hma_matches_reference_period9():
    data = [float(x) for x in [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5, 8, 9, 7, 9, 3, 2, 3]]
    got = hull_moving_average(data, 9)
    ref = _hma_ref(data, 9)
    assert len(got) == len(data)
    for g, r in zip(got, ref):
        if r is None:
            assert g is None
        else:
            assert g == pytest.approx(r)


def test_hma_warmup_then_value():
    # period=4 -> half=2, sqrt=2. raw valid from index 3; WMA(2) of raw needs
    # 2 valid points, so first non-None HMA is index 4 = period + sqrt - 2.
    data = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]
    out = hull_moving_average(data, 4)
    assert out[:4] == [None] * 4
    assert out[4] is not None
    assert out[5] is not None
    assert out[6] is not None


def test_hma_constant_series_equals_constant():
    # A flat input must produce a flat HMA equal to the constant (no garbage).
    out = hull_moving_average([7.0] * 12, 4)
    for v in out:
        if v is not None:
            assert v == pytest.approx(7.0)
    # At least the later positions are filled.
    assert out[-1] == pytest.approx(7.0)


def test_hma_low_lag_tracks_linear_trend():
    # On a perfect linear ramp, a low-lag MA should sit very close to the
    # current value (HMA is designed to minimize lag).
    data = [float(i) for i in range(30)]
    out = hull_moving_average(data, 9)
    last = out[-1]
    assert last is not None
    # HMA on a linear ramp tracks the latest point essentially exactly.
    assert last == pytest.approx(float(len(data) - 1))


def test_hma_period_one_is_identity():
    data = [2.0, 5.0, 1.0, 9.0]
    out = hull_moving_average(data, 1)
    assert out == data


def test_hma_insufficient_data_all_none():
    out = hull_moving_average([1.0, 2.0, 3.0], 9)
    assert out == [None, None, None]


def test_hma_invalid_period_raises():
    with pytest.raises(ValueError):
        hull_moving_average([1, 2, 3], 0)
    with pytest.raises(ValueError):
        hull_moving_average([1, 2, 3], -5)


def test_hma_output_length_matches_input():
    data = [float(i) for i in range(50)]
    assert len(hull_moving_average(data, 10)) == len(data)


def test_hma_empty_input():
    assert hull_moving_average([], 4) == []
