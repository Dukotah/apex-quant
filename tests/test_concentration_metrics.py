"""Tests for apex.risk.concentration_metrics (pure, hand-computed values)."""
from __future__ import annotations

import math

from apex.risk.concentration_metrics import (
    effective_number_of_positions,
    herfindahl_index,
    max_weight,
    normalize_weights,
    top_n_concentration,
)


def _close(a, b, tol=1e-12):
    return abs(a - b) <= tol


# ----------------------------------------------------------------------
# normalize_weights
# ----------------------------------------------------------------------

def test_normalize_basic():
    out = normalize_weights([1.0, 1.0, 2.0])
    assert out == [0.25, 0.25, 0.5]
    assert _close(sum(out), 1.0)


def test_normalize_uses_absolute_value():
    # A -40 short and a +40 long are equal magnitude -> equal normalized weight.
    out = normalize_weights([-40.0, 40.0])
    assert out == [0.5, 0.5]


def test_normalize_empty_returns_none():
    assert normalize_weights([]) is None


def test_normalize_zero_total_returns_none():
    assert normalize_weights([0.0, 0.0]) is None
    # Long/short that exactly cancels in SIGN still has magnitude, so it
    # normalizes fine — only an all-zero magnitude is undefined.
    assert normalize_weights([5.0, -5.0]) == [0.5, 0.5]


# ----------------------------------------------------------------------
# herfindahl_index
# ----------------------------------------------------------------------

def test_hhi_single_position_is_one():
    assert herfindahl_index([100.0]) == 1.0


def test_hhi_equal_weight_is_one_over_n():
    # Four equal names: each 0.25, HHI = 4 * 0.25^2 = 0.25 = 1/N.
    assert _close(herfindahl_index([1.0, 1.0, 1.0, 1.0]), 0.25)


def test_hhi_hand_computed_unequal():
    # weights normalize to 0.5, 0.25, 0.25 -> 0.25 + 0.0625 + 0.0625 = 0.375
    assert _close(herfindahl_index([2.0, 1.0, 1.0]), 0.375)


def test_hhi_bounds_between_inv_n_and_one():
    w = [10.0, 3.0, 2.0, 1.0]
    hhi = herfindahl_index(w)
    assert 1.0 / len(w) <= hhi <= 1.0


def test_hhi_empty_and_zero_return_none():
    assert herfindahl_index([]) is None
    assert herfindahl_index([0.0, 0.0]) is None


# ----------------------------------------------------------------------
# effective_number_of_positions
# ----------------------------------------------------------------------

def test_effective_n_equal_weight_equals_count():
    assert _close(effective_number_of_positions([1.0, 1.0, 1.0, 1.0]), 4.0)


def test_effective_n_single_is_one():
    assert _close(effective_number_of_positions([7.0]), 1.0)


def test_effective_n_concentrated_below_count():
    # 0.375 HHI -> 1/0.375 = 2.6667 effective names from 3 nominal.
    assert _close(effective_number_of_positions([2.0, 1.0, 1.0]), 1.0 / 0.375)


def test_effective_n_empty_returns_none():
    assert effective_number_of_positions([]) is None


# ----------------------------------------------------------------------
# top_n_concentration
# ----------------------------------------------------------------------

def test_top_n_picks_largest():
    # normalized: 0.5, 0.3, 0.2 ; top 2 = 0.8
    assert _close(top_n_concentration([5.0, 3.0, 2.0], 2), 0.8)


def test_top_1_is_largest_weight():
    assert _close(top_n_concentration([5.0, 3.0, 2.0], 1), 0.5)


def test_top_n_exceeding_count_is_full_book():
    assert _close(top_n_concentration([5.0, 3.0, 2.0], 99), 1.0)


def test_top_n_uses_absolute_value():
    # magnitudes 5,3,2 -> normalized 0.5,0.3,0.2 ; top 1 = 0.5
    assert _close(top_n_concentration([-5.0, 3.0, -2.0], 1), 0.5)


def test_top_n_nonpositive_n_returns_none():
    assert top_n_concentration([1.0, 2.0], 0) is None
    assert top_n_concentration([1.0, 2.0], -3) is None


def test_top_n_empty_returns_none():
    assert top_n_concentration([], 1) is None


# ----------------------------------------------------------------------
# max_weight
# ----------------------------------------------------------------------

def test_max_weight_matches_top_1():
    w = [4.0, 1.0, 5.0]
    assert max_weight(w) == top_n_concentration(w, 1)
    assert _close(max_weight(w), 0.5)


def test_max_weight_empty_returns_none():
    assert max_weight([]) is None


# ----------------------------------------------------------------------
# cross-check: effective_n == 1 / hhi exactly
# ----------------------------------------------------------------------

def test_effective_n_is_reciprocal_of_hhi():
    w = [10.0, 3.0, 2.0, 1.0, 1.0]
    hhi = herfindahl_index(w)
    eff = effective_number_of_positions(w)
    assert math.isclose(eff, 1.0 / hhi, rel_tol=1e-12)
