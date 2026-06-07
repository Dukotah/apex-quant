"""Tests for apex.strategy.ind_roc — ROC and price Momentum."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_roc import momentum, roc

# --------------------------------------------------------------------------- #
# roc
# --------------------------------------------------------------------------- #


def test_roc_warmup_is_none():
    # period=3 needs 3+1 values; first `period` positions are None.
    out = roc([10.0, 11.0, 12.0, 13.0], period=3)
    assert out[:3] == [None, None, None]
    assert out[3] is not None


def test_roc_known_values():
    # period=1: each value vs the prior one.
    # 100 -> 110 : +10%, 110 -> 121 : +10%, 121 -> 121 : 0%
    data = [100.0, 110.0, 121.0, 121.0]
    out = roc(data, period=1)
    assert out[0] is None
    assert out[1] == pytest.approx(10.0)
    assert out[2] == pytest.approx(10.0)
    assert out[3] == pytest.approx(0.0)


def test_roc_period_two():
    # period=2: value[i] vs value[i-2].
    data = [50.0, 60.0, 75.0, 90.0]
    out = roc(data, period=2)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx((75.0 / 50.0 - 1.0) * 100.0)  # +50%
    assert out[3] == pytest.approx((90.0 / 60.0 - 1.0) * 100.0)  # +50%


def test_roc_negative_change():
    out = roc([200.0, 150.0], period=1)
    assert out[1] == pytest.approx(-25.0)


def test_roc_zero_base_is_none():
    # Base price of 0 -> division undefined -> fail closed with None.
    out = roc([0.0, 5.0, 10.0], period=1)
    assert out[0] is None  # warmup
    assert out[1] is None  # base is values[0] == 0.0
    assert out[2] == pytest.approx(100.0)  # 10/5 - 1


def test_roc_accepts_decimal_input():
    out = roc([Decimal("100"), Decimal("125")], period=1)
    assert out[1] == pytest.approx(25.0)


def test_roc_insufficient_data_all_none():
    assert roc([42.0], period=3) == [None]
    assert roc([], period=2) == []


def test_roc_invalid_period_raises():
    with pytest.raises(ValueError):
        roc([1.0, 2.0], period=0)
    with pytest.raises(ValueError):
        roc([1.0, 2.0], period=-1)


def test_roc_length_preserved():
    data = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert len(roc(data, period=2)) == len(data)


# --------------------------------------------------------------------------- #
# momentum
# --------------------------------------------------------------------------- #


def test_momentum_warmup_is_none():
    out = momentum([10.0, 11.0, 12.0, 13.0], period=2)
    assert out[:2] == [None, None]
    assert out[2] is not None


def test_momentum_known_values():
    # period=1: simple successive differences.
    data = [10.0, 13.0, 12.0, 20.0]
    out = momentum(data, period=1)
    assert out[0] is None
    assert out[1] == pytest.approx(3.0)
    assert out[2] == pytest.approx(-1.0)
    assert out[3] == pytest.approx(8.0)


def test_momentum_period_three():
    data = [5.0, 7.0, 9.0, 11.0, 8.0]
    out = momentum(data, period=3)
    assert out[:3] == [None, None, None]
    assert out[3] == pytest.approx(11.0 - 5.0)  # 6.0
    assert out[4] == pytest.approx(8.0 - 7.0)  # 1.0


def test_momentum_handles_zero_base():
    # Unlike ROC, momentum is a difference, so a zero base is perfectly valid.
    out = momentum([0.0, 4.0], period=1)
    assert out[1] == pytest.approx(4.0)


def test_momentum_accepts_decimal_input():
    out = momentum([Decimal("100"), Decimal("90")], period=1)
    assert out[1] == pytest.approx(-10.0)


def test_momentum_insufficient_data_all_none():
    assert momentum([42.0], period=3) == [None]
    assert momentum([], period=2) == []


def test_momentum_invalid_period_raises():
    with pytest.raises(ValueError):
        momentum([1.0, 2.0], period=0)
    with pytest.raises(ValueError):
        momentum([1.0, 2.0], period=-2)


def test_momentum_length_preserved():
    data = [1.0, 2.0, 3.0, 4.0]
    assert len(momentum(data, period=2)) == len(data)
