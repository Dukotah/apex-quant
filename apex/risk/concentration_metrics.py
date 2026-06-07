"""
apex.risk.concentration_metrics
===============================
Concentration diagnostics for a portfolio weight vector.

A book that looks diversified by *count* can still be dangerously concentrated
by *weight* — a "10-name" portfolio where one name is 80% of the equity is, for
risk purposes, a single-name bet. These pure functions turn a weight vector into
the two concentration numbers that actually matter:

  - The Herfindahl-Hirschman Index (HHI): the sum of squared (normalized)
    weights. Ranges from 1/N (perfectly equal-weight across N names) to 1.0 (one
    name holds everything). The higher it is, the more concentrated the book.
  - Top-N concentration: what fraction of total weight the N largest positions
    command. A quick read on "how much rides on my biggest bets".

This is metric/diagnostic code in the spirit of apex/validation/metrics.py:
it summarizes a vector of weights, performs no money math, and so uses float
(matching the convention of the metrics layer). It is NOT in the order/cash
path — the RiskManager and Portfolio remain Decimal.

All functions are pure and deterministic given their inputs, do no I/O, and
handle empty / degenerate / negative-weight inputs gracefully (returning None or
a benign value rather than garbage). Tested in tests/test_concentration_metrics.py
against hand-computed values.
"""

from __future__ import annotations

from typing import Optional, Sequence

__all__ = [
    "normalize_weights",
    "herfindahl_index",
    "effective_number_of_positions",
    "top_n_concentration",
    "max_weight",
]


def normalize_weights(weights: Sequence[float]) -> Optional[list[float]]:
    """
    Normalize a vector of weights to fractions of the total ABSOLUTE weight,
    each in [0, 1] and summing to 1.0.

    Absolute value is used so a mix of long and short exposures is measured by
    its magnitude (a -40% short is as concentrated as a +40% long). This is the
    common pre-step for the concentration metrics below.

    Returns None if the vector is empty or the total absolute weight is zero
    (nothing deployed — concentration is undefined, so fail closed to None).
    """
    if not weights:
        return None
    total = sum(abs(w) for w in weights)
    if total <= 0.0:
        return None
    return [abs(w) / total for w in weights]


def herfindahl_index(weights: Sequence[float]) -> Optional[float]:
    """
    Herfindahl-Hirschman Index of concentration: the sum of squared normalized
    (by absolute weight) weights.

      HHI = sum(w_i^2)   where w_i are normalized so sum(w_i) == 1

    Range: [1/N, 1.0].
      - 1/N  -> perfectly equal-weight across N positions (least concentrated).
      - 1.0  -> a single position holds the entire book (most concentrated).

    Returns None when there is nothing to measure (empty vector or zero total
    absolute weight).
    """
    normalized = normalize_weights(weights)
    if normalized is None:
        return None
    return sum(w * w for w in normalized)


def effective_number_of_positions(weights: Sequence[float]) -> Optional[float]:
    """
    The "effective N" implied by the HHI: 1 / HHI.

    This is the number of EQUAL-weight positions that would produce the same
    concentration. An equal-weight 10-name book has effective N = 10; a book
    dominated by one name trends toward 1.0 however many names it nominally
    holds. A far more honest diversification count than len(positions).

    Returns None when HHI is undefined (empty / zero-total vector).
    """
    hhi = herfindahl_index(weights)
    if hhi is None or hhi <= 0.0:
        return None
    return 1.0 / hhi


def top_n_concentration(weights: Sequence[float], n: int) -> Optional[float]:
    """
    Fraction of total (absolute) weight held by the N largest positions.

    Range: [0.0, 1.0]. A value of 0.80 means the top N names command 80% of the
    deployed book. If n >= the number of positions, the result is 1.0 (the whole
    book is in the "top N").

    Returns None for a non-positive n or when the weight vector has no
    measurable total (empty / zero-total).
    """
    if n <= 0:
        return None
    normalized = normalize_weights(weights)
    if normalized is None:
        return None
    ranked = sorted(normalized, reverse=True)
    return sum(ranked[:n])


def max_weight(weights: Sequence[float]) -> Optional[float]:
    """
    The single largest normalized (by absolute weight) position weight — the
    top-1 concentration. A fast guardrail read: "how much is my biggest bet".

    Returns None when there is nothing to measure.
    """
    return top_n_concentration(weights, 1)
