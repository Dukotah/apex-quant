"""Tests for apex.strategy.ind_cci — hand-computed CCI values plus edge cases."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_cci import cci, typical_price


def _flat(values):
    """Build high==low==close==value bars so typical price == value."""
    return list(values), list(values), list(values)


def test_typical_price_basic():
    high = [12, 14]
    low = [8, 10]
    close = [10, 12]
    # (12+8+10)/3 = 10 ; (14+10+12)/3 = 12
    assert typical_price(high, low, close) == [10.0, 12.0]


def test_typical_price_length_mismatch_raises():
    with pytest.raises(ValueError):
        typical_price([1, 2], [1], [1, 2])


def test_typical_price_accepts_decimal():
    h, lo, c = [Decimal("3")], [Decimal("3")], [Decimal("3")]
    assert typical_price(h, lo, c) == [3.0]


def test_cci_known_value_period_3():
    # TP = [10, 11, 12]; window of all three:
    #   ma = 11, mean_dev = (1 + 0 + 1)/3 = 2/3
    #   CCI = (12 - 11) / (0.015 * 2/3) = 1 / 0.01 = 100.0
    h, lo, c = _flat([10, 11, 12])
    out = cci(h, lo, c, period=3)
    assert out[0] is None
    assert out[1] is None
    assert out[2] == pytest.approx(100.0)


def test_cci_symmetric_negative():
    # TP = [12, 11, 10]: last value below the mean by the same amount → -100.
    h, lo, c = _flat([12, 11, 10])
    out = cci(h, lo, c, period=3)
    assert out[2] == pytest.approx(-100.0)


def test_cci_uses_real_hlc():
    # Two bars, period 2. TP_0 = (12+8+10)/3 = 10 ; TP_1 = (14+10+12)/3 = 12.
    # window [10, 12]: ma = 11, mean_dev = (1 + 1)/2 = 1.
    # CCI_1 = (12 - 11) / (0.015 * 1) = 1 / 0.015 = 66.666...
    high = [12, 14]
    low = [8, 10]
    close = [10, 12]
    out = cci(high, low, close, period=2)
    assert out[0] is None
    assert out[1] == pytest.approx(1.0 / 0.015)


def test_cci_insufficient_data_all_none():
    h, lo, c = _flat([1, 2])
    assert cci(h, lo, c, period=5) == [None, None]


def test_cci_empty():
    assert cci([], [], [], period=3) == []


def test_cci_flat_window_returns_none():
    # Constant prices → mean deviation 0 → undefined → None (fail closed).
    h, lo, c = _flat([5, 5, 5, 5])
    out = cci(h, lo, c, period=3)
    assert out == [None, None, None, None]


def test_cci_invalid_period_raises():
    h, lo, c = _flat([1, 2, 3])
    with pytest.raises(ValueError):
        cci(h, lo, c, period=0)


def test_cci_length_matches_input():
    h, lo, c = _flat([1, 2, 3, 4, 5, 6, 7])
    out = cci(h, lo, c, period=4)
    assert len(out) == 7
    assert out[:3] == [None, None, None]
    assert all(v is not None for v in out[3:])


def test_cci_deterministic():
    h, lo, c = _flat([3, 1, 4, 1, 5, 9, 2, 6])
    assert cci(h, lo, c, period=3) == cci(h, lo, c, period=3)


def test_cci_decimal_input_matches_float():
    floats = ([10, 11, 12], [10, 11, 12], [10, 11, 12])
    decs = (
        [Decimal(x) for x in floats[0]],
        [Decimal(x) for x in floats[1]],
        [Decimal(x) for x in floats[2]],
    )
    assert cci(*decs, period=3) == cci(*floats, period=3)
