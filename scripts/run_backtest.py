"""
scripts/run_backtest.py
======================
Capstone entry point: run a library strategy through the full Validation
Gauntlet against the backtester and print the honest graded report.

Usage:
    python -m scripts.run_backtest                # dual_momentum (the anchor)
    python -m scripts.run_backtest rsi2           # RSI(2) mean reversion

NOTE ON DATA: with no market-data vendor wired up yet, this uses the
deterministic SYNTHETIC generator (apex.backtest.synthetic). The grade is a
demonstration of the pipeline, NOT a statement about the strategy's real edge.
For a real run, replace the synthetic events with a HistoricalDataFeed pointed
at actual OHLCV files — the rest of the pipeline is identical.
"""

from __future__ import annotations

import logging
import sys
from decimal import Decimal

from apex.backtest.gauntlet_runner import run_full_gauntlet
from apex.backtest.synthetic import generate_closes, interleave, make_bars
from apex.core.models import AssetClass, Symbol
from apex.risk.risk_manager import RiskConfig
from apex.strategy.library.dual_momentum import DualMomentumStrategy
from apex.strategy.library.rsi2_mean_reversion import RSI2MeanReversionStrategy


def _utf8_stdout() -> None:
    """The Gauntlet report uses ✓/✗ marks; ensure they print on any console."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


# A single-strategy backtest gets its whole sleeve: allow full deployment.
# (Portfolio-level caps apply when several strategies share live capital.)
SLEEVE_RISK = RiskConfig(
    max_position_size_pct=Decimal("1.0"),
    max_total_exposure_pct=Decimal("1.0"),
    max_leverage=Decimal("1.0"),
    max_drawdown_pct=Decimal("0.99"),  # let the Gauntlet measure the raw curve
    max_daily_loss_pct=Decimal("0.99"),  # don't let the daily breaker distort Sharpe
    require_stop_loss=True,
)


def run_dual_momentum():
    n = 4000
    spy = generate_closes(
        1,
        n,
        100,
        [
            (0, 0.0006),
            (700, -0.0009),
            (1100, 0.0008),
            (1900, -0.0010),
            (2400, 0.0007),
            (3200, -0.0008),
        ],
        0.011,
    )
    efa = generate_closes(
        2,
        n,
        50,
        [
            (0, 0.0008),
            (700, -0.0005),
            (1100, 0.0004),
            (1900, 0.0009),
            (2400, -0.0006),
            (3200, 0.0005),
        ],
        0.012,
    )
    agg = generate_closes(3, n, 100, [(0, 0.00015)], 0.003)
    events = interleave(make_bars("SPY", spy), make_bars("EFA", efa), make_bars("AGG", agg))
    syms = [Symbol(t, AssetClass.ETF) for t in ("SPY", "EFA", "AGG")]

    def factory():
        return DualMomentumStrategy("dual_momentum", syms, "SPY", "EFA", "AGG", lookback_window=252)

    def lo():
        return DualMomentumStrategy("dm", syms, "SPY", "EFA", "AGG", lookback_window=202)

    def hi():
        return DualMomentumStrategy("dm", syms, "SPY", "EFA", "AGG", lookback_window=302)

    return run_full_gauntlet(
        "dual_momentum_v1",
        factory,
        events,
        SLEEVE_RISK,
        "SPY",
        param_variants=[("lookback-20%", lo), ("lookback+20%", hi)],
        rebalance_period_bars=21,
    )  # monthly cadence → fair Gate 1


def run_rsi2():
    n = 3000
    spy = generate_closes(7, n, 100, [(0, 0.0004)], 0.014)
    events = interleave(make_bars("SPY", spy))
    syms = [Symbol("SPY", AssetClass.ETF)]

    def factory():
        return RSI2MeanReversionStrategy("rsi2_mr", syms, entry_threshold=Decimal("10"))

    def lo():
        return RSI2MeanReversionStrategy("rsi2_mr", syms, entry_threshold=Decimal("8"))

    def hi():
        return RSI2MeanReversionStrategy("rsi2_mr", syms, entry_threshold=Decimal("12"))

    return run_full_gauntlet(
        "rsi2_mean_reversion_v1",
        factory,
        events,
        SLEEVE_RISK,
        "SPY",
        param_variants=[("thr-20%", lo), ("thr+20%", hi)],
    )


def main() -> None:
    _utf8_stdout()
    logging.basicConfig(level=logging.WARNING)
    which = sys.argv[1] if len(sys.argv) > 1 else "dual_momentum"

    report, inputs = run_rsi2() if which.startswith("rsi2") else run_dual_momentum()

    print()
    print(report.render())
    print()
    print(
        f"  trades={inputs.num_trades}  in-sample Sharpe={inputs.in_sample_sharpe:.2f}  "
        f"OOS Sharpe={inputs.out_of_sample_sharpe:.2f}  "
        f"Sharpe@2x-cost={inputs.sharpe_at_2x_cost:.2f}  "
        f"benchmark Sharpe={inputs.benchmark_sharpe:.2f}  corr={inputs.correlation_to_benchmark:.2f}"
    )
    print()
    print("  NOTE: synthetic data — this demonstrates the pipeline, not a real edge.")


if __name__ == "__main__":
    main()
