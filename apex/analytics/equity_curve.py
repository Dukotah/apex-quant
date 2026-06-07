"""
apex.analytics.equity_curve
===========================
Build an equity curve from an initial capital and either a period-return series
or a trade-PnL series. The output is the canonical input that
``apex.validation.metrics`` (Sharpe, drawdown, Calmar, ...) consumes.

This is analytics/metric code, not money-movement code: it lives in the same
statistical layer as ``apex.validation.metrics`` and follows that layer's
convention of using ``float`` (matching the ``Sequence[float]`` equity curves
that metrics.py expects). Position/cash bookkeeping that must be exact lives in
``apex.risk.portfolio`` and uses Decimal — that is a different layer.

All functions are pure and deterministic given their inputs. They degrade
gracefully on insufficient data: an empty series yields a single-point curve
holding just the initial capital, never garbage. Tested in
tests/test_equity_curve.py against hand-computed values.
"""

from __future__ import annotations

from typing import Sequence


def equity_curve_from_returns(
    initial_capital: float,
    returns: Sequence[float],
) -> list[float]:
    """
    Compound a series of period-over-period returns into an equity curve.

    Each return is a fraction (0.05 = +5%, -0.10 = -10%). The curve begins at
    ``initial_capital`` and is multiplied by ``(1 + r)`` for each return, so the
    result has ``len(returns) + 1`` points (the leading point is the starting
    capital).

    With no returns the curve is just ``[initial_capital]``.
    """
    equity = float(initial_capital)
    curve: list[float] = [equity]
    for r in returns:
        equity *= 1.0 + float(r)
        curve.append(equity)
    return curve


def equity_curve_from_pnl(
    initial_capital: float,
    pnl: Sequence[float],
) -> list[float]:
    """
    Accumulate a series of per-trade (or per-period) absolute PnL into an equity
    curve. Each value is a profit (+) or loss (-) in account currency.

    The curve begins at ``initial_capital`` and adds each PnL in turn, so the
    result has ``len(pnl) + 1`` points.

    With no PnL the curve is just ``[initial_capital]``.
    """
    equity = float(initial_capital)
    curve: list[float] = [equity]
    for p in pnl:
        equity += float(p)
        curve.append(equity)
    return curve


def returns_to_pnl(
    initial_capital: float,
    returns: Sequence[float],
) -> list[float]:
    """
    Convert a compounded period-return series into the absolute per-period PnL
    that produced it, given a starting capital.

    The i-th PnL is the change in equity over period i:
    ``equity[i+1] - equity[i]``. Summing these and adding initial_capital
    reproduces the final equity. Returns an empty list if there are no returns.
    """
    curve = equity_curve_from_returns(initial_capital, returns)
    return [curr - prev for prev, curr in zip(curve, curve[1:])]


def pnl_to_returns(
    initial_capital: float,
    pnl: Sequence[float],
) -> list[float]:
    """
    Convert an absolute per-period PnL series into the period-over-period
    returns it implies, given a starting capital.

    The i-th return is ``pnl[i] / equity_before_period_i``. If equity hits zero
    (or below) the period return is reported as 0.0 rather than dividing by zero
    or producing a nonsensical value (fail closed). Returns an empty list if
    there is no PnL.
    """
    out: list[float] = []
    equity = float(initial_capital)
    for p in pnl:
        if equity == 0.0:
            out.append(0.0)
        else:
            out.append(float(p) / equity)
        equity += float(p)
    return out


def normalize_curve(equity_curve: Sequence[float]) -> list[float]:
    """
    Rebase an equity curve so it starts at 1.0 (a growth-of-$1 curve). Useful
    for plotting and comparing strategies on a common scale.

    Returns an empty list for an empty input, and the original values unchanged
    if the first point is zero (cannot rebase off zero — fail closed).
    """
    if not equity_curve:
        return []
    base = equity_curve[0]
    if base == 0.0:
        return [float(v) for v in equity_curve]
    return [float(v) / base for v in equity_curve]


def final_equity(
    initial_capital: float,
    returns: Sequence[float],
) -> float:
    """
    The ending equity after compounding ``returns`` onto ``initial_capital``.
    Equivalent to ``equity_curve_from_returns(...)[-1]`` but without building the
    whole curve. With no returns it is just ``initial_capital``.
    """
    equity = float(initial_capital)
    for r in returns:
        equity *= 1.0 + float(r)
    return equity
