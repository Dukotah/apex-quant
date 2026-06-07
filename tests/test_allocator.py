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
    inverse_vol_weights,
    returns_by_date,
    run_allocation_backtest,
    tolerance_band_rebalance,
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


# ----------------------------------------------------------------- inverse_vol_weights


def test_inverse_vol_weights_calmer_sleeve_gets_more_weight():
    # "calm" has near-zero daily moves; "wild" swings large — inverse-vol must favour calm.
    calm = [0.001, -0.001, 0.001, -0.001, 0.001]
    wild = [0.10, -0.10, 0.10, -0.10, 0.10]
    w = inverse_vol_weights({"calm": calm, "wild": wild})
    assert w["calm"] > w["wild"], "calmer sleeve should carry more weight"


def test_inverse_vol_weights_sums_to_one():
    calm = [0.001, -0.001, 0.001, -0.001]
    wild = [0.10, -0.10, 0.10, -0.10]
    w = inverse_vol_weights({"calm": calm, "wild": wild})
    assert abs(sum(w.values()) - 1.0) < 1e-12


def test_inverse_vol_weights_known_values():
    # Two sleeves with vols 0.1 and 0.2: inv-vols are 10 and 5; weights 10/15, 5/15.
    # We approximate with series whose pstdev rounds to these vols.
    import statistics

    # Construct series with exact pstdev 0.1 and 0.2 (mean 0, variance = vol^2).
    # pstdev([0.1, -0.1]) = 0.1;  pstdev([0.2, -0.2]) = 0.2
    a = [0.1, -0.1]
    b = [0.2, -0.2]
    assert abs(statistics.pstdev(a) - 0.1) < 1e-12
    assert abs(statistics.pstdev(b) - 0.2) < 1e-12
    w = inverse_vol_weights({"a": a, "b": b})
    assert abs(w["a"] - (10.0 / 15.0)) < 1e-12
    assert abs(w["b"] - (5.0 / 15.0)) < 1e-12


def test_inverse_vol_weights_zero_vol_fallback_doesnt_crash():
    # A flat series (zero vol) falls back to 1.0 inverse-vol rather than dividing by zero.
    flat = [0.0, 0.0, 0.0]
    wild = [0.10, -0.10, 0.05]
    w = inverse_vol_weights({"flat": flat, "wild": wild})
    assert abs(sum(w.values()) - 1.0) < 1e-12
    # flat gets the fallback (1.0 inv-vol) which is far more than wild (< 10 inv-vol for
    # series with vol ~0.075); just confirm no crash and the output is a valid distribution.
    assert 0.0 < w["flat"] <= 1.0
    assert 0.0 < w["wild"] <= 1.0


def test_inverse_vol_weights_empty_returns_empty():
    assert inverse_vol_weights({}) == {}


def test_inverse_vol_weights_single_sleeve_is_one():
    w = inverse_vol_weights({"solo": [0.01, -0.02, 0.03]})
    assert abs(w["solo"] - 1.0) < 1e-12


# ----------------------------------------------------------------- tolerance_band_rebalance


def test_tolerance_band_rebalance_inside_band_no_trade():
    # Both sleeves within their bands — weights must be unchanged (before renorm, which is
    # trivially 1.0 already if the input sums to 1).
    current = {"a": 0.82, "b": 0.18}
    target = {"a": 0.80, "b": 0.20}
    bands = {"a": 0.05, "b": 0.05}
    result = tolerance_band_rebalance(current, target, bands)
    # |0.82 - 0.80| = 0.02 <= 0.05 → keep 0.82; similarly for b.
    assert abs(result["a"] - 0.82 / 1.00) < 1e-12
    assert abs(result["b"] - 0.18 / 1.00) < 1e-12


def test_tolerance_band_rebalance_outside_band_moves_to_edge():
    # Sleeve "a" is at 0.95, target 0.80, band 0.05 → edge = 0.80 + 0.05 = 0.85.
    # Sleeve "b" is at 0.05, target 0.20, band 0.05 → edge = 0.20 - 0.05 = 0.15.
    current = {"a": 0.95, "b": 0.05}
    target = {"a": 0.80, "b": 0.20}
    bands = {"a": 0.05, "b": 0.05}
    result = tolerance_band_rebalance(current, target, bands)
    total = 0.85 + 0.15  # = 1.0 exactly
    assert abs(result["a"] - 0.85 / total) < 1e-12
    assert abs(result["b"] - 0.15 / total) < 1e-12


def test_tolerance_band_rebalance_result_sums_to_one():
    current = {"x": 0.70, "y": 0.20, "z": 0.10}
    target = {"x": 0.50, "y": 0.30, "z": 0.20}
    bands = {"x": 0.05, "y": 0.05, "z": 0.05}
    result = tolerance_band_rebalance(current, target, bands)
    assert abs(sum(result.values()) - 1.0) < 1e-12


