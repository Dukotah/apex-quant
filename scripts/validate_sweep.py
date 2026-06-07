"""
scripts/validate_sweep.py
=========================
The Session-31 PROVING phase. Run every candidate sleeve built this session through
the full Validation Gauntlet on REAL data AND measure its correlation to the deployed
TREND edge — the two questions that decide whether a "feature" earns a live slot:

    1. Does it clear the Gauntlet (grade)?      → is it a real edge, not luck/overfit?
    2. Is it uncorrelated to trend?             → does it actually diversify the book?

A sleeve is only interesting if BOTH hold. This is measurement only — it deploys nothing.

Run:  python -m scripts.validate_sweep
"""

from __future__ import annotations

import logging
import sys
import traceback
from decimal import Decimal

from apex.backtest.backtester import run_backtest
from apex.backtest.gauntlet_runner import run_full_gauntlet
from apex.core.models import AssetClass, Symbol
from apex.data.historical_feed import HistoricalDataFeed
from apex.risk.risk_manager import RiskConfig
from apex.strategy.library.bond_carry import BondCarryStrategy
from apex.strategy.library.breadth_momentum import BreadthMomentumStrategy
from apex.strategy.library.credit_spread import CreditSpreadRegimeStrategy
from apex.strategy.library.long_short_momentum import LongShortMomentumStrategy
from apex.strategy.library.multi_asset_trend import MultiAssetTrendStrategy
from apex.strategy.library.turn_of_month import TurnOfMonthStrategy
from apex.validation import metrics

_SWEEP = "data/real/sweep_universe.csv"
_SMART7 = "data/real/multiasset_smart7.csv"
_CRYPTO = "data/real/crypto.csv"
_LARGE = "data/real/largecaps.csv"
_SLIP = Decimal("0.001")


def _etf(tickers):
    return [Symbol(t, AssetClass.ETF) for t in tickers]


def _load(path, symbols):
    feed = HistoricalDataFeed(symbols, path)
    feed.connect()
    try:
        return list(feed.stream())
    finally:
        feed.disconnect()


def _returns_by_date(equity, ts):
    rets = metrics.returns_from_equity(equity)
    return {ts[i + 1].date(): r for i, r in enumerate(rets)}


def _corr_to_trend(sleeve_rets: dict, trend_rets: dict) -> float:
    common = sorted(set(sleeve_rets) & set(trend_rets))
    if len(common) < 30:
        return float("nan")
    a = [sleeve_rets[d] for d in common]
    b = [trend_rets[d] for d in common]
    return metrics.correlation(a, b)


def _rotation_risk(pos="1.0", allow_short=False, gross=None) -> RiskConfig:
    return RiskConfig(
        max_position_size_pct=Decimal(pos),
        max_total_exposure_pct=Decimal("2.0") if allow_short else Decimal("1.0"),
        max_leverage=Decimal("2.0") if allow_short else Decimal("1.0"),
        max_drawdown_pct=Decimal("0.99"),
        max_daily_loss_pct=Decimal("0.99"),
        require_stop_loss=True,
        allow_short=allow_short,
        max_gross_exposure_pct=Decimal(gross) if gross else None,
    )


def _trend_reference() -> dict:
    """Daily returns of the DEPLOYED trend strategy on smart-7 — the correlation baseline."""
    syms = _etf(["SPY", "EFA", "TLT", "GLD", "DBC", "UUP", "DBA"])
    ev = _load(_SMART7, syms)
    res = run_backtest(
        ev,
        MultiAssetTrendStrategy("trend_ref", syms, fast_period=20, slow_period=200),
        _rotation_risk("0.16"),
        slippage_pct=_SLIP,
    )
    return _returns_by_date(res.equity_curve, res.equity_timestamps)


def _largecap_syms():
    import csv

    seen: list[str] = []
    with open(_LARGE) as f:
        for r in csv.DictReader(f):
            t = r.get("symbol") or r.get("ticker")
            if t and t not in seen:
                seen.append(t)
    return [Symbol(t, AssetClass.EQUITY) for t in seen]


