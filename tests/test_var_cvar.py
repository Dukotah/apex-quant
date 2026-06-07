"""Tests for apex.validation.var_cvar — hand-computed values + edge cases."""

from __future__ import annotations

import math
import statistics

import pytest

from apex.validation.var_cvar import (
    VarCvarResult,
    _norm_pdf,
    _norm_ppf,
    compute_var_cvar,
    historical_cvar,
    historical_var,
    parametric_cvar,
    parametric_var,
)

# ---------------------------------------------------------------------------
# _norm_ppf / _norm_pdf — sanity vs known standard-normal values
# ---------------------------------------------------------------------------


def test_norm_ppf_median_is_zero():
    assert _norm_ppf(0.5) == pytest.approx(0.0, abs=1e-9)


def test_norm_ppf_known_quantiles():
    # Standard textbook z-scores.
    assert _norm_ppf(0.975) == pytest.approx(1.959964, abs=1e-5)
    assert _norm_ppf(0.95) == pytest.approx(1.644854, abs=1e-5)
    assert _norm_ppf(0.05) == pytest.approx(-1.644854, abs=1e-5)
    assert _norm_ppf(0.01) == pytest.approx(-2.326348, abs=1e-5)


def test_norm_ppf_symmetry():
    for p in (0.1, 0.25, 0.4, 0.001):
        assert _norm_ppf(p) == pytest.approx(-_norm_ppf(1.0 - p), abs=1e-6)


def test_norm_ppf_boundaries():
    assert _norm_ppf(0.0) == -math.inf
    assert _norm_ppf(1.0) == math.inf


def test_norm_pdf_known_values():
    assert _norm_pdf(0.0) == pytest.approx(1.0 / math.sqrt(2.0 * math.pi), abs=1e-12)
    assert _norm_pdf(1.0) == pytest.approx(0.241970724519143, abs=1e-12)
    assert _norm_pdf(-1.0) == pytest.approx(_norm_pdf(1.0), abs=1e-12)


# ---------------------------------------------------------------------------
# historical_var — hand-computed
# ---------------------------------------------------------------------------


def test_historical_var_hand_computed():
    # 20 returns, worst is -0.10. At 95% conf, alpha=0.05, idx=floor(0.05*20)=1
    # -> the 2nd worst return. Sorted ascending: [-0.10, -0.05, ...]
    returns = [-0.10, -0.05] + [0.01 * i for i in range(18)]
    # sorted ascending: -0.10, -0.05, 0.0, 0.01, ...
    # idx=1 -> -0.05 -> loss 0.05
    assert historical_var(returns, 0.95) == pytest.approx(0.05, abs=1e-12)


def test_historical_var_worst_observation_at_99():
    returns = [-0.20] + [0.01] * 99  # n=100, alpha=0.01, idx=floor(1.0)=1
    # sorted: -0.20, 0.01, 0.01, ... idx=1 -> 0.01 -> gain -> clamps 0
    assert historical_var(returns, 0.99) == 0.0
    # but at a lower conf the worst shows up: alpha=0.02 -> idx=floor(2.0)=2 -> 0.01
    # use a dataset where two big losses exist
    returns2 = [-0.20, -0.15] + [0.01] * 98
    # n=100, conf 0.99 -> alpha 0.01 -> idx=1 -> -0.15 -> 0.15
    assert historical_var(returns2, 0.99) == pytest.approx(0.15, abs=1e-12)


def test_historical_var_gain_tail_clamps_to_zero():
    # All positive returns: even the tail is a gain -> 0 loss.
    returns = [0.01, 0.02, 0.03, 0.04, 0.05]
    assert historical_var(returns, 0.95) == 0.0


def test_historical_var_single_observation():
    assert historical_var([-0.07], 0.95) == pytest.approx(0.07, abs=1e-12)


# ---------------------------------------------------------------------------
# historical_cvar — hand-computed (mean of the tail)
# ---------------------------------------------------------------------------


def test_historical_cvar_hand_computed():
    # n=20, alpha=0.05. 0.05*20 == 1.0000000000000002 in float, so
    # tail_count = ceil(...) = 2 -> mean of the two worst (-0.10, -0.05) = -0.075.
    returns = [-0.10, -0.05] + [0.01 * i for i in range(18)]
    assert historical_cvar(returns, 0.95) == pytest.approx(0.075, abs=1e-12)


def test_historical_cvar_exact_single_tail():
    # n=10, alpha=0.10 -> 0.10*10 == 1.0 cleanly -> tail_count 1 -> worst only.
    returns = [-0.09] + [0.01 * i for i in range(9)]
    assert historical_cvar(returns, 0.90) == pytest.approx(0.09, abs=1e-12)


def test_historical_cvar_multi_observation_tail():
    # n=10, conf 0.80 -> alpha 0.20 -> tail_count=ceil(2.0)=2.
    returns = [-0.10, -0.06, -0.02, 0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06]
    # two worst: -0.10, -0.06 -> mean -0.08 -> loss 0.08
    assert historical_cvar(returns, 0.80) == pytest.approx(0.08, abs=1e-12)


def test_historical_cvar_at_least_one_in_tail():
    # alpha tiny -> ceil still gives 1.
    returns = [-0.30] + [0.01] * 9
    assert historical_cvar(returns, 0.95) == pytest.approx(0.30, abs=1e-12)


