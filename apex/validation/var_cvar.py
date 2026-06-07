"""
apex.validation.var_cvar
========================
Value-at-Risk (VaR) and Conditional VaR (CVaR / Expected Shortfall) for a series
of returns, computed two ways:

  1. Historical (non-parametric): read the loss quantile straight off the
     empirical distribution of realized returns. Makes no shape assumption, so
     it captures fat tails the strategy actually exhibited.
  2. Parametric (Gaussian): assume returns ~ Normal(mean, std) and derive the
     quantile analytically. Cheaper and smoother, but blind to fat tails.

Conventions follow the rest of the validation layer:
  - Pure functions, deterministic given inputs.
  - float math (statistical layer — matches metrics.py), stdlib only (math +
    statistics), so it runs on the free CI runner with no installs.
  - Insufficient-data windows return None rather than garbage (fail closed).

Sign convention
---------------
VaR and CVaR are reported as POSITIVE fractions representing a LOSS, mirroring
how max_drawdown is reported in metrics.py. A 95% VaR of 0.04 means: on the
worst 5% of periods, you expect to lose AT LEAST 4%. CVaR of 0.06 means the
AVERAGE loss within that worst 5% tail is 6%. A non-positive (gain) quantile is
clamped to 0.0 — "no expected loss at this confidence."

Tested in tests/test_var_cvar.py against hand-computed values.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class VarCvarResult:
    """VaR and CVaR (both positive-loss fractions) at a confidence level."""

    confidence: float  # e.g. 0.95
    historical_var: float  # empirical loss quantile
    historical_cvar: float  # mean loss beyond the historical VaR
    parametric_var: float  # Gaussian loss quantile
    parametric_cvar: float  # Gaussian expected shortfall
    observations: int  # number of returns the estimate is based on

    def summary(self) -> str:
        pct = self.confidence * 100.0
        return (
            f"VaR/CVaR @ {pct:.0f}% (n={self.observations}): "
            f"hist VaR {self.historical_var:.2%} / CVaR {self.historical_cvar:.2%}, "
            f"param VaR {self.parametric_var:.2%} / CVaR {self.parametric_cvar:.2%}"
        )


def _norm_ppf(p: float) -> float:
    """
    Inverse standard-normal CDF (quantile function) via the Acklam rational
    approximation. Accurate to ~1.15e-9 over the open interval, plenty for risk
    estimates and avoids a SciPy dependency. Defined on (0, 1).
    """
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf

    # Coefficients for the rational approximation.
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
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00)

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


def _norm_pdf(x: float) -> float:
    """Standard-normal probability density at x."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def historical_var(
    returns: Sequence[float],
    confidence: float = 0.95,
) -> Optional[float]:
    """
    Historical (empirical) Value-at-Risk as a positive loss fraction.

    The loss at the (1 - confidence) tail of the realized return distribution.
    Uses the lower-interpolated empirical quantile of the returns: returns are
    sorted ascending and we read off the alpha = (1 - confidence) quantile, then
    flip its sign so a loss is reported positive.

    Returns None for empty input or a confidence not in the open (0, 1) interval.
    A tail quantile that is actually a gain clamps to 0.0 (no expected loss).
    """
    if not 0.0 < confidence < 1.0:
        return None
    if len(returns) < 1:
        return None

    ordered = sorted(returns)
    alpha = 1.0 - confidence
    # Lower-bound index of the alpha quantile (conservative: floor toward worse).
    idx = int(math.floor(alpha * len(ordered)))
    if idx >= len(ordered):
        idx = len(ordered) - 1
    quantile_return = ordered[idx]
    loss = -quantile_return
    return loss if loss > 0.0 else 0.0