def _sleeves():
    """(name, data_path, feed_symbols, factory, benchmark, risk, variants, rebalance_bars)."""
    spy = Symbol("SPY", AssetClass.ETF)
    ief = Symbol("IEF", AssetClass.ETF)
    shv = Symbol("SHV", AssetClass.ETF)
    hyg = Symbol("HYG", AssetClass.ETF)
    lqd = Symbol("LQD", AssetClass.ETF)
    tnx = Symbol("^TNX", AssetClass.ETF)
    irx = Symbol("^IRX", AssetClass.ETF)
    s7 = _etf(["SPY", "EFA", "TLT", "GLD", "DBC", "UUP", "DBA"])
    crypto = [Symbol("BTC-USD", AssetClass.ETF), Symbol("ETH-USD", AssetClass.ETF)]
    breadth = _etf(["SPY", "EFA", "EEM", "AGG", "LQD", "IEF", "SHV"])
    cs = [spy, ief, hyg, lqd]
    bc = [ief, shv, tnx, irx, spy]
    large = _largecap_syms()

    out = []

    # 0. EWMA trend — confirm the A/B winner still grades A.
    out.append(
        (
            "trend_ewma",
            _SMART7,
            s7,
            lambda: MultiAssetTrendStrategy("trend_ewma", s7, vol_method="ewma"),
            "SPY",
            _rotation_risk("0.16"),
            [
                (
                    "slow150",
                    lambda: MultiAssetTrendStrategy("e", s7, slow_period=150, vol_method="ewma"),
                ),
                (
                    "slow250",
                    lambda: MultiAssetTrendStrategy("e", s7, slow_period=250, vol_method="ewma"),
                ),
            ],
            1,
        )
    )
    # 1. Turn-of-month (SPY, cash otherwise).
    out.append(
        (
            "turn_of_month",
            _SWEEP,
            [spy],
            lambda: TurnOfMonthStrategy("tom", spy),
            "SPY",
            _rotation_risk("1.0"),
            [
                ("end22", lambda: TurnOfMonthStrategy("t", spy, month_end_day=22)),
                ("end26", lambda: TurnOfMonthStrategy("t", spy, month_end_day=26)),
            ],
            21,
        )
    )
    # 2. Breadth momentum (VAA).
    out.append(
        (
            "breadth_momentum",
            _SWEEP,
            breadth,
            lambda: BreadthMomentumStrategy("vaa", breadth),
            "SPY",
            _rotation_risk("1.0"),
            [
                ("trig2", lambda: BreadthMomentumStrategy("v", breadth, breadth_trigger=2)),
            ],
            21,
        )
    )
    # 3. Credit-spread regime.
    out.append(
        (
            "credit_spread",
            _SWEEP,
            cs,
            lambda: CreditSpreadRegimeStrategy(
                "cs", cs, risk_sym=spy, defensive_sym=ief, hyg_sym=hyg, lqd_sym=lqd
            ),
            "SPY",
            _rotation_risk("1.0"),
            [
                (
                    "z15",
                    lambda: CreditSpreadRegimeStrategy(
                        "c",
                        cs,
                        risk_sym=spy,
                        defensive_sym=ief,
                        hyg_sym=hyg,
                        lqd_sym=lqd,
                        enter_z=Decimal("-1.5"),
                    ),
                ),
            ],
            21,
        )
    )
    # 4. Bond carry (yield-curve slope).
    out.append(
        (
            "bond_carry",
            _SWEEP,
            bc,
            lambda: BondCarryStrategy("carry", bc, long_etf=ief, short_etf=shv),
            "SPY",
            _rotation_risk("1.0"),
            [
                (
                    "buf25",
                    lambda: BondCarryStrategy(
                        "c", bc, long_etf=ief, short_etf=shv, inversion_buffer=Decimal("0.25")
                    ),
                ),
            ],
            21,
        )
    )
    # 5. Crypto trend.
    out.append(
        (
            "crypto_trend",
            _CRYPTO,
            crypto,
            lambda: MultiAssetTrendStrategy("crypto", crypto, slow_period=150),
            "BTC-USD",
            _rotation_risk("0.5"),
            [
                ("slow100", lambda: MultiAssetTrendStrategy("c", crypto, slow_period=100)),
            ],
            1,
        )
    )
    # 6. Long/short momentum (single names, allow_short).
    out.append(
        (
            "long_short_mom",
            _LARGE,
            large,
            lambda: LongShortMomentumStrategy("ls", large, mom_period=126, top_k=3, bot_k=3),
            large[0].ticker,
            _rotation_risk("0.10", allow_short=True, gross="2.0"),
            [],
            21,
        )
    )
    return out


def main() -> int:
    logging.disable(logging.WARNING)
    print("Computing trend reference returns (smart-7) ...", flush=True)
    trend_rets = _trend_reference()

    print(
        f"\n{'sleeve':>16} {'grade':>6} {'full':>6} {'OOS':>6} {'2xcost':>7} {'corrTrend':>9} {'trades':>7}"
    )
    print("-" * 72)
    rows = []
    for name, path, syms, factory, bench, risk, variants, reb in _sleeves():
        try:
            report, inp = run_full_gauntlet(
                name,
                factory,
                _load(path, syms),
                risk,
                benchmark_ticker=bench,
                slippage_pct=_SLIP,
                param_variants=variants,
                mc_iterations=800,
                rebalance_period_bars=reb,
            )
            res = run_backtest(_load(path, syms), factory(), risk, slippage_pct=_SLIP)
            corr = _corr_to_trend(
                _returns_by_date(res.equity_curve, res.equity_timestamps), trend_rets
            )
            print(
                f"{name:>16} {report.grade.value:>6} {inp.full_sharpe:>6.2f} "
                f"{inp.out_of_sample_sharpe:>6.2f} {inp.sharpe_at_2x_cost:>7.2f} {corr:>9.2f} {inp.num_trades:>7}",
                flush=True,
            )
            rows.append((name, report.grade.value, inp.full_sharpe, corr))
        except Exception as exc:  # noqa: BLE001
            print(f"{name:>16}  ERROR: {exc}", flush=True)
            traceback.print_exc(limit=2, file=sys.stdout)

    print("\nVERDICT — a sleeve earns a live slot only if it CLEARS the Gauntlet (grade A/B)")
    print("AND comes back uncorrelated to trend (|corr| < ~0.4):")
    for name, grade, sharpe, corr in rows:
        ok = grade in ("A", "B") and abs(corr) < 0.4 if corr == corr else False
        print(f"  {name:>16}: {'KEEP' if ok else 'park'} (grade {grade}, corr {corr:.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
