"""
Tests for apex.backtest — the backtester harness and the 7-gate Gauntlet runner.

Uses the deterministic synthetic generator (seeded) so runs are reproducible.
Drives the already-built SMA crossover strategy (low warmup) through the engine
and the full Gauntlet, asserting the machinery runs end-to-end and that the
reduce-aware exit path lets positions actually close.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from apex.backtest.backtester import make_slice_backtest_fn, run_backtest
from apex.backtest.gauntlet_runner import (
    GauntletInputs,
    run_full_gauntlet,
    run_gauntlet_from_csv,
)
from apex.backtest.synthetic import generate_closes, interleave, make_bars
from apex.core.models import AssetClass, Symbol
from apex.risk.risk_manager import RiskConfig
from apex.strategy.library.sma_crossover import SMACrossoverStrategy
from apex.validation.gauntlet import Grade

SYM = Symbol("SYN", AssetClass.ETF)


def _full_risk():
    return RiskConfig(
        max_position_size_pct=Decimal("1.0"),
        max_total_exposure_pct=Decimal("1.0"),
        max_leverage=Decimal("1.0"),
        max_drawdown_pct=Decimal("0.99"),
        require_stop_loss=True,
    )


def _trending_series(n=600):
    # Alternating up/down regimes so the fast/slow MAs cross repeatedly.
    closes = generate_closes(
        seed=11,
        n=n,
        start_price=100,
        drift_schedule=[(0, 0.0010), (120, -0.0010), (240, 0.0012), (360, -0.0011), (480, 0.0010)],
        vol=0.008,
    )
    return interleave(make_bars("SYN", closes))


def test_run_backtest_produces_equity_and_trades():
    events = _trending_series()
    strat = SMACrossoverStrategy("sma", [SYM], fast_period=10, slow_period=30)
    result = run_backtest(events, strat, _full_risk())
    assert len(result.equity_curve) > 100
    assert len(result.equity_curve) == len(result.equity_timestamps)
    # The crossover strategy enters and exits → some fills occur.
    assert len(result.fills) > 0


def test_reduce_aware_exit_lets_positions_close():
    """Death-cross SELLs must actually flatten the long (reduce-aware sizing)."""
    events = _trending_series()
    strat = SMACrossoverStrategy("sma", [SYM], fast_period=10, slow_period=30)
    result = run_backtest(events, strat, _full_risk())
    buys = sum(1 for f in result.fills if f.side.value == "buy")
    sells = sum(1 for f in result.fills if f.side.value == "sell")
    # If exits were rejected we'd see buys with (almost) no sells and 0 trades.
    assert sells > 0
    assert len(result.trade_returns) > 0
    assert abs(buys - sells) <= 1  # roughly paired entries/exits


def test_full_gauntlet_returns_graded_report():
    events = _trending_series(n=700)
    syms = [SYM]

    def factory():
        return SMACrossoverStrategy("sma_x", syms, fast_period=10, slow_period=30)

    report, inputs = run_full_gauntlet(
        "sma_crossover_test",
        factory,
        events,
        _full_risk(),
        "SYN",
        param_variants=[("a", factory), ("b", factory)],
        mc_iterations=100,
    )
    # The pipeline ran all seven gates and produced a graded report.
    assert len(report.gates) == 7
    assert isinstance(report.grade, Grade)
    assert isinstance(report.paper_approved, bool)
    assert isinstance(inputs, GauntletInputs)
    # Realistic DD and quarantine floor are populated (numbers, not None).
    assert report.realistic_max_drawdown >= 0.0
    # Rendering must not raise (Windows-safe is handled by callers' encoding).
    assert "sma_crossover_test" in report.render()


def test_run_gauntlet_from_csv(tmp_path):
    """File → HistoricalDataFeed → full Gauntlet, the real-history entry path."""
    closes = generate_closes(
        seed=5,
        n=600,
        start_price=100,
        drift_schedule=[(0, 0.0010), (150, -0.0010), (300, 0.0012), (450, -0.0009)],
        vol=0.008,
    )
    # Write a single-symbol OHLCV CSV (no symbol column → one configured symbol).
    lines = ["timestamp,open,high,low,close,volume"]
    start = datetime(2018, 1, 1, tzinfo=timezone.utc)
    prev = closes[0]
    for i, c in enumerate(closes):
        ts = (start + timedelta(days=i)).date().isoformat()
        hi = max(prev, c) * 1.005
        lo = min(prev, c) * 0.995
        lines.append(f"{ts},{prev:.4f},{hi:.4f},{lo:.4f},{c:.4f},1000000")
        prev = c
    path = tmp_path / "syn.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    sym = Symbol("SYN", AssetClass.ETF)

    def factory():
        return SMACrossoverStrategy("sma_csv", [sym], fast_period=10, slow_period=30)

    report, inputs = run_gauntlet_from_csv(
        "sma_from_csv",
        factory,
        str(path),
        [sym],
        "SYN",
        risk_config=_full_risk(),
        mc_iterations=80,
    )
    assert len(report.gates) == 7
    assert isinstance(report.grade, Grade)
    assert inputs.num_trades >= 0
    assert len(report.render()) > 0


def test_gauntlet_is_deterministic():
    events = _trending_series(n=500)
    syms = [SYM]

    def factory():
        return SMACrossoverStrategy("sma_x", syms, fast_period=10, slow_period=30)

    r1, i1 = run_full_gauntlet("d", factory, events, _full_risk(), "SYN", mc_iterations=80)
    r2, i2 = run_full_gauntlet("d", factory, events, _full_risk(), "SYN", mc_iterations=80)
    # Same inputs → same grade and same measured Sharpes.
    assert r1.grade == r2.grade
    assert i1.full_sharpe == i2.full_sharpe
    assert i1.num_trades == i2.num_trades


# ---- make_slice_backtest_fn -------------------------------------------------


def test_make_slice_backtest_fn_returns_callable():
    events = _trending_series(n=200)

    def factory():
        return SMACrossoverStrategy("sma", [SYM], fast_period=10, slow_period=30)

    fn = make_slice_backtest_fn(events, factory, _full_risk())
    assert callable(fn)


def test_make_slice_backtest_fn_short_window_returns_fallback():
    events = _trending_series(n=200)

    def factory():
        return SMACrossoverStrategy("sma", [SYM], fast_period=10, slow_period=30)

    fn = make_slice_backtest_fn(events, factory, _full_risk())
    # test_end - test_start = 1 event → too short, must return the fallback.
    result = fn(0, 100, 0, 1)
    assert result == [1.0, 1.0]


def test_make_slice_backtest_fn_valid_window_returns_curve():
    events = _trending_series(n=400)

    def factory():
        return SMACrossoverStrategy("sma", [SYM], fast_period=10, slow_period=30)

    fn = make_slice_backtest_fn(events, factory, _full_risk())
    # 200-event test window — enough bars for the strategy to warm up and trade.
    result = fn(0, 200, 200, 400)
    assert isinstance(result, list)
    assert len(result) >= 2
    assert all(isinstance(v, float) for v in result)


def test_make_slice_backtest_fn_empty_window_returns_fallback():
    events = _trending_series(n=100)

    def factory():
        return SMACrossoverStrategy("sma", [SYM], fast_period=10, slow_period=30)

    fn = make_slice_backtest_fn(events, factory, _full_risk())
    # test_start == test_end → empty slice → fallback.
    result = fn(0, 50, 80, 80)
    assert result == [1.0, 1.0]