def historical_cvar(
    returns: Sequence[float],
    confidence: float = 0.95,
) -> Optional[float]:
    """
    Historical Conditional VaR (Expected Shortfall) as a positive loss fraction.

    The MEAN loss across the worst (1 - confidence) fraction of returns — i.e.
    the average of the tail, which captures how bad things get beyond the VaR
    threshold, not just where the threshold sits.

    The tail size is ceil(alpha * n) so at least one observation is always
    included. Returns None for empty input or confidence outside (0, 1).
    A tail whose average is a gain clamps to 0.0.
    """
    if not 0.0 < confidence < 1.0:
        return None
    if len(returns) < 1:
        return None

    ordered = sorted(returns)
    alpha = 1.0 - confidence
    tail_count = int(math.ceil(alpha * len(ordered)))
    if tail_count < 1:
        tail_count = 1
    tail = ordered[:tail_count]
    mean_tail_return = statistics.fmean(tail)
    loss = -mean_tail_return
    return loss if loss > 0.0 else 0.0


def parametric_var(
    returns: Sequence[float],
    confidence: float = 0.95,
) -> Optional[float]:
    """
    Parametric (Gaussian) Value-at-Risk as a positive loss fraction.

    Assumes returns ~ Normal(mu, sigma). The alpha = (1 - confidence) quantile is
    mu + sigma * z_alpha where z_alpha = Phi^-1(alpha) is negative, so the loss is
    -(mu + sigma * z_alpha) = sigma * z_confidence - mu, with z_confidence > 0.

    Needs at least 2 observations to estimate a (sample) standard deviation.
    Returns None for fewer than 2 points or confidence outside (0, 1).
    A negative (gain) quantile clamps to 0.0.
    """
    if not 0.0 < confidence < 1.0:
        return None
    if len(returns) < 2:
        return None

    mu = statistics.fmean(returns)
    sigma = statistics.stdev(returns)  # sample std (n-1), like a real estimate
    if sigma == 0.0:
        loss = -mu
        return loss if loss > 0.0 else 0.0

    alpha = 1.0 - confidence
    z_alpha = _norm_ppf(alpha)  # negative for alpha < 0.5
    quantile_return = mu + sigma * z_alpha
    loss = -quantile_return
    return loss if loss > 0.0 else 0.0


def parametric_cvar(
    returns: Sequence[float],
    confidence: float = 0.95,
) -> Optional[float]:
    """
    Parametric (Gaussian) Conditional VaR / Expected Shortfall, positive loss.

    For a Normal distribution the expected shortfall at the alpha tail has the
    closed form:

        ES = sigma * phi(z_alpha) / alpha - mu

    where alpha = 1 - confidence, z_alpha = Phi^-1(alpha) and phi is the normal
    pdf. This is the mean loss in the tail under the Gaussian assumption.

    Needs at least 2 observations. Returns None for fewer / bad confidence.
    A gain clamps to 0.0.
    """
    if not 0.0 < confidence < 1.0:
        return None
    if len(returns) < 2:
        return None

    mu = statistics.fmean(returns)
    sigma = statistics.stdev(returns)
    if sigma == 0.0:
        loss = -mu
        return loss if loss > 0.0 else 0.0

    alpha = 1.0 - confidence
    z_alpha = _norm_ppf(alpha)
    es = sigma * _norm_pdf(z_alpha) / alpha - mu
    return es if es > 0.0 else 0.0


def compute_var_cvar(
    returns: Sequence[float],
    confidence: float = 0.95,
) -> Optional[VarCvarResult]:
    """
    Bundle all four estimates into a single VarCvarResult at one confidence level.

    Returns None when there is not enough data for the parametric estimates
    (< 2 observations) or when confidence is outside (0, 1) — fail closed rather
    than emit a partial/garbage result.
    """
    if not 0.0 < confidence < 1.0:
        return None
    if len(returns) < 2:
        return None

    h_var = historical_var(returns, confidence)
    h_cvar = historical_cvar(returns, confidence)
    p_var = parametric_var(returns, confidence)
    p_cvar = parametric_cvar(returns, confidence)
    # All are guaranteed non-None here given the guards above, but be defensive.
    if h_var is None or h_cvar is None or p_var is None or p_cvar is None:
        return None

    return VarCvarResult(
        confidence=confidence,
        historical_var=h_var,
        historical_cvar=h_cvar,
        parametric_var=p_var,
        parametric_cvar=p_cvar,
        observations=len(returns),
    )
