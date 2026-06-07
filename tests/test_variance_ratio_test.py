"""
Tests for apex.validation.variance_ratio_test (Lo-MacKinlay VR test).

Behavioral guarantees locked in:
  - VR(q) == 1 (z ~ 0) for a clean random walk -> cannot reject the null.
  - A strongly mean-reverting series gives VR(q) < 1.
  - A strongly trending/momentum series gives VR(q) > 1.
  - VR(q) is computed exactly (verified against a hand calculation).
  - Insufficient / degenerate data fails closed (returns None).
  - Pure & deterministic: same inputs -> same outputs.
"""

from __future__ import annotations

import math
import random

from apex.validation.variance_ratio_test import (
    VarianceRatioResult,
    lo_mackinlay_test,
    log_returns,
    two_sided_p_value,
    variance_ratio,
    variance_ratio_test_from_prices,
)


def _approx(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


# --------------------------------------------------------------------------
# Exact hand-computed variance ratio.
# --------------------------------------------------------------------------
def test_variance_ratio_hand_computed():
    # returns: r = [1, -1, 1, -1, 1, -1]  (perfectly mean-reverting, n=6)
    r = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0]  # n = 6, mean (mu) = 0 (sum is 0)
    q = 2

    # var_1 = sum((r-mu)^2)/(n-1) = 6/5 = 1.2
    var_1 = 6.0 / 5.0
    assert _approx(var_1, 1.2)

    # Overlapping 2-period returns (centered, mu=0):
    # t=1: r0+r1 = 0 ; t=2: r1+r2 = 0 ; ... all consecutive pairs sum to 0.
    # So every q-period centered return is 0 -> var_q numerator = 0 -> VR = 0.
    vr = variance_ratio(r, q)
    assert vr is not None
    assert _approx(vr, 0.0)


def test_variance_ratio_hand_computed_nonzero():
    # A series where the pairs do NOT all cancel, computed fully by hand.
    # r = [1, 2, 3, 4]  (a perfect uptrend), n=4, q=2
    r = [1.0, 2.0, 3.0, 4.0]
    n = 4
    q = 2
    mu = (1 + 2 + 3 + 4) / 4.0  # 2.5
    dev = [x - mu for x in r]  # [-1.5, -0.5, 0.5, 1.5]

    var_1 = sum(d * d for d in dev) / (n - 1)  # (2.25+0.25+0.25+2.25)/3 = 5/3
    assert _approx(var_1, 5.0 / 3.0)

    # m = q*(n-q+1)*(1 - q/n) = 2*3*(1-0.5) = 3
    m = q * (n - q + 1) * (1.0 - q / n)
    assert _approx(m, 3.0)

    # overlapping centered q-returns: sum(dev[t-1:t+1]) for t=1,2,3
    # t=1: -1.5 + -0.5 = -2.0
    # t=2: -0.5 + 0.5 = 0.0
    # t=3: 0.5 + 1.5 = 2.0
    sum_sq = (-2.0) ** 2 + 0.0**2 + 2.0**2  # 8
    var_q = sum_sq / m  # 8/3 (already per-period: m carries the factor of q)
    expected_vr = var_q / var_1  # (8/3) / (5/3) = 8/5 = 1.6

    vr = variance_ratio(r, q)
    assert vr is not None
    assert _approx(vr, 1.6)
    assert _approx(vr, expected_vr)


# --------------------------------------------------------------------------
# log_returns helper.
# --------------------------------------------------------------------------
def test_log_returns_basic():
    prices = [100.0, 110.0, 121.0]
    lr = log_returns(prices)
    assert len(lr) == 2
    assert _approx(lr[0], math.log(1.1))
    assert _approx(lr[1], math.log(1.1))


def test_log_returns_fails_closed_on_nonpositive():
    assert log_returns([100.0, 0.0, 110.0]) == []
    assert log_returns([100.0, -5.0]) == []
    assert log_returns([100.0]) == []
    assert log_returns([]) == []


# --------------------------------------------------------------------------
# two_sided_p_value sanity.
# --------------------------------------------------------------------------
def test_two_sided_p_value():
    # z = 0 -> p = 1.0
    assert _approx(two_sided_p_value(0.0), 1.0)
    # z = 1.96 -> p ~ 0.05 (two-sided)
    assert abs(two_sided_p_value(1.96) - 0.05) < 0.001
    # symmetric in sign
    assert _approx(two_sided_p_value(2.5), two_sided_p_value(-2.5))


