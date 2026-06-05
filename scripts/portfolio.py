"""
scripts/portfolio.py
====================
Multi-strategy portfolio analysis — can combining uncorrelated sleeves clear the
Sharpe >= 1.0 bar that no single long-only strategy reaches?

Method (the honest, no-overfitting version):
  1. Backtest each component strategy on the SAME real events (full capital each).
  2. Build the aligned daily-return matrix and the pairwise correlation matrix.
  3. On the IN-SAMPLE window (first 70%), grid-search long-only weights (sum to 1)
     for the max combined Sharpe.
  4. Report that combo's OUT-OF-SAMPLE Sharpe (last 30%) — weights chosen in-sample,
     judged out-of-sample, so a good number isn't just curve-fit to the whole period.

A portfolio of two return streams with Sharpes S1,S2 and correlation rho reaches,
at best weight, sqrt((S1^2 + S2^2 - 2*rho*S1*S2)/(1-rho^2)) — diversification only
helps to the extent the sleeves are genuinely uncorrelated. This script measures
whether the available strategies actually combine to >= 1.0.

Usage:
    python -m scripts.portfolio data/real/dm_long.csv
"""
from __future__ import annotations

import sys
from decimal import Decimal
from itertools import combinations_with_replacement
from typing import Dict, List, Tuple

import numpy as np

from apex.backtest.backtester import run_backtest
from apex.core.models import AssetClass, Symbol
from apex.data.historical_feed import HistoricalDataFeed
from apex.risk.risk_manager import RiskConfig
from apex.strategy.library.dual_momentum import DualMomentumStrategy
from apex.strategy.library.rsi2_mean_reversion import RSI2MeanReversionStrategy
from apex.strategy.library.rsi2_vol_filtered import RSI2VolFilteredStrategy
from apex.strategy.library.trend_bond import TrendBondStrategy
from apex.strategy.library.sma_crossover import SMACrossoverStrategy

SLEEVE_RISK = RiskConfig(
    max_position_size_pct=Decimal("1.0"), max_total_exposure_pct=Decimal("1.0"),
    max_leverage=Decimal("1.0"), max_drawdown_pct=Decimal("0.99"),
    max_daily_loss_pct=Decimal("0.99"), require_stop_loss=True,
)
TRADING_DAYS = 252.0


def _utf8():
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def _components(symbols):
    spy, agg = symbols[0], symbols[-1]
    return {
        "spy_trend":   lambda: SMACrossoverStrategy("spy_trend", [spy], fast_period=20, slow_period=200),
        "dual_mom":    lambda: DualMomentumStrategy("dual_mom", symbols, "SPY", "EFA", "AGG", lookback_window=252),
        "trend_bond":  lambda: TrendBondStrategy("trend_bond", [spy, agg], slow_period=200),
        "rsi2":        lambda: RSI2MeanReversionStrategy("rsi2", [spy], entry_threshold=Decimal("10")),
        "rsi2_vol":    lambda: RSI2VolFilteredStrategy("rsi2_vol", [spy], entry_threshold=Decimal("10")),
    }


def _daily_returns(equity: List[float]) -> np.ndarray:
    e = np.asarray(equity, dtype=float)
    return np.diff(e) / e[:-1]


def _sharpe(returns: np.ndarray) -> float:
    if returns.size < 2 or returns.std(ddof=1) == 0:
        return 0.0
    return float(returns.mean() / returns.std(ddof=1) * np.sqrt(TRADING_DAYS))


def _simplex_weights(n: int, step: int = 10):
    """All long-only weight vectors on an n-simplex in 1/step increments."""
    for combo in combinations_with_replacement(range(n), step):
        w = np.zeros(n)
        for c in combo:
            w[c] += 1.0 / step
        yield w


def main() -> None:
    _utf8()
    data = sys.argv[1] if len(sys.argv) > 1 else "data/real/dm_long.csv"
    symbols = [Symbol(t, AssetClass.ETF) for t in ("SPY", "EFA", "AGG")]

    feed = HistoricalDataFeed(symbols, data)
    feed.connect()
    events = list(feed.stream())
    feed.disconnect()

    comps = _components(symbols)
    names = list(comps.keys())
    print(f"Backtesting {len(names)} components on {data} ({len(events)} events)...\n")

    # Backtest each; collect aligned daily returns (all share the same trading days).
    rets: Dict[str, np.ndarray] = {}
    for name in names:
        res = run_backtest(events, comps[name](), SLEEVE_RISK, initial_capital=Decimal("100000"))
        rets[name] = _daily_returns(res.equity_curve)

    # Align to the shortest curve (warmups differ slightly).
    n = min(len(r) for r in rets.values())
    R = np.column_stack([rets[name][-n:] for name in names])   # (days, strategies)

    print("Standalone Sharpe (full period):")
    for i, name in enumerate(names):
        print(f"  {name:12s} {_sharpe(R[:, i]):.2f}")

    print("\nCorrelation matrix:")
    corr = np.corrcoef(R, rowvar=False)
    print("            " + "  ".join(f"{x[:8]:>8s}" for x in names))
    for i, name in enumerate(names):
        print(f"  {name:10s} " + "  ".join(f"{corr[i, j]:8.2f}" for j in range(len(names))))

    # In-sample (70%) weight search, out-of-sample (30%) validation.
    split = int(0.70 * n)
    R_is, R_oos = R[:split], R[split:]
    best = (-9.9, None)
    for w in _simplex_weights(len(names), step=10):
        s = _sharpe(R_is @ w)
        if s > best[0]:
            best = (s, w)
    is_sharpe, w = best
    oos_sharpe = _sharpe(R_oos @ w)
    full_sharpe = _sharpe(R @ w)

    print("\nBest in-sample long-only combination:")
    for name, wt in zip(names, w):
        if wt > 0:
            print(f"  {name:12s} {wt:.0%}")
    print(f"\n  In-sample Sharpe:     {is_sharpe:.2f}")
    print(f"  Out-of-sample Sharpe: {oos_sharpe:.2f}   <- the honest number (weights fixed from IS)")
    print(f"  Full-period Sharpe:   {full_sharpe:.2f}")
    print(f"\n  {'>= 1.0 CLEARED' if oos_sharpe >= 1.0 else 'below 1.0'} out-of-sample.")


if __name__ == "__main__":
    main()
