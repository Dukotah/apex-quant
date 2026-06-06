"""
apex.validation.gauntlet
========================
The orchestrator. Runs all ten gates and produces the honest confidence grade
described in docs/VALIDATION_GAUNTLET.md.

This module defines the data structures and the grading logic. Gates 3 and 4
(walk-forward, Monte Carlo) are implemented in their own modules. Gates 1, 2, 5,
6, 7 are implemented here as straightforward metric checks. Gates 8-10 are the
overfitting battery (Deflated/Probabilistic Sharpe, Probability of Backtest
Overfitting via CSCV, and capacity/turnover sanity) built on the Bailey & Lopez
de Prado statistics in metrics.py. Gates that require a live backtester (cost
stress, parameter sweep) take an injected backtest callable so they integrate
with the Phase 5 engine when it exists.

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
    A = "A"        # all gates clean
    B = "B"        # all gates, some (<=2) warnings
    C = "C"        # marginal (3+ warnings, no hard fail)
    FAIL = "FAIL"  # any hard gate failed (1-5 core + 8-10 overfitting)


@dataclass(frozen=True)
class GateResult:
    name: str
    status: GateStatus
    detail: str
    is_hard_gate: bool   # gates 1-5 are hard fails; 6-7 can only warn


@dataclass(frozen=True)
class GauntletReport:
    strategy_name: str
    gates: list[GateResult]
    grade: Grade
    paper_approved: bool
    realistic_max_drawdown: float   # from Monte Carlo — size around THIS
    quarantine_sharpe_floor: float  # auto-quarantine if live Sharpe drops below
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
            f"Auto-quarantine if live 30-day Sharpe falls below "
            f"{self.quarantine_sharpe_floor:.2f}."
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
MIN_TRADES_FLOOR = 20      # below this a Sharpe is not statistically credible, period
MIN_PROFIT_FACTOR = 1.3
OOS_SHARPE_RATIO_FLOOR = 0.70        # OOS Sharpe must be >= 70% of in-sample
COST_STRESS_SHARPE_FLOOR = 0.50      # must stay > 0.5 Sharpe at 2x cost
MAX_BENCHMARK_CORRELATION = 0.50

# --- Overfitting gates (Bailey & Lopez de Prado). HARD fails: passing the
# point-estimate gates while these flag is the signature of a curve-fit mirage. ---

# Gate 8 — Deflated Sharpe. Probability the edge survives selection bias must be
# convincingly above a coin flip. 0.90 is the standard confidence bar; the DSR
# already deflates for how many trials were tried, so this is a strict test.
MIN_DEFLATED_SHARPE_PROB = 0.90
# When the DSR can't be computed (e.g. the caller never reported how many
# variants were tried) we fall back to the PSR vs a Sharpe-0 reference at the
# same confidence — "is the edge distinguishable from zero at all?"
MIN_PROBABILISTIC_SHARPE = 0.90

# Gate 9 — Probability of Backtest Overfitting (CSCV). PBO is the fraction of
# CSCV splits where the in-sample champion was below-median out of sample. >= 0.5
# means selection is no better than random (pure overfit); we demand it stay
# comfortably below half.
MAX_PBO = 0.50

# Gate 10 — Capacity / turnover sanity. The strategy's gross edge must cover its
# trading costs several times over, and it must clear a hard minimum trade count
# (a Sharpe on too few trades is not statistically credible — see MIN_TRADES_FLOOR).
MIN_CAPACITY_SCORE = 2.0


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
    bar_note = "" if min_trades == MIN_TRADES else f" [min trades relaxed to {min_trades} for cadence]"

    if failures:
        return GateResult("Gate 1 In-Sample Sanity", GateStatus.FAIL,
                          "; ".join(failures) + bar_note, is_hard_gate=True)
    return GateResult("Gate 1 In-Sample Sanity", GateStatus.PASS,
                      f"Sharpe {sharpe:.2f}, {n} trades, DD {dd:.0%}, PF {pf:.2f}" + bar_note,
                      is_hard_gate=True)


def evaluate_gate2_out_of_sample(
    in_sample_sharpe: float,
    out_of_sample_sharpe: float,
) -> GateResult:
    """Gate 2 — Out-of-Sample Holdout. The most important gate."""
    if in_sample_sharpe <= 0:
        return GateResult("Gate 2 Out-of-Sample", GateStatus.FAIL,
                          "no positive in-sample edge", is_hard_gate=True)
    ratio = out_of_sample_sharpe / in_sample_sharpe
    if ratio < OOS_SHARPE_RATIO_FLOOR:
        return GateResult("Gate 2 Out-of-Sample", GateStatus.FAIL,
                          f"OOS Sharpe {out_of_sample_sharpe:.2f} = {ratio:.0%} of IS "
                          f"(need {OOS_SHARPE_RATIO_FLOOR:.0%}) — likely overfit",
                          is_hard_gate=True)
    return GateResult("Gate 2 Out-of-Sample", GateStatus.PASS,
                      f"OOS Sharpe {out_of_sample_sharpe:.2f} = {ratio:.0%} of IS",
                      is_hard_gate=True)


def evaluate_gate3_walk_forward(wf: WalkForwardResult) -> GateResult:
    """Gate 3 — Walk-Forward (delegates to walk_forward module)."""
    status = GateStatus.PASS if wf.passed else GateStatus.FAIL
    return GateResult("Gate 3 Walk-Forward", status,
                      f"WF Sharpe {wf.stitched_sharpe:.2f}, eff {wf.walk_forward_efficiency:.2f}",
                      is_hard_gate=True)


def evaluate_gate4_monte_carlo(mc: MonteCarloResult) -> GateResult:
    """Gate 4 — Monte Carlo (delegates to monte_carlo module)."""
    status = GateStatus.PASS if mc.passed else GateStatus.FAIL
    return GateResult("Gate 4 Monte Carlo", status,
                      f"p={mc.p_value:.3f}, realistic DD {mc.realistic_max_drawdown:.0%}",
                      is_hard_gate=True)


def evaluate_gate5_cost_stress(sharpe_at_2x_cost: float) -> GateResult:
    """Gate 5 — Transaction Cost Stress. Survives 2x expected cost?"""
    if sharpe_at_2x_cost < COST_STRESS_SHARPE_FLOOR:
        return GateResult("Gate 5 Cost Stress", GateStatus.FAIL,
                          f"Sharpe {sharpe_at_2x_cost:.2f} at 2x cost "
                          f"(<{COST_STRESS_SHARPE_FLOOR}) — edge < costs",
                          is_hard_gate=True)
    return GateResult("Gate 5 Cost Stress", GateStatus.PASS,
                      f"Sharpe {sharpe_at_2x_cost:.2f} at 2x cost", is_hard_gate=True)


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
        return GateResult("Gate 6 Param Sensitivity", GateStatus.WARN,
                          "no sweep provided", is_hard_gate=False)
    avg_neighbor = sum(neighbor_sharpes) / len(neighbor_sharpes)
    # If neighbors hold up reasonably (>=70% of chosen), it's a plateau = good.
    if chosen_sharpe > 0 and avg_neighbor >= 0.70 * chosen_sharpe:
        return GateResult("Gate 6 Param Sensitivity", GateStatus.PASS,
                          f"neighbors avg {avg_neighbor:.2f} vs {chosen_sharpe:.2f} — robust plateau",
                          is_hard_gate=False)
    return GateResult("Gate 6 Param Sensitivity", GateStatus.WARN,
                      f"neighbors avg {avg_neighbor:.2f} vs {chosen_sharpe:.2f} — sharp peak, possible overfit",
                      is_hard_gate=False)


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
        return GateResult("Gate 7 Benchmark/Correlation", GateStatus.PASS,
                          why, is_hard_gate=False)
    return GateResult("Gate 7 Benchmark/Correlation", GateStatus.WARN,
                      f"neither beats SPY nor diversifies (corr {correlation_to_benchmark:.2f})",
                      is_hard_gate=False)


def evaluate_gate8_deflated_sharpe(
    returns: list[float],
    num_trials: int,
    *,
    trial_sharpes: list[float] | None = None,
    periods_per_year: int = metrics.TRADING_DAYS_PER_YEAR,
) -> GateResult:
    """
    Gate 8 — Deflated Sharpe Ratio (HARD). Is the headline Sharpe real once you
    account for sample length, fat tails, AND how many configurations were tried?

    Uses the Deflated Sharpe Ratio when the spread of the tried-strategy Sharpes
    is available (``num_trials`` > 1 and ``trial_sharpes`` provided / inferable);
    otherwise falls back to the Probabilistic Sharpe Ratio against a zero
    reference (the weaker "is it even non-zero?" question), and SAYS which it used.

    FAILS CLOSED: too few returns, or a probability below the bar, → FAIL.
    """
    if len(returns) < 4:
        return GateResult("Gate 8 Deflated Sharpe", GateStatus.FAIL,
                          f"only {len(returns)} returns — cannot deflate, fail closed",
                          is_hard_gate=True)

    used_dsr = num_trials >= 2 and trial_sharpes is not None and len(trial_sharpes) >= 2
    if used_dsr:
        prob = metrics.deflated_sharpe_ratio(
            returns, num_trials=num_trials, trial_sharpes=trial_sharpes,
            periods_per_year=periods_per_year,
        )
        bar = MIN_DEFLATED_SHARPE_PROB
        label = f"DSR p={prob:.2f} over {num_trials} trials"
    else:
        prob = metrics.probabilistic_sharpe_ratio(
            returns, reference_sharpe=0.0, periods_per_year=periods_per_year,
        )
        bar = MIN_PROBABILISTIC_SHARPE
        label = f"PSR p={prob:.2f} vs SR=0 (no trial count → fell back to PSR)"

    if prob < bar:
        return GateResult("Gate 8 Deflated Sharpe", GateStatus.FAIL,
                          f"{label} < {bar:.2f} — Sharpe likely a selection-bias mirage",
                          is_hard_gate=True)
    return GateResult("Gate 8 Deflated Sharpe", GateStatus.PASS, label,
                      is_hard_gate=True)


def evaluate_gate9_pbo(
    performance_matrix: list[list[float]],
    *,
    n_splits: int = 16,
    seed: int = 42,
) -> GateResult:
    """
    Gate 9 — Probability of Backtest Overfitting via CSCV (HARD).

    ``performance_matrix[t][c]`` = performance of configuration ``c`` in time
    slice ``t``. PBO is the fraction of combinatorial splits where the in-sample
    champion landed below-median out of sample. >= MAX_PBO ⇒ selecting by
    backtest is no better than a coin flip ⇒ FAIL.

    FAILS CLOSED: an empty / unusable matrix yields PBO=1.0 ⇒ FAIL.
    """
    if not performance_matrix or len(performance_matrix) < 4:
        return GateResult("Gate 9 PBO (CSCV)", GateStatus.FAIL,
                          "matrix too small to run CSCV — fail closed",
                          is_hard_gate=True)
    pbo = metrics.probability_of_backtest_overfitting(
        performance_matrix, n_splits=n_splits, seed=seed,
    )
    if pbo >= MAX_PBO:
        return GateResult("Gate 9 PBO (CSCV)", GateStatus.FAIL,
                          f"PBO {pbo:.0%} >= {MAX_PBO:.0%} — config selection no better than luck",
                          is_hard_gate=True)
    return GateResult("Gate 9 PBO (CSCV)", GateStatus.PASS,
                      f"PBO {pbo:.0%} < {MAX_PBO:.0%}", is_hard_gate=True)


def evaluate_gate10_capacity(
    num_trades: int,
    annualized_return_estimate: float,
    annual_turnover: float,
    *,
    cost_per_turn: float = 0.001,
    min_trades_floor: int = MIN_TRADES_FLOOR,
) -> GateResult:
    """
    Gate 10 — Capacity & Turnover Sanity (HARD).

    Two fail-closed guards rolled into one gate:
      * a HARD minimum trade count (below ``min_trades_floor`` a Sharpe is not
        statistically credible regardless of cadence), and
      * a capacity ratio: gross annualized return must cover its annual trading
        cost (turnover × cost_per_turn) at least ``MIN_CAPACITY_SCORE`` times.

    Catches the high-turnover "edge" that is real but smaller than its costs, and
    the lucky few-trade chart that no statistic can defend.
    """
    failures: list[str] = []
    if num_trades < min_trades_floor:
        failures.append(f"{num_trades} trades<{min_trades_floor} floor")

    cap = metrics.capacity_score(annualized_return_estimate, annual_turnover,
                                 cost_per_turn=cost_per_turn)
    cap_label = "inf" if cap == float("inf") else f"{cap:.1f}x"
    if cap < MIN_CAPACITY_SCORE:
        failures.append(f"capacity {cap_label}<{MIN_CAPACITY_SCORE:.0f}x cost")

    if failures:
        return GateResult("Gate 10 Capacity/Turnover", GateStatus.FAIL,
                          "; ".join(failures), is_hard_gate=True)
    return GateResult("Gate 10 Capacity/Turnover", GateStatus.PASS,
                      f"{num_trades} trades, capacity {cap_label} cost", is_hard_gate=True)


def grade_and_assemble(strategy_name: str, gates: list[GateResult],
                       realistic_dd: float, validated_sharpe: float) -> GauntletReport:
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
        notes=notes,
    )
