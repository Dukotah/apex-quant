"""
Tests for apex.validation.walk_forward — focused on the efficiency metric, which used to
be a return-ratio that exploded (stitched cumulative return / single in-sample window
return → 66-397). It is now OOS-Sharpe / in-sample-Sharpe: bounded and scale-free.
"""

from __future__ import annotations

from apex.validation.walk_forward import run_walk_forward


def _ramp(n: int, step: float = 1.002) -> list[float]:
    """A steadily-rising equity curve of length n."""
    return [1000.0 * (step**i) for i in range(max(n, 0))]


def test_efficiency_is_bounded_when_oos_matches_in_sample():
    # Every window (and the in-sample window) gets the SAME steady ramp, so OOS Sharpe
    # should ~equal in-sample Sharpe → efficiency ~1.0, NOT an exploded return ratio.
    def bt(_tr_s, _tr_e, te_s, te_e):
        return _ramp(te_e - te_s)

    res = run_walk_forward(
        total_bars=1000, backtest_fn=bt, train_bars=400, test_bars=100, step_bars=100
    )
    assert res.num_windows >= 2
    assert 0.5 < res.walk_forward_efficiency < 2.0  # bounded near 1, not 60-400
    assert res.walk_forward_efficiency < 10.0  # explicitly: no explosion


def test_efficiency_zero_when_is_sharpe_near_zero():
    # If the in-sample reference window is flat (IS Sharpe ~ 0), the efficiency ratio is
    # undefined.  We must return 0.0 (fail-closed), not a spuriously large number.
    def bt(_tr_s, _tr_e, te_s, te_e):
        # IS reference call is (0, train_bars, 0, train_bars): return flat.
        if te_s == 0:
            return [1000.0] * (te_e - te_s)  # flat → IS Sharpe ~ 0
        return _ramp(te_e - te_s)  # OOS rises (ratio would blow up without the guard)

    res = run_walk_forward(
        total_bars=1000, backtest_fn=bt, train_bars=400, test_bars=100, step_bars=100
    )
    assert res.walk_forward_efficiency == 0.0  # guard fires: near-zero IS Sharpe → 0, not ∞
    assert res.passed is False  # efficiency 0 < min_efficiency 0.5 → gate fails (fail-closed)


def test_efficiency_collapses_when_oos_decays():
    # In-sample rises (positive Sharpe) but every OOS test window is flat → OOS Sharpe ~0
    # → efficiency ~0 → fails the min-efficiency gate. This is the overfit signal we want.
    def bt(tr_s, tr_e, te_s, te_e):
        # The in-sample reference call is (0, train_bars, 0, train_bars): rising.
        if te_s == 0:
            return _ramp(te_e - te_s)
        return [1000.0] * (te_e - te_s)  # flat OOS

    res = run_walk_forward(
        total_bars=1000, backtest_fn=bt, train_bars=400, test_bars=100, step_bars=100
    )
    assert res.walk_forward_efficiency < 0.5
    assert res.passed is False