def test_cvar_ge_var_historical():
    # CVaR (mean of tail) is always >= VaR (the threshold) for the same conf.
    returns = [-0.12, -0.08, -0.05, -0.01, 0.0, 0.02, 0.03, 0.04, 0.05, 0.10]
    v = historical_var(returns, 0.80)
    c = historical_cvar(returns, 0.80)
    assert c >= v


# ---------------------------------------------------------------------------
# parametric_var / parametric_cvar — closed-form checks
# ---------------------------------------------------------------------------


def test_parametric_var_zero_mean_known_z():
    # Construct symmetric returns with known sample std and zero mean.
    returns = [-0.02, 0.02, -0.02, 0.02]
    mu = statistics.fmean(returns)
    sigma = statistics.stdev(returns)
    assert mu == pytest.approx(0.0, abs=1e-15)
    # VaR @95% = sigma * z_0.95 - mu
    expected = sigma * 1.6448536269514722
    assert parametric_var(returns, 0.95) == pytest.approx(expected, abs=1e-6)


def test_parametric_cvar_closed_form():
    returns = [-0.03, 0.01, 0.02, -0.01, 0.04, -0.02]
    mu = statistics.fmean(returns)
    sigma = statistics.stdev(returns)
    alpha = 0.05
    z = _norm_ppf(alpha)
    expected_es = sigma * _norm_pdf(z) / alpha - mu
    assert parametric_cvar(returns, 0.95) == pytest.approx(expected_es, abs=1e-9)


def test_parametric_cvar_ge_var():
    returns = [-0.03, 0.01, 0.02, -0.01, 0.04, -0.02, 0.005, -0.015]
    assert parametric_cvar(returns, 0.95) >= parametric_var(returns, 0.95)


def test_parametric_zero_variance_uses_negative_mean():
    # Constant losing return -> sigma 0 -> loss is -mu.
    returns = [-0.01, -0.01, -0.01]
    assert parametric_var(returns, 0.95) == pytest.approx(0.01, abs=1e-12)
    assert parametric_cvar(returns, 0.95) == pytest.approx(0.01, abs=1e-12)


def test_parametric_zero_variance_gain_clamps():
    returns = [0.02, 0.02, 0.02]
    assert parametric_var(returns, 0.95) == 0.0
    assert parametric_cvar(returns, 0.95) == 0.0


# ---------------------------------------------------------------------------
# Guards / edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("conf", [0.0, 1.0, -0.1, 1.5])
def test_bad_confidence_returns_none(conf):
    returns = [-0.01, 0.02, 0.03]
    assert historical_var(returns, conf) is None
    assert historical_cvar(returns, conf) is None
    assert parametric_var(returns, conf) is None
    assert parametric_cvar(returns, conf) is None
    assert compute_var_cvar(returns, conf) is None


def test_empty_returns_none_for_historical():
    assert historical_var([], 0.95) is None
    assert historical_cvar([], 0.95) is None


def test_too_few_for_parametric():
    assert parametric_var([-0.01], 0.95) is None
    assert parametric_cvar([-0.01], 0.95) is None
    assert compute_var_cvar([-0.01], 0.95) is None


def test_higher_confidence_means_larger_or_equal_var():
    # More extreme confidence -> deeper into the tail -> VaR not smaller.
    returns = [
        -0.10,
        -0.06,
        -0.04,
        -0.02,
        0.0,
        0.01,
        0.02,
        0.03,
        0.04,
        0.05,
        0.06,
        0.07,
        0.08,
        0.09,
        0.10,
        -0.08,
        -0.05,
        -0.03,
        0.005,
        0.015,
    ]
    assert historical_var(returns, 0.95) >= historical_var(returns, 0.80)
    assert parametric_var(returns, 0.99) >= parametric_var(returns, 0.90)


# ---------------------------------------------------------------------------
# compute_var_cvar bundle
# ---------------------------------------------------------------------------


def test_compute_bundle_matches_individual():
    returns = [-0.05, -0.02, 0.01, 0.03, -0.01, 0.04, 0.02, -0.03, 0.05, -0.04]
    res = compute_var_cvar(returns, 0.90)
    assert isinstance(res, VarCvarResult)
    assert res.confidence == 0.90
    assert res.observations == 10
    assert res.historical_var == historical_var(returns, 0.90)
    assert res.historical_cvar == historical_cvar(returns, 0.90)
    assert res.parametric_var == parametric_var(returns, 0.90)
    assert res.parametric_cvar == parametric_cvar(returns, 0.90)


def test_result_is_frozen():
    res = compute_var_cvar([-0.01, 0.02, 0.03, -0.04], 0.95)
    assert res is not None
    with pytest.raises(Exception):
        res.historical_var = 0.5  # type: ignore[misc]


def test_summary_contains_confidence_and_counts():
    res = compute_var_cvar([-0.05, -0.02, 0.01, 0.03, -0.01, 0.04], 0.95)
    assert res is not None
    s = res.summary()
    assert "95%" in s
    assert "n=6" in s


def test_determinism():
    returns = [0.013, -0.024, 0.005, -0.011, 0.031, -0.042, 0.018, -0.007]
    a = compute_var_cvar(returns, 0.95)
    b = compute_var_cvar(returns, 0.95)
    assert a == b
