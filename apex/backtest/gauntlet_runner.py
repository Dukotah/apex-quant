"""
apex.backtest.gauntlet_runner
============================
Drives a strategy through all seven Validation Gauntlet gates against the real
backtester and produces a graded GauntletReport.

This is the piece docs/VALIDATION_GAUNTLET.md described as "needs the Phase 5
backtester to feed real per-window equity curves." It connects the engine to the
already-built validation layer:

    strategy + events ─▶ backtests ─▶ (equity curves, trade returns)
                                   └─▶ gates 1-7 ─▶ graded report

The runner makes NO claim of profitability. It runs the real machinery and
reports whatever grade emerges — including an honest FAIL when, for example, a
low-turnover strategy doesn't produce the >=50 trades Gate 1 demands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, List, Optional, Sequence, Tuple

from apex.backtest.backtester import run_backtest
from apex.core.events import MarketEvent
from apex.core.models import Symbol
from apex.data.historical_feed import HistoricalDataFeed
from apex.risk.risk_manager import RiskConfig
from apex.strategy.base_strategy import BaseStrategy
from apex.validation import gauntlet, metrics, pbo
from apex.validation.monte_carlo import run_monte_carlo
from apex.validation.walk_forward import run_walk_forward

logger = logging.getLogger(__name__)

StrategyFactory = Callable[[], BaseStrategy]


# --------------------------------------------------------------------- helpers


def _unique_days(events: Sequence[MarketEvent]) -> List[datetime]:
    """Ordered list of distinct bar timestamps (one entry per trading day)."""
    seen: List[datetime] = []
    last = None
    for ev in events:
        ts = ev.bar.timestamp
        if ts != last:
            seen.append(ts)
            last = ts
    return seen


def _events_in_day_range(
    events: Sequence[MarketEvent], lo: datetime, hi: datetime
) -> List[MarketEvent]:
    """Events whose bar timestamp is in [lo, hi)."""
    return [ev for ev in events if lo <= ev.bar.timestamp < hi]


def _benchmark_equity(events: Sequence[MarketEvent], ticker: str) -> List[float]:
    """Buy-and-hold equity (the ticker's daily closes) for Gate 7."""
    return [float(ev.bar.close) for ev in events if ev.bar.symbol.ticker == ticker]


@dataclass
class GauntletInputs:
    """The raw measurements behind the gates (handy for tests/inspection)."""

    in_sample_sharpe: float
    out_of_sample_sharpe: float
    num_trades: int
    full_sharpe: float
    sharpe_at_2x_cost: float
    benchmark_sharpe: float
    correlation_to_benchmark: float


def run_full_gauntlet(
    strategy_name: str,
    strategy_factory: StrategyFactory,
    events: Sequence[MarketEvent],
    risk_config: RiskConfig,
    benchmark_ticker: str,
    initial_capital: Decimal = Decimal("100000"),
    slippage_pct: Decimal = Decimal("0.001"),
    train_frac: float = 0.70,
    param_variants: Optional[Sequence[Tuple[str, StrategyFactory]]] = None,
    wf_train_bars: Optional[int] = None,
    wf_test_bars: Optional[int] = None,
    mc_iterations: int = 2000,
    rebalance_period_bars: Optional[int] = None,
    run_mcpt: bool = False,
    mcpt_iterations: int = 200,
) -> Tuple[gauntlet.GauntletReport, GauntletInputs]:
    """
    Run all seven gates and return (report, inputs).

    Args:
        strategy_name:   label for the report.
        strategy_factory: builds a FRESH strategy each call (walk-forward needs many).
        events:          chronological MarketEvents (e.g. synthetic.interleave(...)).
        risk_config:     the RiskConfig to size with (single-strategy backtests
                         typically allow full deployment).
        benchmark_ticker: ticker used as the buy-and-hold benchmark (Gate 7).
        param_variants:  optional [(label, factory), ...] for the Gate 6 sweep
                         (e.g. lookback ±20%). If None, Gate 6 warns "no sweep".
        rebalance_period_bars: the strategy's rebalance cadence in bars (e.g. ~21
                         for a monthly strategy). When given, Gate 1's trade-count
                         minimum is made regime-aware so a low-frequency strategy
                         isn't failed for a cadence the window can't accommodate.
                         None / 1 → the full MIN_TRADES bar (daily strategies).
    """
    days = _unique_days(events)
    n_days = len(days)
    if n_days < 4:
        raise ValueError("Not enough data to run the Gauntlet")
    split_idx = int(train_frac * n_days)
    split_day = days[split_idx]

    # --- One full backtest for the headline curve + trades. ---
    full = run_backtest(
        list(events),
        strategy_factory(),
        risk_config,
        initial_capital=initial_capital,
        slippage_pct=slippage_pct,
    )
    full_returns = metrics.returns_from_equity(full.equity_curve)
    full_sharpe = metrics.sharpe_ratio(full_returns)

    # Split the daily equity + trades into in-sample / out-of-sample by date.
    in_eq = [e for e, ts in zip(full.equity_curve, full.equity_timestamps) if ts < split_day]
    oos_eq = [e for e, ts in zip(full.equity_curve, full.equity_timestamps) if ts >= split_day]
    in_trades = [r for r, ts in zip(full.trade_returns, full.trade_timestamps) if ts < split_day]

    in_sharpe = metrics.sharpe_ratio(metrics.returns_from_equity(in_eq))
    oos_sharpe = metrics.sharpe_ratio(metrics.returns_from_equity(oos_eq))

    # ---- Gate 1: In-sample sanity (training period). ----
    # Make the trade-count minimum fair to the strategy's rebalance cadence over
    # the in-sample window (a monthly strategy can't show 50 trades in 2 years).
    g1_min_trades = gauntlet.regime_aware_min_trades(len(in_eq), rebalance_period_bars or 1)
    g1 = gauntlet.evaluate_gate1_in_sample(in_eq, in_trades, min_trades=g1_min_trades)

    # ---- Gate 2: Out-of-sample holdout. ----
    g2 = gauntlet.evaluate_gate2_out_of_sample(in_sharpe, oos_sharpe)

    # ---- Gate 3: Walk-forward (rolling, with warm-up per window). ----
    train_bars = wf_train_bars or max(60, n_days // 4)
    test_bars = wf_test_bars or max(20, n_days // 8)

    def wf_backtest_fn(tr_start: int, tr_end: int, te_start: int, te_end: int) -> List[float]:
        # Warm the strategy up on [tr_start, te_start) then measure on [te_start, te_end).
        warm_lo = days[tr_start]
        test_lo = days[te_start]
        # te_end is an EXCLUSIVE day index. The final fold has te_end == n_days, so we
        # can't index days[te_end]; use a sentinel one day past the last bar to KEEP the
        # last trading day (the exclusive `< test_hi` filter would otherwise drop it).
        test_hi = days[te_end] if te_end < n_days else days[-1] + timedelta(days=1)
        window_events = _events_in_day_range(events, warm_lo, test_hi)
        if len(window_events) < 2:
            return [1.0, 1.0]
        res = run_backtest(
            window_events,
            strategy_factory(),
            risk_config,
            initial_capital=initial_capital,
            slippage_pct=slippage_pct,
        )
        curve = [e for e, ts in zip(res.equity_curve, res.equity_timestamps) if ts >= test_lo]
        return curve if len(curve) >= 2 else [1.0, 1.0]

    wf = run_walk_forward(n_days, wf_backtest_fn, train_bars=train_bars, test_bars=test_bars)
    g3 = gauntlet.evaluate_gate3_walk_forward(wf)

    # ---- Gate 4: Monte Carlo on realized trades. ----
    mc = run_monte_carlo(full.trade_returns, iterations=mc_iterations)
    g4 = gauntlet.evaluate_gate4_monte_carlo(mc)

    # ---- Gate 5: Cost stress at 2x slippage. ----
    stressed = run_backtest(
        list(events),
        strategy_factory(),
        risk_config,
        initial_capital=initial_capital,
        slippage_pct=slippage_pct * Decimal("2"),
    )
    sharpe_2x = metrics.sharpe_ratio(metrics.returns_from_equity(stressed.equity_curve))
    g5 = gauntlet.evaluate_gate5_cost_stress(sharpe_2x)

    # ---- Gate 6: Parameter sensitivity (optional sweep). ----
    # Retain each variant's full equity curve too: those configurations ARE the
    # field Gate 9 (PBO/CSCV) tests for overfitting, not just their scalar Sharpe.
    neighbor_sharpes: List[float] = []
    variant_curves: List[List[float]] = []
    for _, variant_factory in param_variants or []:
        v = run_backtest(
            list(events),
            variant_factory(),
            risk_config,
            initial_capital=initial_capital,
            slippage_pct=slippage_pct,
        )
        variant_curves.append(list(v.equity_curve))
        neighbor_sharpes.append(metrics.sharpe_ratio(metrics.returns_from_equity(v.equity_curve)))
    g6 = gauntlet.evaluate_gate6_param_sensitivity(neighbor_sharpes, full_sharpe)

    # ---- Gate 7: Benchmark & correlation. ----
    bench_eq = _benchmark_equity(events, benchmark_ticker)
    bench_returns = metrics.returns_from_equity(bench_eq)
    bench_sharpe = metrics.sharpe_ratio(bench_returns)
    corr = metrics.correlation(full_returns, bench_returns)
    g7 = gauntlet.evaluate_gate7_benchmark(full_sharpe, bench_sharpe, corr)

    # ---- Gate 8: Overfitting / Deflated Sharpe (soft). ----
    # Reuse the Gate-6 sweep as the trial population: the chosen strategy plus every
    # variant we backtested ARE the multiple tests the Deflated Sharpe must correct
    # for. With no sweep this is a single trial and DSR collapses to PSR.
    trial_sharpes = [full_sharpe, *neighbor_sharpes]
    g8, overfit = gauntlet.evaluate_gate8_overfitting(full_returns, trial_sharpes)

    # ---- Gate 9: Probability of Backtest Overfitting (CSCV, soft). ----
    # Reuse the Gate-6 configurations (chosen strategy + each variant) as the
    # parameter field. An even slice count >= 4 is required; with no sweep the
    # matrix is empty and the gate passes with a "not evaluated" note.
    config_curves = [list(full.equity_curve), *variant_curves]
    n_slices = 16 if len(full.equity_curve) >= 17 else (len(full.equity_curve) - 1)
    if n_slices % 2 != 0:
        n_slices -= 1
    perf_matrix = pbo.build_performance_matrix(config_curves, n_slices)
    g9, pbo_value = gauntlet.evaluate_gate9_pbo(perf_matrix)

    gates = [g1, g2, g3, g4, g5, g6, g7, g8, g9]
    report = gauntlet.grade_and_assemble(
        strategy_name,
        gates,
        realistic_dd=mc.realistic_max_drawdown,
        validated_sharpe=wf.stitched_sharpe,
    )
    # Surface the overfitting numbers in the report (mutating the notes list is fine
    # even on the frozen report — we're appending, not rebinding the attribute).
    report.notes.append(overfit.summary())
    if perf_matrix:
        report.notes.append(f"PBO (CSCV) {pbo_value:.0%} across {len(config_curves)} configs.")

    # Optional stronger Gate-4 companion: the Monte-Carlo PERMUTATION test re-runs the
    # whole strategy on shuffled price paths to test the SIGNAL LOGIC (not just realized
    # trades). OFF by default (it re-backtests many times); surfaced as a report note so
    # it never changes the gate list or grade.
    if run_mcpt:
        from apex.validation.permutation import monte_carlo_permutation_test

        mcpt = monte_carlo_permutation_test(
            list(events),
            strategy_factory,
            risk_config,
            iterations=mcpt_iterations,
            slippage_pct=slippage_pct,
        )
        report.notes.append(mcpt.summary())
    inputs = GauntletInputs(
        in_sample_sharpe=in_sharpe,
        out_of_sample_sharpe=oos_sharpe,
        num_trades=len(full.trade_returns),
        full_sharpe=full_sharpe,
        sharpe_at_2x_cost=sharpe_2x,
        benchmark_sharpe=bench_sharpe,
        correlation_to_benchmark=corr,
    )
    return report, inputs


def run_gauntlet_from_csv(
    strategy_name: str,
    strategy_factory: StrategyFactory,
    path: str,
    symbols: Sequence[Symbol],
    benchmark_ticker: str,
    **kwargs,
) -> Tuple[gauntlet.GauntletReport, GauntletInputs]:
    """
    Run the full Gauntlet on REAL history from an OHLCV CSV/Parquet file.

    Loads the file through HistoricalDataFeed (UTC-normalized, validated,
    chronological), then runs the same seven-gate pipeline used for synthetic
    data. This is the one-call path to "validate a strategy on actual history" —
    swap in any real OHLCV file and nothing else changes.
    """
    feed = HistoricalDataFeed(symbols, path)
    feed.connect()
    try:
        events = list(feed.stream())
    finally:
        feed.disconnect()
    return run_full_gauntlet(
        strategy_name, strategy_factory, events, benchmark_ticker=benchmark_ticker, **kwargs
    )
