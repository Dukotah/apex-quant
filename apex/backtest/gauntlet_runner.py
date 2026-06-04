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
from datetime import datetime
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from apex.backtest.backtester import run_backtest
from apex.core.events import MarketEvent
from apex.core.models import Symbol
from apex.data.historical_feed import HistoricalDataFeed
from apex.strategy.base_strategy import BaseStrategy
from apex.validation import gauntlet, metrics
from apex.validation.monte_carlo import run_monte_carlo
from apex.validation.walk_forward import run_walk_forward
from apex.risk.risk_manager import RiskConfig

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


def _events_in_day_range(events: Sequence[MarketEvent], lo: datetime,
                         hi: datetime) -> List[MarketEvent]:
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
    """
    days = _unique_days(events)
    n_days = len(days)
    if n_days < 4:
        raise ValueError("Not enough data to run the Gauntlet")
    split_idx = int(train_frac * n_days)
    split_day = days[split_idx]

    # --- One full backtest for the headline curve + trades. ---
    full = run_backtest(list(events), strategy_factory(), risk_config,
                        initial_capital=initial_capital, slippage_pct=slippage_pct)
    full_returns = metrics.returns_from_equity(full.equity_curve)
    full_sharpe = metrics.sharpe_ratio(full_returns)

    # Split the daily equity + trades into in-sample / out-of-sample by date.
    in_eq = [e for e, ts in zip(full.equity_curve, full.equity_timestamps) if ts < split_day]
    oos_eq = [e for e, ts in zip(full.equity_curve, full.equity_timestamps) if ts >= split_day]
    in_trades = [r for r, ts in zip(full.trade_returns, full.trade_timestamps) if ts < split_day]

    in_sharpe = metrics.sharpe_ratio(metrics.returns_from_equity(in_eq))
    oos_sharpe = metrics.sharpe_ratio(metrics.returns_from_equity(oos_eq))

    # ---- Gate 1: In-sample sanity (training period). ----
    g1 = gauntlet.evaluate_gate1_in_sample(in_eq, in_trades)

    # ---- Gate 2: Out-of-sample holdout. ----
    g2 = gauntlet.evaluate_gate2_out_of_sample(in_sharpe, oos_sharpe)

    # ---- Gate 3: Walk-forward (rolling, with warm-up per window). ----
    train_bars = wf_train_bars or max(60, n_days // 4)
    test_bars = wf_test_bars or max(20, n_days // 8)

    def wf_backtest_fn(tr_start: int, tr_end: int, te_start: int, te_end: int) -> List[float]:
        # Warm the strategy up on [tr_start, te_start) then measure on [te_start, te_end).
        warm_lo = days[tr_start]
        test_lo = days[te_start]
        test_hi = days[min(te_end, n_days - 1)]
        window_events = _events_in_day_range(events, warm_lo, test_hi)
        if len(window_events) < 2:
            return [1.0, 1.0]
        res = run_backtest(window_events, strategy_factory(), risk_config,
                           initial_capital=initial_capital, slippage_pct=slippage_pct)
        curve = [e for e, ts in zip(res.equity_curve, res.equity_timestamps) if ts >= test_lo]
        return curve if len(curve) >= 2 else [1.0, 1.0]

    wf = run_walk_forward(n_days, wf_backtest_fn, train_bars=train_bars, test_bars=test_bars)
    g3 = gauntlet.evaluate_gate3_walk_forward(wf)

    # ---- Gate 4: Monte Carlo on realized trades. ----
    mc = run_monte_carlo(full.trade_returns, iterations=mc_iterations)
    g4 = gauntlet.evaluate_gate4_monte_carlo(mc)

    # ---- Gate 5: Cost stress at 2x slippage. ----
    stressed = run_backtest(list(events), strategy_factory(), risk_config,
                            initial_capital=initial_capital,
                            slippage_pct=slippage_pct * Decimal("2"))
    sharpe_2x = metrics.sharpe_ratio(metrics.returns_from_equity(stressed.equity_curve))
    g5 = gauntlet.evaluate_gate5_cost_stress(sharpe_2x)

    # ---- Gate 6: Parameter sensitivity (optional sweep). ----
    neighbor_sharpes: List[float] = []
    for _, variant_factory in (param_variants or []):
        v = run_backtest(list(events), variant_factory(), risk_config,
                         initial_capital=initial_capital, slippage_pct=slippage_pct)
        neighbor_sharpes.append(metrics.sharpe_ratio(metrics.returns_from_equity(v.equity_curve)))
    g6 = gauntlet.evaluate_gate6_param_sensitivity(neighbor_sharpes, full_sharpe)

    # ---- Gate 7: Benchmark & correlation. ----
    bench_eq = _benchmark_equity(events, benchmark_ticker)
    bench_returns = metrics.returns_from_equity(bench_eq)
    bench_sharpe = metrics.sharpe_ratio(bench_returns)
    corr = metrics.correlation(full_returns, bench_returns)
    g7 = gauntlet.evaluate_gate7_benchmark(full_sharpe, bench_sharpe, corr)

    gates = [g1, g2, g3, g4, g5, g6, g7]
    report = gauntlet.grade_and_assemble(
        strategy_name, gates,
        realistic_dd=mc.realistic_max_drawdown,
        validated_sharpe=wf.stitched_sharpe,
    )
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
    return run_full_gauntlet(strategy_name, strategy_factory, events,
                             benchmark_ticker=benchmark_ticker, **kwargs)
