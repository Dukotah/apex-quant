"""Tests for apex.data.winsorize — winsorize/clip a numeric series at percentiles."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.data.winsorize import clip, winsorize

# ----------------------------------------------------------------- winsorize


def test_clips_extremes_to_percentile_bounds():
    # 0..10, eleven values. last index = 10.
    # lower 0.1 -> floor(0.1*10)=index 1 -> value 1 ; upper 0.9 -> ceil(0.9*10)=index 9 -> value 9.
    series = list(range(11))  # 0,1,2,...,10
    out = winsorize(series, 0.1, 0.9)
    assert out == [1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 9]


def test_default_percentiles_5_95():
    # 0..19, twenty values, last=19.
    # lower 0.05 -> floor(0.05*19)=floor(0.95)=0 -> value 0 (no lower clip).
    # upper 0.95 -> ceil(0.95*19)=ceil(18.05)=19 -> value 19 (no upper clip on this clean ramp).
    series = list(range(20))
    assert winsorize(series) == series


def test_preserves_order_and_length():
    series = [5, 1, 9, 3, 7, 2, 8]
    out = winsorize(series, 0.2, 0.8)
    assert len(out) == len(series)
    # bounds: sorted=[1,2,3,5,7,8,9], last=6. low floor(0.2*6)=floor(1.2)=1 ->2 ; high ceil(0.8*6)=ceil(4.8)=5 ->8
    assert out == [5, 2, 8, 3, 7, 2, 8]


def test_outlier_is_pulled_in():
    # one huge outlier should be capped to the upper-percentile value.
    series = [10, 11, 12, 13, 1000]
    out = winsorize(series, 0.0, 0.75)
    # sorted=[10,11,12,13,1000], last=4. low=index0=10 ; high=ceil(0.75*4)=ceil(3)=3 ->13
    assert out == [10, 11, 12, 13, 13]


def test_no_op_full_range():
    series = [3, 1, 4, 1, 5, 9, 2, 6]
    assert winsorize(series, 0.0, 1.0) == series


def test_decimal_type_preserved_exactly():
    series = [
        Decimal("100.10"),
        Decimal("100.20"),
        Decimal("100.30"),
        Decimal("100.40"),
        Decimal("999.99"),
    ]
    out = winsorize(series, 0.0, 0.75)
    # high bound = ceil(0.75*4)=index3 -> Decimal("100.40")
    assert out[-1] == Decimal("100.40")
    assert all(isinstance(v, Decimal) for v in out)
    # the clipped value is the literal bound element, not a float artifact
    assert out[-1] is series[3]


def test_empty_series_returns_empty():
    assert winsorize([]) == []


def test_single_element():
    assert winsorize([Decimal("42")], 0.1, 0.9) == [Decimal("42")]


def test_two_elements():
    # last=1. low floor(0.25*1)=0 ; high ceil(0.75*1)=1 -> no clip
    assert winsorize([7, 3], 0.25, 0.75) == [7, 3]


def test_lower_equals_upper_is_a_point_clip():
    # both bounds at the median index -> all values pulled to that single value
    series = [0, 1, 2, 3, 4]  # last=4
    # p=0.5 -> low floor(0.5*4)=2 ; high ceil(0.5*4)=2 -> both bounds = value 2.
    # so 0,1 raise to 2 and 3,4 lower to 2.
    assert winsorize(series, 0.5, 0.5) == [2, 2, 2, 2, 2]


def test_rejects_none_element():
    with pytest.raises(ValueError, match="None"):
        winsorize([1.0, None, 3.0])


@pytest.mark.parametrize("lo,hi", [(-0.1, 0.9), (0.1, 1.1), (0.8, 0.2)])
def test_invalid_percentiles_raise(lo, hi):
    with pytest.raises(ValueError):
        winsorize([1, 2, 3, 4], lo, hi)


# ----------------------------------------------------------------------- clip


def test_clip_explicit_bounds():
    assert clip([1, 5, 10, 15, 20], 5, 15) == [5, 5, 10, 15, 15]


def test_clip_decimal_preserved():
    lo, hi = Decimal("1.0"), Decimal("3.0")
    out = clip([Decimal("0.5"), Decimal("2.0"), Decimal("9.0")], lo, hi)
    assert out == [Decimal("1.0"), Decimal("2.0"), Decimal("3.0")]
    assert out[0] is lo and out[2] is hi


def test_clip_empty():
    assert clip([], 0, 10) == []


def test_clip_rejects_inverted_bounds():
    with pytest.raises(ValueError, match="must be <="):
        clip([1, 2, 3], 10, 1)
