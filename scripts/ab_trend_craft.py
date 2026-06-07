"""
scripts/ab_trend_craft.py
=========================
A/B the Session-31 trend-craft options against the DEPLOYED config on the real
smart-7 history. Same universe, same risk, same data — only the new knobs change,
so any difference is the knob and nothing else.

Variants:
  baseline   — deployed: simple realized vol, 20/200 SMA cross
  ewma       — EWMA (RiskMetrics λ=0.94) vol sizing, same 20/200 timing
  barbell    — multi-speed 63/200 price-vs-SMA vote, simple vol
  ewma+barbell — both

Reports full-period Sharpe, realized max drawdown, and trade count per variant.
RESEARCH measurement only — does NOT change the deployed strategy.

Run:  python -m scripts.ab_trend_craft
"""

from __future__ import annotations

import logging
import sys
from decimal import Decimal

from apex.backtest.backtester import run_backtest
from apex.core.models import AssetClass, Symbol
from apex.data.historical_feed import HistoricalDataFeed
from apex.risk.risk_manager import RiskConfig
from apex.strategy.library.multi_asset_trend import MultiAssetTrendStrategy
from apex.validation import metrics

_DATA = "data/real/multiasset_smart7.csv"
_ASSETS = ["SPY", "EFA", "TLT", "GLD", "DBC", "UUP", "DBA"]
_SLIPPAGE = Decimal("0.001")


def _risk() -> RiskConfig:
    return RiskConfig(
        max_position_size_pct=Decimal("0.16"),
        max_total_exposure_pct=Decimal("1.0"),
        max_leverage=Decimal("1.0"),
        max_drawdown_pct=Decimal("0.99"),
        max_daily_loss_pct=Decimal("0.99"),
        require_stop_loss=True,
    )


def _syms() -> list[Symbol]:
    return [Symbol(t, AssetClass.ETF) for t in _ASSETS]


_VARIANTS = {
    "baseline": {},
    "ewma": {"vol_method": "ewma"},
    "barbell": {"trend_lookbacks": [63, 200]},
    "ewma+barbell": {"vol_method": "ewma", "trend_lookbacks": [63, 200]},
}


def main() -> int:
    logging.disable(logging.WARNING)
    feed = HistoricalDataFeed(_syms(), _DATA)
    feed.connect()
    try:
        events = list(feed.stream())
    finally:
        feed.disconnect()
    if not events:
        print(f"No data in {_DATA}", file=sys.stderr)
        return 1

    print(f"A/B trend-craft on {_DATA} ({len(events)} bars, {_ASSETS})\n")
    print(f"{'variant':>14}  {'Sharpe':>7}  {'maxDD':>6}  {'trades':>7}  {'finalEq':>10}")
    print("-" * 56)
    for name, kwargs in _VARIANTS.items():
        strat = MultiAssetTrendStrategy("ab", _syms(), fast_period=20, slow_period=200, **kwargs)
        res = run_backtest(list(events), strat, _risk(), slippage_pct=_SLIPPAGE)
        rets = metrics.returns_from_equity(res.equity_curve)
        sharpe = metrics.sharpe_ratio(rets)
        dd = metrics.max_drawdown(res.equity_curve)
        print(
            f"{name:>14}  {sharpe:>7.2f}  {dd:>6.0%}  {res.num_trades:>7}  {res.final_equity:>10,.0f}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
