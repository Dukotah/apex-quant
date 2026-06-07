"""
apex.validation.deflated_sharpe
===============================
The Probabilistic Sharpe Ratio (PSR) and Deflated Sharpe Ratio (DSR) of
Bailey & Lopez de Prado.

A raw Sharpe ratio is a point estimate computed from a finite, noisy, and
often non-normal sample of returns. Two distortions inflate it:

  1. **Sampling noise + non-normality.** Short track records, negative skew,
     and fat tails make the observed Sharpe an unreliable estimate of the true
     one. The PSR gives the probability that the *true* Sharpe exceeds a
     benchmark, correcting for sample length, skew, and kurtosis.

  2. **Multiple testing / selection bias.** If you try N strategy
     configurations and report the best one, the best observed Sharpe is
     inflated purely by luck. The DSR deflates the benchmark to the maximum
     Sharpe you'd EXPECT from N independent trials with no real edge, then
     reports the PSR against that tougher bar.

Both return a probability in [0, 1]: higher = more confident the edge is real.

Deliberately dependency-light (stdlib math + statistics) so it runs anywhere,
including the free GitHub Actions runner. All functions are pure and
deterministic given their inputs. Tested in tests/test_deflated_sharpe.py
against hand-computed values.

References:
  - Bailey, D. & Lopez de Prado, M. (2012) "The Sharpe Ratio Efficient
    Frontier." Journal of Risk.
  - Bailey, D. & Lopez de Prado, M. (2014) "The Deflated Sharpe Ratio:
    Correcting for Selection Bias, Backtest Overfitting and Non-Normality."
    Journal of Portfolio Management.
"""

from __future__ import annotations

import math
import statistics
from typing import Sequence

# Euler-Mascheroni constant, used in the expected-maximum-Sharpe formula.
_EULER_MASCHERONI = 0.5772156649015329


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function (stdlib, no SciPy needed)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """
    Inverse standard normal CDF (quantile function).

    Uses Acklam's rational approximation, accurate to ~1.15e-9 absolute error
    over the open interval (0, 1). Pure stdlib so we avoid a SciPy dependency.
    """
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf

    # Coefficients for Acklam's algorithm.
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )

    p_low = 0.02425
    p_high = 1.0 - p_low

    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
    )


def _skewness(returns: Sequence[float], mean: float, sd: float) -> float:
    """Population (biased) skewness. Returns 0.0 if there's no dispersion."""
    if sd == 0:
        return 0.0
    n = len(returns)
    return sum(((r - mean) / sd) ** 3 for r in returns) / n


def _kurtosis(returns: Sequence[float], mean: float, sd: float) -> float:
    """
    Population (biased) kurtosis — the NON-excess form (normal == 3.0).

    The PSR formula uses raw kurtosis, where a Gaussian has kurtosis 3.
    Returns 3.0 (the Gaussian value) if there's no dispersion.
    """
    if sd == 0:
        return 3.0
    n = len(returns)
    return sum(((r - mean) / sd) ** 4 for r in returns) / n


def sample_sharpe(returns: Sequence[float]) -> float:
    """
    Non-annualized per-period Sharpe of a return series (mean / std).

    This is the raw, unannualized ratio the PSR/DSR machinery operates on.
    The annualization factor cancels out of the PSR, so we keep it per-period
    to stay aligned with Bailey & Lopez de Prado's derivation.

    Returns 0.0 if there's no variance or too few points.
    """
    if len(returns) < 2:
        return 0.0
    mean = statistics.fmean(returns)
    sd = statistics.pstdev(returns)
    if sd == 0:
        return 0.0
    return mean / sd


