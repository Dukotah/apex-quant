"""
apex.risk.erc_weights
=====================
Equal-Risk-Contribution (a.k.a. "risk parity") portfolio weights from a
covariance matrix, computed by deterministic fixed-point iteration.

In a long-only ERC portfolio every asset contributes the SAME amount to total
portfolio volatility. Contrast this with equal-DOLLAR weighting (1/N), where a
single volatile or highly-correlated asset can quietly dominate the book's
actual risk. ERC is the standard managed-futures / multi-strategy answer to
"don't let one bet drive the whole drawdown."

Definitions (for a long-only weight vector w, w_i >= 0, sum w = 1, and
covariance matrix Sigma):

    portfolio variance      sigma^2 = w' Sigma w
    marginal risk of i      MRC_i   = (Sigma w)_i
    risk contribution of i  RC_i    = w_i * MRC_i
                            sum_i RC_i = sigma^2

The ERC target is RC_i = sigma^2 / N for all i. We reach it with the classic
deterministic multiplicative fixed-point update

    w_i  <-  w_i * sqrt( target / RC_i )   then renormalise to sum 1,

iterated to convergence. The update is monotone toward the equal-contribution
fixed point and needs no randomness and no external solver — it is pure stdlib
arithmetic, so it runs anywhere (including the free CI runner).

Convention (golden rules): this is statistical / portfolio-math, analogous to
apex.validation.metrics and apex.risk.position_sizing's advisory layer, so it
works in FLOAT — it does NOT touch money, prices, or order quantities. It is
ADVISORY ONLY: the RiskManager remains the sole sizer/gatekeeper of real orders
(CLAUDE.md rules 2, 3). A caller may translate these weights into a per-signal
`strength`, but the hard caps still have the final word.

Design invariants:
  - Pure & deterministic: same inputs -> same outputs, no I/O, no wall clock,
    no randomness, no external dependency.
  - Fail closed: degenerate / insufficient / nonsensical input returns an empty
    list (no weights) rather than garbage or an exception for the normal
    "can't solve" case.
  - Output is always long-only and normalised to sum 1 (within tolerance) when
    a solution exists.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence

# Defaults for the fixed-point solver. Tight enough for portfolio work, cheap
# enough to converge in a handful of iterations for well-behaved covariances.
_DEFAULT_MAX_ITER = 10_000
_DEFAULT_TOL = 1e-10


def _is_finite_number(x: object) -> bool:
    """True if x is a real, finite int/float (rejects NaN/inf/bool-as-int-ok)."""
    if isinstance(x, bool):
        # bools are ints in Python; allow them but they are degenerate weights.
        return True
    if not isinstance(x, (int, float)):
        return False
    return math.isfinite(float(x))


def _validate_cov(cov: Sequence[Sequence[float]]) -> Optional[List[List[float]]]:
    """
    Coerce and validate a covariance matrix. Returns a clean NxN list-of-lists
    of floats, or None if the input is not a usable square matrix with finite
    entries and a strictly-positive diagonal.

    A non-positive diagonal entry means an asset with zero/negative variance —
    risk contributions are undefined, so we fail closed.
    """
    if cov is None:
        return None
    n = len(cov)
    if n == 0:
        return None
    clean: List[List[float]] = []
    for row in cov:
        if row is None or len(row) != n:
            return None
        clean_row: List[float] = []
        for val in row:
            if not _is_finite_number(val):
                return None
            clean_row.append(float(val))
        clean.append(clean_row)
    # Diagonal must be strictly positive (each asset has real variance).
    for i in range(n):
        if clean[i][i] <= 0.0:
            return None
    return clean


def _symmetrize(cov: List[List[float]]) -> List[List[float]]:
    """
    Return the symmetric part 0.5*(C + C'). Covariance matrices are symmetric in
    theory; tiny numerical asymmetries in an estimated matrix are smoothed here
    so the risk-contribution math is well defined and deterministic.
    """
    n = len(cov)
    return [
        [0.5 * (cov[i][j] + cov[j][i]) for j in range(n)]
        for i in range(n)
    ]


def _matvec(mat: List[List[float]], vec: Sequence[float]) -> List[float]:
    """Matrix-vector product mat @ vec for square mat and len-N vec."""
    return [sum(mat[i][j] * vec[j] for j in range(len(vec))) for i in range(len(mat))]


def risk_contributions(
    weights: Sequence[float],
    cov: Sequence[Sequence[float]],
) -> List[float]:
    """
    Per-asset risk contributions RC_i = w_i * (Sigma w)_i for the given weights.

    The contributions sum to the portfolio variance w' Sigma w. Useful both for
    callers that want to inspect concentration and for the tests that verify the
    ERC solution is actually equal-contribution.

    Returns an empty list if the covariance matrix is invalid or its size does
    not match the weight vector (fail closed).
    """
    clean = _validate_cov(cov)
    if clean is None or len(weights) != len(clean):
        return []
    for w in weights:
        if not _is_finite_number(w):
            return []
    sym = _symmetrize(clean)
    sigma_w = _matvec(sym, weights)
    return [float(weights[i]) * sigma_w[i] for i in range(len(weights))]


def erc_weights(
    cov: Sequence[Sequence[float]],
    *,
    max_iter: int = _DEFAULT_MAX_ITER,
    tol: float = _DEFAULT_TOL,
) -> List[float]:
    """
    Long-only Equal-Risk-Contribution weights for the given covariance matrix.

    Solves for w (w_i >= 0, sum w = 1) such that every asset's risk
    contribution RC_i = w_i * (Sigma w)_i is equal, via the deterministic
    multiplicative fixed-point iteration

        w_i <- w_i * sqrt( target / RC_i ),  then renormalise,

    starting from the 1/N portfolio. Iteration stops when the weights stop
    moving (max abs change < tol) or after `max_iter` sweeps.

    Special cases:
      - N == 1            -> [1.0].
      - diagonal Sigma    -> closed-form-equivalent w_i proportional to
                             1/sigma_i (inverse-volatility weighting); the
                             iteration reproduces it.

    Returns the weight list (length N, summing to 1.0 within tolerance), or an
    EMPTY list if the covariance matrix is not a usable square matrix with a
    strictly-positive diagonal (fail closed — no weights rather than garbage).
    """
    clean = _validate_cov(cov)
    if clean is None:
        return []
    if max_iter < 1 or not math.isfinite(tol) or tol <= 0.0:
        return []

    n = len(clean)
    if n == 1:
        return [1.0]

    sym = _symmetrize(clean)

    # Start from the equal-weight (1/N) portfolio — a deterministic seed.
    w = [1.0 / n] * n

    for _ in range(max_iter):
        sigma_w = _matvec(sym, w)
        # Risk contributions; portfolio variance is their sum.
        rc = [w[i] * sigma_w[i] for i in range(n)]
        port_var = sum(rc)
        if port_var <= 0.0 or not math.isfinite(port_var):
            # Degenerate covariance (e.g. perfectly offsetting) — fail closed.
            return []
        target = port_var / n

        new_w: List[float] = []
        for i in range(n):
            rc_i = rc[i]
            if rc_i <= 0.0 or not math.isfinite(rc_i):
                # An asset with non-positive RC means the multiplicative step is
                # undefined; the input is not solvable as a long-only ERC book.
                return []
            new_w.append(w[i] * math.sqrt(target / rc_i))

        total = sum(new_w)
        if total <= 0.0 or not math.isfinite(total):
            return []
        new_w = [x / total for x in new_w]

        max_change = max(abs(new_w[i] - w[i]) for i in range(n))
        w = new_w
        if max_change < tol:
            break

    # Final renormalisation guard against accumulated float drift.
    total = sum(w)
    if total <= 0.0 or not math.isfinite(total):
        return []
    return [x / total for x in w]


def inverse_variance_weights(cov: Sequence[Sequence[float]]) -> List[float]:
    """
    Inverse-VARIANCE weights w_i proportional to 1/Sigma_ii, normalised to sum
    1. This is the naive risk-parity proxy that ignores correlations entirely;
    it equals the true ERC solution only when assets are uncorrelated. Provided
    as a cheap baseline / sanity reference for callers and tests.

    Returns an empty list on an invalid covariance matrix (fail closed).
    """
    clean = _validate_cov(cov)
    if clean is None:
        return []
    n = len(clean)
    inv = [1.0 / clean[i][i] for i in range(n)]  # diagonal is positive (validated)
    total = sum(inv)
    if total <= 0.0 or not math.isfinite(total):
        return []
    return [x / total for x in inv]
