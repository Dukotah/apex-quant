"""
scripts/validate_real.py
=======================
Run library strategies through the full Validation Gauntlet on REAL OHLCV history
(downloaded by scripts/fetch_yahoo.py), not synthetic data. This is the honest
edge test: the Gauntlet either grades a real edge or kills a mirage.

Usage:
    python -m scripts.fetch_yahoo SPY EFA AGG --range 15y --out data/real/dm.csv
    python -m scripts.validate_real                 # dual momentum on real data
    python -m scripts.validate_real rsi2            # RSI(2) on real SPY
"""
from __future__ import annotations

import sys
from decimal import Decimal

from apex.backtest.gauntlet_runner import run_gauntlet_from_csv
from apex.core.models import AssetClass, Symbol
from apex.risk.risk_manager import RiskConfig
from apex.strategy.library.dual_momentum import DualMomentumStrategy
from apex.strategy.library.etf_rotation import ETFRotationStrategy
from apex.strategy.library.rsi2_mean_reversion import RSI2MeanReversionStrategy
from apex.strategy.library.rsi2_vol_filtered import RSI2VolFilteredStrategy
from apex.strategy.library.sma_crossover import SMACrossoverStrategy
from apex.strategy.library.trend_bond import TrendBondStrategy

DATA = "data/real/dm.csv"            # SPY/EFA/AGG
SECTORS = "data/real/sectors.csv"    # XLK..XLB + AGG + SPY (benchmark)

# Single-strategy edge test → full deployment (portfolio caps are for live sharing).
SLEEVE_RISK = RiskConfig(
    max_position_size_pct=Decimal("1.0"),
    max_total_exposure_pct=Decimal("1.0"),
    max_leverage=Decimal("1.0"),
    max_drawdown_pct=Decimal("0.99"),
    max_daily_loss_pct=Decimal("0.99"),   # raw-edge measurement: don't let the
    require_stop_loss=True,                # daily circuit breaker contaminate Sharpe
)


def _utf8():
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def validate_dual_momentum():
    syms = [Symbol(t, AssetClass.ETF) for t in ("SPY", "EFA", "AGG")]

    def factory():
        return DualMomentumStrategy("dual_momentum", syms, "SPY", "EFA", "AGG", lookback_window=252)

    def lo():
        return DualMomentumStrategy("dm", syms, "SPY", "EFA", "AGG", lookback_window=202)

    def hi():
        return DualMomentumStrategy("dm", syms, "SPY", "EFA", "AGG", lookback_window=302)

    return run_gauntlet_from_csv(
        "dual_momentum_REAL", factory, DATA, syms, benchmark_ticker="SPY",
        risk_config=SLEEVE_RISK,
        param_variants=[("lookback-20%", lo), ("lookback+20%", hi)],
        rebalance_period_bars=21,
    )


def validate_rsi2():
    syms = [Symbol("SPY", AssetClass.ETF)]   # feed skips EFA/AGG rows (unsubscribed)

    def factory():
        return RSI2MeanReversionStrategy("rsi2_mr", syms, entry_threshold=Decimal("10"))

    def lo():
        return RSI2MeanReversionStrategy("rsi2_mr", syms, entry_threshold=Decimal("8"))

    def hi():
        return RSI2MeanReversionStrategy("rsi2_mr", syms, entry_threshold=Decimal("12"))

    return run_gauntlet_from_csv(
        "rsi2_REAL", factory, DATA, syms, benchmark_ticker="SPY",
        risk_config=SLEEVE_RISK,
        param_variants=[("thr-20%", lo), ("thr+20%", hi)],
    )


def validate_rsi2_vol():
    syms = [Symbol("SPY", AssetClass.ETF)]

    def factory():
        return RSI2VolFilteredStrategy("rsi2_vol", syms, entry_threshold=Decimal("10"))

    def lo():
        return RSI2VolFilteredStrategy("rsi2_vol", syms, entry_threshold=Decimal("8"))

    def hi():
        return RSI2VolFilteredStrategy("rsi2_vol", syms, entry_threshold=Decimal("12"))

    return run_gauntlet_from_csv(
        "rsi2_vol_filtered_REAL", factory, DATA, syms, benchmark_ticker="SPY",
        risk_config=SLEEVE_RISK,
        param_variants=[("thr-20%", lo), ("thr+20%", hi)],
    )


