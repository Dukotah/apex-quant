"""
scripts/allocate.py
===================
Phase F3.2 — does pairing the deployed TREND edge with the VALUE edge actually lift the
combined risk-adjusted return? A second edge is only worth running if the BLEND beats the
best single sleeve (that was the whole point — Session 16/22).

METHOD (mirrors scripts/value_vs_trend.py, cross-universe). Backtest each edge on its own
universe with full capital, take the daily return streams, align them on common dates, then
for a sweep of capital weights w report the blended Sharpe, the blended max drawdown, and the
(constant) correlation between the two return streams. A low correlation + a blended Sharpe
above either standalone = a real diversification win.

  TREND: multi_asset_trend, smart-7 ETFs (the DEPLOYED strategy).
  VALUE: cross_asset_value single-names + hysteresis (the F3.1 second-edge candidate).

RESEARCH ONLY — this does NOT wire a second strategy into run_once. It quantifies whether
building the live allocation engine is worth it. Deterministic; no RNG.

NOTE: both edges are measured on SURVIVOR universes; per DECISIONS S28 the live-capital gate
stays blocked on survivorship-free validation (W8) regardless of what the blend shows here.

Run:  python -m scripts.allocate
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from decimal import Decimal

from apex.backtest.backtester import run_backtest
from apex.core.models import AssetClass, Symbol
from apex.data.historical_feed import HistoricalDataFeed
from apex.risk.risk_manager import RiskConfig
from apex.strategy.library.multi_asset_trend import MultiAssetTrendStrategy
from apex.validation import metrics
from scripts.survivorship_stress import _UNIVERSE as _VALUE_NAMES
from scripts.survivorship_stress import _risk as _value_risk
from scripts.survivorship_stress import _strategy as _value_strategy

_SLIPPAGE = Decimal("0.001")
_TREND_DATA = "data/real/multiasset_smart7.csv"
_VALUE_DATA = "data/real/single_names.csv"
_TREND_ASSETS = ["SPY", "EFA", "TLT", "GLD", "DBC", "UUP", "DBA"]


def _trend_strategy():
    syms = [Symbol(t, AssetClass.ETF) for t in _TREND_ASSETS]
    return MultiAssetTrendStrategy("multi_trend_s7", syms, fast_period=20, slow_period=200)


def _trend_risk() -> RiskConfig:
    return RiskConfig(
        max_position_size_pct=Decimal("0.16"),
        max_total_exposure_pct=Decimal("1.0"),
        max_leverage=Decimal("1.0"),
        max_drawdown_pct=Decimal("0.99"),
        max_daily_loss_pct=Decimal("0.99"),
        require_stop_loss=True,
    )


# ----------------------------------------------------------------- pure helpers


def returns_by_date(equity: list[float], timestamps: list) -> dict[date, float]:
    """Map each date to that day's return. returns[i] pairs with timestamps[i+1]."""
    rets = metrics.returns_from_equity(equity)
    out: dict[date, float] = {}
    for i, r in enumerate(rets):
        out[timestamps[i + 1].date()] = r
    return out


def align(
    a: dict[date, float], b: dict[date, float]
) -> tuple[list[date], list[float], list[float]]:
    """Common dates (sorted) and the two aligned return lists."""
    common = sorted(set(a) & set(b))
    return common, [a[d] for d in common], [b[d] for d in common]


def blend(trend: list[float], value: list[float], w_value: float) -> list[float]:
    """Daily blended return for a value weight w_value (trend gets 1 - w_value)."""
    wt = 1.0 - w_value
    return [wt * t + w_value * v for t, v in zip(trend, value)]


def equity_from_returns(rets: list[float]) -> list[float]:
    """Rebuild a normalized equity curve from a daily return series."""
    eq = [1.0]
    for r in rets:
        eq.append(eq[-1] * (1.0 + r))
    return eq


# ----------------------------------------------------------------- run


def _load(path: str, tickers: list[str], extra: list[str] | None = None) -> list:
    syms = [Symbol(t, AssetClass.EQUITY if extra else AssetClass.ETF) for t in tickers]
    if extra:
        syms += [Symbol(t, AssetClass.ETF) for t in extra]
    feed = HistoricalDataFeed(syms, path)
    feed.connect()
    try:
        return list(feed.stream())
    finally:
        feed.disconnect()


def _utf8() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def main() -> int:
    _utf8()
    logging.disable(logging.WARNING)
    try:
        trend_events = _load(_TREND_DATA, _TREND_ASSETS)
        value_events = _load(_VALUE_DATA, _VALUE_NAMES, extra=["SPY"])
    except Exception as exc:  # noqa: BLE001
        print(f"Could not load data: {exc}", file=sys.stderr)
        print("Regenerate the CSVs — see commands in scripts/validate_real.py.", file=sys.stderr)
        return 1

    print(
        "Allocation backtest: TREND (smart-7) + VALUE (single-names). Full capital each, "
        "then blend daily returns.\nRunning both backtests ...",
        flush=True,
    )
    tr = run_backtest(trend_events, _trend_strategy(), _trend_risk(), slippage_pct=_SLIPPAGE)
    vr = run_backtest(value_events, _value_strategy(), _value_risk(), slippage_pct=_SLIPPAGE)

    dates, t_rets, v_rets = align(
        returns_by_date(tr.equity_curve, tr.equity_timestamps),
        returns_by_date(vr.equity_curve, vr.equity_timestamps),
    )
    if len(dates) < 2:
        print("Not enough overlapping dates to blend.", file=sys.stderr)
        return 1

    corr = metrics.correlation(t_rets, v_rets)
    print(f"\nOverlap: {len(dates)} days, {dates[0]}..{dates[-1]}")
    print(f"Correlation(trend, value) daily returns = {corr:+.2f}\n")

    print(f"{'value wt':>8}  {'Sharpe':>7}  {'maxDD':>6}")
    print("-" * 26)
    best = None
    for w in [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0]:
        b = blend(t_rets, v_rets, w)
        sh = metrics.sharpe_ratio(b)
        dd = metrics.max_drawdown(equity_from_returns(b))
        print(f"{w:>8.0%}  {sh:>7.2f}  {dd:>6.0%}", flush=True)
        if best is None or sh > best[1]:
            best = (w, sh, dd)

    trend_sh = metrics.sharpe_ratio(t_rets)
    value_sh = metrics.sharpe_ratio(v_rets)
    lift = best[1] - max(trend_sh, value_sh)
    print(
        f"\nStandalone: trend Sharpe {trend_sh:.2f}, value Sharpe {value_sh:.2f}. "
        f"Best blend: {best[0]:.0%} value → Sharpe {best[1]:.2f} (maxDD {best[2]:.0%})."
    )
    if lift > 0.05 and corr < 0.5:
        print(
            f"VERDICT: DIVERSIFICATION WIN — best blend Sharpe {best[1]:.2f} beats the best "
            f"standalone by {lift:+.2f} at correlation {corr:+.2f}. A real second edge."
        )
    else:
        print(
            f"VERDICT: marginal — best blend lifts Sharpe by only {lift:+.2f} (corr {corr:+.2f}); "
            f"the second edge does not clearly improve the combined book."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
