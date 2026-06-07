"""
apex.validation.information_coefficient
=======================================
The Information Coefficient (IC): how well a signal predicts future returns.

A trading signal (an indicator value, a model score, a conviction number) is only
worth acting on if it lines up with what the market does NEXT. The IC measures
that linkage as a correlation between the signal at time t and the forward return
realized over the following period(s).

Two flavors:
  * Pearson IC  — linear correlation between signal and forward return. Sensitive
    to outliers and to the signal's scale.
  * Rank IC (Spearman) — correlation of the RANKS instead of the raw values. This
    is the one quants quote: it only asks "did higher signals tend to earn higher
    returns?" and is robust to outliers and non-linear-but-monotonic edges.

A small but persistent IC (think 0.03-0.10 daily) is already a real, tradable
edge. A high IC is rare and usually a sign of look-ahead leakage.

Deliberately dependency-light (stdlib math + statistics) so it runs anywhere,
including the free GitHub Actions runner. Pure and deterministic given inputs;
insufficient-data windows return None rather than garbage (fail closed). This is
statistical/indicator code, so it follows the validation layer's float convention.

Tested in tests/test_information_coefficient.py against hand-computed values.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Sequence


def forward_returns(prices: Sequence[float], horizon: int = 1) -> list[float]:
    """
    Compute forward returns over `horizon` periods for each price.

    forward_return[t] = price[t + horizon] / price[t] - 1

    The result has len(prices) - horizon entries (the last `horizon` prices have
    no future to look at). Returns an empty list if there isn't enough data or the
    horizon is non-positive. A zero base price yields 0.0 for that entry (we can't
    form a ratio, so we refuse to invent one).
    """
    if horizon < 1 or len(prices) <= horizon:
        return []
    out: list[float] = []
    for t in range(len(prices) - horizon):
        base = prices[t]
        if base == 0:
            out.append(0.0)
        else:
            out.append(prices[t + horizon] / base - 1.0)
    return out


def _rank(values: Sequence[float]) -> list[float]:
    """
    Fractional (average) ranks of `values`. Ties share the mean of the ranks they
    would occupy, which is what Spearman's rank correlation requires to stay exact.
    Ranks are 1-based (smallest value -> rank 1.0).
    """
    indexed = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    n = len(values)
    while i < n:
        j = i
        # Group consecutive equal values (a tie block).
        while j + 1 < n and values[indexed[j + 1]] == values[indexed[i]]:
            j += 1
        # Average rank for the tie block (1-based positions i+1 .. j+1).
        avg_rank = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def _pearson(a: Sequence[float], b: Sequence[float]) -> float | None:
    """
    Pearson correlation of two equal-length series. Returns None if undefined
    (fewer than 2 points or zero variance in either series).
    """
    n = min(len(a), len(b))
    if n < 2:
        return None
    a, b = a[:n], b[:n]
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    denom = math.sqrt(var_a * var_b)
    if denom == 0:
        return None
    return cov / denom


def information_coefficient(
    signal: Sequence[float],
    fwd_returns: Sequence[float],
) -> float | None:
    """
    Pearson IC: linear correlation between a signal and the forward returns it is
    meant to predict. The two series are aligned by position and truncated to the
    shorter length, so you can pass a full signal series alongside `forward_returns`
    output (which is already shorter by `horizon`).

    Returns None when undefined (fewer than 2 aligned points or zero variance) so
    callers never act on a meaningless number.
    """
    return _pearson(signal, fwd_returns)


def rank_information_coefficient(
    signal: Sequence[float],
    fwd_returns: Sequence[float],
) -> float | None:
    """
    Rank IC (Spearman): correlation of the RANKS of the signal and the forward
    returns. Robust to outliers and to any monotonic (not just linear) relationship.
    This is the headline number for judging a predictive signal.

    Returns None when undefined (fewer than 2 aligned points or no rank variance,
    e.g. every signal value identical).
    """
    n = min(len(signal), len(fwd_returns))
    if n < 2:
        return None
    sig_ranks = _rank(signal[:n])
    ret_ranks = _rank(fwd_returns[:n])
    return _pearson(sig_ranks, ret_ranks)


@dataclass(frozen=True)
class ICReport:
    """Summary of a signal's predictive power against forward returns."""
    ic: float | None             # Pearson IC
    rank_ic: float | None        # Spearman (rank) IC
    n: int                       # number of aligned (signal, forward-return) pairs
    horizon: int                 # forward horizon used, in periods

    def summary(self) -> str:
        ic_s = "n/a" if self.ic is None else f"{self.ic:+.4f}"
        ric_s = "n/a" if self.rank_ic is None else f"{self.rank_ic:+.4f}"
        return (
            f"IC[h={self.horizon}, n={self.n}]: "
            f"pearson={ic_s}, rank={ric_s}"
        )


def ic_report(
    signal: Sequence[float],
    prices: Sequence[float],
    horizon: int = 1,
) -> ICReport:
    """
    Convenience: from a raw signal series and a price series, compute forward
    returns over `horizon`, align them with the signal, and report both ICs.

    Alignment: forward_return[t] is realized over (t, t+horizon], so it pairs with
    signal[t]. We truncate the signal to the number of available forward returns,
    using the LEADING entries (each signal matched with the return that follows it).

    With insufficient data both ICs are None and n is 0 — never garbage.
    """
    fwd = forward_returns(prices, horizon)
    n = min(len(signal), len(fwd))
    if n < 2:
        return ICReport(ic=None, rank_ic=None, n=max(n, 0), horizon=horizon)
    sig = signal[:n]
    fwd = fwd[:n]
    return ICReport(
        ic=information_coefficient(sig, fwd),
        rank_ic=rank_information_coefficient(sig, fwd),
        n=n,
        horizon=horizon,
    )
