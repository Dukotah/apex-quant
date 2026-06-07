"""Tests for apex.validation.t_test_returns — one-sided one-sample t-test."""
from __future__ import annotations

import math

from apex.validation.t_test_returns import (
    TTestResult,
    student_t_sf,
    t_test_mean_gt_zero,
)


def test_insufficient_data_returns_none():
    assert t_test_mean_gt_zero([]) is None
    assert t_test_mean_gt_zero([0.01]) is None


def test_known_t_statistic_hand_computed():
    # Sample: [1, 2, 3, 4, 5]. mean=3, sample std (n-1)= sqrt(2.5)=1.5811388.
    # SE = std/sqrt(5) = 1.5811388/2.2360680 = 0.7071068.
    # t = mean/SE = 3 / 0.7071068 = 4.2426407.
    res = t_test_mean_gt_zero([1.0, 2.0, 3.0, 4.0, 5.0])
    assert res is not None
    assert res.n == 5
    assert res.df == 4
    assert math.isclose(res.mean, 3.0, rel_tol=1e-12)
    assert math.isclose(res.std, math.sqrt(2.5), rel_tol=1e-12)
    assert math.isclose(res.t_statistic, 4.2426406871, rel_tol=1e-9)


def test_p_value_matches_known_table_value():
    # For the sample above, t=4.2426406871, df=4. The df=4 Student-t CDF has a
    # closed form: F(t)=1/2 + (3/8)*u*(1 - (1/12)*t^2/(1+t^2/4)), u=t/sqrt(1+t^2/4).
    # That yields a one-sided p (= 1 - F) of exactly 0.00661779978184... here.
    res = t_test_mean_gt_zero([1.0, 2.0, 3.0, 4.0, 5.0])
    assert res is not None
    assert math.isclose(res.p_value, 0.0066177997818413, abs_tol=1e-9)
    assert res.significant is True


def test_student_t_sf_symmetry_and_half_at_zero():
    # At t=0 the one-sided survival function is exactly 0.5 for any df.
    assert math.isclose(student_t_sf(0.0, 1), 0.5, abs_tol=1e-12)
    assert math.isclose(student_t_sf(0.0, 30), 0.5, abs_tol=1e-12)
    # SF(t) + SF(-t) == 1 by symmetry.
    for t in (0.5, 1.7, 3.2):
        assert math.isclose(student_t_sf(t, 8) + student_t_sf(-t, 8), 1.0, abs_tol=1e-10)


def test_student_t_sf_against_normal_large_df():
    # With large df the t-distribution approaches the standard normal.
    # P(Z >= 1.96) ~= 0.025.
    sf = student_t_sf(1.96, 100000)
    assert math.isclose(sf, 0.025, abs_tol=2e-4)


def test_negative_mean_not_significant():
    res = t_test_mean_gt_zero([-0.02, -0.01, -0.03, -0.015])
    assert res is not None
    assert res.t_statistic < 0.0
    assert res.p_value > 0.5
    assert res.significant is False


def test_zero_variance_positive_mean_is_significant():
    res = t_test_mean_gt_zero([0.01, 0.01, 0.01])
    assert res is not None
    assert res.std == 0.0
    assert res.t_statistic == float("inf")
    assert res.p_value == 0.0
    assert res.significant is True


def test_zero_variance_nonpositive_mean_not_significant():
    res_zero = t_test_mean_gt_zero([0.0, 0.0, 0.0])
    assert res_zero is not None
    assert res_zero.p_value == 1.0
    assert res_zero.significant is False

    res_neg = t_test_mean_gt_zero([-0.5, -0.5])
    assert res_neg is not None
    assert res_neg.t_statistic == float("-inf")
    assert res_neg.p_value == 1.0
    assert res_neg.significant is False


def test_significance_threshold_is_respected():
    # Marginal positive edge: borderline, flip with the threshold.
    returns = [0.01, -0.005, 0.012, 0.003, -0.002, 0.008, 0.001]
    strict = t_test_mean_gt_zero(returns, significance=0.05)
    loose = t_test_mean_gt_zero(returns, significance=0.5)
    assert strict is not None and loose is not None
    assert strict.p_value == loose.p_value  # same underlying test
    # Looser threshold can only ever be >= as permissive.
    assert (loose.significant or not strict.significant)


def test_result_is_frozen_and_has_summary():
    res = t_test_mean_gt_zero([0.01, 0.02, 0.015])
    assert isinstance(res, TTestResult)
    assert "t-test" in res.summary()
    try:
        res.n = 99  # type: ignore[misc]
        raised = False
    except Exception:
        raised = True
    assert raised
