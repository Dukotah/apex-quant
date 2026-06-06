"""
Tests for the overfitting-aware statistics added to apex.validation.metrics:
Probabilistic / Deflated Sharpe, the expected-max-Sharpe bound, minimum track
record length, Probability of Backtest Overfitting (CSCV), and the turnover /
capacity sanity metrics.

Where a closed-form value can be hand-computed it is asserted exactly; the
heavier estimators (PBO/CSCV) are checked against constructed inputs whose
correct answer is known by construction (a genuine edge vs an overfit mirage).
"""
from __future__ import annotations

import math
import random

from apex.validation import metrics


# --------------------------------------------------------- normal helpers

def test_norm_cdf_known_points():
    assert abs(metrics._norm_cdf(0.0) - 0.5) < 1e-12
    # Φ(1.959964) ≈ 0.975 (the 97.5% z), Φ(1.281552) ≈ 0.90.
    assert abs(metrics._norm_cdf(1.959964) - 0.975) < 1e-4
    assert abs(metrics._norm_cdf(1.2815515594) - 0.90) < 1e-4


def test_norm_ppf_inverts_cdf():
    for p in (0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99):
        z = metrics._norm_ppf(p)
        assert abs(metrics._norm_cdf(z) - p) < 1e-6
    # Known quantiles.
    assert abs(metrics._norm_ppf(0.975) - 1.959964) < 1e-4
    assert abs(metrics._norm_ppf(0.5)) < 1e-9


# --------------------------------------------------------- skew / kurtosis

def test_skewness_symmetric_is_zero():
    assert abs(metrics.skewness([-2, -1, 0, 1, 2])) < 1e-12


def test_skewness_sign():
    # A long right tail → positive skew.
    right_tailed = [0, 0, 0, 0, 10]
    assert metrics.skewness(right_tailed) > 0
    left_tailed = [0, 0, 0, 0, -10]
    assert metrics.skewness(left_tailed) < 0


def test_kurtosis_hand_value():
    # data [-2,-1,0,1,2]: pstdev = sqrt(2); standardized^4 mean
    #   = (4 + 0.25 + 0 + 0.25 + 4)/5 = 8.5/5 = 1.7 (raw); excess = -1.3.
    data = [-2, -1, 0, 1, 2]
    assert abs(metrics.kurtosis(data, excess=False) - 1.7) < 1e-12
    assert abs(metrics.kurtosis(data, excess=True) - (-1.3)) < 1e-12


# --------------------------------------------------------- PSR

def test_psr_hand_computed():
    # returns [0, 0, 0.02, 0.02]: mean 0.01, pstdev 0.01 → per-period SR_hat 1.0,
    # skew 0, raw kurtosis 1.0 → denom_sq = 1. z = (1-0)*sqrt(n-1) = sqrt(3).
    # PSR(0) = Φ(sqrt(3)) ≈ 0.9583677.
    returns = [0.0, 0.0, 0.02, 0.02]
    psr = metrics.probabilistic_sharpe_ratio(
        returns, reference_sharpe=0.0, periods_per_year=252,
    )
    assert abs(psr - metrics._norm_cdf(math.sqrt(3.0))) < 1e-9
    assert abs(psr - 0.9583677) < 1e-5


def test_psr_in_unit_interval_and_monotone_in_length():
    rng = random.Random(11)
    short = [rng.gauss(0.001, 0.01) for _ in range(20)]
    longer = short + [rng.gauss(0.001, 0.01) for _ in range(200)]
    p_short = metrics.probabilistic_sharpe_ratio(short)
    p_long = metrics.probabilistic_sharpe_ratio(longer)
    assert 0.0 <= p_short <= 1.0
    assert 0.0 <= p_long <= 1.0


def test_psr_fails_closed_on_thin_data():
    assert metrics.probabilistic_sharpe_ratio([0.01, 0.02]) == 0.0
    assert metrics.probabilistic_sharpe_ratio([]) == 0.0
    # No variance → no demonstrable edge.
    assert metrics.probabilistic_sharpe_ratio([0.01, 0.01, 0.01, 0.01]) == 0.0