def validate_etf_rotation():
    sectors = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLI", "XLP", "XLU", "XLB"]
    # Strategy universe: sectors + AGG (bond sleeve, must be LAST).
    strat_syms = [Symbol(t, AssetClass.ETF) for t in sectors + ["AGG"]]
    # Feed universe: also load SPY so Gate 7 has a benchmark (strategy ignores it).
    feed_syms = strat_syms + [Symbol("SPY", AssetClass.ETF)]

    def factory():
        return ETFRotationStrategy("etf_rotation", strat_syms, momentum_period=63, top_k=3)

    def lo():
        return ETFRotationStrategy("etf_rotation", strat_syms, momentum_period=42, top_k=3)

    def hi():
        return ETFRotationStrategy("etf_rotation", strat_syms, momentum_period=84, top_k=3)

    return run_gauntlet_from_csv(
        "etf_rotation_REAL", factory, SECTORS, feed_syms, benchmark_ticker="SPY",
        risk_config=SLEEVE_RISK,
        param_variants=[("mom-20%", lo), ("mom+20%", hi)],
        rebalance_period_bars=5,   # weekly cadence
    )


def validate_spy_trend():
    """Time-series trend filter: long SPY above its 200-day SMA, flat below."""
    syms = [Symbol("SPY", AssetClass.ETF)]

    def factory():
        return SMACrossoverStrategy("spy_trend", syms, fast_period=20, slow_period=200)

    def lo():
        return SMACrossoverStrategy("spy_trend", syms, fast_period=20, slow_period=150)

    def hi():
        return SMACrossoverStrategy("spy_trend", syms, fast_period=20, slow_period=250)

    return run_gauntlet_from_csv(
        "spy_trend_REAL", factory, DATA, syms, benchmark_ticker="SPY",
        risk_config=SLEEVE_RISK,
        param_variants=[("slow-25%", lo), ("slow+25%", hi)],
    )


def validate_multiasset():
    """Multi-asset trend following: long/flat each asset on its 200d SMA, 20% each."""
    assets = ["SPY", "EFA", "TLT", "GLD", "DBC"]
    syms = [Symbol(t, AssetClass.ETF) for t in assets]
    # Equal-weight sleeve sizing: 20% max per position, up to 100% total deployed.
    risk = RiskConfig(
        max_position_size_pct=Decimal("0.20"), max_total_exposure_pct=Decimal("1.0"),
        max_leverage=Decimal("1.0"), max_drawdown_pct=Decimal("0.99"),
        max_daily_loss_pct=Decimal("0.99"), require_stop_loss=True,
    )

    def factory():
        return SMACrossoverStrategy("multi_trend", syms, fast_period=20, slow_period=200)

    def lo():
        return SMACrossoverStrategy("multi_trend", syms, fast_period=20, slow_period=150)

    def hi():
        return SMACrossoverStrategy("multi_trend", syms, fast_period=20, slow_period=250)

    return run_gauntlet_from_csv(
        "multiasset_trend_REAL", factory, "data/real/multiasset.csv", syms,
        benchmark_ticker="SPY", risk_config=risk,
        param_variants=[("slow-25%", lo), ("slow+25%", hi)],
    )


