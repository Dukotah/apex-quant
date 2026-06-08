"""
tests/test_stateful_sim.py
==========================
Tests for StatefulSimExecutionEngine — the simulated engine that reports a seeded
position snapshot as broker truth so a paper experiment book remembers its
positions across cron cycles. We assert the new reconciliation behavior and its
copy semantics; the fill simulation itself is the parent class's contract.
"""

from __future__ import annotations

from decimal import Decimal

from apex.execution.stateful_sim import StatefulSimExecutionEngine

_SEED = {
    "SPY": {"qty": "10", "avg_entry_price": "500", "current_price": "510"},
    "GLD": {"qty": "5", "avg_entry_price": "300", "current_price": "299"},
}


def test_empty_seed_reconciles_to_nothing():
    assert StatefulSimExecutionEngine().reconcile_positions() == {}
    assert StatefulSimExecutionEngine(seed_positions=None).reconcile_positions() == {}


def test_seed_is_reported_as_broker_truth():
    engine = StatefulSimExecutionEngine(seed_positions=_SEED)
    truth = engine.reconcile_positions()
    assert set(truth) == {"SPY", "GLD"}
    assert truth["SPY"]["qty"] == "10"
    assert truth["SPY"]["avg_entry_price"] == "500"


def test_reconcile_returns_a_defensive_copy():
    engine = StatefulSimExecutionEngine(seed_positions=_SEED)
    truth = engine.reconcile_positions()
    truth["SPY"]["qty"] = "999"  # mutate the returned copy
    # The engine's own truth is unaffected on the next read.
    assert engine.reconcile_positions()["SPY"]["qty"] == "10"


def test_construction_copies_the_input_seed():
    seed = {"SPY": {"qty": "10", "avg_entry_price": "500"}}
    engine = StatefulSimExecutionEngine(seed_positions=seed)
    seed["SPY"]["qty"] = "0"  # mutate caller's dict after construction
    assert engine.reconcile_positions()["SPY"]["qty"] == "10"


def test_still_a_paper_engine_with_sim_passthrough():
    engine = StatefulSimExecutionEngine(seed_positions=_SEED, slippage_pct=Decimal("0.001"))
    assert engine.is_paper is True
    # The parent's price registry still works (used by run_once before submit).
    engine.update_price("SPY", Decimal("510"))
    assert engine._prices["SPY"] == Decimal("510")
