"""Tests for apex.validation.information_coefficient.

Hand-computed known values plus edge cases. Pure and fast.
"""

from __future__ import annotations

import math

from apex.validation.information_coefficient import (
    ICReport,
    forward_returns,
    ic_report,
    information_coefficient,
    rank_information_coefficient,
)


# --------------------------------------------------------------------------- #
# forward_returns
# --------------------------------------------------------------------------- #
def test_forward_returns_horizon_1():
    # 100 -> 110 -> 99 -> 99*1.5=148.5
    prices = [100.0, 110.0, 99.0, 148.5]
    fwd = forward_returns(prices, horizon=1)
    assert len(fwd) == 3
    assert math.isclose(fwd[0], 0.10)
    assert math.isclose(fwd[1], -0.10)
    assert math.isclose(fwd[2], 0.50)


def test_forward_returns_horizon_2():
    prices = [100.0, 110.0, 120.0, 130.0]
    fwd = forward_returns(prices, horizon=2)
    # len = 4 - 2 = 2; [120/100-1, 130/110-1]
    assert len(fwd) == 2
    assert math.isclose(fwd[0], 0.20)
    assert math.isclose(fwd[1], 130.0 / 110.0 - 1.0)


def test_forward_returns_insufficient_data():
    assert forward_returns([100.0], horizon=1) == []
    assert forward_returns([100.0, 101.0], horizon=2) == []
    assert forward_returns([], horizon=1) == []


def test_forward_returns_bad_horizon():
    assert forward_returns([1.0, 2.0, 3.0], horizon=0) == []
    assert forward_returns([1.0, 2.0, 3.0], horizon=-1) == []


def test_forward_returns_zero_base_price():
    fwd = forward_returns([0.0, 50.0, 60.0], horizon=1)
    # first base is zero -> 0.0 (refuse to invent a ratio)
    assert fwd[0] == 0.0
    assert math.isclose(fwd[1], 0.20)


# --------------------------------------------------------------------------- #
# Pearson IC
# --------------------------------------------------------------------------- #
def test_perfect_positive_ic():
    signal = [1.0, 2.0, 3.0, 4.0]
    fwd = [0.1, 0.2, 0.3, 0.4]  # perfectly linear in signal
    ic = information_coefficient(signal, fwd)
    assert ic is not None
    assert math.isclose(ic, 1.0, abs_tol=1e-12)


def test_perfect_negative_ic():
    signal = [1.0, 2.0, 3.0, 4.0]
    fwd = [0.4, 0.3, 0.2, 0.1]
    ic = information_coefficient(signal, fwd)
    assert ic is not None
    assert math.isclose(ic, -1.0, abs_tol=1e-12)


def test_known_pearson_value():
    # signal = [1,2,3], returns = [2,1,3]
    # means: 2, 2. deviations sig: -1,0,1 ret: 0,-1,1
    # cov = (-1*0)+(0*-1)+(1*1) = 1
    # var_a = 1+0+1 = 2 ; var_b = 0+1+1 = 2 ; denom = 2
    # corr = 1/2 = 0.5
    ic = information_coefficient([1.0, 2.0, 3.0], [2.0, 1.0, 3.0])
    assert ic is not None
    assert math.isclose(ic, 0.5, abs_tol=1e-12)


def test_ic_truncates_to_shorter():
    # extra signal entry should be ignored (aligns to forward returns length)
    signal = [1.0, 2.0, 3.0, 4.0, 999.0]
    fwd = [0.1, 0.2, 0.3, 0.4]
    ic = information_coefficient(signal, fwd)
    assert ic is not None
    assert math.isclose(ic, 1.0, abs_tol=1e-12)


def test_ic_zero_variance_returns_none():
    assert information_coefficient([5.0, 5.0, 5.0], [0.1, 0.2, 0.3]) is None
    assert information_coefficient([1.0, 2.0, 3.0], [0.25, 0.25, 0.25]) is None


def test_ic_insufficient_points_returns_none():
    assert information_coefficient([1.0], [0.1]) is None
    assert information_coefficient([], []) is None


# --------------------------------------------------------------------------- #
# Rank IC (Spearman)
# --------------------------------------------------------------------------- #
def test_rank_ic_perfect_monotonic_nonlinear():
    # strictly increasing but non-linear relationship -> rank IC == 1
    signal = [1.0, 2.0, 3.0, 4.0]
    fwd = [0.01, 0.04, 0.09, 0.16]  # squares: monotonic, not linear
    ric = rank_information_coefficient(signal, fwd)
    assert ric is not None
    assert math.isclose(ric, 1.0, abs_tol=1e-12)