def validate_multiasset_vp():
    """Multi-asset trend with INVERSE-VOL (risk-parity) sizing — the DD-cutter."""
    from apex.strategy.library.multi_asset_trend import MultiAssetTrendStrategy
    assets = ["SPY", "EFA", "TLT", "GLD", "DBC"]
    syms = [Symbol(t, AssetClass.ETF) for t in assets]
    risk = RiskConfig(
        max_position_size_pct=Decimal("0.20"), max_total_exposure_pct=Decimal("1.0"),
        max_leverage=Decimal("1.0"), max_drawdown_pct=Decimal("0.99"),
        max_daily_loss_pct=Decimal("0.99"), require_stop_loss=True,
    )

    def factory():
        return MultiAssetTrendStrategy("multi_trend_vp", syms, fast_period=20, slow_period=200)

    def lo():
        return MultiAssetTrendStrategy("multi_trend_vp", syms, fast_period=20, slow_period=150)

    def hi():
        return MultiAssetTrendStrategy("multi_trend_vp", syms, fast_period=20, slow_period=250)

    return run_gauntlet_from_csv(
        "multiasset_trend_VP_REAL", factory, "data/real/multiasset.csv", syms,
        benchmark_ticker="SPY", risk_config=risk,
        param_variants=[("slow-25%", lo), ("slow+25%", hi)],
    )


def validate_multiasset_expanded():
    """Expanded inverse-vol trend: 10 uncorrelated sleeves across asset classes."""
    from apex.strategy.library.multi_asset_trend import MultiAssetTrendStrategy
    assets = ["SPY", "EFA", "EEM", "TLT", "IEF", "LQD", "GLD", "SLV", "DBC", "VNQ"]
    syms = [Symbol(t, AssetClass.ETF) for t in assets]
    # ~10 sleeves: 12% cap each (inverse-vol tilts within it), up to 100% deployed.
    risk = RiskConfig(
        max_position_size_pct=Decimal("0.12"), max_total_exposure_pct=Decimal("1.0"),
        max_leverage=Decimal("1.0"), max_drawdown_pct=Decimal("0.99"),
        max_daily_loss_pct=Decimal("0.99"), require_stop_loss=True,
    )

    def factory():
        return MultiAssetTrendStrategy("multi_trend_x", syms, fast_period=20, slow_period=200)

    def lo():
        return MultiAssetTrendStrategy("multi_trend_x", syms, fast_period=20, slow_period=150)

    def hi():
        return MultiAssetTrendStrategy("multi_trend_x", syms, fast_period=20, slow_period=250)

    return run_gauntlet_from_csv(
        "multiasset_trend_EXPANDED", factory, "data/real/multiasset_expanded.csv", syms,
        benchmark_ticker="SPY", risk_config=risk,
        param_variants=[("slow-25%", lo), ("slow+25%", hi)],
    )


def validate_multiasset_smart7():
    """Smart expansion: the 5 uncorrelated sleeves + dollar (UUP) + ags (DBA) only."""
    from apex.strategy.library.multi_asset_trend import MultiAssetTrendStrategy
    assets = ["SPY", "EFA", "TLT", "GLD", "DBC", "UUP", "DBA"]
    syms = [Symbol(t, AssetClass.ETF) for t in assets]
    risk = RiskConfig(
        max_position_size_pct=Decimal("0.16"), max_total_exposure_pct=Decimal("1.0"),
        max_leverage=Decimal("1.0"), max_drawdown_pct=Decimal("0.99"),
        max_daily_loss_pct=Decimal("0.99"), require_stop_loss=True,
    )

    def factory():
        return MultiAssetTrendStrategy("multi_trend_s7", syms, fast_period=20, slow_period=200)

    def lo():
        return MultiAssetTrendStrategy("multi_trend_s7", syms, fast_period=20, slow_period=150)

    def hi():
        return MultiAssetTrendStrategy("multi_trend_s7", syms, fast_period=20, slow_period=250)

    return run_gauntlet_from_csv(
        "multiasset_trend_SMART7", factory, "data/real/multiasset_smart7.csv", syms,
        benchmark_ticker="SPY", risk_config=risk,
        param_variants=[("slow-25%", lo), ("slow+25%", hi)],
    )


