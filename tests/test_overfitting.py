"""
Tests for apex.validation.overfitting — PSR / DSR / MinTRL.

Pure math, so everything is asserted against hand-reasoned known values and the
monotonicity properties the formulas must satisfy (the same discipline used for
the indicator and metrics tests).
"""

from __future__ import annotations

import math

from apex.validation import gauntlet
from apex.validation import overfitting as O
from apex.validation.gauntlet import GateStatus

# ----------------------------------------------------------------- moments


def test_moments_symmetric_series():
    # Symmetric two-point series: zero skew, mean is the midpoint.
    rets = [0.01, -0.01] * 100
    mean, sd, skew, kurt = O.return_moments(rets)
    assert abs(mean) < 1e-12
    assert abs(skew) < 1e-9
    assert sd > 0


def test_per_period_sharpe_zero_variance():
    assert O.per_period_sharpe([0.0] * 50) == 0.0
    assert O.per_period_sharpe([]) == 0.0


def test_per_period_sharpe_known():
    # [0.01, 0.005] repeated: mean 0.0075, deviation ±0.0025 → SR = 3.0.
    assert abs(O.per_period_sharpe([0.01, 0.005] * 100) - 3.0) < 1e-9


# ----------------------------------------------------------------- PSR


def test_psr_at_benchmark_is_half():
    # Observed exactly equals benchmark → z=0 → PSR = 0.5.
    assert abs(O.probabilistic_sharpe_ratio(0.1, 1000, 0.0, 3.0, benchmark_sr=0.1) - 0.5) < 1e-9


def test_psr_needs_data():
    assert O.probabilistic_sharpe_ratio(0.1, 1, 0.0, 3.0) == 0.0


def test_psr_increases_with_history():
    short = O.probabilistic_sharpe_ratio(0.1, 100, 0.0, 3.0)
    long = O.probabilistic_sharpe_ratio(0.1, 2000, 0.0, 3.0)
    assert 0.5 < short < long < 1.0


def test_psr_penalizes_negative_skew_and_fat_tails():
    clean = O.probabilistic_sharpe_ratio(0.1, 1000, skew=0.0, kurtosis=3.0)
    nasty = O.probabilistic_sharpe_ratio(0.1, 1000, skew=-1.0, kurtosis=8.0)
    assert nasty < clean  # crash-risk return shape is worth less for the same Sharpe


# ----------------------------------------------------------------- expected max / DSR


def test_expected_max_sharpe_single_trial_is_zero():
    assert O.expected_max_sharpe(0.04, 1) == 0.0
    assert O.expected_max_sharpe(0.0, 50) == 0.0  # no cross-trial variance


def test_expected_max_sharpe_grows_with_trials():
    e2 = O.expected_max_sharpe(0.04, 2)
    e50 = O.expected_max_sharpe(0.04, 50)
    e500 = O.expected_max_sharpe(0.04, 500)
    assert 0 < e2 < e50 < e500  # more trials → higher luckiest-loser bar


def test_dsr_collapses_to_psr_without_trials():
    psr = O.probabilistic_sharpe_ratio(0.1, 1000, 0.0, 3.0)
    dsr = O.deflated_sharpe_ratio(0.1, 1000, 0.0, 3.0, sr_variance_across_trials=0.04, n_trials=1)
    assert abs(dsr - psr) < 1e-12


def test_dsr_below_psr_when_many_trials():
    psr = O.probabilistic_sharpe_ratio(0.1, 1000, 0.0, 3.0)
    dsr = O.deflated_sharpe_ratio(0.1, 1000, 0.0, 3.0, sr_variance_across_trials=0.04, n_trials=100)
    assert dsr < psr  # deflation for selection bias makes the bar harder


# ----------------------------------------------------------------- MinTRL


def test_mintrl_infinite_without_edge():
    assert math.isinf(O.min_track_record_length(0.0, 0.0, 3.0))
    assert math.isinf(O.min_track_record_length(-0.1, 0.0, 3.0))


def test_mintrl_shrinks_as_edge_grows():
    weak = O.min_track_record_length(0.05, 0.0, 3.0)
    strong = O.min_track_record_length(0.20, 0.0, 3.0)
    assert strong < weak  # a bigger edge needs less data to prove


# ----------------------------------------------------------------- assess()


def _strong_returns(n: int) -> list[float]:
    # Per-period Sharpe 3.0, long history → unambiguously real.
    return [0.01, 0.005] * (n // 2)


def _noisy_short_returns() -> list[float]:
    # Near-zero mean, short → can't be distinguished from luck.
    return [0.02, -0.018, 0.01, -0.012, 0.005, -0.006] * 5


def test_assess_strong_long_series_passes():
    res = O.assess(_strong_returns(2000))
    assert res.passed is True
    assert res.dsr > 0.99
    assert res.n_observations == 2000
    assert res.n_trials == 1
    # Annualized Sharpe ≈ per-period 3.0 * sqrt(252).
    assert abs(res.observed_sharpe_annual - 3.0 * math.sqrt(252)) < 1e-6


def test_assess_short_noisy_series_fails():
    res = O.assess(_noisy_short_returns())
    assert res.passed is False
    assert res.dsr < O.DSR_FLOOR


def test_assess_deflates_with_trial_population():
    rets = _strong_returns(800)
    solo = O.assess(rets)
    # Many high-variance trials should pull the DSR below the un-deflated PSR.
    swept = O.assess(rets, trial_sharpes_annual=[5.0, 40.0, 60.0, 20.0, 47.0])
    assert swept.n_trials == 5
    assert swept.dsr <= solo.dsr


# ----------------------------------------------------------------- gate wiring


def test_gate8_is_soft_and_passes_strong():
    gate, res = gauntlet.evaluate_gate8_overfitting(_strong_returns(2000))
    assert gate.is_hard_gate is False
    assert gate.status == GateStatus.PASS
    assert "DSR" in gate.detail
    assert res.passed is True


def test_gate8_warns_not_fails_on_weak():
    gate, _ = gauntlet.evaluate_gate8_overfitting(_noisy_short_returns())
    # Soft gate: the worst it can do is WARN — it can never hard-fail a strategy.
    assert gate.status == GateStatus.WARN
    assert gate.is_hard_gate is False
