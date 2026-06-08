"""
apex.validation.pbo
====================
Gate 9 statistic — **Probability of Backtest Overfitting (PBO)** via
Combinatorially-Symmetric Cross-Validation (CSCV), per Bailey, Borwein,
López de Prado & Zhu (2017), "The Probability of Backtest Overfitting".

The question PBO answers: *when you pick the configuration that looked best in a
backtest, how often does that choice actively hurt you out of sample?* If the
answer is "about half the time," your selection is no better than a coin flip —
the classic fingerprint of overfitting to noise.

This is the one genuinely-new piece relative to what main already has: main's
Gate 8 already corrects a single Sharpe for multiple-testing (Deflated Sharpe in
``apex.validation.overfitting``); PBO instead interrogates the *config-selection
process itself* across a parameter sweep.

Everything here is pure, deterministic, and stdlib-only (Golden Rule 10): the
sampling path uses a seeded RNG so the same matrix always yields the same PBO.
"""

from __future__ import annotations

import itertools
import math
import random
import statistics
from typing import Sequence

from apex.validation import metrics


def slice_sharpes(equity_curve: Sequence[float], n_slices: int) -> list[float]:
    """Cut an equity curve into ``n_slices`` contiguous chunks, scoring each chunk
    by its per-period Sharpe — one configuration's performance vector across time.

    Returns ``0.0`` for chunks too short to score so every configuration yields a
    same-length vector (the CSCV matrix must be rectangular).
    """
    m = len(equity_curve)
    if m < n_slices + 1 or n_slices < 1:
        return [0.0] * max(n_slices, 0)
    edges = [round(i * m / n_slices) for i in range(n_slices + 1)]
    out: list[float] = []
    for lo, hi in zip(edges, edges[1:]):
        chunk = list(equity_curve[lo:hi])
        out.append(metrics.sharpe_ratio(metrics.returns_from_equity(chunk)))
    return out


def build_performance_matrix(
    config_curves: Sequence[Sequence[float]], n_slices: int
) -> list[list[float]]:
    """Turn per-configuration equity curves into a CSCV performance matrix
    ``matrix[t][c]`` (rows = time slices, columns = configurations).

    Returns an empty list when the inputs can't support CSCV — fewer than two
    configurations, or an odd / too-small slice count. The caller treats an empty
    matrix as "no config field to test" (see :func:`gauntlet.evaluate_gate9_pbo`).
    """
    if len(config_curves) < 2 or n_slices < 4 or n_slices % 2 != 0:
        return []
    per_config = [slice_sharpes(c, n_slices) for c in config_curves]
    return [[per_config[c][t] for c in range(len(per_config))] for t in range(n_slices)]


def probability_of_backtest_overfitting(
    performance_matrix: Sequence[Sequence[float]],
    n_splits: int = 16,
    seed: int = 42,
) -> float:
    """Probability of Backtest Overfitting (PBO) via CSCV.

    ``performance_matrix[t][c]`` is the performance (Sharpe, or any
    larger-is-better score) of configuration ``c`` in time slice ``t``. There are
    ``T`` rows (time slices; must be even and ``>= 4``) and ``N`` columns (the
    configurations that were tried).

    CSCV splits the ``T`` rows into two equal halves. For each split it picks the
    config that was BEST in-sample (one half) and records its RANK out-of-sample
    (the other half). The relative rank ``ω = rank / (N + 1)`` becomes a logit
    ``λ = ln(ω / (1 - ω))``. **PBO is the fraction of splits where the in-sample
    champion landed in the bottom half out of sample** (``λ < 0``) — i.e. where
    chasing the best backtest actively hurt you. PBO near ``0.5`` means selection
    is no better than luck.

    To stay cheap on the free CI runner, when ``C(T, T/2)`` exceeds ``n_splits``
    we sample ``n_splits`` distinct combinations with a SEEDED RNG rather than
    enumerating all of them; with few enough rows we enumerate exhaustively.

    Fails CLOSED: returns ``1.0`` (maximally overfit / no confidence) when the
    matrix is too small or malformed to evaluate.
    """
    t_rows = len(performance_matrix)
    if t_rows < 4 or t_rows % 2 != 0:
        return 1.0
    n_cols = len(performance_matrix[0])
    if n_cols < 2 or any(len(row) != n_cols for row in performance_matrix):
        return 1.0

    half = t_rows // 2
    all_rows = list(range(t_rows))

    total_combos = math.comb(t_rows, half)
    if total_combos <= n_splits:
        is_row_sets = [set(c) for c in itertools.combinations(all_rows, half)]
    else:
        rng = random.Random(seed)
        seen: set[frozenset[int]] = set()
        is_row_sets = []
        attempts = 0
        max_attempts = n_splits * 50
        while len(is_row_sets) < n_splits and attempts < max_attempts:
            combo = frozenset(rng.sample(all_rows, half))
            attempts += 1
            if combo not in seen:
                seen.add(combo)
                is_row_sets.append(set(combo))

    if not is_row_sets:
        return 1.0

    logits: list[float] = []
    for is_rows in is_row_sets:
        oos_rows = [r for r in all_rows if r not in is_rows]
        is_perf = [
            statistics.fmean([performance_matrix[t][c] for t in is_rows]) for c in range(n_cols)
        ]
        best_c = max(range(n_cols), key=lambda c: is_perf[c])
        oos_perf = [
            statistics.fmean([performance_matrix[t][c] for t in oos_rows]) for c in range(n_cols)
        ]
        champion_oos = oos_perf[best_c]
        rank = sum(1 for c in range(n_cols) if oos_perf[c] <= champion_oos)
        omega = rank / (n_cols + 1)
        omega = min(max(omega, 1e-12), 1.0 - 1e-12)
        logits.append(math.log(omega / (1.0 - omega)))

    below = sum(1 for lam in logits if lam < 0.0)
    return below / len(logits)
