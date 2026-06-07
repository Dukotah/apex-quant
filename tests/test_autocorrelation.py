"""Tests for apex.validation.autocorrelation — hand-computed values + edges."""
from __future__ import annotations

import math

from apex.validation.autocorrelation import (
    LjungBoxResult,
    autocorrelation,
    autocorrelation_function,
    ljung_box,
)


def test_lag0_is_one():
    assert autocorrelation([1.0, 2.0, 3.0, 4.0], 0) == 1.0


def test_lag0_constant_series_is_none():
    # Zero variance => undefined even at lag 0.
    assert autocorrelation([5.0, 5.0, 5.0], 0) is None


def test_negative_lag_is_none():
    assert autocorrelation([1.0, 2.0, 3.0], -1) is None


def test_insufficient_data_is_none():
    # Need lag+2 points: lag 1 needs at least 3.
    assert autocorrelation([1.0, 2.0], 1) is None
    assert autocorrelation([1.0, 2.0, 3.0], 1) is not None


def test_constant_series_lagk_is_none():
    assert autocorrelation([2.0, 2.0, 2.0, 2.0], 1) is None


def test_lag1_hand_computed():
    # series = [1,2,3,4], mean = 2.5
    # deviations: [-1.5, -0.5, 0.5, 1.5]
    # denom = 1.5^2 + 0.5^2 + 0.5^2 + 1.5^2 = 2.25+0.25+0.25+2.25 = 5.0
    # numer (lag1) = (-0.5*-1.5)+(0.5*-0.5)+(1.5*0.5) = 0.75 -0.25 +0.75 = 1.25
    # r_1 = 1.25 / 5.0 = 0.25
    r1 = autocorrelation([1.0, 2.0, 3.0, 4.0], 1)
    assert r1 is not None
    assert math.isclose(r1, 0.25, rel_tol=1e-12)


def test_lag2_hand_computed():
    # series = [1,2,3,4], deviations [-1.5,-0.5,0.5,1.5], denom = 5.0
    # numer (lag2) = (0.5*-1.5)+(1.5*-0.5) = -0.75 -0.75 = -1.5
    # r_2 = -1.5 / 5.0 = -0.3
    r2 = autocorrelation([1.0, 2.0, 3.0, 4.0], 2)
    assert r2 is not None
    assert math.isclose(r2, -0.3, rel_tol=1e-12)


def test_autocorrelation_bounded():
    series = [0.01, -0.02, 0.015, -0.005, 0.03, -0.01, 0.02, -0.025]
    for k in range(1, 4):
        r = autocorrelation(series, k)
        assert r is not None
        assert -1.0 <= r <= 1.0


def test_acf_length_and_values():
    series = [1.0, 2.0, 3.0, 4.0]
    acf = autocorrelation_function(series, 2)
    assert len(acf) == 2
    assert math.isclose(acf[0], 0.25, rel_tol=1e-12)
    assert math.isclose(acf[1], -0.3, rel_tol=1e-12)


def test_acf_max_lag_zero_is_empty():
    assert autocorrelation_function([1.0, 2.0, 3.0], 0) == []


def test_acf_undefined_lags_zero_filled():
    # Only 3 points: lag 1 defined, lag 2 needs 4 points -> undefined -> 0.0.
    acf = autocorrelation_function([1.0, 2.0, 3.0], 3)
    assert len(acf) == 3
    assert acf[1] == 0.0
    assert acf[2] == 0.0


def test_ljung_box_hand_computed():
    # series = [1,2,3,4], N=4, r_1=0.25, r_2=-0.3
    # Q = N(N+2) * [ r1^2/(N-1) + r2^2/(N-2) ]
    #   = 4*6 * [ 0.0625/3 + 0.09/2 ]
    #   = 24   * [ 0.0208333... + 0.045 ]
    #   = 24   * 0.0658333... = 1.58
    res = ljung_box([1.0, 2.0, 3.0, 4.0], lags=2)
    assert res is not None
    assert isinstance(res, LjungBoxResult)
    assert res.lags == 2
    assert res.dof == 2
    assert math.isclose(res.statistic, 1.58, rel_tol=1e-9)
    assert res.autocorrelations == (0.25, -0.3)
    # Q=1.58 < chi2_95(2)=5.991 -> not significant.
    assert res.significant is False


def test_ljung_box_insufficient_data():
    # lags=10 needs at least 12 points.
    assert ljung_box([0.01] * 5, lags=10) is None


def test_ljung_box_bad_lags():
    assert ljung_box([1.0, 2.0, 3.0, 4.0], lags=0) is None


def test_ljung_box_constant_series_none():
    assert ljung_box([3.0] * 20, lags=5) is None


def test_ljung_box_strong_serial_correlation_significant():
    # A perfectly increasing ramp is highly autocorrelated at short lags.
    series = [float(i) for i in range(60)]
    res = ljung_box(series, lags=5)
    assert res is not None
    assert res.significant is True
    assert res.statistic > 11.070  # chi2_95(5)


def test_ljung_box_large_dof_fails_closed():
    # dof > 20 is untabulated -> significant should be False even if Q is large.
    series = [float(i) for i in range(100)]
    res = ljung_box(series, lags=25)
    assert res is not None
    assert res.lags == 25
    assert res.significant is False


def test_summary_strings():
    res = ljung_box([1.0, 2.0, 3.0, 4.0], lags=2)
    assert res is not None
    assert "Ljung-Box" in res.summary()
    assert "no serial correlation" in res.summary()
