"""
Tests for apex.validation.gauntlet — the grading/orchestration logic.

Locks in: hard-gate failures block approval; clean runs grade A; warnings drop
the grade but can still approve; the quarantine floor is computed correctly.
"""
from __future__ import annotations

from apex.validation import gauntlet as G
from apex.validation.gauntlet import GateStatus, Grade


def _pass(name, hard=True):
    return G.GateResult(name, GateStatus.PASS, "ok", is_hard_gate=hard)


def _fail(name, hard=True):
    return G.GateResult(name, GateStatus.FAIL, "nope", is_hard_gate=hard)


def _warn(name):
    return G.GateResult(name, GateStatus.WARN, "meh", is_hard_gate=False)


def test_all_pass_grades_A():
    gates = [_pass(f"Gate {i}") for i in range(1, 6)] + [
        _pass("Gate 6", hard=False), _pass("Gate 7", hard=False)
    ]
    report = G.grade_and_assemble("strat", gates, realistic_dd=0.2, validated_sharpe=1.2)
    assert report.grade == Grade.A
    assert report.paper_approved is True


def test_hard_fail_blocks_approval():
    gates = [_pass("Gate 1"), _fail("Gate 2")] + [_pass(f"Gate {i}") for i in range(3, 6)]
    report = G.grade_and_assemble("strat", gates, realistic_dd=0.2, validated_sharpe=1.0)
    assert report.grade == Grade.FAIL
    assert report.paper_approved is False


def test_warnings_drop_to_B_but_approve():
    gates = [_pass(f"Gate {i}") for i in range(1, 6)] + [
        _warn("Gate 6"), _pass("Gate 7", hard=False)
    ]
    report = G.grade_and_assemble("strat", gates, realistic_dd=0.2, validated_sharpe=1.0)
    assert report.grade == Grade.B
    assert report.paper_approved is True


def test_many_warnings_drop_to_C():
    gates = [_pass(f"Gate {i}") for i in range(1, 6)] + [_warn("Gate 6"), _warn("Gate 7")]
    # Add a third soft warning to push past the <=2 threshold.
    gates.append(_warn("Gate 5b"))
    report = G.grade_and_assemble("strat", gates, realistic_dd=0.2, validated_sharpe=1.0)
    assert report.grade == Grade.C
    assert report.paper_approved is True


def test_quarantine_floor():
    gates = [_pass(f"Gate {i}") for i in range(1, 6)] + [
        _pass("Gate 6", hard=False), _pass("Gate 7", hard=False)
    ]
    report = G.grade_and_assemble("strat", gates, realistic_dd=0.3, validated_sharpe=1.0)
    assert abs(report.quarantine_sharpe_floor - 0.70) < 1e-9


def test_gate1_rejects_insufficient_trades():
    # Strong-looking curve but only a handful of trades → fail (luck, not edge).
    equity = [1.0 + 0.01 * i for i in range(50)]
    result = G.evaluate_gate1_in_sample(equity, trade_returns=[0.02, 0.03, -0.01])
    assert result.status == GateStatus.FAIL


def test_gate2_catches_overfit():
    # In-sample Sharpe 2.0, out-of-sample 0.3 → 15% ratio → overfit → fail.
    result = G.evaluate_gate2_out_of_sample(in_sample_sharpe=2.0, out_of_sample_sharpe=0.3)
    assert result.status == GateStatus.FAIL


def test_gate2_passes_robust():
    result = G.evaluate_gate2_out_of_sample(in_sample_sharpe=1.4, out_of_sample_sharpe=1.1)
    assert result.status == GateStatus.PASS


def test_gate5_kills_high_cost_strategy():
    result = G.evaluate_gate5_cost_stress(sharpe_at_2x_cost=0.2)
    assert result.status == GateStatus.FAIL


def test_gate6_robust_plateau_passes():
    result = G.evaluate_gate6_param_sensitivity(
        neighbor_sharpes=[1.1, 1.0, 1.2, 0.95], chosen_sharpe=1.2
    )
    assert result.status == GateStatus.PASS


def test_gate6_sharp_needle_warns():
    result = G.evaluate_gate6_param_sensitivity(
        neighbor_sharpes=[0.1, 0.2, -0.3], chosen_sharpe=1.8
    )
    assert result.status == GateStatus.WARN


def test_gate7_diversifier_passes_even_if_weaker():
    # Lower Sharpe than SPY, but uncorrelated → still earns its place.
    result = G.evaluate_gate7_benchmark(
        strategy_sharpe=0.6, benchmark_sharpe=0.9, correlation_to_benchmark=0.1
    )
    assert result.status == GateStatus.PASS
