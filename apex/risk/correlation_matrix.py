"""
apex.risk.correlation_matrix
============================
Pairwise Pearson correlation matrix across a set of symbols' return series.

Diversification is the only free lunch in markets: a book of strategies (or
symbols) that all move together is, risk-wise, a single concentrated bet. This
module turns a mapping of ``symbol -> return series`` into the full pairwise
correlation matrix so the risk layer (and Gate 7 of the Gauntlet) can see how
correlated the holdings really are and flag dangerous clusters.

This is statistical/metric code (it consumes return *series*, not money), so —
exactly like apex.validation.metrics — it works in float, not Decimal, and
leans only on stdlib (math + statistics). Pure and deterministic: same inputs
always produce the same matrix. Insufficient-data pairs degrade gracefully to a
correlation of None rather than emitting garbage.
"""
from __future__ import annotations

import math
import statistics
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

# A correlation cell is None when it is undefined (too few overlapping points,
# or a flat/zero-variance series). Callers must treat None as "unknown", never 0.
CorrelationMatrix = Dict[str, Dict[str, Optional[float]]]


def pairwise_correlation(
    a: Sequence[float],
    b: Sequence[float],
    min_periods: int = 2,
) -> Optional[float]:
    """
    Pearson correlation between two return series.

    The two series are aligned from the FRONT to their common length (the same
    convention apex.validation.metrics.correlation uses). Returns None — not a
    misleading 0.0 — when the correlation is undefined:
      * fewer than ``min_periods`` overlapping observations, or
      * either series has zero variance over the overlap (a flat series has no
        direction to correlate with).

    The result is clamped to [-1.0, 1.0] to absorb floating-point overshoot.
    """
    if min_periods < 2:
        min_periods = 2

    n = min(len(a), len(b))
    if n < min_periods:
        return None

    a = a[:n]
    b = b[:n]

    mean_a = statistics.fmean(a)
    mean_b = statistics.fmean(b)

    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)

    denom = math.sqrt(var_a * var_b)
    if denom == 0.0:
        return None

    corr = cov / denom
    # Clamp to guard against tiny floating-point excursions beyond [-1, 1].
    if corr > 1.0:
        return 1.0
    if corr < -1.0:
        return -1.0
    return corr


def correlation_matrix(
    returns_by_symbol: Mapping[str, Sequence[float]],
    min_periods: int = 2,
) -> CorrelationMatrix:
    """
    Build the full symmetric pairwise correlation matrix.

    Args:
        returns_by_symbol: mapping of symbol -> its return series.
        min_periods: minimum overlapping observations required for a defined
            correlation; pairs below this are reported as None.

    Returns:
        A nested dict ``matrix[s1][s2]`` of correlations. The diagonal
        ``matrix[s][s]`` is 1.0 for any symbol with >= ``min_periods`` and
        non-zero variance, else None (a flat/too-short series has no self
        correlation to speak of). The matrix is symmetric:
        ``matrix[s1][s2] == matrix[s2][s1]``.

    An empty input yields an empty matrix. The output key order follows the
    iteration order of the input mapping, so the result is deterministic for an
    ordered (e.g. dict) input.
    """
    symbols: List[str] = list(returns_by_symbol.keys())

    matrix: CorrelationMatrix = {s: {} for s in symbols}

    for i, s1 in enumerate(symbols):
        series1 = returns_by_symbol[s1]
        for s2 in symbols[i:]:
            if s1 == s2:
                # Diagonal: a series correlates perfectly with itself, but only
                # if it is actually well-defined (enough points, has variance).
                corr = pairwise_correlation(series1, series1, min_periods)
                value: Optional[float] = None if corr is None else 1.0
            else:
                value = pairwise_correlation(
                    series1, returns_by_symbol[s2], min_periods
                )
            matrix[s1][s2] = value
            matrix[s2][s1] = value  # mirror for symmetry

    return matrix


def average_correlation(
    matrix: CorrelationMatrix,
    use_abs: bool = False,
) -> Optional[float]:
    """
    Mean of the off-diagonal (unique pair) correlations — a single-number read
    on how clustered the book is. Higher means less real diversification.

    Only defined pairs (non-None) are averaged; the diagonal is excluded. When
    ``use_abs`` is True the magnitudes are averaged (so a -0.9 and +0.9 both
    count as highly correlated rather than cancelling). Returns None when there
    is no defined pair to average.
    """
    symbols = list(matrix.keys())
    total = 0.0
    count = 0

    for i, s1 in enumerate(symbols):
        row = matrix[s1]
        for s2 in symbols[i + 1:]:
            value = row.get(s2)
            if value is None:
                continue
            total += abs(value) if use_abs else value
            count += 1

    if count == 0:
        return None
    return total / count


def most_correlated_pair(
    matrix: CorrelationMatrix,
    use_abs: bool = True,
) -> Optional[Tuple[str, str, float]]:
    """
    The single most correlated off-diagonal pair — the tightest cluster, i.e.
    the place where adding the second name buys you the least diversification.

    By default ranks by absolute correlation (so strong negative correlation is
    also surfaced); set ``use_abs=False`` to rank by signed correlation. Returns
    ``(symbol_a, symbol_b, correlation)`` with the ORIGINAL signed correlation,
    or None if no defined pair exists. Ties resolve to the first pair in input
    order, keeping the result deterministic.
    """
    symbols = list(matrix.keys())
    best: Optional[Tuple[str, str, float]] = None
    best_key = -math.inf

    for i, s1 in enumerate(symbols):
        row = matrix[s1]
        for s2 in symbols[i + 1:]:
            value = row.get(s2)
            if value is None:
                continue
            key = abs(value) if use_abs else value
            if key > best_key:
                best_key = key
                best = (s1, s2, value)

    return best
