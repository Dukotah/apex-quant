"""
Tests for apex.validation.pbo — the Probability of Backtest Overfitting (CSCV)
statistic and its Gate 9 wrapper. The matrix helpers use hand-constructed cases
(a config that is genuinely best everywhere vs. pure noise) so the assertions
pin down real behaviour, not a single random draw.
"""

from __future__ import annotations

import random

from apex.validation import gauntlet, pbo
from apex.validation.gauntlet import GateStatus


def _genuine_edge_matrix(t_slices: int = 8, n_configs: int = 6) -> list[list[float]]:
    """Config 0 is consistently best across every slice → in-sample champion is the
    OOS champion every split → PBO ~ 0."""
    matrix: list[list[float]] = []
    for t in range(t_slices):
        row = []
        for c in range(n_configs):
            base = 2.0 if c == 0 else (1.0 - 0.1 * c)
            row.append(base + 0.01 * ((t % 3) - 1))
        matrix.append(row)
    return matrix


def _overfit_matrix(t_slices: int = 8, n_configs: int = 12, seed: int = 1) -> list[list[float]]:
    """Pure noise: whichever config wins in-sample has no reason to win OOS → PBO ~ 0.5."""
    rng = random.Random(seed)
    return [[rng.gauss(0.0, 1.0) for _ in range(n_configs)] for _ in range(t_slices)]


# --------------------------------------------------------- probability_of_backtest_overfitting


def test_pbo_low_for_genuine_edge():
    assert pbo.probability_of_backtest_overfitting(_genuine_edge_matrix(), n_splits=50) < 0.10


def test_pbo_high_for_noise():
    # Average a few seeds so the assertion isn't a knife-edge on one draw.
    vals = [
        pbo.probability_of_backtest_overfitting(
            _overfit_matrix(t_slices=10, n_configs=20, seed=s), n_splits=60, seed=s
        )
        for s in range(5)
    ]
    assert sum(vals) / len(vals) > 0.30


def test_pbo_deterministic():
    m = _overfit_matrix(t_slices=12, n_configs=20, seed=2)
    a = pbo.probability_of_backtest_overfitting(m, n_splits=40, seed=99)
    b = pbo.probability_of_backtest_overfitting(m, n_splits=40, seed=99)
    assert a == b


def test_pbo_fails_closed_on_bad_matrix():
    assert pbo.probability_of_backtest_overfitting([]) == 1.0
    assert pbo.probability_of_backtest_overfitting([[1.0, 2.0]]) == 1.0  # T < 4
    assert pbo.probability_of_backtest_overfitting([[1.0]] * 4) == 1.0  # N < 2
    assert pbo.probability_of_backtest_overfitting([[1.0, 2.0]] * 5) == 1.0  # odd T


def test_pbo_exhaustive_enumeration_small():
    # T=4 → C(4,2)=6 <= n_splits, so it enumerates exhaustively (no RNG path).
    m = _genuine_edge_matrix(t_slices=4, n_configs=4)
    assert pbo.probability_of_backtest_overfitting(m, n_splits=16) == 0.0


# --------------------------------------------------------- slice_sharpes / build_performance_matrix


def test_slice_sharpes_length_and_short_guard():
    curve = [100.0 * (1.003**i) for i in range(40)]
    assert len(pbo.slice_sharpes(curve, 8)) == 8
    # Too short to slice → zero-filled vector of the requested length (rectangular matrix).
    assert pbo.slice_sharpes([1.0, 2.0], 8) == [0.0] * 8


def test_build_performance_matrix_shape_and_unusable():
    curves = [[100.0 * (1.002**i) for i in range(50)] for _ in range(3)]
    matrix = pbo.build_performance_matrix(curves, 8)
    assert len(matrix) == 8 and all(len(row) == 3 for row in matrix)  # [t][c]
    # Unusable inputs → empty matrix (the caller treats this as "no sweep").
    assert pbo.build_performance_matrix(curves, 5) == []  # odd slice count
    assert pbo.build_performance_matrix(curves[:1], 8) == []  # < 2 configs


# --------------------------------------------------------- Gate 9 wrapper


def test_gate9_passes_with_note_when_no_matrix():
    gate, value = gauntlet.evaluate_gate9_pbo([])
    assert gate.status == GateStatus.PASS  # missing evidence is not evidence of overfit
    assert gate.is_hard_gate is False
    assert value == 0.0
    assert "not evaluated" in gate.detail


def test_gate9_passes_for_genuine_edge():
    gate, value = gauntlet.evaluate_gate9_pbo(_genuine_edge_matrix(), n_splits=50)
    assert gate.status == GateStatus.PASS
    assert value < gauntlet.MAX_PBO


def test_gate9_warns_for_overfit_field():
    # A noise field where selection is ~coin-flip should warn, never hard-fail.
    gate, value = gauntlet.evaluate_gate9_pbo(
        _overfit_matrix(t_slices=10, n_configs=20, seed=3), n_splits=60, seed=3
    )
    assert gate.is_hard_gate is False
    if value >= gauntlet.MAX_PBO:
        assert gate.status == GateStatus.WARN
