"""
Tests for apex.backtest.allocator — the multi-strategy capital-allocation engine.

The pure helpers (config validation, weight gating, blend/align math) are asserted
against known values; the engine is exercised end-to-end with an INJECTED fake
backtester so it runs offline and deterministically (no real engine, no data files).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apex.backtest.allocator import (
    AllocationConfig,
    Sleeve,
    SleeveSpec,
    align_streams,
    blend,
    equity_from_returns,
    returns_by_date,
    run_allocation_backtest,
)
from apex.execution.engine import BacktestResult

_T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _days(n: int) -> list[datetime]:
    return [_T0 + timedelta(days=i) for i in range(n)]


# ----------------------------------------------------------------- config


def test_config_accepts_valid_split():
    cfg = AllocationConfig((Sleeve("trend", Decimal("0.8")), Sleeve("value", Decimal("0.2"))))
    assert cfg.names() == ["trend", "value"]


def test_config_rejects_weights_not_summing_to_one():
    with pytest.raises(ValueError):
        AllocationConfig((Sleeve("a", Decimal("0.8")), Sleeve("b", Decimal("0.1"))))


def test_config_rejects_weight_out_of_range():
    with pytest.raises(ValueError):
        AllocationConfig((Sleeve("a", Decimal("1.2")), Sleeve("b", Decimal("-0.2"))))


def test_config_rejects_duplicate_names():
    with pytest.raises(ValueError):
        AllocationConfig((Sleeve("a", Decimal("0.5")), Sleeve("a", Decimal("0.5"))))


def test_config_rejects_empty():
    with pytest.raises(ValueError):
        AllocationConfig(())


def test_target_vs_live_weights_gating():
    # Value is built/measured but NOT funded (W8 gate) → live capital flows only to trend.
    cfg = AllocationConfig(
        (
            Sleeve("trend", Decimal("0.8"), funded=True),
            Sleeve("value", Decimal("0.2"), funded=False),
        )
    )
    assert cfg.target_weights() == {"trend": 0.8, "value": 0.2}
    live = cfg.live_weights()
    assert live["value"] == 0.0
    assert abs(live["trend"] - 1.0) < 1e-12  # renormalized across funded sleeves


def test_live_weights_all_unfunded_is_all_zero():
    cfg = AllocationConfig(
        (Sleeve("a", Decimal("0.5"), funded=False), Sleeve("b", Decimal("0.5"), funded=False))
    )
    assert cfg.live_weights() == {"a": 0.0, "b": 0.0}  # fail closed: nothing trades


# ----------------------------------------------------------------- pure helpers


def test_returns_by_date_pairs_with_next_timestamp():
    ts = _days(3)
    eq = [100.0, 110.0, 99.0]  # returns: +0.10 (-> ts[1]), -0.10 (-> ts[2])
    out = returns_by_date(eq, ts)
    assert set(out) == {ts[1].date(), ts[2].date()}
    assert abs(out[ts[1].date()] - 0.10) < 1e-12
    assert abs(out[ts[2].date()] - (-0.10)) < 1e-12


def test_align_streams_intersects_common_dates():
    d = _days(4)
    a = {d[1].date(): 0.01, d[2].date(): 0.02, d[3].date(): 0.03}
    b = {d[2].date(): -0.01, d[3].date(): 0.04}  # missing d[1]
    dates, aligned = align_streams({"a": a, "b": b})
    assert dates == [d[2].date(), d[3].date()]
    assert aligned["a"] == [0.02, 0.03]
    assert aligned["b"] == [-0.01, 0.04]


def test_blend_is_weighted_sum():
    aligned = {"t": [0.10, -0.05], "v": [0.00, 0.20]}
    out = blend(aligned, {"t": 0.8, "v": 0.2})
    assert abs(out[0] - (0.8 * 0.10 + 0.2 * 0.00)) < 1e-12
    assert abs(out[1] - (0.8 * -0.05 + 0.2 * 0.20)) < 1e-12


def test_equity_from_returns_compounds():
    eq = equity_from_returns([0.10, -0.10], start=100.0)
    assert eq[0] == 100.0
    assert abs(eq[1] - 110.0) < 1e-9
    assert abs(eq[2] - 99.0) < 1e-9


# ----------------------------------------------------------------- engine (injected backtester)


def _fake_backtester(curves: dict[str, list[float]], ts: list[datetime]):
    """A run_backtest stand-in: maps the sleeve marker (passed as `strategy`) to a canned curve."""

    def _run(events, strategy, risk_config, slippage_pct=Decimal("0.001")):
        return BacktestResult(equity_curve=curves[strategy], equity_timestamps=ts)

    return _run


def _spec(name: str) -> SleeveSpec:
    # strategy/risk are opaque here — the injected backtester keys off the name marker.
    return SleeveSpec(name=name, events=[], strategy=name, risk_config=None)


def test_run_allocation_backtest_blends_and_reports():
    ts = _days(6)
    curves = {
        "trend": [100, 101, 102, 103, 104, 105],  # steady up
        "value": [100, 99, 100, 101, 100, 101],  # choppy
    }
    cfg = AllocationConfig((Sleeve("trend", Decimal("0.8")), Sleeve("value", Decimal("0.2"))))
    res = run_allocation_backtest(
        [_spec("trend"), _spec("value")], cfg, run_backtest_fn=_fake_backtester(curves, ts)
    )
    # 5 common return-days (returns[i] pairs with ts[i+1]).
    assert len(res.dates) == 5
    assert len(res.blended_returns) == 5
    # Per-sleeve standalone metrics + the trend/value correlation pair are present.
    assert {s.name for s in res.sleeves} == {"trend", "value"}
    assert ("trend", "value") in res.correlations
    # lift = blended Sharpe - best standalone Sharpe (the whole point of blending).
    assert abs(res.lift - (res.blended_sharpe - res.best_standalone_sharpe)) < 1e-12
    assert res.summary()  # renders without error


def test_run_allocation_backtest_honors_live_weights():
    ts = _days(4)
    curves = {"trend": [100, 101, 102, 103], "value": [100, 90, 120, 80]}
    cfg = AllocationConfig(
        (
            Sleeve("trend", Decimal("0.8"), funded=True),
            Sleeve("value", Decimal("0.2"), funded=False),
        )
    )
    res = run_allocation_backtest(
        [_spec("trend"), _spec("value")],
        cfg,
        weights=cfg.live_weights(),  # value unfunded → 100% trend
        run_backtest_fn=_fake_backtester(curves, ts),
    )
    # With value zero-weighted, the blend equals the trend stream exactly.
    trend_rets = [s for s in res.sleeves if s.name == "trend"][0]
    assert trend_rets.weight == pytest.approx(1.0)
    assert [s for s in res.sleeves if s.name == "value"][0].weight == 0.0


def test_run_allocation_backtest_rejects_spec_config_mismatch():
    cfg = AllocationConfig((Sleeve("trend", Decimal("1.0")),))
    with pytest.raises(ValueError):
        run_allocation_backtest(
            [_spec("value")], cfg, run_backtest_fn=_fake_backtester({"value": [1, 2]}, _days(2))
        )
