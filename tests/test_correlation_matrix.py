"""Tests for apex.risk.correlation_matrix (pure, fast, hand-computed values)."""

from __future__ import annotations

import math

from apex.risk.correlation_matrix import (
    average_correlation,
    correlation_matrix,
    most_correlated_pair,
    pairwise_correlation,
)

# ----------------------------------------------------------------------
# pairwise_correlation
# ----------------------------------------------------------------------


def test_perfect_positive_correlation():
    a = [1.0, 2.0, 3.0, 4.0]
    b = [2.0, 4.0, 6.0, 8.0]  # exactly 2 * a
    assert pairwise_correlation(a, b) == 1.0


def test_perfect_negative_correlation():
    a = [1.0, 2.0, 3.0, 4.0]
    b = [4.0, 3.0, 2.0, 1.0]
    assert pairwise_correlation(a, b) == -1.0


def test_known_value():
    # Hand-computed Pearson r.
    a = [1.0, 2.0, 3.0]
    b = [2.0, 1.0, 4.0]
    # mean_a = 2, mean_b = 7/3
    # cov = (-1)(-1/3) + 0 + (1)(5/3) = 1/3 + 5/3 = 2
    # var_a = 1 + 0 + 1 = 2
    # var_b = (2-7/3)^2 + (1-7/3)^2 + (4-7/3)^2
    #       = (1/9) + (16/9) + (25/9) = 42/9
    # r = 2 / sqrt(2 * 42/9) = 2 / sqrt(84/9) = 2 / (sqrt(84)/3) = 6 / sqrt(84)
    expected = 6.0 / math.sqrt(84.0)
    result = pairwise_correlation(a, b)
    assert result is not None
    assert math.isclose(result, expected, rel_tol=1e-12)


def test_zero_variance_returns_none():
    a = [5.0, 5.0, 5.0]  # flat -> no variance
    b = [1.0, 2.0, 3.0]
    assert pairwise_correlation(a, b) is None


def test_insufficient_data_returns_none():
    assert pairwise_correlation([1.0], [2.0]) is None
    assert pairwise_correlation([], []) is None


def test_unequal_length_aligns_to_front():
    a = [1.0, 2.0, 3.0, 99.0]
    b = [2.0, 4.0, 6.0]  # 2 * a[:3]
    assert pairwise_correlation(a, b) == 1.0


def test_min_periods_enforced():
    a = [1.0, 2.0]
    b = [2.0, 4.0]
    assert pairwise_correlation(a, b, min_periods=3) is None
    assert pairwise_correlation(a, b, min_periods=2) == 1.0


def test_clamped_to_unit_range():
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    b = [10.0, 20.0, 30.0, 40.0, 50.0]
    r = pairwise_correlation(a, b)
    assert r is not None
    assert -1.0 <= r <= 1.0
    assert r == 1.0


# ----------------------------------------------------------------------
# correlation_matrix
# ----------------------------------------------------------------------


def test_matrix_structure_and_symmetry():
    data = {
        "AAA": [1.0, 2.0, 3.0, 4.0],
        "BBB": [2.0, 4.0, 6.0, 8.0],  # perfectly correlated with AAA
        "CCC": [4.0, 3.0, 2.0, 1.0],  # perfectly anti-correlated with AAA
    }
    m = correlation_matrix(data)

    assert set(m.keys()) == {"AAA", "BBB", "CCC"}
    # Diagonal = 1.0
    assert m["AAA"]["AAA"] == 1.0
    assert m["BBB"]["BBB"] == 1.0
    assert m["CCC"]["CCC"] == 1.0
    # Off-diagonal known values
    assert m["AAA"]["BBB"] == 1.0
    assert m["AAA"]["CCC"] == -1.0
    # Symmetry
    for s1 in data:
        for s2 in data:
            assert m[s1][s2] == m[s2][s1]


def test_matrix_empty_input():
    assert correlation_matrix({}) == {}


def test_matrix_single_symbol():
    m = correlation_matrix({"AAA": [1.0, 2.0, 3.0]})
    assert m == {"AAA": {"AAA": 1.0}}


