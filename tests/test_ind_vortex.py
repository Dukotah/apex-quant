"""
tests for apex.strategy.ind_vortex — Vortex Indicator (VI+, VI-).

Hand-computed values plus edge cases (warmup, insufficient data, validation,
flat market, Decimal inputs, determinism).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_vortex import vortex


def _approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


def test_known_values_period_2():
    # Per-bar (i>=1):
    #   i=1: VM+=|12-8|=4, VM-=|9-10|=1, TR=max(3,3,0)=3
    #   i=2: VM+=|11-9|=2, VM-=|9-12|=3, TR=max(2,0,2)=2
    #   i=3: VM+=|13-9|=4, VM-=|10-11|=1, TR=max(3,3,0)=3
    high = [10, 12, 11, 13]
    low = [8, 9, 9, 10]
    close = [9, 11, 10, 12]
    vip, vim = vortex(high, low, close, period=2)

    # Warmup: indices 0 and 1 are None (need period+1 = 3 bars).
    assert vip[0] is None and vim[0] is None
    assert vip[1] is None and vim[1] is None

    # index 2: window indices 1..2 -> VI+ = 6/5, VI- = 4/5
    assert _approx(vip[2], 6 / 5)
    assert _approx(vim[2], 4 / 5)

    # index 3: window indices 2..3 -> VI+ = 6/5, VI- = 4/5
    assert _approx(vip[3], 6 / 5)
    assert _approx(vim[3], 4 / 5)


def test_length_matches_input():
    high = list(range(20))
    low = [x - 1 for x in high]
    close = high
    vip, vim = vortex(high, low, close, period=5)
    assert len(vip) == len(high)
    assert len(vim) == len(high)


def test_insufficient_data_all_none():
    # n < period + 1 -> all None, no garbage.
    high = [10, 11, 12]
    low = [9, 10, 11]
    close = [9.5, 10.5, 11.5]
    vip, vim = vortex(high, low, close, period=5)
    assert vip == [None, None, None]
    assert vim == [None, None, None]


def test_exact_minimum_data():
    # n == period + 1 -> exactly one valid value at the last index.
    high = [10, 12, 11]
    low = [8, 9, 9]
    close = [9, 11, 10]
    vip, vim = vortex(high, low, close, period=2)
    assert vip[0] is None and vip[1] is None
    assert vip[2] is not None and vim[2] is not None
    # window 1..2: VI+ = (4+2)/(3+2) = 6/5, VI- = (1+3)/5 = 4/5
    assert _approx(vip[2], 6 / 5)
    assert _approx(vim[2], 4 / 5)


def test_flat_market_zero_true_range_is_none():
    # Perfectly flat: high==low==close everywhere -> TR sum is 0 -> None (fail closed).
    high = [5.0] * 6
    low = [5.0] * 6
    close = [5.0] * 6
    vip, vim = vortex(high, low, close, period=3)
    assert all(v is None for v in vip)
    assert all(v is None for v in vim)


def test_decimal_inputs_accepted():
    high = [Decimal("10"), Decimal("12"), Decimal("11"), Decimal("13")]
    low = [Decimal("8"), Decimal("9"), Decimal("9"), Decimal("10")]
    close = [Decimal("9"), Decimal("11"), Decimal("10"), Decimal("12")]
    vip, vim = vortex(high, low, close, period=2)
    assert _approx(vip[2], 6 / 5)
    assert _approx(vim[2], 4 / 5)
    assert _approx(vip[3], 6 / 5)
    assert _approx(vim[3], 4 / 5)


def test_period_must_be_positive():
    with pytest.raises(ValueError):
        vortex([1, 2, 3], [1, 2, 3], [1, 2, 3], period=0)
    with pytest.raises(ValueError):
        vortex([1, 2, 3], [1, 2, 3], [1, 2, 3], period=-1)


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        vortex([1, 2, 3], [1, 2], [1, 2, 3], period=2)
    with pytest.raises(ValueError):
        vortex([1, 2, 3], [1, 2, 3], [1, 2], period=2)


def test_empty_inputs():
    vip, vim = vortex([], [], [], period=14)
    assert vip == []
    assert vim == []


def test_determinism():
    high = [10, 12, 11, 13, 14, 12, 15]
    low = [8, 9, 9, 10, 11, 10, 12]
    close = [9, 11, 10, 12, 13, 11, 14]
    a = vortex(high, low, close, period=3)
    b = vortex(high, low, close, period=3)
    assert a == b


def test_sliding_window_matches_recompute():
    # Cross-check the incremental sliding window against a brute-force recompute.
    high = [10, 12, 11, 13, 14, 12, 15, 13, 16, 14]
    low = [8, 9, 9, 10, 11, 10, 12, 11, 13, 12]
    close = [9, 11, 10, 12, 13, 11, 14, 12, 15, 13]
    period = 4
    vip, vim = vortex(high, low, close, period=period)

    # Brute force.
    n = len(close)
    vm_plus = [0.0] * n
    vm_minus = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        vm_plus[i] = abs(high[i] - low[i - 1])
        vm_minus[i] = abs(low[i] - high[i - 1])
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    for idx in range(period, n):
        lo = idx - period + 1
        s_tr = sum(tr[lo : idx + 1])
        exp_plus = sum(vm_plus[lo : idx + 1]) / s_tr
        exp_minus = sum(vm_minus[lo : idx + 1]) / s_tr
        assert _approx(vip[idx], exp_plus)
        assert _approx(vim[idx], exp_minus)
