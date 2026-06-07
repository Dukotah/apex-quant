"""
Tests for apex.risk.erc_weights — the Equal-Risk-Contribution solver.

The defining property is checked directly: at the solution every asset's risk
contribution RC_i = w_i * (Sigma w)_i is equal. We also pin a couple of
closed-form cases (uncorrelated => inverse-vol; single asset; 2x2 with known
correlation) against hand-computed values.
"""
from __future__ import annotations

from apex.risk.erc_weights import (
    erc_weights,
    inverse_variance_weights,
    risk_contributions,
)


def _approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


# --------------------------------------------------------------------------
# Core ERC property
# --------------------------------------------------------------------------

def test_single_asset():
    assert erc_weights([[0.04]]) == [1.0]


def test_uncorrelated_equals_inverse_vol():
    # Diagonal covariance: variances 0.01, 0.04 -> vols 0.1, 0.2.
    # ERC == inverse-VOLATILITY: w ∝ 1/sigma = (10, 5) -> normalise -> (2/3, 1/3).
    cov = [[0.01, 0.0], [0.0, 0.04]]
    w = erc_weights(cov)
    assert _approx(w[0], 2.0 / 3.0)
    assert _approx(w[1], 1.0 / 3.0)
    assert _approx(sum(w), 1.0)


def test_equal_variances_uncorrelated_is_equal_weight():
    cov = [[0.02, 0.0], [0.0, 0.02]]
    w = erc_weights(cov)
    assert _approx(w[0], 0.5)
    assert _approx(w[1], 0.5)


def test_risk_contributions_are_equal_2x2_correlated():
    # Two assets, equal variance, positive correlation 0.5.
    cov = [[0.04, 0.02], [0.02, 0.04]]
    w = erc_weights(cov)
    # Symmetric problem -> equal weights, and equal risk contributions.
    assert _approx(w[0], 0.5)
    assert _approx(w[1], 0.5)
    rc = risk_contributions(w, cov)
    assert _approx(rc[0], rc[1])


def test_risk_contributions_equal_3x3():
    # Asymmetric covariance: different variances + correlations. The solver must
    # still equalise risk contributions even though weights differ.
    cov = [
        [0.10, 0.02, 0.01],
        [0.02, 0.05, 0.015],
        [0.01, 0.015, 0.02],
    ]
    w = erc_weights(cov)
    assert len(w) == 3
    assert _approx(sum(w), 1.0)
    assert all(x > 0.0 for x in w)
    rc = risk_contributions(w, cov)
    # All three contributions equal to within solver tolerance.
    assert _approx(rc[0], rc[1], tol=1e-5)
    assert _approx(rc[1], rc[2], tol=1e-5)
    # Lower-variance asset (index 2) should carry the LARGEST weight.
    assert w[2] > w[0]
    assert w[2] > w[1]


def test_rc_sum_equals_portfolio_variance():
    cov = [[0.04, 0.01], [0.01, 0.09]]
    w = erc_weights(cov)
    rc = risk_contributions(w, cov)
    # sum(RC) == w' Sigma w
    sigma_w = [
        cov[0][0] * w[0] + cov[0][1] * w[1],
        cov[1][0] * w[0] + cov[1][1] * w[1],
    ]
    port_var = w[0] * sigma_w[0] + w[1] * sigma_w[1]
    assert _approx(sum(rc), port_var)


def test_asymmetric_input_is_symmetrized():
    # Slightly non-symmetric estimate; result should match its symmetric part
    # and still be a valid ERC solution (equal contributions).
    cov = [[0.04, 0.025], [0.015, 0.04]]  # off-diagonals differ
    w = erc_weights(cov)
    assert _approx(sum(w), 1.0)
    rc = risk_contributions(w, cov)
    assert _approx(rc[0], rc[1], tol=1e-5)


# --------------------------------------------------------------------------
# inverse_variance_weights baseline
# --------------------------------------------------------------------------

def test_inverse_variance_weights():
    # 1/var = (1/0.01, 1/0.04) = (100, 25) -> normalise -> (0.8, 0.2).
    cov = [[0.01, 0.0], [0.0, 0.04]]
    w = inverse_variance_weights(cov)
    assert _approx(w[0], 0.8)
    assert _approx(w[1], 0.2)


def test_inverse_variance_differs_from_erc():
    # Inverse-VARIANCE != inverse-VOLATILITY for unequal vols.
    cov = [[0.01, 0.0], [0.0, 0.04]]
    iv = inverse_variance_weights(cov)
    erc = erc_weights(cov)
    assert not _approx(iv[0], erc[0])


# --------------------------------------------------------------------------
# Fail-closed / edge cases
# --------------------------------------------------------------------------

def test_empty_matrix_fails_closed():
    assert erc_weights([]) == []
    assert inverse_variance_weights([]) == []


def test_non_square_fails_closed():
    assert erc_weights([[0.04, 0.01]]) == []          # 1x2
    assert erc_weights([[0.04, 0.01], [0.01]]) == []  # ragged


def test_non_positive_diagonal_fails_closed():
    assert erc_weights([[0.0, 0.0], [0.0, 0.04]]) == []
    assert erc_weights([[-0.04, 0.0], [0.0, 0.04]]) == []


def test_non_finite_entry_fails_closed():
    assert erc_weights([[float("nan"), 0.0], [0.0, 0.04]]) == []
    assert erc_weights([[float("inf"), 0.0], [0.0, 0.04]]) == []


def test_none_input_fails_closed():
    assert erc_weights(None) == []
    assert inverse_variance_weights(None) == []


def test_bad_solver_params_fail_closed():
    cov = [[0.04, 0.0], [0.0, 0.04]]
    assert erc_weights(cov, max_iter=0) == []
    assert erc_weights(cov, tol=0.0) == []
    assert erc_weights(cov, tol=-1.0) == []
    assert erc_weights(cov, tol=float("inf")) == []


def test_risk_contributions_invalid_inputs():
    assert risk_contributions([0.5, 0.5], []) == []          # bad cov
    assert risk_contributions([0.5], [[0.04, 0.0], [0.0, 0.04]]) == []  # size mismatch
    assert risk_contributions([float("nan"), 0.5], [[0.04, 0.0], [0.0, 0.04]]) == []


def test_weights_are_nonnegative():
    cov = [
        [0.09, 0.01, 0.00],
        [0.01, 0.04, 0.02],
        [0.00, 0.02, 0.16],
    ]
    w = erc_weights(cov)
    assert all(x >= 0.0 for x in w)
    assert _approx(sum(w), 1.0)


def test_determinism():
    cov = [[0.05, 0.01, 0.0], [0.01, 0.03, 0.005], [0.0, 0.005, 0.02]]
    assert erc_weights(cov) == erc_weights(cov)
