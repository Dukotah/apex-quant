"""
apex.analytics.contribution_analysis
====================================
Decompose a portfolio's return into per-asset contributions, answering the
question "where did the return actually come from?"

For a single period the arithmetic is exact and well known: the portfolio return
is the weight-dot-return sum, ``r_p = sum_i w_i * r_i``, so asset ``i`` contributed
exactly ``w_i * r_i``. The interesting (and subtle) part is multi-period: simple
period contributions do NOT add up to the compounded total return, because the
total return is geometric, not arithmetic. We use the Cariño smoothing-coefficient
linking algorithm so that the summed contributions reconcile EXACTLY to the
portfolio's geometric total return.

This is analytics/metric code, not money-movement code: it lives in the same
statistical layer as ``apex.validation.metrics`` and ``apex.analytics.equity_curve``
and follows that layer's convention of using ``float``. Exact position/cash
bookkeeping lives in ``apex.risk.portfolio`` and uses Decimal — a different layer.

All functions are pure and deterministic given their inputs. They degrade
gracefully on insufficient or malformed data (empty inputs, length mismatches,
zero-return edge cases) by returning empty/None rather than garbage, and they
never perform I/O. Tested in tests/test_contribution_analysis.py against
hand-computed values.
"""

from __future__ import annotations

import math
from typing import Dict, Mapping, Sequence


def single_period_contributions(
    weights: Mapping[str, float],
    returns: Mapping[str, float],
) -> Dict[str, float]:
    """
    Per-asset contribution to portfolio return for ONE period.

    Contribution of asset ``i`` is ``weight_i * return_i``. The sum of all
    contributions equals the portfolio return for that period
    (``portfolio_return`` below computes the same total).

    Only assets present in ``weights`` are included; an asset missing from
    ``returns`` is treated as a 0.0 return (it held a weight but did not move).
    Returns an empty dict if ``weights`` is empty.
    """
    out: Dict[str, float] = {}
    for asset, w in weights.items():
        r = float(returns.get(asset, 0.0))
        out[asset] = float(w) * r
    return out


def portfolio_return(
    weights: Mapping[str, float],
    returns: Mapping[str, float],
) -> float:
    """
    Single-period portfolio return = ``sum_i weight_i * return_i``.

    This is exactly the sum of :func:`single_period_contributions`. Returns
    0.0 for empty weights.
    """
    return math.fsum(single_period_contributions(weights, returns).values())


def _carino_coefficient(period_returns: Sequence[float]) -> list[float]:
    """
    Cariño smoothing coefficients ``k_t`` for each period and the overall ``k``.

    The Cariño linking algorithm scales each period's arithmetic contributions
    by ``k_t / k`` so that, after summing across periods, the per-asset totals
    reconcile EXACTLY to the geometric total return.

    For a period with return ``r_t`` the local coefficient is
    ``k_t = ln(1 + r_t) / r_t`` (and ``-> 1`` as ``r_t -> 0``). The overall
    coefficient is ``k = ln(1 + R) / R`` where ``R`` is the compounded total
    return. Returns the list of per-period ``k_t / k`` scale factors.

    Both ``k_t`` and ``k`` have a removable singularity at a zero return whose
    limit is 1.0 (``ln(1 + x) / x -> 1`` as ``x -> 0``), so a total return of
    exactly 0 uses ``k = 1.0`` rather than dividing by zero. Per-period scaling
    is still applied, which keeps the contributions reconciling to the geometric
    total even in that degenerate case.
    """
    total_growth = 1.0
    for r in period_returns:
        total_growth *= 1.0 + r
    total_return = total_growth - 1.0

    # ln(1 + R) / R, taking the x -> 0 limit of 1.0 to avoid the singularity.
    k = 1.0 if total_return == 0.0 else math.log1p(total_return) / total_return

    scales: list[float] = []
    for r in period_returns:
        if r == 0.0:
            k_t = 1.0
        else:
            k_t = math.log1p(r) / r
        scales.append(k_t / k)
    return scales


def multi_period_contributions(
    weights_by_period: Sequence[Mapping[str, float]],
    returns_by_period: Sequence[Mapping[str, float]],
) -> Dict[str, float]:
    """
    Per-asset contribution to the COMPOUNDED total portfolio return, linked with
    the Cariño algorithm so the contributions sum exactly to the geometric total.

    ``weights_by_period[t]`` and ``returns_by_period[t]`` describe period ``t``.
    The two sequences must be the same length; if they are not, or either is
    empty, an empty dict is returned (insufficient/malformed data, fail closed).

    The returned dict maps each asset that appeared in any period to its total
    contribution. Summing the values reproduces
    ``geometric_total_return(...)`` (to floating-point precision).
    """
    n = len(weights_by_period)
    if n == 0 or n != len(returns_by_period):
        return {}

    period_returns = [
        portfolio_return(weights_by_period[t], returns_by_period[t]) for t in range(n)
    ]
    scales = _carino_coefficient(period_returns)

    totals: Dict[str, float] = {}
    for t in range(n):
        contribs = single_period_contributions(weights_by_period[t], returns_by_period[t])
        scale = scales[t]
        for asset, c in contribs.items():
            totals[asset] = totals.get(asset, 0.0) + c * scale
    return totals


def geometric_total_return(
    weights_by_period: Sequence[Mapping[str, float]],
    returns_by_period: Sequence[Mapping[str, float]],
) -> float:
    """
    Compounded total portfolio return across all periods.

    ``R = prod_t (1 + r_p_t) - 1`` where ``r_p_t`` is the period portfolio
    return. Returns 0.0 for empty/mismatched input. This is the figure the
    per-asset totals from :func:`multi_period_contributions` reconcile to.
    """
    n = len(weights_by_period)
    if n == 0 or n != len(returns_by_period):
        return 0.0
    growth = 1.0
    for t in range(n):
        growth *= 1.0 + portfolio_return(weights_by_period[t], returns_by_period[t])
    return growth - 1.0


def contribution_fractions(contributions: Mapping[str, float]) -> Dict[str, float]:
    """
    Normalize a contributions dict into fractions of the total contribution.

    Each value becomes ``contribution_i / sum(contributions)``, so the result
    sums to 1.0 (e.g. 0.6 means that asset drove 60% of the realized return).

    Returns an empty dict if ``contributions`` is empty, and returns all-zero
    fractions when the total contribution is exactly 0.0 (undefined share —
    fail closed rather than divide by zero).
    """
    if not contributions:
        return {}
    total = math.fsum(contributions.values())
    if total == 0.0:
        return {asset: 0.0 for asset in contributions}
    return {asset: c / total for asset, c in contributions.items()}