# --------------------------------------------------------------------------
# Insufficient / degenerate data: fail closed.
# --------------------------------------------------------------------------
def test_too_few_returns_returns_none():
    assert variance_ratio([0.1], 2) is None  # need q+1 returns
    assert variance_ratio([0.1, 0.2], 3) is None  # n=2 < q+1=4
    assert lo_mackinlay_test([0.1, 0.2], 3) is None


def test_q_less_than_two_returns_none():
    assert variance_ratio([0.1, 0.2, 0.3], 1) is None
    assert lo_mackinlay_test([0.1, 0.2, 0.3], 1) is None


def test_zero_variance_returns_none():
    flat = [0.0] * 50  # no variance at all
    assert variance_ratio(flat, 4) is None
    assert lo_mackinlay_test(flat, 4) is None


def test_from_prices_fails_closed():
    assert variance_ratio_test_from_prices([100.0], 2) is None
    assert variance_ratio_test_from_prices([100.0, 0.0, 99.0], 2) is None


# --------------------------------------------------------------------------
# Statistical behavior on generated series.
# --------------------------------------------------------------------------
def test_random_walk_cannot_be_rejected():
    # A genuine random walk: VR(q) ~ 1, robust z ~ 0, should NOT reject.
    rng = random.Random(12345)
    rets = [rng.gauss(0.0, 1.0) for _ in range(2000)]
    res = lo_mackinlay_test(rets, q=4)
    assert res is not None
    assert isinstance(res, VarianceRatioResult)
    # VR should be near 1.
    assert abs(res.variance_ratio - 1.0) < 0.15
    # And we should fail to reject the random-walk null at 5%.
    assert res.rejects_random_walk is False


def test_mean_reverting_series_has_vr_below_one():
    # Strongly alternating returns -> negative autocorrelation -> VR(2) < 1.
    rng = random.Random(7)
    rets = []
    prev = rng.gauss(0.0, 1.0)
    rets.append(prev)
    for _ in range(999):
        # next return tends to flip the sign of the previous (mean reversion).
        nxt = -0.8 * prev + 0.2 * rng.gauss(0.0, 1.0)
        rets.append(nxt)
        prev = nxt
    res = lo_mackinlay_test(rets, q=2)
    assert res is not None
    assert res.variance_ratio < 1.0
    assert res.rejects_random_walk is True
    assert res.z_heteroskedastic < 0.0


def test_trending_series_has_vr_above_one():
    # Positively autocorrelated returns -> momentum -> VR(2) > 1.
    rng = random.Random(3)
    rets = []
    prev = rng.gauss(0.0, 1.0)
    rets.append(prev)
    for _ in range(999):
        nxt = 0.6 * prev + 0.4 * rng.gauss(0.0, 1.0)
        rets.append(nxt)
        prev = nxt
    res = lo_mackinlay_test(rets, q=2)
    assert res is not None
    assert res.variance_ratio > 1.0
    assert res.rejects_random_walk is True
    assert res.z_heteroskedastic > 0.0


# --------------------------------------------------------------------------
# Determinism.
# --------------------------------------------------------------------------
def test_deterministic():
    rng = random.Random(1)
    rets = [rng.gauss(0.0, 1.0) for _ in range(500)]
    r1 = lo_mackinlay_test(rets, q=4)
    r2 = lo_mackinlay_test(rets, q=4)
    assert r1 == r2  # frozen dataclass equality


def test_result_summary_is_descriptive():
    rng = random.Random(2)
    rets = [rng.gauss(0.0, 1.0) for _ in range(300)]
    res = lo_mackinlay_test(rets, q=2)
    assert res is not None
    s = res.summary()
    assert "VR(q=2)" in s
    assert "p=" in s


def test_from_prices_matches_log_return_path():
    prices = [100.0]
    rng = random.Random(99)
    for _ in range(500):
        prices.append(prices[-1] * math.exp(rng.gauss(0.0, 0.01)))
    via_prices = variance_ratio_test_from_prices(prices, q=4)
    via_returns = lo_mackinlay_test(log_returns(prices), q=4)
    assert via_prices == via_returns
