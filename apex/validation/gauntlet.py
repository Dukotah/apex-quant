"""
apex.validation.gauntlet
========================
The orchestrator. Runs all seven gates and produces the honest confidence grade
described in docs/VALIDATION_GAUNTLET.md.

This module defines the data structures and the grading logic. Gates 3 and 4
(walk-forward, Monte Carlo) are implemented in their own modules. Gates 1, 2, 5,
6, 7 are implemented here as straightforward metric checks. Gates that require a
live backtester (cost stress, parameter sweep) take an injected backtest callable
so they integrate with the Phase 5 engine when it exists.

The Gauntlet NEVER outputs "this will be profitable." It outputs a graded,
multi-dimensional truth report. See module docs for the philosophy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from apex.validation import metrics
from apex.validation.monte_carlo import MonteCarloResult
from apex.validation.walk_forward import WalkForwardResult


class GateStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class Grade(str, Enum):
    A = "A"  # all 7 clean
    B = "B"  # all 7, some warnings
    C = "C"  # marginal (5-6 pass)
    FAIL = "FAIL"  # any hard gate (1-5) failed


@dataclass(frozen=True)
class GateResult:
    name: str
    status: GateStatus
    detail: str
    is_hard_gate: bool  # gates 1-5 are hard fails; 6-7 can only warn


@dataclass(frozen=True)
class GauntletReport:
    strategy_name: str
    gates: list[GateResult]
    grade: Grade
    paper_approved: bool
    realistic_max_drawdown: float  # from Monte Carlo — size around THIS
    quarantine_sharpe_floor: float  # auto-quarantine if live Sharpe drops below
    validated_sharpe: float = (
        0.0  # walk-forward Sharpe the floor derives from (stored, not back-calculated)
    )
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"STRATEGY: {self.strategy_name}",
            "=" * 55,
        ]
        for g in self.gates:
            mark = {"PASS": "✓", "WARN": "!", "FAIL": "✗"}[g.status.value]
            lines.append(f"{mark} {g.name:.<34} {g.status.value} ({g.detail})")
        lines.append("=" * 55)
        verdict = "PAPER-APPROVED" if self.paper_approved else "NOT APPROVED"
        lines.append(f"VERDICT: {verdict} (grade {self.grade.value})")
        lines.append(
            f"Size positions around the realistic max drawdown: "
            f"{self.realistic_max_drawdown:.1%} (NOT the backtest's lucky number)."
        )
        lines.append(
            f"Auto-quarantine if live 30-day Sharpe falls below {self.quarantine_sharpe_floor:.2f}."
        )
        for note in self.notes:
            lines.append(f"  • {note}")
        return "\n".join(lines)


# --- Thresholds (the bar a strategy must clear). Tune centrally, here. ---

# Gate 1 is the IN-SAMPLE SANITY check — "does it even work on training data?" — not
# the final verdict. An absolute Sharpe >= 1.0 is miscalibrated for the long-only
# risk-premia lane this architecture targets: buy-and-hold SPY itself only scores
# ~0.6-0.9 over a full cycle, so a 1.0 in-sample bar rejects every long-biased
# strategy regardless of quality. The real overfitting defense is the HARD gates that
# follow — Gate 2 (out-of-sample >= 70% of in-sample), Gate 3 (walk-forward), Gate 4
# (Monte-Carlo significance), Gate 5 (survives 2x costs). So Gate 1 requires a
# meaningful-but-achievable in-sample edge (Sharpe >= 0.5) and lets those gates filter.
MIN_IN_SAMPLE_SHARPE = 0.5
MAX_DRAWDOWN_LIMIT = 0.25
MIN_TRADES = 50
MIN_TRADES_FLOOR = 20  # below this a Sharpe is not statistically credible, period
MIN_PROFIT_FACTOR = 1.3
OOS_SHARPE_RATIO_FLOOR = 0.70  # OOS Sharpe must be >= 70% of in-sample
COST_STRESS_SHARPE_FLOOR = 0.50  # must stay > 0.5 Sharpe at 2x cost
MAX_BENCHMARK_CORRELATION = 0.50


def regime_aware_min_trades(
    num_bars: int,
    rebalance_period_bars: int = 1,
    default: int = MIN_TRADES,
) -> int:
    """
    A fair trade-count minimum for Gate 1, scaled to how many rebalance
    opportunities the test window actually permits.

    A daily strategy (period = 1) faces the full ``default`` bar. A monthly
    strategy (period ≈ 21) tested over a window too short to *physically* allow
    ``default`` rebalances cannot produce that many trades — failing it for the
    arithmetic of the calendar, not for lack of edge, is the bug this fixes. The
    minimum is therefore capped at the number of rebalance opportunities in the
    window, but never drops below ``MIN_TRADES_FLOOR`` (below which a Sharpe is
    not statistically credible regardless of cadence).

    Scope: this corrects the WINDOW-vs-CADENCE mismatch only. It does NOT lower
    the bar for a low-TURNOVER strategy over a LONG window (where opportunities
    ≥ default but the strategy rarely changes position). That — grading rotation
    strategies on holding-period bets rather than round-trips — is a separate,
    still-open design question (see DECISIONS.md).
    """
    if rebalance_period_bars <= 1:
        return default
    opportunities = num_bars // rebalance_period_bars
    return max(MIN_TRADES_FLOOR, min(default, opportunities))


def evaluate_gate1_in_sample(
    in_sample_equity: list[float],
    trade_returns: list[float],
    *,
    min_trades: int = MIN_TRADES,
) -> GateResult:
    """
    Gate 1 — In-Sample Sanity. Does it even work on its own training data?

    ``min_trades`` is the required trade count (default ``MIN_TRADES``). Pass a
    regime-aware value (see :func:`regime_aware_min_trades`) for low-frequency
    strategies so they are not failed for a cadence the window can't accommodate.
    """
    rets = metrics.returns_from_equity(in_sample_equity)
    sharpe = metrics.sharpe_ratio(rets)
    dd = metrics.max_drawdown(in_sample_equity)
    n = len(trade_returns)
    pf = metrics.profit_factor(trade_returns)

    failures = []
    if sharpe < MIN_IN_SAMPLE_SHARPE:
        failures.append(f"Sharpe {sharpe:.2f}<{MIN_IN_SAMPLE_SHARPE}")
    if dd > MAX_DRAWDOWN_LIMIT:
        failures.append(f"DD {dd:.0%}>{MAX_DRAWDOWN_LIMIT:.0%}")
    if n < min_trades:
        failures.append(f"{n} trades<{min_trades}")
    if pf < MIN_PROFIT_FACTOR:
        failures.append(f"PF {pf:.2f}<{MIN_PROFIT_FACTOR}")

    # Surface when the bar was lowered, so the report stays honest about it.
    bar_note = (
        "" if min_trades == MIN_TRADES else f" [min trades relaxed to {min_trades} for cadence]"
    )

    if failures:
        return GateResult(
            "Gate 1 In-Sample Sanity",
            GateStatus.FAIL,
            "; ".join(failures) + bar_note,
            is_hard_gate=True,
        )
    return GateResult(
        "Gate 1 In-Sample Sanity",
        GateStatus.PASS,
        f"Sharpe {sharpe:.2f}, {n} trades, DD {dd:.0%}, PF {pf:.2f}" + bar_note,
        is_hard_gate=True,
    )


def evaluate_gate2_out_of_sample(
    in_sample_sharpe: float,
    out_of_sample_sharpe: float,
) -> GateResult:
    """Gate 2 — Out-of-Sample Holdout. The most important gate."""
    if in_sample_sharpe <= 0:
        return GateResult(
            "Gate 2 Out-of-Sample", GateStatus.FAIL, "no positive in-sample edge", is_hard_gate=True
        )
    ratio = out_of_sample_sharpe / in_sample_sharpe
    if ratio < OOS_SHARPE_RATIO_FLOOR:
        return GateResult(
            "Gate 2 Out-of-Sample",
            GateStatus.FAIL,
            f"OOS Sharpe {out_of_sample_sharpe:.2f} = {ratio:.0%} of IS "
            f"(need {OOS_SHARPE_RATIO_FLOOR:.0%}) — likely overfit",
            is_hard_gate=True,
        )
    return GateResult(
        "Gate 2 Out-of-Sample",
        GateStatus.PASS,
        f"OOS Sharpe {out_of_sample_sharpe:.2f} = {ratio:.0%} of IS",
        is_hard_gate=True,
    )


def evaluate_gate3_walk_forward(wf: WalkForwardResult) -> GateResult:
    """Gate 3 — Walk-Forward (delegates to walk_forward module)."""
    status = GateStatus.PASS if wf.passed else GateStatus.FAIL
    return GateResult(
        "Gate 3 Walk-Forward",
        status,
        f"WF Sharpe {wf.stitched_sharpe:.2f}, eff {wf.walk_forward_efficiency:.2f}",
        is_hard_gate=True,
    )


def evaluate_gate4_monte_carlo(mc: MonteCarloResult) -> GateResult:
    """Gate 4 — Monte Carlo (delegates to monte_carlo module)."""
    status = GateStatus.PASS if mc.passed else GateStatus.FAIL
    return GateResult(
        "Gate 4 Monte Carlo",
        status,
        f"p={mc.p_value:.3f}, realistic DD {mc.realistic_max_drawdown:.0%}",
        is_hard_gate=True,
    )


def evaluate_gate5_cost_stress(sharpe_at_2x_cost: float) -> GateResult:
    """Gate 5 — Transaction Cost Stress. Survives 2x expected cost?"""
    if sharpe_at_2x_cost < COST_STRESS_SHARPE_FLOOR:
        return GateResult(
            "Gate 5 Cost Stress",
            GateStatus.FAIL,
            f"Sharpe {sharpe_at_2x_cost:.2f} at 2x cost "
            f"(<{COST_STRESS_SHARPE_FLOOR}) — edge < costs",
            is_hard_gate=True,
        )
    return GateResult(
        "Gate 5 Cost Stress",
        GateStatus.PASS,
        f"Sharpe {sharpe_at_2x_cost:.2f} at 2x cost",
        is_hard_gate=True,
    )


def evaluate_gate6_param_sensitivity(
    neighbor_sharpes: list[float],
    chosen_sharpe: float,
) -> GateResult:
    """
    Gate 6 — Parameter Sensitivity (soft gate, can only WARN).
    Is the chosen parameter a robust plateau or a lucky needle?
    neighbor_sharpes = Sharpes at +-20% around each chosen parameter.
    """
    if not neighbor_sharpes:
        return GateResult(
            "Gate 6 Param Sensitivity", GateStatus.WARN, "no sweep provided", is_hard_gate=False
        )
    avg_neighbor = sum(neighbor_sharpes) / len(neighbor_sharpes)
    # If neighbors hold up reasonably (>=70% of chosen), it's a plateau = good.
    if chosen_sharpe > 0 and avg_neighbor >= 0.70 * chosen_sharpe:
        return GateResult(
            "Gate 6 Param Sensitivity",
            GateStatus.PASS,
            f"neighbors avg {avg_neighbor:.2f} vs {chosen_sharpe:.2f} — robust plateau",
            is_hard_gate=False,
        )
    return GateResult(
        "Gate 6 Param Sensitivity",
        GateStatus.WARN,
        f"neighbors avg {avg_neighbor:.2f} vs {chosen_sharpe:.2f} — sharp peak, possible overfit",
        is_hard_gate=False,
    )


def evaluate_gate7_benchmark(
    strategy_sharpe: float,
    benchmark_sharpe: float,
    correlation_to_benchmark: float,
) -> GateResult:
    """
    Gate 7 — Benchmark & Correlation (soft gate, can only WARN).
    Beats SPY risk-adjusted OR diversifies (low correlation).
    """
    beats = strategy_sharpe > benchmark_sharpe
    diversifies = abs(correlation_to_benchmark) < MAX_BENCHMARK_CORRELATION
    if beats or diversifies:
        why = "beats SPY" if beats else f"diversifies (corr {correlation_to_benchmark:.2f})"
        return GateResult("Gate 7 Benchmark/Correlation", GateStatus.PASS, why, is_hard_gate=False)
    return GateResult(
        "Gate 7 Benchmark/Correlation",
        GateStatus.WARN,
        f"neither beats SPY nor diversifies (corr {correlation_to_benchmark:.2f})",
        is_hard_gate=False,
    )


def grade_and_assemble(
    strategy_name: str, gates: list[GateResult], realistic_dd: float, validated_sharpe: float
) -> GauntletReport:
    """Apply the grading rubric and assemble the final report."""
    hard_fails = [g for g in gates if g.is_hard_gate and g.status == GateStatus.FAIL]
    warns = [g for g in gates if g.status == GateStatus.WARN]

    if hard_fails:
        grade = Grade.FAIL
        approved = False
    elif not warns:
        grade = Grade.A
        approved = True
    elif len(warns) <= 2:
        grade = Grade.B
        approved = True
    else:
        grade = Grade.C
        approved = True

    notes = []
    if grade == Grade.FAIL:
        notes.append("Failed a hard gate. Archive and study why — do NOT deploy.")
    elif grade == Grade.C:
        notes.append("Marginal. Paper only, low conviction, expect possible decay.")

    # Auto-quarantine floor = 70% of the validated (walk-forward) Sharpe.
    quarantine_floor = 0.70 * validated_sharpe

    return GauntletReport(
        strategy_name=strategy_name,
        gates=gates,
        grade=grade,
        paper_approved=approved,
        realistic_max_drawdown=realistic_dd,
        quarantine_sharpe_floor=quarantine_floor,
        validated_sharpe=validated_sharpe,
        notes=notes,
    )