def test_tolerance_band_rebalance_mixed_inside_outside():
    # "a" inside its wide band; "b" outside its tight band.
    current = {"a": 0.62, "b": 0.38}
    target = {"a": 0.60, "b": 0.30}
    bands = {"a": 0.10, "b": 0.05}
    result = tolerance_band_rebalance(current, target, bands)
    # a: |0.62 - 0.60| = 0.02 <= 0.10 → keep 0.62
    # b: |0.38 - 0.30| = 0.08 > 0.05 → edge = 0.30 + 0.05 = 0.35
    raw_a, raw_b = 0.62, 0.35
    total = raw_a + raw_b
    assert abs(result["a"] - raw_a / total) < 1e-12
    assert abs(result["b"] - raw_b / total) < 1e-12
    assert abs(sum(result.values()) - 1.0) < 1e-12


def test_tolerance_band_rebalance_key_mismatch_raises():
    with pytest.raises(ValueError):
        tolerance_band_rebalance({"a": 0.5, "b": 0.5}, {"a": 1.0}, {"a": 0.05, "b": 0.05})


def test_tolerance_band_rebalance_empty_returns_empty():
    assert tolerance_band_rebalance({}, {}, {}) == {}


# ----------------------------------------------------------------- engine: inverse_vol weighting


def test_run_allocation_backtest_inverse_vol_calmer_sleeve_heavier():
    # trend: steady (+1% / day) → low vol; value: noisy → high vol.
    # inverse_vol_weights must give trend a heavier weight.
    ts = _days(11)
    trend_curve = [100.0 * (1.01**i) for i in range(11)]  # smooth compounding
    value_curve = [100.0, 90.0, 110.0, 85.0, 115.0, 90.0, 110.0, 85.0, 115.0, 90.0, 110.0]
    curves = {"trend": trend_curve, "value": value_curve}
    cfg = AllocationConfig((Sleeve("trend", Decimal("0.5")), Sleeve("value", Decimal("0.5"))))
    res = run_allocation_backtest(
        [_spec("trend"), _spec("value")],
        cfg,
        weighting="inverse_vol",
        run_backtest_fn=_fake_backtester(curves, ts),
    )
    trend_sleeve = next(s for s in res.sleeves if s.name == "trend")
    value_sleeve = next(s for s in res.sleeves if s.name == "value")
    assert trend_sleeve.weight > value_sleeve.weight, (
        "lower-vol trend sleeve must receive greater inverse-vol weight"
    )
    assert abs(trend_sleeve.weight + value_sleeve.weight - 1.0) < 1e-12


def test_run_allocation_backtest_inverse_vol_weights_sum_to_one():
    ts = _days(6)
    curves = {"a": [100, 101, 102, 101, 103, 102], "b": [100, 95, 105, 95, 105, 100]}
    cfg = AllocationConfig((Sleeve("a", Decimal("0.5")), Sleeve("b", Decimal("0.5"))))
    res = run_allocation_backtest(
        [_spec("a"), _spec("b")],
        cfg,
        weighting="inverse_vol",
        run_backtest_fn=_fake_backtester(curves, ts),
    )
    total = sum(s.weight for s in res.sleeves)
    assert abs(total - 1.0) < 1e-12


def test_run_allocation_backtest_default_weighting_unchanged():
    # Default weighting="config" must produce the same result as before (80/20 config weights).
    ts = _days(6)
    curves = {"trend": [100, 101, 102, 103, 104, 105], "value": [100, 99, 100, 101, 100, 101]}
    cfg = AllocationConfig((Sleeve("trend", Decimal("0.8")), Sleeve("value", Decimal("0.2"))))
    res_default = run_allocation_backtest(
        [_spec("trend"), _spec("value")], cfg, run_backtest_fn=_fake_backtester(curves, ts)
    )
    res_config = run_allocation_backtest(
        [_spec("trend"), _spec("value")],
        cfg,
        weighting="config",
        run_backtest_fn=_fake_backtester(curves, ts),
    )
    # Both must agree on blended returns (byte-identical logic).
    assert res_default.blended_returns == res_config.blended_returns
    # Weights shown must be 0.8 / 0.2 from config.
    for s in res_default.sleeves:
        expected = 0.8 if s.name == "trend" else 0.2
        assert abs(s.weight - expected) < 1e-12


def test_run_allocation_backtest_rejects_unknown_weighting():
    ts = _days(3)
    cfg = AllocationConfig((Sleeve("a", Decimal("1.0")),))
    with pytest.raises(ValueError, match="weighting"):
        run_allocation_backtest(
            [_spec("a")],
            cfg,
            weighting="mean_variance",
            run_backtest_fn=_fake_backtester({"a": [1, 2, 3]}, ts),
        )
