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
from apex.strategy.library.rsi2_mean_reversion import RSI2MeanReversionStrategy

DATA = "data/real/dm.csv"

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


def main() -> None:
    _utf8()
    which = sys.argv[1] if len(sys.argv) > 1 else "dual_momentum"
    report, inputs = validate_rsi2() if which.startswith("rsi2") else validate_dual_momentum()
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
