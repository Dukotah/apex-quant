"""
apex.validation.turnover_metrics
================================
Portfolio turnover and the implied holding period it implies, computed from a
sequence of portfolio weight vectors (one vector per rebalance / period).

Why this matters: two strategies with the same Sharpe are NOT equal if one
churns the book every day and the other rebalances quarterly. Turnover is the
hidden tax — every unit of it pays commission and slippage. A strategy that
looks great gross can be a loser net once you account for how hard it trades.
The implied holding period (the average time a dollar of capital stays put) is
the intuitive companion number: high turnover <=> short holding period.

Conventions (mirrors the rest of this layer):
  * A "weight vector" is a mapping {asset -> weight} OR a positional sequence of
    weights. Weights are fractions of the portfolio (0.25 = 25%); they may be
    negative (shorts) and need not sum to 1 (cash is the implicit remainder).
  * One-way turnover for a single rebalance is the classic
        0.5 * sum_i |w_t[i] - w_{t-1}[i]|
    i.e. the fraction of the portfolio that changed hands (buys == sells when
    fully invested, so we halve the L1 distance). An asset missing from one side
    is treated as weight 0 on that side.
  * average_turnover is the mean one-way turnover PER PERIOD.
  * Annualized turnover scales by periods_per_year.
  * Implied holding period = 1 / average_turnover, expressed in PERIODS. With
    turnover 0 (a buy-and-hold book that never trades) the holding period is
    infinite -> we return math.inf, which is the honest answer.

This is metric/statistical code, so it follows the float convention of
apex/validation/metrics.py (NOT Decimal). Pure, deterministic, stdlib-only.
Tested in tests/test_turnover_metrics.py against hand-computed values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence, Union

TRADING_DAYS_PER_YEAR = 252

# A single period's portfolio weights: either keyed by asset, or positional.
WeightVector = Union[Mapping[str, float], Sequence[float]]


def _as_mapping(weights: WeightVector) -> dict[str, float]:
    """Normalize a weight vector into a {asset -> weight} dict.

    A positional sequence is keyed by its integer index (as a string) so two
    positional vectors align element-wise. A mapping is copied as-is.
    """
    if isinstance(weights, Mapping):
        return {str(k): float(v) for k, v in weights.items()}
    return {str(i): float(v) for i, v in enumerate(weights)}


def one_way_turnover(prev: WeightVector, curr: WeightVector) -> float:
    """One-way turnover between two consecutive weight vectors.

        0.5 * sum_i |curr[i] - prev[i]|

    Assets present in only one vector are treated as weight 0 in the other.
    This is the fraction of the portfolio that was traded at this rebalance
    (0.0 = nothing changed, 1.0 = the whole long book was replaced).
    """
    p = _as_mapping(prev)
    c = _as_mapping(curr)
    keys = set(p) | set(c)
    l1 = sum(abs(c.get(k, 0.0) - p.get(k, 0.0)) for k in keys)
    return 0.5 * l1


def turnover_series(weight_vectors: Sequence[WeightVector]) -> list[float]:
    """Per-rebalance one-way turnover across a sequence of weight vectors.

    For N weight vectors there are N-1 transitions, so the returned list has
    length max(len - 1, 0). With fewer than two vectors there are no
    transitions and we return an empty list (insufficient data, never garbage).
    """
    if len(weight_vectors) < 2:
        return []
    return [
        one_way_turnover(weight_vectors[i - 1], weight_vectors[i])
        for i in range(1, len(weight_vectors))
    ]


def average_turnover(weight_vectors: Sequence[WeightVector]) -> float:
    """Mean one-way turnover PER PERIOD over the sequence.

    Returns 0.0 when there are no transitions (insufficient data).
    """
    series = turnover_series(weight_vectors)
    if not series:
        return 0.0
    return sum(series) / len(series)


def annualized_turnover(
    weight_vectors: Sequence[WeightVector],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Average per-period turnover scaled to a yearly figure.

    e.g. average daily turnover of 0.10 with 252 trading days/year => 25.2.
    A value of 1.0 means the portfolio is, on average, fully replaced once
    per year.
    """
    return average_turnover(weight_vectors) * periods_per_year


def implied_holding_period(weight_vectors: Sequence[WeightVector]) -> float:
    """Average holding period implied by turnover, expressed in PERIODS.

    holding_period = 1 / average_turnover

    Intuition: if you replace 1/4 of the book each period, the average dollar
    stays roughly 4 periods. Turnover of 0 (never trades) => math.inf. With
    insufficient data (fewer than two weight vectors) we also return math.inf,
    since no trading has been observed.
    """
    avg = average_turnover(weight_vectors)
    if avg <= 0.0:
        return math.inf
    return 1.0 / avg


@dataclass(frozen=True)
class TurnoverReport:
    """Summary of a strategy's trading intensity over a weight-vector history."""

    n_periods: int  # number of rebalance transitions observed
    average_turnover: float  # mean one-way turnover per period
    annualized_turnover: float  # scaled to periods_per_year
    implied_holding_period: float  # 1 / average_turnover, in periods
    max_turnover: float  # busiest single rebalance
    min_turnover: float  # quietest single rebalance

    def summary(self) -> str:
        hp = (
            "inf"
            if math.isinf(self.implied_holding_period)
            else f"{self.implied_holding_period:.1f}"
        )
        return (
            f"Turnover: avg {self.average_turnover:.1%}/period, "
            f"{self.annualized_turnover:.2f}x/year, "
            f"holding period ~{hp} periods "
            f"(over {self.n_periods} rebalances)"
        )


def turnover_report(
    weight_vectors: Sequence[WeightVector],
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> TurnoverReport:
    """Build a full TurnoverReport from a sequence of weight vectors.

    With fewer than two weight vectors there is no observed trading, so every
    turnover figure is 0.0 and the implied holding period is math.inf.
    """
    series = turnover_series(weight_vectors)
    if not series:
        return TurnoverReport(
            n_periods=0,
            average_turnover=0.0,
            annualized_turnover=0.0,
            implied_holding_period=math.inf,
            max_turnover=0.0,
            min_turnover=0.0,
        )
    avg = sum(series) / len(series)
    return TurnoverReport(
        n_periods=len(series),
        average_turnover=avg,
        annualized_turnover=avg * periods_per_year,
        implied_holding_period=(1.0 / avg if avg > 0.0 else math.inf),
        max_turnover=max(series),
        min_turnover=min(series),
    )