def probabilistic_sharpe_ratio(
    returns: Sequence[float],
    sr_benchmark: float = 0.0,
) -> float:
    """
    Probabilistic Sharpe Ratio (PSR).

    The probability that the strategy's TRUE (non-annualized) Sharpe ratio
    exceeds ``sr_benchmark``, given the observed sample, accounting for:
      - sample length (more data → tighter estimate),
      - skewness (negative skew makes a high Sharpe less trustworthy),
      - kurtosis (fat tails widen the estimate's uncertainty).

    Formula (Bailey & Lopez de Prado 2012):

        PSR = CDF( (SR_hat - SR*) * sqrt(n - 1) /
                   sqrt(1 - g3*SR_hat + (g4 - 1)/4 * SR_hat^2) )

    where SR_hat is the observed per-period Sharpe, SR* the benchmark, n the
    number of observations, g3 the skewness and g4 the (raw) kurtosis.

    Args:
        returns: per-period return series (fractions; 0.01 = +1%).
        sr_benchmark: the per-period Sharpe to test against (default 0.0,
            i.e. "is there any edge at all?"). Must be on the same per-period
            scale as the returns.

    Returns:
        A probability in [0, 1]. Returns 0.0 if there is too little data or no
        variance (fail-closed: we can't claim an edge we can't measure).
    """
    n = len(returns)
    if n < 2:
        return 0.0

    mean = statistics.fmean(returns)
    sd = statistics.pstdev(returns)
    if sd == 0:
        return 0.0

    sr_hat = mean / sd
    skew = _skewness(returns, mean, sd)
    kurt = _kurtosis(returns, mean, sd)

    # Standard error of the Sharpe estimate (Mertens / Bailey-LdP form).
    variance = 1.0 - skew * sr_hat + (kurt - 1.0) / 4.0 * sr_hat * sr_hat
    if variance <= 0:
        # Degenerate denominator; the estimate's uncertainty is undefined.
        # Fail closed rather than emit a garbage probability.
        return 0.0

    numerator = (sr_hat - sr_benchmark) * math.sqrt(n - 1)
    z = numerator / math.sqrt(variance)
    return _norm_cdf(z)


def expected_max_sharpe(
    num_trials: int,
    variance_of_trial_sharpes: float = 1.0,
) -> float:
    """
    Expected maximum Sharpe ratio across ``num_trials`` independent trials
    whose individual (per-period) Sharpes have zero mean and the given
    variance — i.e. the best Sharpe you'd expect to see from pure luck.

    E[max] ≈ sqrt(V) * ( (1-γ) * Z^-1(1 - 1/N) + γ * Z^-1(1 - 1/(N*e)) )

    where γ is the Euler-Mascheroni constant and Z^-1 the inverse normal CDF.
    This is the deflation benchmark used by the DSR.

    Args:
        num_trials: number of independent strategy configurations tried (>= 1).
        variance_of_trial_sharpes: variance of the trial Sharpe estimates. The
            default of 1.0 corresponds to standardized Sharpes; pass the
            measured cross-trial variance for a sharper benchmark.

    Returns:
        The expected maximum Sharpe under the null. 0.0 for a single trial
        (no selection bias to correct).
    """
    if num_trials <= 1:
        return 0.0
    if variance_of_trial_sharpes <= 0:
        return 0.0

    n = float(num_trials)
    term1 = (1.0 - _EULER_MASCHERONI) * _norm_ppf(1.0 - 1.0 / n)
    term2 = _EULER_MASCHERONI * _norm_ppf(1.0 - 1.0 / (n * math.e))
    return math.sqrt(variance_of_trial_sharpes) * (term1 + term2)


def deflated_sharpe_ratio(
    returns: Sequence[float],
    num_trials: int,
    variance_of_trial_sharpes: float = 1.0,
) -> float:
    """
    Deflated Sharpe Ratio (DSR).

    The PSR computed against a deflated benchmark: the expected maximum Sharpe
    you'd get from ``num_trials`` independent attempts with NO real edge. This
    corrects for selection bias / backtest overfitting on top of the PSR's
    non-normality and small-sample corrections.

    As ``num_trials`` grows, the deflation benchmark rises, so the DSR shrinks
    toward 0 — exactly the discipline you want: the more configurations you
    tried, the higher the observed Sharpe must be to remain credible.

    Args:
        returns: per-period return series of the SELECTED (best) strategy.
        num_trials: number of independent configurations tried before selecting
            this one. With 1 trial the DSR reduces to PSR vs 0.
        variance_of_trial_sharpes: variance of the per-period Sharpes across the
            trials. Default 1.0 (standardized); supply the measured variance for
            a tighter, fairer benchmark.

    Returns:
        A probability in [0, 1]. Higher = the edge survives the multiple-testing
        correction. 0.0 if there's too little data or no variance (fail-closed).
    """
    sr_star = expected_max_sharpe(num_trials, variance_of_trial_sharpes)
    return probabilistic_sharpe_ratio(returns, sr_benchmark=sr_star)
