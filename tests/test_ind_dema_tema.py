"""
Tests for apex.strategy.ind_dema_tema (DEMA / TEMA).

Hand-computed against the seeded-EMA convention (each EMA seeded with the SMA of
its first `period` inputs, then alpha = 2/(period+1)). A linear price ramp is a
useful oracle: a lag-reduced average of a straight line should land exactly on
the line once warmed up.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_dema_tema import dema, tema


def _approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


# --------------------------------------------------------------------------- #
# Length + warmup contract
# --------------------------------------------------------------------------- #
def test_output_length_matches_input():
    data = [float(i) for i in range(20)]
    assert len(dema(data, 5)) == len(data)
    assert len(tema(data, 5)) == len(data)


def test_dema_warmup_is_2p_minus_1():
    # period=3 -> first non-None at index 2*3-1 - 1 = index 4 (i.e. 2p-1 values).
    period = 3
    data = [float(i) for i in range(10)]
    out = dema(data, period)
    first_valid = next(i for i, v in enumerate(out) if v is not None)
    assert first_valid == 2 * period - 2  # 0-based index == (2p-1) values seen


def test_tema_warmup_is_3p_minus_2():
    period = 3
    data = [float(i) for i in range(15)]
    out = tema(data, period)
    first_valid = next(i for i, v in enumerate(out) if v is not None)
    assert first_valid == 3 * period - 3  # 0-based index == (3p-2) values seen


def test_insufficient_data_all_none():
    # Fewer than 2p-1 / 3p-2 values -> all None, never garbage.
    assert dema([1.0, 2.0], 3) == [None, None]
    assert tema([1.0, 2.0, 3.0, 4.0], 3) == [None, None, None, None]


def test_empty_input():
    assert dema([], 4) == []
    assert tema([], 4) == []


# --------------------------------------------------------------------------- #
# Hand-computed values (period=2, alpha=2/3), data = [1,2,3,4,5]
# EMA1 = [None,1.5,2.5,3.5,4.5]
# EMA2 = [None,None,2.0,3.0,4.0]
# DEMA = 2*E1 - E2 = [None,None,3.0,4.0,5.0]
# EMA3 = [None,None,None,2.5,3.5]
# TEMA = 3*E1-3*E2+E3 = [None,None,None,4.0,5.0]
# --------------------------------------------------------------------------- #
def test_dema_hand_computed():
    out = dema([1, 2, 3, 4, 5], 2)
    assert out[0] is None and out[1] is None
    assert _approx(out[2], 3.0)
    assert _approx(out[3], 4.0)
    assert _approx(out[4], 5.0)


def test_tema_hand_computed():
    out = tema([1, 2, 3, 4, 5], 2)
    assert out[0] is None and out[1] is None and out[2] is None
    assert _approx(out[3], 4.0)
    assert _approx(out[4], 5.0)


# --------------------------------------------------------------------------- #
# Linear-ramp oracle: lag-reduced MAs sit exactly on a straight line once warm.
# --------------------------------------------------------------------------- #
def test_dema_tracks_linear_ramp():
    data = [float(i) for i in range(30)]
    out = dema(data, 5)
    for i, v in enumerate(out):
        if v is not None:
            assert _approx(v, float(i), tol=1e-6)


def test_tema_tracks_linear_ramp():
    data = [float(i) for i in range(30)]
    out = tema(data, 5)
    for i, v in enumerate(out):
        if v is not None:
            assert _approx(v, float(i), tol=1e-6)


# --------------------------------------------------------------------------- #
# Misc behaviour
# --------------------------------------------------------------------------- #
def test_constant_series_equals_constant():
    data = [7.0] * 20
    for out in (dema(data, 4), tema(data, 4)):
        for v in out:
            if v is not None:
                assert _approx(v, 7.0)


def test_accepts_decimal_input():
    data = [Decimal(i) for i in range(1, 11)]
    out = dema(data, 3)
    # Same ramp oracle holds for Decimal inputs (converted to float internally).
    for i, v in enumerate(out):
        if v is not None:
            assert _approx(v, float(i + 1), tol=1e-6)


def test_determinism():
    data = [1.0, 3.2, 2.1, 5.5, 4.4, 6.6, 7.1, 3.3, 9.9, 8.8, 2.2, 5.0]
    assert dema(data, 4) == dema(data, 4)
    assert tema(data, 4) == tema(data, 4)


def test_period_zero_or_negative_raises():
    with pytest.raises(ValueError):
        dema([1.0, 2.0, 3.0], 0)
    with pytest.raises(ValueError):
        tema([1.0, 2.0, 3.0], -1)
