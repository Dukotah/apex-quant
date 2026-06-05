"""
scripts/value_vs_trend.py
=========================
The decisive second-edge measurement: is cross-asset VALUE actually uncorrelated to the
DEPLOYED multi-asset trend strategy, and does blending the two clear Sharpe >= 1.0?

Sessions 19-20 proved momentum-family signals are correlated to trend BY CONSTRUCTION and
short-horizon reversal fails long-only. Value (long-horizon reversal) is the remaining
candidate. A second edge is only worth deploying if it genuinely diversifies trend, so the
number that matters is NOT value's standalone Sharpe but its CORRELATION to trend — a
low-Sharpe sleeve with low/negative correlation can still lift the combined Sharpe:
    blended_best = sqrt((S1^2 + S2^2 - 2*rho*S1*S2) / (1 - rho^2))

Method mirrors scripts/portfolio.py: backtest both on the SAME smart-7 events (full
capital each), align daily returns, report correlation, and do an in-sample (70%) long-only
blend search validated out-of-sample (30%).

Run:  python -m scripts.value_vs_trend [data/real/multiasset_smart7.csv]
"""
from __future__ import annotations

import sys
from decimal import Decimal
from typing import Dict, List

import numpy as np

from apex.backtest.backtester import run_backtest
from apex.core.models import AssetClass, Symbol
from apex.data.historical_feed import HistoricalDataFeed
from apex.risk.risk_manager import RiskConfig
from apex.strategy.library.cross_asset_value import CrossAssetValueStrategy
from apex.strategy.library.multi_asset_trend import MultiAssetTrendStrategy

ASSETS = ["SPY", "EFA", "TLT", "GLD", "DBC", "UUP", "DBA"]
DEFAULT_DATA = "data/real/multiasset_smart7.csv"
TRADING_DAYS = 252.0

TREND_RISK = RiskConfig(
    max_position_size_pct=Decimal("0.16"), max_total_exposure_pct=Decimal("1.0"),
    max_leverage=Decimal("1.0"), max_drawdown_pct=Decimal("0.99"),
    max_daily_loss_pct=Decimal("0.99"), require_stop_loss=True,
)
VALUE_RISK = RiskConfig(
    max_position_size_pct=Decimal("0.34"), max_total_exposure_pct=Decimal("1.0"),
    max_leverage=Decimal("1.0"), max_drawdown_pct=Decimal("0.99"),
    max_daily_loss_pct=Decimal("0.99"), require_stop_loss=True,
)


def _utf8():
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def _daily_returns(equity: List[float]) -> np.ndarray:
    e = np.asarray(equity, dtype=float)
    return np.diff(e) / e[:-1]


def _sharpe(returns: np.ndarray) -> float:
    if returns.size < 2 or returns.std(ddof=1) == 0:
        return 0.0
    return float(returns.mean() / returns.std(ddof=1) * np.sqrt(TRADING_DAYS))


def main() -> None:
    _utf8()
    data = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DATA
    syms = [Symbol(t, AssetClass.ETF) for t in ASSETS]

    feed = HistoricalDataFeed(syms, data)
    feed.connect()
    events = list(feed.stream())
    feed.disconnect()
    print(f"Backtesting trend vs value on {data} ({len(events)} events)...\n")

    trend = run_backtest(
        events, MultiAssetTrendStrategy("trend", syms, fast_period=20, slow_period=200),
        TREND_RISK, initial_capital=Decimal("100000"))
    value = run_backtest(
        events, CrossAssetValueStrategy("value", syms, value_period=1260, skip_recent=252, top_k=3),
        VALUE_RISK, initial_capital=Decimal("100000"))

    rets: Dict[str, np.ndarray] = {
        "trend": _daily_returns(trend.equity_curve),
        "value": _daily_returns(value.equity_curve),
    }
    n = min(len(r) for r in rets.values())
    R = np.column_stack([rets["trend"][-n:], rets["value"][-n:]])

    print("Standalone Sharpe (full period):")
    print(f"  trend  {_sharpe(R[:, 0]):.2f}")
    print(f"  value  {_sharpe(R[:, 1]):.2f}")

    rho = float(np.corrcoef(R, rowvar=False)[0, 1])
    print(f"\nCorrelation(trend, value): {rho:+.2f}   "
          f"{'<- UNCORRELATED, a real diversifier' if abs(rho) < 0.3 else '<- too correlated to help' if rho > 0.5 else '<- mildly correlated'}")

    # In-sample (70%) long-only blend search, out-of-sample (30%) validation.
    split = int(0.70 * n)
    R_is, R_oos = R[:split], R[split:]
    best = (-9.9, 0.0)
    for i in range(11):
        w = np.array([i / 10.0, 1 - i / 10.0])
        s = _sharpe(R_is @ w)
        if s > best[0]:
            best = (s, w[0])
    is_sharpe, w_trend = best
    w = np.array([w_trend, 1 - w_trend])
    oos_sharpe = _sharpe(R_oos @ w)
    full_sharpe = _sharpe(R @ w)

    print(f"\nBest in-sample blend:  trend {w_trend:.0%} / value {1 - w_trend:.0%}")
    print(f"  In-sample Sharpe:     {is_sharpe:.2f}")
    print(f"  Out-of-sample Sharpe: {oos_sharpe:.2f}   <- the honest number (weights fixed from IS)")
    print(f"  Full-period Sharpe:   {full_sharpe:.2f}")
    print(f"\n  trend-alone full Sharpe {_sharpe(R[:, 0]):.2f}  ->  best blend {full_sharpe:.2f}  "
          f"({'blend HELPS' if full_sharpe > _sharpe(R[:, 0]) + 0.02 else 'no improvement'})")


if __name__ == "__main__":
    main()