def test_matrix_flat_series_diagonal_none():
    data = {"FLAT": [5.0, 5.0, 5.0], "X": [1.0, 2.0, 3.0]}
    m = correlation_matrix(data)
    # Flat series has no self-correlation defined.
    assert m["FLAT"]["FLAT"] is None
    assert m["X"]["X"] == 1.0
    # Cross with a flat series is undefined.
    assert m["FLAT"]["X"] is None
    assert m["X"]["FLAT"] is None


def test_matrix_too_short_series_is_none():
    data = {"A": [1.0], "B": [2.0]}
    m = correlation_matrix(data)
    assert m["A"]["A"] is None
    assert m["A"]["B"] is None


# ----------------------------------------------------------------------
# average_correlation
# ----------------------------------------------------------------------


def test_average_correlation():
    data = {
        "AAA": [1.0, 2.0, 3.0, 4.0],
        "BBB": [2.0, 4.0, 6.0, 8.0],  # corr with AAA = +1
        "CCC": [4.0, 3.0, 2.0, 1.0],  # corr with AAA = -1, with BBB = -1
    }
    m = correlation_matrix(data)
    # off-diagonal unique pairs: (AAA,BBB)=1, (AAA,CCC)=-1, (BBB,CCC)=-1
    # mean = (1 - 1 - 1) / 3 = -1/3
    assert average_correlation(m) == (1.0 - 1.0 - 1.0) / 3.0
    # abs version: (1 + 1 + 1)/3 = 1
    assert average_correlation(m, use_abs=True) == 1.0


def test_average_correlation_none_when_no_pairs():
    m = correlation_matrix({"A": [1.0, 2.0, 3.0]})
    assert average_correlation(m) is None
    assert average_correlation({}) is None


def test_average_correlation_skips_none_pairs():
    data = {
        "A": [1.0, 2.0, 3.0],
        "B": [2.0, 4.0, 6.0],  # corr +1 with A
        "FLAT": [5.0, 5.0, 5.0],  # undefined with everything
    }
    m = correlation_matrix(data)
    # Only the (A, B) pair is defined -> average is that single value.
    assert average_correlation(m) == 1.0


# ----------------------------------------------------------------------
# most_correlated_pair
# ----------------------------------------------------------------------


def test_most_correlated_pair_abs():
    data = {
        "AAA": [1.0, 2.0, 3.0, 4.0],
        "BBB": [2.0, 4.0, 6.0, 8.0],  # +1 with AAA
        "CCC": [4.0, 3.0, 2.0, 1.0],  # -1 with AAA and BBB
    }
    m = correlation_matrix(data)
    pair = most_correlated_pair(m, use_abs=True)
    assert pair is not None
    a, b, val = pair
    # All |corr| are 1.0 -> first pair in input order wins (AAA, BBB).
    assert (a, b) == ("AAA", "BBB")
    assert val == 1.0


def test_most_correlated_pair_signed():
    data = {
        "AAA": [1.0, 2.0, 3.0, 4.0],
        "CCC": [4.0, 3.0, 2.0, 1.0],  # -1 with AAA
        "BBB": [2.0, 4.0, 6.0, 8.0],  # +1 with AAA
    }
    m = correlation_matrix(data)
    pair = most_correlated_pair(m, use_abs=False)
    assert pair is not None
    a, b, val = pair
    assert val == 1.0
    assert {a, b} == {"AAA", "BBB"}


def test_most_correlated_pair_none_when_no_defined_pairs():
    assert most_correlated_pair({}) is None
    m = correlation_matrix({"A": [1.0, 2.0, 3.0]})
    assert most_correlated_pair(m) is None


def test_returned_signed_value_is_original():
    data = {
        "A": [1.0, 2.0, 3.0, 4.0],
        "B": [8.0, 6.0, 4.0, 2.0],  # -1 with A
    }
    m = correlation_matrix(data)
    pair = most_correlated_pair(m, use_abs=True)
    assert pair is not None
    _, _, val = pair
    # Ranked by abs but reports the original signed value.
    assert val == -1.0
