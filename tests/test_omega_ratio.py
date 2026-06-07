"""Tests for apex.validation.omega_ratio (hand-computed values + edge cases)."""

from __future__ import annotations

import math

from apex.validation.omega_ratio import (
    omega_ratio,
    omega_ratios,
    omega_threshold_crossing,
)


def test_known_value_threshold_zero():
    # gains = 0.10 + 0.02 = 0.12 ; losses = 0.05 + 0.03 = 0.08 ; 0.12/0.08 = 1.5
    returns = [0.10, -0.05, 0.02, -0.03]
    assert math.isclose(omega_ratio(returns, 0.0), 1.5)


def test_default_threshold_is_zero():
    returns = [0.10, -0.05, 0.02, -0.03]
    assert omega_ratio(returns) == omega_ratio(returns, 0.0)


def test_known_value_nonzero_threshold():
    # threshold tau = 0.01:
    #   diffs: 0.09, -0.06, 0.01, -0.04
    #   gains = 0.09 + 0.01 = 0.10 ; losses = 0.06 + 0.04 = 0.10 ; ratio = 1.0
    returns = [0.10, -0.05, 0.02, -0.03]
    assert math.isclose(omega_ratio(returns, 0.01), 1.0)


def test_higher_threshold_lowers_ratio():
    # Omega is monotonically non-increasing in the threshold.
    returns = [0.10, -0.05, 0.02, -0.03, 0.07, -0.01]
    r0 = omega_ratio(returns, 0.0)
    r1 = omega_ratio(returns, 0.02)
    r2 = omega_ratio(returns, 0.05)
    assert r0 >= r1 >= r2


def test_empty_returns_none():
    assert omega_ratio([]) is None


def test_no_downside_returns_none():
    # All returns above threshold -> denominator zero -> undefined.
    assert omega_ratio([0.01, 0.02, 0.03], 0.0) is None


def test_at_threshold_exactly_not_counted():
    # A return exactly equal to the threshold is neither gain nor loss.
    # gains=0.05, losses=0.05 -> 1.0; the 0.0-diff element is ignored.
    returns = [0.05, 0.0, -0.05]
    assert math.isclose(omega_ratio(returns, 0.0), 1.0)


def test_all_below_threshold_returns_zero():
    # Downside but no upside at this threshold -> 0.0 (a losing series).
    assert omega_ratio([-0.01, -0.02], 0.0) == 0.0


def test_single_loss_returns_zero():
    assert omega_ratio([-0.04], 0.0) == 0.0


def test_single_gain_returns_none():
    assert omega_ratio([0.04], 0.0) is None


def test_omega_ratios_curve():
    returns = [0.10, -0.05, 0.02, -0.03]
    out = omega_ratios(returns, [0.0, 0.01])
    assert len(out) == 2
    assert math.isclose(out[0], 1.5)
    assert math.isclose(out[1], 1.0)


def test_omega_ratios_includes_undefined():
    out = omega_ratios([0.01, 0.02], [0.0])
    assert out == [None]


def test_threshold_crossing_finds_unit_threshold():
    # From the known case, Omega = 1.0 exactly at tau = 0.01, > 1 below it.
    # The highest scanned threshold with Omega >= 1.0 should be ~0.01.
    returns = [0.10, -0.05, 0.02, -0.03]
    crossing = omega_threshold_crossing(returns, low=0.0, high=0.02, steps=200)
    assert crossing is not None
    assert math.isclose(crossing, 0.01, abs_tol=1e-4)


def test_threshold_crossing_none_when_never_clears():
    # A losing series: Omega < 1 across the whole positive scan range.
    returns = [-0.01, -0.02, 0.001]
    assert omega_threshold_crossing(returns, low=0.0, high=0.05, steps=50) is None


def test_threshold_crossing_degenerate_inputs():
    assert omega_threshold_crossing([], low=0.0, high=1.0) is None
    assert omega_threshold_crossing([0.1, -0.1], low=1.0, high=0.0) is None
    assert omega_threshold_crossing([0.1, -0.1], low=0.0, high=1.0, steps=0) is None


def test_determinism():
    returns = [0.10, -0.05, 0.02, -0.03, 0.07, -0.01]
    a = omega_ratio(returns, 0.0)
    b = omega_ratio(returns, 0.0)
    assert a == b
