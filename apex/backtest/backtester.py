"""
apex.backtest.backtester
========================
Thin harness that runs a strategy through the TradingEngine and returns the
BacktestResult (equity curve + trade returns + fills). This is the adapter that
turns a strategy into the (equity_curve, trade_returns) the Validation Gauntlet
consumes.

It also exposes a slice-based backtest function compatible with the walk-forward
framework's `backtest_fn(train_start, train_end, test_start, test_end)` contract.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Callable, List

from apex.core.events import MarketEvent
from apex.execution.engine import BacktestResult, TradingEngine
from apex.execution.simulated import SimulatedExecutionEngine
from apex.risk.portfolio import Portfolio
from apex.risk.risk_manager import RiskConfig, RiskManager
from apex.strategy.base_strategy import BaseStrategy


def run_backtest(
    events: List[MarketEvent],
    strategy: BaseStrategy,
    risk_config: RiskConfig,
    initial_capital: Decimal = Decimal("100000"),
    slippage_pct: Decimal = Decimal("0.001"),
    commission_per_share: Decimal = Decimal("0"),
    fill_timing: str = "next_open",
) -> BacktestResult:
    """Run one strategy over `events` and return the result."""
    portfolio = Portfolio(initial_capital)
    risk = RiskManager(risk_config)
    execution = SimulatedExecutionEngine(
        slippage_pct=slippage_pct,
        commission_per_share=commission_per_share,
    )
    engine = TradingEngine(events, [strategy], risk, portfolio, execution, fill_timing=fill_timing)
    return engine.run()


def make_slice_backtest_fn(
    events: List[MarketEvent],
    strategy_factory: Callable[[], BaseStrategy],
    risk_config: RiskConfig,
    **kwargs,
) -> Callable[[int, int, int, int], List[float]]:
    """
    Build a walk-forward-compatible backtest_fn over an event list.

    The returned fn runs a FRESH strategy over events[test_start:test_end] and
    returns that window's equity curve (list[float]). The train range is accepted
    for API compatibility; these rules-based strategies are not re-fit per window,
    so training is a warm-up pass rather than an optimization.
    """

    def backtest_fn(
        train_start: int, train_end: int, test_start: int, test_end: int
    ) -> List[float]:
        window = events[test_start:test_end]
        if len(window) < 2:
            return [1.0, 1.0]
        result = run_backtest(window, strategy_factory(), risk_config, **kwargs)
        curve = result.equity_curve
        return curve if len(curve) >= 2 else [1.0, 1.0]

    return backtest_fn