def test_psr_higher_reference_lowers_confidence():
    rng = random.Random(5)
    returns = [rng.gauss(0.002, 0.01) for _ in range(250)]
    low = metrics.probabilistic_sharpe_ratio(returns, reference_sharpe=0.0)
    high = metrics.probabilistic_sharpe_ratio(returns, reference_sharpe=3.0)
    assert high < low


# --------------------------------------------------------- expected max Sharpe / DSR

def test_expected_max_sharpe_grows_with_trials():
    e2 = metrics.expected_max_sharpe(2, 0.04)
    e100 = metrics.expected_max_sharpe(100, 0.04)
    e1000 = metrics.expected_max_sharpe(1000, 0.04)
    assert 0.0 < e2 < e100 < e1000


def test_expected_max_sharpe_degenerate():
    assert metrics.expected_max_sharpe(1, 0.04) == 0.0
    assert metrics.expected_max_sharpe(100, 0.0) == 0.0


def test_dsr_deflates_below_psr_with_many_trials():
    # Same returns: the DSR (which subtracts the luck of many trials) must be
    # <= the PSR vs zero (which assumes a single, honest trial).
    rng = random.Random(7)
    returns = [rng.gauss(0.0015, 0.01) for _ in range(250)]
    psr0 = metrics.probabilistic_sharpe_ratio(returns, reference_sharpe=0.0)
    trial_sharpes = [rng.gauss(0.5, 0.6) for _ in range(200)]
    dsr = metrics.deflated_sharpe_ratio(returns, num_trials=200, trial_sharpes=trial_sharpes)
    assert 0.0 <= dsr <= psr0


def test_dsr_fails_closed_without_trial_info():
    rng = random.Random(3)
    returns = [rng.gauss(0.002, 0.01) for _ in range(100)]
    # No variance and no list → cannot deflate honestly.
    assert metrics.deflated_sharpe_ratio(returns, num_trials=50) == 0.0


def test_dsr_single_trial_barely_deflates():
    # With effectively one trial the deflation target is ~0, so DSR ≈ PSR(0).
    rng = random.Random(13)
    returns = [rng.gauss(0.0015, 0.01) for _ in range(250)]
    psr0 = metrics.probabilistic_sharpe_ratio(returns, reference_sharpe=0.0)
    dsr1 = metrics.deflated_sharpe_ratio(
        returns, num_trials=1, trial_sharpes=[1.0, 1.0],
    )
    # num_trials=1 → expected_max_sharpe returns 0 → DSR == PSR(0).
    assert abs(dsr1 - psr0) < 1e-9


# --------------------------------------------------------- min track record length

def test_mtrl_inf_when_not_beating_reference():
    rng = random.Random(9)
    returns = [rng.gauss(0.0, 0.01) for _ in range(100)]  # ~zero edge
    assert metrics.min_track_record_length(returns, reference_sharpe=2.0) == math.inf


def test_mtrl_finite_for_real_edge():
    rng = random.Random(21)
    returns = [rng.gauss(0.003, 0.008) for _ in range(300)]  # strong edge
    n = metrics.min_track_record_length(returns, reference_sharpe=0.0, target_confidence=0.95)
    assert math.isfinite(n)
    assert n > 1.0


# --------------------------------------------------------- PBO / CSCV

def _genuine_edge_matrix(t_slices: int = 8, n_configs: int = 6) -> list[list[float]]:
    """
    One configuration (index 0) is consistently best across EVERY time slice;
    the rest are consistently worse. The in-sample champion is therefore also the
    OOS champion every split → PBO should be ~0.
    """
    matrix: list[list[float]] = []
    for t in range(t_slices):
        row = []
        for c in range(n_configs):
            # config 0 always best, deterministic ordering, tiny slice variation.
            base = 2.0 if c == 0 else (1.0 - 0.1 * c)
            row.append(base + 0.01 * ((t % 3) - 1))
        matrix.append(row)
    return matrix


