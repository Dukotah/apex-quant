"""
apex.validation.overfitting
===========================
Defenses against the single most dangerous lie in quant: a backtest that looks
good because we TRIED MANY THINGS, not because the edge is real. This module adds
the multiple-testing / non-normality corrections from the López de Prado & Bailey
literature, computed purely from a return series plus a count of how many strategy
variants were tried.

Three tools, all pure and deterministic (stdlib only — runs on the free CI runner):

  • Probabilistic Sharpe Ratio (PSR) — Bailey & López de Prado (2012). The
    probability the TRUE Sharpe exceeds a benchmark, correcting the observed Sharpe
    for sample length, skewness, and kurtosis. A fat-tailed, negatively-skewed,
    short-history Sharpe of 1.0 is worth far less than a long, well-behaved one.

  • Deflated Sharpe Ratio (DSR) — Bailey & López de Prado (2014). PSR with the
    benchmark replaced by the EXPECTED MAXIMUM Sharpe under the null given N trials.
    Searching 100 variants and keeping the best inflates Sharpe by chance; DSR
    deflates it back. DSR > 0.95 ≈ "the winner is not just the luckiest loser."

  • Minimum Track Record Length (MinTRL) — the minimum number of observations
    needed before an observed Sharpe is statistically distinguishable from the
    benchmark at a confidence level, given its skew/kurtosis. If your history is
    shorter than this, you cannot yet claim the edge — full stop.

CONVENTION: PSR/DSR/MinTRL operate on the PER-PERIOD (non-annualized) Sharpe ratio,
which is what the closed-form distributions assume. Helpers convert a raw return
series (and annualized trial Sharpes) into the per-period quantities internally.
References: Bailey & López de Prado, "The Sharpe Ratio Efficient Frontier" (2012);
"The Deflated Sharpe Ratio" (2014).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist, fmean, pvariance
from typing import Sequence

_NORMAL = NormalDist()
_EULER_MASCHERONI = 0.5772156649015329

# DSR/PSR confidence that the true Sharpe clears the bar (literature standard).
DSR_FLOOR = 0.95
# Confidence level for the Minimum Track Record Length.
MINTRL_CONFIDENCE = 0.95


# --------------------------------------------------------------------- moments


def return_moments(returns: Sequence[float]) -> tuple[float, float, float, float]:
    """(mean, std, skewness, kurtosis) of a return series. Kurtosis is NON-excess
    (a normal distribution gives 3). Population moments — deterministic, no SciPy."""
    n = len(returns)
    if n == 0:
        return 0.0, 0.0, 0.0, 3.0
    mean = fmean(returns)
    m2 = fmean([(r - mean) ** 2 for r in returns])
    sd = math.sqrt(m2)
    if sd == 0:
        return mean, 0.0, 0.0, 3.0
    m3 = fmean([(r - mean) ** 3 for r in returns])
    m4 = fmean([(r - mean) ** 4 for r in returns])
    skew = m3 / sd**3
    kurt = m4 / sd**4
    return mean, sd, skew, kurt


def per_period_sharpe(returns: Sequence[float]) -> float:
    """Non-annualized Sharpe (mean / std of returns). 0 if no variance."""
    mean, sd, _, _ = return_moments(returns)
    return 0.0 if sd == 0 else mean / sd


# --------------------------------------------------------------------- core formulas


def probabilistic_sharpe_ratio(
    observed_sr: float,
    n_obs: int,
    skew: float,
    kurtosis: float,
    benchmark_sr: float = 0.0,
) -> float:
    """PSR: P(true per-period Sharpe > benchmark_sr). All Sharpes are PER-PERIOD.

    Returns 0.0 when there isn't enough data to say anything (n_obs < 2).
    """
    if n_obs < 2:
        return 0.0
    # Standard error of the Sharpe estimator under non-normality (Bailey & LdP).
    variance = 1.0 - skew * observed_sr + ((kurtosis - 1.0) / 4.0) * observed_sr**2
    denom = math.sqrt(max(variance, 1e-12))
    z = (observed_sr - benchmark_sr) * math.sqrt(n_obs - 1) / denom
    return _NORMAL.cdf(z)


def expected_max_sharpe(sr_variance_across_trials: float, n_trials: int) -> float:
    """Expected MAXIMUM per-period Sharpe under the null (true edge = 0) after
    N independent trials — the bar a lucky winner clears by chance alone.

    Extreme-value estimate (Bailey & López de Prado 2014). Returns 0.0 for a single
    trial or zero cross-trial variance (then DSR degenerates to PSR vs 0).
    """
    if n_trials < 2 or sr_variance_across_trials <= 0:
        return 0.0
    g = _EULER_MASCHERONI
    a = _NORMAL.inv_cdf(1.0 - 1.0 / n_trials)
    b = _NORMAL.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(sr_variance_across_trials) * ((1.0 - g) * a + g * b)


def deflated_sharpe_ratio(
    observed_sr: float,
    n_obs: int,
    skew: float,
    kurtosis: float,
    sr_variance_across_trials: float,
    n_trials: int,
) -> float:
    """DSR: PSR measured against the expected-max-Sharpe-under-null for N trials.
    All Sharpes per-period. Collapses to PSR(benchmark=0) when n_trials < 2."""
    sr0 = expected_max_sharpe(sr_variance_across_trials, n_trials)
    return probabilistic_sharpe_ratio(observed_sr, n_obs, skew, kurtosis, benchmark_sr=sr0)


def min_track_record_length(
    observed_sr: float,
    skew: float,
    kurtosis: float,
    benchmark_sr: float = 0.0,
    confidence: float = MINTRL_CONFIDENCE,
) -> float:
    """Minimum observations to distinguish observed_sr from benchmark_sr at
    `confidence`, given skew/kurtosis. Per-period Sharpes. ``inf`` if the observed
    Sharpe does not exceed the benchmark (you can never prove a non-edge)."""
    if observed_sr <= benchmark_sr:
        return math.inf
    z = _NORMAL.inv_cdf(confidence)
    variance = 1.0 - skew * observed_sr + ((kurtosis - 1.0) / 4.0) * observed_sr**2
    return 1.0 + variance * (z / (observed_sr - benchmark_sr)) ** 2


# --------------------------------------------------------------------- assessment


@dataclass(frozen=True)
class OverfittingResult:
    """The overfitting read on a strategy's returns, given how many variants were tried."""

    observed_sharpe_annual: float
    psr: float  # P(true Sharpe > 0), non-normality-corrected
    dsr: float  # PSR vs expected-max-under-null for n_trials (== psr if n_trials<2)
    n_trials: int
    n_observations: int
    min_track_record_length: float  # in observations (e.g. trading days)
    skew: float
    kurtosis: float
    passed: bool  # DSR clears the floor AND history is long enough (>= MinTRL)

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "WEAK"
        mtl = (
            "inf"
            if math.isinf(self.min_track_record_length)
            else f"{self.min_track_record_length:.0f}"
        )
        return (
            f"Overfitting [{verdict}]: DSR {self.dsr:.2f} (N_trials {self.n_trials}), "
            f"PSR {self.psr:.2f}, MinTRL {mtl} vs {self.n_observations} obs"
        )