def test_rank_ic_perfect_negative():
    signal = [1.0, 2.0, 3.0, 4.0]
    fwd = [0.4, 0.3, 0.2, 0.1]
    ric = rank_information_coefficient(signal, fwd)
    assert ric is not None
    assert math.isclose(ric, -1.0, abs_tol=1e-12)


def test_rank_ic_with_ties():
    # signal ranks with a tie: values [1,1,2] -> ranks [1.5,1.5,3]
    # returns [10,20,30] -> ranks [1,2,3]
    # pearson of [1.5,1.5,3] vs [1,2,3]:
    #   mean_a = 2.0, mean_b = 2.0
    #   dev_a = -0.5,-0.5,1.0 ; dev_b = -1,0,1
    #   cov = 0.5 + 0 + 1.0 = 1.5
    #   var_a = 0.25+0.25+1 = 1.5 ; var_b = 1+0+1 = 2
    #   denom = sqrt(3) ; corr = 1.5/sqrt(3) ~= 0.8660254
    ric = rank_information_coefficient([1.0, 1.0, 2.0], [10.0, 20.0, 30.0])
    assert ric is not None
    assert math.isclose(ric, 1.5 / math.sqrt(3.0), abs_tol=1e-12)


def test_rank_ic_all_identical_signal_none():
    assert rank_information_coefficient([3.0, 3.0, 3.0], [0.1, 0.2, 0.3]) is None


def test_rank_ic_insufficient_points_none():
    assert rank_information_coefficient([1.0], [0.1]) is None


def test_rank_differs_from_pearson_on_outlier():
    # one huge outlier inflates Pearson but rank IC stays moderate/robust
    signal = [1.0, 2.0, 3.0, 4.0]
    fwd = [0.0, 0.0, 0.0, 100.0]
    ic = information_coefficient(signal, fwd)
    ric = rank_information_coefficient(signal, fwd)
    assert ic is not None and ric is not None
    # rank IC should not be a near-perfect 1.0 the way pearson is dominated
    assert ric < ic or ric < 0.95


# --------------------------------------------------------------------------- #
# ic_report
# --------------------------------------------------------------------------- #
def test_ic_report_basic():
    # signal predicts next-period return perfectly
    prices = [100.0, 110.0, 99.0, 148.5]  # fwd = [0.10, -0.10, 0.50]
    signal = [1.0, -1.0, 5.0]  # aligns: 1->0.10, -1->-0.10, 5->0.50
    report = ic_report(signal, prices, horizon=1)
    assert isinstance(report, ICReport)
    assert report.n == 3
    assert report.horizon == 1
    assert report.ic is not None
    assert report.rank_ic is not None
    # signal order matches return order -> perfect rank IC
    assert math.isclose(report.rank_ic, 1.0, abs_tol=1e-12)


def test_ic_report_truncates_signal_to_forward_returns():
    prices = [100.0, 110.0, 99.0, 148.5]  # 3 forward returns
    signal = [1.0, -1.0, 5.0, 7.0, 9.0]  # longer; trailing entries dropped
    report = ic_report(signal, prices, horizon=1)
    assert report.n == 3


def test_ic_report_insufficient_data():
    report = ic_report([1.0], [100.0, 110.0], horizon=1)
    # only 1 forward return -> n < 2 -> all None
    assert report.ic is None
    assert report.rank_ic is None
    assert report.n == 1
    assert report.horizon == 1


def test_ic_report_empty():
    report = ic_report([], [], horizon=1)
    assert report.ic is None
    assert report.rank_ic is None
    assert report.n == 0


def test_ic_report_summary_string():
    prices = [100.0, 110.0, 99.0, 148.5]
    signal = [1.0, -1.0, 5.0]
    s = ic_report(signal, prices, horizon=1).summary()
    assert "pearson=" in s
    assert "rank=" in s
    assert "h=1" in s


def test_ic_report_summary_handles_none():
    s = ic_report([], [], horizon=1).summary()
    assert "n/a" in s


def test_icreport_is_frozen():
    report = ICReport(ic=0.1, rank_ic=0.2, n=5, horizon=1)
    try:
        report.ic = 0.5  # type: ignore[misc]
        raised = False
    except Exception:
        raised = True
    assert raised