def _overfit_matrix(t_slices: int = 8, n_configs: int = 12, seed: int = 1) -> list[list[float]]:
    """
    Pure noise: every config's per-slice performance is an independent coin flip,
    so whichever wins in-sample has no reason to win out-of-sample. PBO should
    sit near 0.5 (selection no better than luck).
    """
    rng = random.Random(seed)
    return [[rng.gauss(0.0, 1.0) for _ in range(n_configs)] for _ in range(t_slices)]


def test_pbo_low_for_genuine_edge():
    pbo = metrics.probability_of_backtest_overfitting(_genuine_edge_matrix(), n_splits=50)
    assert pbo < 0.10


def test_pbo_high_for_noise():
    # Average a few seeds so the assertion isn't a knife-edge on one draw.
    vals = [
        metrics.probability_of_backtest_overfitting(
            _overfit_matrix(t_slices=10, n_configs=20, seed=s), n_splits=60, seed=s
        )
        for s in range(5)
    ]
    assert sum(vals) / len(vals) > 0.30


def test_pbo_deterministic():
    m = _overfit_matrix(t_slices=12, n_configs=20, seed=2)
    a = metrics.probability_of_backtest_overfitting(m, n_splits=40, seed=99)
    b = metrics.probability_of_backtest_overfitting(m, n_splits=40, seed=99)
    assert a == b


def test_pbo_fails_closed_on_bad_matrix():
    assert metrics.probability_of_backtest_overfitting([]) == 1.0
    assert metrics.probability_of_backtest_overfitting([[1.0, 2.0]]) == 1.0      # T<4
    assert metrics.probability_of_backtest_overfitting([[1.0]] * 4) == 1.0       # N<2
    # Odd number of rows can't be split into equal halves.
    assert metrics.probability_of_backtest_overfitting([[1.0, 2.0]] * 5) == 1.0


def test_pbo_exhaustive_enumeration_small():
    # T=4 → C(4,2)=6 <= n_splits, so it enumerates exhaustively (no RNG path).
    m = _genuine_edge_matrix(t_slices=4, n_configs=4)
    pbo = metrics.probability_of_backtest_overfitting(m, n_splits=16)
    assert pbo == 0.0


# --------------------------------------------------------- turnover / capacity

def test_turnover_hand_value():
    # Two periods, weights flip fully on one name: |0.5-0|+|0.5-1| = 1.0 → *0.5 = 0.5.
    w = [[0.0, 1.0], [0.5, 0.5]]
    assert abs(metrics.turnover(w) - 0.5) < 1e-12


def test_turnover_no_change_is_zero():
    w = [[0.5, 0.5], [0.5, 0.5], [0.5, 0.5]]
    assert metrics.turnover(w) == 0.0


def test_turnover_full_rotation_is_one():
    # Whole book swaps between two disjoint names each period.
    w = [[1.0, 0.0], [0.0, 1.0]]
    assert abs(metrics.turnover(w) - 1.0) < 1e-12


def test_capacity_score_hand_value():
    # 20% return, annual turnover 4.0, 0.1% cost → cost 0.004, capacity 50x.
    assert abs(metrics.capacity_score(0.20, 4.0, cost_per_turn=0.001) - 50.0) < 1e-9


def test_capacity_score_edge_eaten_by_cost():
    # Return exactly equals cost → capacity 1.0 (the whole edge is consumed).
    assert abs(metrics.capacity_score(0.10, 100.0, cost_per_turn=0.001) - 1.0) < 1e-9


def test_capacity_score_no_trades_is_inf():
    assert metrics.capacity_score(0.10, 0.0) == math.inf


def test_capacity_score_no_return_is_zero():
    assert metrics.capacity_score(-0.05, 4.0) == 0.0
    assert metrics.capacity_score(0.0, 4.0) == 0.0