def assess(
    full_returns: Sequence[float],
    trial_sharpes_annual: Sequence[float] | None = None,
    *,
    periods_per_year: int = 252,
    dsr_floor: float = DSR_FLOOR,
    confidence: float = MINTRL_CONFIDENCE,
) -> OverfittingResult:
    """Assess overfitting risk from a return series and the Sharpes of the variants tried.

    Args:
        full_returns: the strategy's period-over-period returns (e.g. daily).
        trial_sharpes_annual: ANNUALIZED Sharpe of every variant tried during the
            search (including the chosen one). 2+ entries enable the deflation; fewer
            (or None) make DSR collapse to PSR. Reusing the Gate-6 parameter sweep
            here is the natural source of the trial count.
        periods_per_year: annualization factor for converting Sharpes (252 = daily).

    Passes when DSR >= ``dsr_floor`` AND the history is at least MinTRL long.
    """
    n = len(full_returns)
    _, sd, skew, kurt = return_moments(full_returns)
    sr_pp = 0.0 if sd == 0 else per_period_sharpe(full_returns)
    ann = math.sqrt(periods_per_year)
    observed_sharpe_annual = sr_pp * ann

    trials = list(trial_sharpes_annual or [])
    n_trials = max(1, len(trials))
    psr = probabilistic_sharpe_ratio(sr_pp, n, skew, kurt, benchmark_sr=0.0)
    if n_trials >= 2:
        trial_sr_pp = [s / ann for s in trials]
        sr_var = pvariance(trial_sr_pp)
        dsr = deflated_sharpe_ratio(sr_pp, n, skew, kurt, sr_var, n_trials)
    else:
        dsr = psr

    mtl = min_track_record_length(sr_pp, skew, kurt, benchmark_sr=0.0, confidence=confidence)
    enough_history = n >= mtl
    passed = dsr >= dsr_floor and enough_history

    return OverfittingResult(
        observed_sharpe_annual=observed_sharpe_annual,
        psr=psr,
        dsr=dsr,
        n_trials=n_trials,
        n_observations=n,
        min_track_record_length=mtl,
        skew=skew,
        kurtosis=kurt,
        passed=passed,
    )
