"""
Tests for apex.strategy.ind_coppock_curve.

All expected values are hand-computed. The Coppock Curve is:
    WMA(wma_period) of ( ROC(long_roc) + ROC(short_roc) )
with ROC expressed as a percent (classic convention).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_coppock_curve import coppock_curve, roc, wma


def approx(x, y, tol=1e-9):
    return abs(x - y) <= tol


# --------------------------------------------------------------------------
# roc
# --------------------------------------------------------------------------
def test_roc_basic_percent():
    # 100 -> 110 -> 121 -> 133.1, each step +10%
    data = [100.0, 110.0, 121.0, 133.1]
    out = roc(data, 1)
    assert out[0] is None
    assert approx(out[1], 10.0)
    assert approx(out[2], 10.0)
    assert approx(out[3], 10.0)


def test_roc_two_period():
    data = [100.0, 110.0, 121.0, 133.1]
    out = roc(data, 2)
    assert out[0] is None
    assert out[1] is None
    # 121/100 - 1 = 21%
    assert approx(out[2], 21.0)
    # 133.1/110 - 1 = 21%
    assert approx(out[3], 21.0)


def test_roc_negative_change():
    data = [100.0, 80.0]
    out = roc(data, 1)
    assert approx(out[1], -20.0)


def test_roc_zero_reference_is_none():
    # division by zero reference must fail closed (None), not crash/garbage
    data = [0.0, 5.0, 10.0]
    out = roc(data, 1)
    assert out[0] is None
    assert out[1] is None  # reference value is 0.0
    assert approx(out[2], 100.0)


def test_roc_insufficient_data():
    assert roc([100.0], 1) == [None]
    assert roc([100.0, 110.0], 5) == [None, None]


def test_roc_period_must_be_positive():
    with pytest.raises(ValueError):
        roc([1.0, 2.0], 0)


# --------------------------------------------------------------------------
# wma
# --------------------------------------------------------------------------
def test_wma_period_one_is_identity():
    out = wma([1.0, 2.0, 3.0], 1)
    assert [approx(o, e) for o, e in zip(out, [1.0, 2.0, 3.0])] == [True, True, True]


def test_wma_known_values():
    # period 3: weights 1,2,3 (newest heaviest), denom = 6
    data = [1.0, 2.0, 3.0, 4.0]
    out = wma(data, 3)
    assert out[0] is None
    assert out[1] is None
    # (1*1 + 2*2 + 3*3)/6 = (1+4+9)/6 = 14/6
    assert approx(out[2], 14.0 / 6.0)
    # (2*1 + 3*2 + 4*3)/6 = (2+6+12)/6 = 20/6
    assert approx(out[3], 20.0 / 6.0)


def test_wma_skips_none_windows():
    # warmup Nones must propagate until a full clean window exists
    data = [None, None, 1.0, 2.0, 3.0]
    out = wma(data, 3)
    assert out[0] is None
    assert out[1] is None
    assert out[2] is None  # window [None, None, 1.0]
    assert out[3] is None  # window [None, 1.0, 2.0]
    # window [1.0, 2.0, 3.0] -> (1*1 + 2*2 + 3*3)/6 = 14/6
    assert approx(out[4], 14.0 / 6.0)


def test_wma_period_must_be_positive():
    with pytest.raises(ValueError):
        wma([1.0, 2.0], -1)


# --------------------------------------------------------------------------
# coppock_curve
# --------------------------------------------------------------------------
def test_coppock_length_matches_input():
    data = list(range(1, 60))
    out = coppock_curve(data)
    assert len(out) == len(data)


def test_coppock_warmup_all_none_when_too_short():
    # default params: max ROC = 14, then wma needs 10 -> first value at index
    # 14 + 9 = 23. Anything shorter is entirely None.
    data = [100.0 + i for i in range(10)]
    out = coppock_curve(data)
    assert all(o is None for o in out)


def test_coppock_first_value_index_default_params():
    # 30 bars; first non-None should be exactly at index 23 (14 + (10-1)).
    data = [100.0 * (1.01 ** i) for i in range(30)]
    out = coppock_curve(data)
    for i in range(23):
        assert out[i] is None, f"index {i} should be warmup None"
    assert out[23] is not None


def test_coppock_hand_computed_small_params():
    # long_roc=2, short_roc=1, wma_period=2 on a clean +10%/step series.
    data = [100.0, 110.0, 121.0, 133.1, 146.41]
    out = coppock_curve(data, long_roc=2, short_roc=1, wma_period=2)
    # ROC(1) percent: idx1=10, idx2=10, idx3=10, idx4=10
    # ROC(2) percent: idx2=21, idx3=21, idx4=21
    # combined: idx2=31, idx3=31, idx4=31
    # wma(2): first available at idx3 (needs combined[idx2], combined[idx3])
    assert out[0] is None
    assert out[1] is None
    assert out[2] is None  # only one combined value so far
    # idx3 = (31*1 + 31*2)/3 = 93/3 = 31
    assert approx(out[3], 31.0)
    # idx4 = (31*1 + 31*2)/3 = 31
    assert approx(out[4], 31.0)


def test_coppock_hand_computed_varying_series():
    # Distinct combined values so the weighting is actually exercised.
    # long_roc=1, short_roc=1, wma_period=2  -> combined = 2 * ROC(1)
    data = [100.0, 110.0, 121.0, 145.2]
    out = coppock_curve(data, long_roc=1, short_roc=1, wma_period=2)
    # ROC(1) percent: idx1=10, idx2=10, idx3=20
    # combined (2x): idx1=20, idx2=20, idx3=40
    # wma(2): idx2 = (20*1 + 20*2)/3 = 60/3 = 20
    #         idx3 = (20*1 + 40*2)/3 = 100/3
    assert out[0] is None
    assert out[1] is None
    assert approx(out[2], 20.0)
    assert approx(out[3], 100.0 / 3.0)


def test_coppock_accepts_decimal_input():
    # money-style Decimal prices must be accepted (converted to float internally)
    data = [Decimal("100"), Decimal("110"), Decimal("121"),
            Decimal("133.1"), Decimal("146.41")]
    out = coppock_curve(data, long_roc=2, short_roc=1, wma_period=2)
    assert approx(out[3], 31.0)
    assert approx(out[4], 31.0)


def test_coppock_zero_reference_propagates_none():
    # a zero price makes a ROC reference undefined; result must be None there,
    # never garbage.
    data = [0.0, 10.0, 11.0, 12.1, 13.31]
    out = coppock_curve(data, long_roc=2, short_roc=1, wma_period=2)
    # ROC(2) at idx2 references data[0]=0 -> None, so combined[idx2] is None,
    # which keeps the first wma window incomplete.
    assert out[2] is None
    assert out[3] is None  # window includes the None combined[idx2]


def test_coppock_invalid_params():
    with pytest.raises(ValueError):
        coppock_curve([1.0, 2.0], long_roc=0)
    with pytest.raises(ValueError):
        coppock_curve([1.0, 2.0], short_roc=-1)
    with pytest.raises(ValueError):
        coppock_curve([1.0, 2.0], wma_period=0)