def validate_value():
    """
    Cross-asset VALUE (long-horizon reversal) on the smart-7 universe — the second-edge
    candidate. Same universe as the deployed trend strategy, so its OOS Sharpe AND its
    correlation to trend (scripts/portfolio) are apples-to-apples. Trend filter OFF:
    we want PURE value's correlation, not a trend-contaminated hybrid.
    """
    from apex.strategy.library.cross_asset_value import CrossAssetValueStrategy
    assets = ["SPY", "EFA", "TLT", "GLD", "DBC", "UUP", "DBA"]
    syms = [Symbol(t, AssetClass.ETF) for t in assets]
    risk = RiskConfig(
        max_position_size_pct=Decimal("0.34"), max_total_exposure_pct=Decimal("1.0"),
        max_leverage=Decimal("1.0"), max_drawdown_pct=Decimal("0.99"),
        max_daily_loss_pct=Decimal("0.99"), require_stop_loss=True,
    )

    def factory():
        return CrossAssetValueStrategy("xasset_value", syms, value_period=1260,
                                       skip_recent=252, top_k=3)

    def lo():
        return CrossAssetValueStrategy("xasset_value", syms, value_period=1008,
                                       skip_recent=252, top_k=3)

    def hi():
        return CrossAssetValueStrategy("xasset_value", syms, value_period=1512,
                                       skip_recent=252, top_k=3)

    return run_gauntlet_from_csv(
        "cross_asset_value_REAL", factory, "data/real/multiasset_smart7.csv", syms,
        benchmark_ticker="SPY", risk_config=risk,
        param_variants=[("window-20%", lo), ("window+20%", hi)],
        rebalance_period_bars=21,
    )


def validate_trend_bond():
    """Cash-drag-fixed trend: hold SPY above its 200d SMA, rotate to AGG below."""
    syms = [Symbol("SPY", AssetClass.ETF), Symbol("AGG", AssetClass.ETF)]

    def factory():
        return TrendBondStrategy("trend_bond", syms, slow_period=200)

    def lo():
        return TrendBondStrategy("trend_bond", syms, slow_period=150)

    def hi():
        return TrendBondStrategy("trend_bond", syms, slow_period=250)

    return run_gauntlet_from_csv(
        "trend_bond_REAL", factory, DATA, syms, benchmark_ticker="SPY",
        risk_config=SLEEVE_RISK,
        param_variants=[("slow-25%", lo), ("slow+25%", hi)],
    )


def main() -> None:
    _utf8()
    global DATA, SECTORS
    which = sys.argv[1] if len(sys.argv) > 1 else "dual_momentum"
    if len(sys.argv) > 2:                 # optional data-file override
        DATA = SECTORS = sys.argv[2]
    if which in ("value", "val", "xvalue"):
        report, inputs = validate_value()
    elif which in ("smart7", "s7"):
        report, inputs = validate_multiasset_smart7()
    elif which in ("expanded", "x", "multix"):
        report, inputs = validate_multiasset_expanded()
    elif which in ("multivp", "multi_vp", "vp", "multiasset_vp"):
        report, inputs = validate_multiasset_vp()
    elif which.startswith("multi"):
        report, inputs = validate_multiasset()
    elif which.startswith("trend_bond") or which == "tb":
        report, inputs = validate_trend_bond()
    elif which.startswith("trend") or which.startswith("spy"):
        report, inputs = validate_spy_trend()
    elif which.startswith("rsi2_vol") or which == "volrsi":
        report, inputs = validate_rsi2_vol()
    elif which.startswith("etf") or which.startswith("rotation"):
        report, inputs = validate_etf_rotation()
    elif which.startswith("rsi2"):
        report, inputs = validate_rsi2()
    else:
        report, inputs = validate_dual_momentum()
    print()
    print(report.render())
    print()
    print(f"  trades={inputs.num_trades}  in-sample Sharpe={inputs.in_sample_sharpe:.2f}  "
          f"OOS Sharpe={inputs.out_of_sample_sharpe:.2f}  "
          f"full Sharpe={inputs.full_sharpe:.2f}  "
          f"Sharpe@2x-cost={inputs.sharpe_at_2x_cost:.2f}  "
          f"benchmark Sharpe={inputs.benchmark_sharpe:.2f}  corr={inputs.correlation_to_benchmark:.2f}")
    print()
    print("  DATA: real adjusted-close history (Yahoo) — this is a genuine edge test.")


if __name__ == "__main__":
    main()
