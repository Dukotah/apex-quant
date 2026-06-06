"""
apex.validation.walk_forward
============================
Gate 3 of the Validation Gauntlet: Walk-Forward Analysis.

The most realistic non-overfit test there is. Instead of one train/test split,
it rolls a window through history the way you'd ACTUALLY deploy a strategy:

    [---- train ----][test]
              [---- train ----][test]
                        [---- train ----][test]   ...

Each test window is unseen at the time. We stitch all the test windows into one
continuous out-of-sample equity curve. If that stitched curve is strong, the
strategy works on data it never trained on, repeatedly, across regimes.

This module provides the windowing/orchestration framework. The actual per-window
backtest is injected as a callable (so it works with the Phase 5 backtester once
that's built). Pure stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from apex.validation import metrics


@dataclass(frozen=True)
class WalkForwardWindow:
    """One train/test fold."""

    train_start: int
    train_end: int  # exclusive
    test_start: int
    test_end: int  # exclusive


@dataclass(frozen=True)
class WalkForwardResult:
    """Aggregate outcome across all folds."""

    stitched_sharpe: float
    stitched_max_drawdown: float
    stitched_total_return: float
    in_sample_sharpe: float
    walk_forward_efficiency: float  # WF return / in-sample return
    num_windows: int
    worst_window_drawdown: float
    passed: bool

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        return (
            f"Walk-Forward [{verdict}]: stitched Sharpe {self.stitched_sharpe:.2f}, "
            f"efficiency {self.walk_forward_efficiency:.2f}, "
            f"{self.num_windows} windows, worst-window DD {self.worst_window_drawdown:.1%}"
        )


def generate_windows(
    total_bars: int,
    train_bars: int,
    test_bars: int,
    step_bars: int | None = None,
) -> list[WalkForwardWindow]:
    """
    Build the rolling train/test folds.

    Args:
        total_bars: length of the full dataset.
        train_bars: bars in each training window (e.g. 504 = ~2 years daily).
        test_bars: bars in each test window (e.g. 126 = ~6 months daily).
        step_bars: how far to slide each fold (defaults to test_bars = non-overlapping
                   test windows, which is what you want for a clean stitched curve).
    """
    if step_bars is None:
        step_bars = test_bars
    windows: list[WalkForwardWindow] = []
    train_start = 0
    while True:
        train_end = train_start + train_bars
        test_start = train_end
        test_end = test_start + test_bars
        if test_end > total_bars:
            break
        windows.append(WalkForwardWindow(train_start, train_end, test_start, test_end))
        train_start += step_bars
    return windows


def run_walk_forward(
    total_bars: int,
    backtest_fn: Callable[[int, int, int, int], list[float]],
    train_bars: int = 504,
    test_bars: int = 126,
    step_bars: int | None = None,
    min_stitched_sharpe: float = 0.7,
    min_efficiency: float = 0.5,
) -> WalkForwardResult:
    """
    Run walk-forward analysis.

    Args:
        total_bars: length of the dataset.
        backtest_fn: a callable (train_start, train_end, test_start, test_end) ->
                     test-window equity curve (list[float]). This is where the
                     Phase 5 backtester plugs in. It should optimize/fit on the
                     train range and report equity on the test range ONLY.
        train_bars, test_bars, step_bars: window geometry.
        min_stitched_sharpe / min_efficiency: pass thresholds.

    Returns a WalkForwardResult. Fails closed if there aren't enough windows.
    """
    windows = generate_windows(total_bars, train_bars, test_bars, step_bars)
    if len(windows) < 2:
        return WalkForwardResult(
            stitched_sharpe=0.0,
            stitched_max_drawdown=1.0,
            stitched_total_return=0.0,
            in_sample_sharpe=0.0,
            walk_forward_efficiency=0.0,
            num_windows=len(windows),
            worst_window_drawdown=1.0,
            passed=False,
        )

    # Stitch all out-of-sample test equity curves end to end.
    stitched: list[float] = [1.0]
    worst_window_dd = 0.0
    for w in windows:
        test_equity = backtest_fn(w.train_start, w.train_end, w.test_start, w.test_end)
        if len(test_equity) < 2:
            continue
        window_dd = metrics.max_drawdown(test_equity)
        worst_window_dd = max(worst_window_dd, window_dd)
        # Re-base this window's curve onto the end of the stitched curve.
        base = stitched[-1]
        start_val = test_equity[0] if test_equity[0] != 0 else 1.0
        for v in test_equity[1:]:
            stitched.append(base * (v / start_val))

    stitched_returns = metrics.returns_from_equity(stitched)
    stitched_sharpe = metrics.sharpe_ratio(stitched_returns)
    stitched_dd = metrics.max_drawdown(stitched)
    stitched_ret = metrics.total_return(stitched)

    # In-sample reference: backtest over the very first training window.
    is_equity = backtest_fn(0, train_bars, 0, train_bars)
    is_sharpe = metrics.sharpe_ratio(metrics.returns_from_equity(is_equity))
    is_ret = metrics.total_return(is_equity)
    efficiency = (stitched_ret / is_ret) if is_ret > 0 else 0.0

    passed = stitched_sharpe >= min_stitched_sharpe and efficiency >= min_efficiency

    return WalkForwardResult(
        stitched_sharpe=stitched_sharpe,
        stitched_max_drawdown=stitched_dd,
        stitched_total_return=stitched_ret,
        in_sample_sharpe=is_sharpe,
        walk_forward_efficiency=efficiency,
        num_windows=len(windows),
        worst_window_drawdown=worst_window_dd,
        passed=passed,
    )
