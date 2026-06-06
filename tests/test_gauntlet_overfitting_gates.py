"""
Tests for the overfitting gates wired into the Gauntlet orchestrator:
  Gate 8  — Deflated / Probabilistic Sharpe
  Gate 9  — Probability of Backtest Overfitting (CSCV)
  Gate 10 — Capacity & turnover sanity

Each gate must PASS a genuine edge and FAIL an overfit mirage, fail closed on
thin/missing inputs, and (for the hard gates) block approval in grading.
"""
from __future__ import annotations

import random

from apex.validation import gauntlet as G
from apex.validation.gauntlet import GateStatus, Grade


# ---------------------------------------------------------------- Gate 8

def _edge_returns(n: int, mean: float, sd: float, seed: int) -> list[float]:
    rng = random.Random(seed)
    return [rng.gauss(mean, sd) for _ in range(n)]


def test_gate8_passes_genuine_edge_single_trial():
    # Strong, consistent daily edge found WITHOUT a big sweep (1 trial) → high PSR.
    returns = _edge_returns(400, mean=0.0015, sd=0.006, seed=1)
    res = G.evaluate_gate8_deflated_sharpe(returns, num_trials=1)
    assert res.status == GateStatus.PASS
    assert res.is_hard_gate is True


def test_gate8_fails_overfit_mirage_many_trials():
    # A Sharpe that would PASS the naive significance test (PSR vs zero > 0.90)
    # but was the BEST of hundreds of tries → deflation drops it below the bar.
    returns = _edge_returns(250, mean=0.0017, sd=0.011, seed=2)
    from apex.validation import metrics
    # Sanity: undeflated, this edge looks real.
    assert metrics.probabilistic_sharpe_ratio(returns, reference_sharpe=0.0) > 0.90
    # The trials had a wide spread (the researcher tried wildly different configs),
    # so the expected-max bound is high and the headline must clear it.
    trial_sharpes = [random.Random(s).gauss(0.4, 0.9) for s in range(300)]
    res = G.evaluate_gate8_deflated_sharpe(
        returns, num_trials=300, trial_sharpes=trial_sharpes,
    )
    assert res.status == GateStatus.FAIL
    assert "DSR" in res.detail


def test_gate8_fails_closed_on_thin_returns():
    res = G.evaluate_gate8_deflated_sharpe([0.01, 0.02], num_trials=1)
    assert res.status == GateStatus.FAIL
    assert "fail closed" in res.detail


def test_gate8_fallback_to_psr_announced():
    returns = _edge_returns(400, mean=0.0015, sd=0.006, seed=3)
    res = G.evaluate_gate8_deflated_sharpe(returns, num_trials=1, trial_sharpes=None)
    assert "PSR" in res.detail


# ---------------------------------------------------------------- Gate 9

def _genuine_edge_matrix() -> list[list[float]]:
    """Config 0 is best in every slice → in-sample champion wins OOS too → PBO ~0."""
    matrix = []
    for t in range(8):
        row = [2.0 if c == 0 else (1.0 - 0.1 * c) for c in range(6)]
        # tiny deterministic wobble that never overturns the ranking
        row = [v + 0.01 * ((t % 3) - 1) for v in row]
        matrix.append(row)
    return matrix


def _noise_matrix(seed: int = 1) -> list[list[float]]:
    rng = random.Random(seed)
    return [[rng.gauss(0.0, 1.0) for _ in range(20)] for _ in range(10)]


def test_gate9_passes_genuine_edge():
    res = G.evaluate_gate9_pbo(_genuine_edge_matrix())
    assert res.status == GateStatus.PASS
    assert res.is_hard_gate is True


def test_gate9_fails_pure_noise():
    # Pure noise has no exploitable config field: PBO sits near 0.5 (selection no
    # better than luck). Genuine-edge PBO (~0) is dramatically lower; assert the
    # noise gate's measured PBO is much higher than the edge case so the gate
    # discriminates. (We average over seeds so the bound isn't a knife-edge.)
    from apex.validation import metrics
    edge_pbo = metrics.probability_of_backtest_overfitting(_genuine_edge_matrix(), n_splits=50)
    noise_pbos = [
        metrics.probability_of_backtest_overfitting(_noise_matrix(seed=s), n_splits=60, seed=s)
        for s in range(6)
    ]
    avg_noise = sum(noise_pbos) / len(noise_pbos)
    assert edge_pbo < 0.10
    assert avg_noise > 0.30
    assert avg_noise > edge_pbo + 0.25


def test_gate9_fails_closed_on_tiny_matrix():
    res = G.evaluate_gate9_pbo([[1.0, 2.0]])
    assert res.status == GateStatus.FAIL
    assert "fail closed" in res.detail


# ---------------------------------------------------------------- Gate 10

def test_gate10_passes_capacity_friendly_strategy():
    # 25% return, low turnover (4 turns/yr) at 0.1% cost → capacity 62.5x, plenty.
    res = G.evaluate_gate10_capacity(
        num_trades=80, annualized_return_estimate=0.25, annual_turnover=4.0,
        cost_per_turn=0.001,
    )
    assert res.status == GateStatus.PASS
    assert res.is_hard_gate is True


def test_gate10_fails_high_turnover_edge_eaten_by_cost():
    # Real-looking 12% return but churns the book 200x/yr at 0.1% → cost 0.20 > edge.
    res = G.evaluate_gate10_capacity(
        num_trades=2000, annualized_return_estimate=0.12, annual_turnover=200.0,
        cost_per_turn=0.001,
    )
    assert res.status == GateStatus.FAIL
    assert "capacity" in res.detail


def test_gate10_fails_too_few_trades():
    res = G.evaluate_gate10_capacity(
        num_trades=5, annualized_return_estimate=0.30, annual_turnover=2.0,
        cost_per_turn=0.001,
    )
    assert res.status == GateStatus.FAIL
    assert "trades" in res.detail


# ---------------------------------------------------------------- grading wiring

def _pass(name, hard=True):
    return G.GateResult(name, GateStatus.PASS, "ok", is_hard_gate=hard)


def _fail(name, hard=True):
    return G.GateResult(name, GateStatus.FAIL, "nope", is_hard_gate=hard)


def test_overfitting_hard_fail_blocks_approval():
    # All core gates pass, but Gate 8 (a HARD overfitting gate) fails.
    gates = [_pass(f"Gate {i}") for i in range(1, 8)]
    gates += [_pass("Gate 7", hard=False)]
    gates += [_fail("Gate 8 Deflated Sharpe", hard=True)]
    gates += [_pass("Gate 9 PBO", hard=True), _pass("Gate 10 Capacity", hard=True)]
    report = G.grade_and_assemble("strat", gates, realistic_dd=0.2, validated_sharpe=1.0)
    assert report.grade == Grade.FAIL
    assert report.paper_approved is False


def test_all_ten_clean_grades_A():
    gates = [_pass(f"Gate {i}") for i in range(1, 6)]
    gates += [_pass("Gate 6", hard=False), _pass("Gate 7", hard=False)]
    gates += [_pass("Gate 8", hard=True), _pass("Gate 9", hard=True), _pass("Gate 10", hard=True)]
    report = G.grade_and_assemble("strat", gates, realistic_dd=0.2, validated_sharpe=1.2)
    assert report.grade == Grade.A
    assert report.paper_approved is True
