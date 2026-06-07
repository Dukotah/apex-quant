"""Tests for apex.strategy.ind_fisher_transform — hand-computed values + edges."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from apex.strategy.ind_fisher_transform import fisher_signal, fisher_transform


def test_output_same_length_as_input() -> None:
    data = [1, 2, 3, 4, 5, 6]
    assert len(fisher_transform(data, 3)) == len(data)


def test_warmup_is_none_until_full_window() -> None:
    out = fisher_transform([10, 11, 12, 13], period=3)
    # First two positions lack a full window of 3.
    assert out[0] is None
    assert out[1] is None
    assert out[2] is not None
    assert out[3] is not None


def test_insufficient_data_all_none() -> None:
    assert fisher_transform([1, 2], period=3) == [None, None]
    assert fisher_transform([], period=5) == []


def test_first_value_hand_computed() -> None:
    # data [1,2,3], period 3:
    #   window [1,2,3] -> mx=3, mn=1, raw = 2*((3-1)/2)-1 = 1.0
    #   value = 0.33*2*1.0 + 0.67*0 = 0.66
    #   fisher = 0.5*ln(1.66/0.34) + 0.5*0
    out = fisher_transform([1, 2, 3], period=3)
    expected = 0.5 * math.log(1.66 / 0.34)
    assert out[2] == pytest.approx(expected, rel=1e-12)


def test_recursion_and_clamp_hand_computed() -> None:
    # data [1,2,3,4], period 3. Second output (i=3):
    #   window [2,3,4] -> raw = 1.0
    #   value = 0.33*2*1.0 + 0.67*0.66 = 1.1022 -> clamped to 0.999
    #   fisher = 0.5*ln(1.999/0.001) + 0.5*prev_fisher
    out = fisher_transform([1, 2, 3, 4], period=3)
    prev_fisher = 0.5 * math.log(1.66 / 0.34)
    v = 0.999
    expected = 0.5 * math.log((1 + v) / (1 - v)) + 0.5 * prev_fisher
    assert out[3] == pytest.approx(expected, rel=1e-12)


def test_flat_window_yields_zero_not_garbage() -> None:
    # A flat window has zero spread; normalized value is centered -> raw 0,
    # so the smoothed value and fisher stay at 0. Must not divide by zero.
    out = fisher_transform([5, 5, 5, 5], period=2)
    assert out[0] is None
    for v in out[1:]:
        assert v == pytest.approx(0.0)


def test_monotonic_rising_series_is_positive() -> None:
    # Strictly rising price -> latest is always at the top of its window ->
    # raw = +1 each bar -> fisher should be positive and (after seeding) grow.
    out = fisher_transform(list(range(1, 11)), period=3)
    vals = [v for v in out if v is not None]
    assert all(v > 0 for v in vals)
    # Recursion pushes the saturated line monotonically upward.
    assert all(b >= a for a, b in zip(vals, vals[1:]))


def test_monotonic_falling_series_is_negative() -> None:
    out = fisher_transform(list(range(10, 0, -1)), period=3)
    vals = [v for v in out if v is not None]
    assert all(v < 0 for v in vals)


def test_decimal_input_matches_float_input() -> None:
    floats = [1.0, 2.5, 2.0, 3.0, 4.5, 4.0]
    decs = [Decimal(str(x)) for x in floats]
    assert fisher_transform(decs, 3) == fisher_transform(floats, 3)


def test_deterministic() -> None:
    data = [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5]
    assert fisher_transform(data, 4) == fisher_transform(data, 4)


def test_clamp_keeps_log_finite_under_saturation() -> None:
    # A long strictly rising series keeps raw = +1 every bar. The smoothed
    # value compounds (0.66 + 0.67*prev) toward a fixed point of 2.0 and is
    # clamped to 0.999, so the log never hits +/-inf. Each bar's own log term
    # is bounded by 0.5*ln(1.999/0.001); assert every output is finite and the
    # later, saturated bars converge toward the fixed point of the recursion
    # fisher = bound + 0.5*fisher  ->  fisher* = 2*bound.
    out = fisher_transform(list(range(1, 60)), period=3)
    bound = 0.5 * math.log((1 + 0.999) / (1 - 0.999))
    vals = [v for v in out if v is not None]
    assert all(math.isfinite(v) for v in vals)
    assert vals[-1] == pytest.approx(2.0 * bound, rel=1e-9)


def test_period_must_be_positive() -> None:
    with pytest.raises(ValueError):
        fisher_transform([1, 2, 3], 0)
    with pytest.raises(ValueError):
        fisher_transform([1, 2, 3], -2)


def test_fisher_signal_trigger_is_lagged_fisher() -> None:
    fisher, trigger = fisher_signal([1, 2, 3, 4, 5], period=3)
    assert len(fisher) == len(trigger) == 5
    # trigger[i] == fisher[i-1] wherever both defined.
    for i in range(1, len(fisher)):
        if fisher[i] is not None and fisher[i - 1] is not None:
            assert trigger[i] == fisher[i - 1]
        else:
            assert trigger[i] is None


def test_fisher_signal_first_defined_trigger_is_none() -> None:
    # The first non-None fisher has no prior fisher, so its trigger is None.
    fisher, trigger = fisher_signal([1, 2, 3, 4], period=3)
    first_idx = next(i for i, v in enumerate(fisher) if v is not None)
    assert trigger[first_idx] is None
