"""Tests for apex.strategy.ind_kama (Kaufman Adaptive Moving Average)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_kama import efficiency_ratio, kama


# --------------------------------------------------------------------------- #
# efficiency_ratio
# --------------------------------------------------------------------------- #
def test_er_insufficient_data_all_none():
    # Need period+1 values; 3 values with period=3 is not enough.
    assert efficiency_ratio([1.0, 2.0, 3.0], 3) == [None, None, None]


def test_er_warmup_then_values():
    er = efficiency_ratio([float(i) for i in range(1, 8)], 3)  # 1..7
    assert er[:3] == [None, None, None]
    # A steady +1 ramp is perfectly directional → ER == 1.0 everywhere valid.
    assert er[3:] == [1.0, 1.0, 1.0, 1.0]


def test_er_perfectly_flat_is_zero_not_div_by_zero():
    # Zero volatility must yield ER 0.0, never a ZeroDivisionError / garbage.
    er = efficiency_ratio([5.0] * 6, 3)
    assert er == [None, None, None, 0.0, 0.0, 0.0]


def test_er_choppy_between_zero_and_one():
    # Net change small relative to path length → ER strictly between 0 and 1.
    # values: 10,11,10,11,10 ; period=4 at idx4:
    #   change = |10-10| = 0 → ER = 0.0 (no net progress despite movement)
    er = efficiency_ratio([10.0, 11.0, 10.0, 11.0, 10.0], 4)
    assert er[4] == 0.0
    # Partial directionality: 10,11,12,11,13 period=4 idx4:
    #   change=|13-10|=3 vol=1+1+1+2=5 → 0.6
    er2 = efficiency_ratio([10.0, 11.0, 12.0, 11.0, 13.0], 4)
    assert er2[4] == pytest.approx(0.6)


def test_er_rejects_nonpositive_period():
    with pytest.raises(ValueError):
        efficiency_ratio([1.0, 2.0, 3.0], 0)


# --------------------------------------------------------------------------- #
# kama
# --------------------------------------------------------------------------- #
def test_kama_insufficient_data_all_none():
    assert kama([1.0, 2.0, 3.0], 3) == [None, None, None]


def test_kama_seed_is_sma_of_first_period():
    out = kama([float(i) for i in range(1, 8)], 3, 2, 4)
    # Warmup before the seed index.
    assert out[0] is None and out[1] is None
    # Seed at index period-1 == SMA of first `period` values = (1+2+3)/3 = 2.0
    assert out[2] == pytest.approx(2.0)


def test_kama_hand_computed_directional_step():
    # period=3, fast=2, slow=4. Steady +1 ramp → ER=1 → SC = fast_sc**2.
    # fast_sc = 2/3, slow_sc = 2/5 ; ER=1 → SC = (2/3)**2.
    out = kama([float(i) for i in range(1, 8)], 3, 2, 4)
    sc = (2.0 / 3.0) ** 2
    expected_idx3 = 2.0 + sc * (4.0 - 2.0)  # 2.8888...
    assert out[3] == pytest.approx(expected_idx3)
    expected_idx4 = expected_idx3 + sc * (5.0 - expected_idx3)
    assert out[4] == pytest.approx(expected_idx4)


def test_kama_flat_market_stays_put():
    # ER=0 → SC = slow_sc**2 ; on a flat series KAMA never moves off the seed.
    out = kama([5.0] * 6, 3, 2, 4)
    assert out[2:] == [pytest.approx(5.0)] * 4


def test_kama_lags_between_price_and_seed():
    # KAMA must sit between its previous value and the new price (it's a
    # convex blend with 0 < SC < 1), so it never overshoots.
    data = [10.0, 10.0, 10.0, 20.0]  # period=3 → seed=10 at idx2, jump at idx3
    out = kama(data, 3, 2, 4)
    assert out[2] == pytest.approx(10.0)
    assert 10.0 < out[3] < 20.0


def test_kama_accepts_decimal_input():
    # Money-typed inputs (Decimal) must be coerced to float without error.
    data = [Decimal(str(i)) for i in range(1, 8)]
    out_dec = kama(data, 3, 2, 4)
    out_flt = kama([float(i) for i in range(1, 8)], 3, 2, 4)
    for a, b in zip(out_dec, out_flt):
        if a is None:
            assert b is None
        else:
            assert a == pytest.approx(b)


def test_kama_output_length_matches_input():
    data = [float(i) for i in range(50)]
    assert len(kama(data, 10, 2, 30)) == len(data)


def test_kama_deterministic():
    data = [1.0, 3.0, 2.0, 5.0, 4.0, 7.0, 6.0, 9.0, 8.0, 11.0, 10.0]
    assert kama(data, 4, 2, 8) == kama(data, 4, 2, 8)


def test_kama_rejects_bad_periods():
    with pytest.raises(ValueError):
        kama([1.0, 2.0, 3.0], 0)
    with pytest.raises(ValueError):
        kama([1.0, 2.0, 3.0, 4.0], 3, 0, 4)
    with pytest.raises(ValueError):
        kama([1.0, 2.0, 3.0, 4.0], 3, 2, 0)
