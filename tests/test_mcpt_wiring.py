"""
Tests that the Monte-Carlo permutation test is an opt-in companion in the Gauntlet
runner: when run_mcpt=True it adds an MCPT note WITHOUT changing the gate list or grade.
"""

from __future__ import annotations

from decimal import Decimal

from apex.backtest.gauntlet_runner import run_full_gauntlet
from apex.backtest.synthetic import generate_closes, interleave, make_bars
from apex.core.models import AssetClass, Symbol
from apex.risk.risk_manager import RiskConfig

SYM = Symbol("SYN", AssetClass.ETF)


def _risk():
    return RiskConfig(
        max_position_size_pct=Decimal("1.0"),
        max_total_exposure_pct=Decimal("1.0"),
        max_leverage=Decimal("1.0"),
        max_drawdown_pct=Decimal("0.99"),
        require_stop_loss=True,
    )


def _events(n=500):
    closes = generate_closes(
        seed=7,
        n=n,
        start_price=100,
        drift_schedule=[(0, 0.0010), (150, -0.0010), (300, 0.0012)],
        vol=0.008,
    )
    return interleave(make_bars("SYN", closes))


def _factory():
    from apex.strategy.library.sma_crossover import SMACrossoverStrategy

    return SMACrossoverStrategy("sma_x", [SYM], fast_period=10, slow_period=30)


def test_run_mcpt_adds_note_without_changing_gates_or_grade():
    events = _events()
    base, _ = run_full_gauntlet(
        "syn", _factory, events, _risk(), benchmark_ticker="SYN", mc_iterations=80
    )
    withmc, _ = run_full_gauntlet(
        "syn",
        _factory,
        events,
        _risk(),
        benchmark_ticker="SYN",
        mc_iterations=80,
        run_mcpt=True,
        mcpt_iterations=20,
    )
    # Same structure: the gate list and grade are untouched by MCPT.
    assert len(withmc.gates) == len(base.gates)
    assert withmc.grade == base.grade
    # MCPT shows up only as a report note when enabled.
    assert any("perm" in n.lower() or "mcpt" in n.lower() for n in withmc.notes)
    assert not any("perm" in n.lower() or "mcpt" in n.lower() for n in base.notes)
