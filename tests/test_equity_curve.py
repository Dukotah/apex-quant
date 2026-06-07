"""Tests for apex.analytics.equity_curve — hand-computed values + edge cases."""
from __future__ import annotations

import math

from apex.analytics.equity_curve import (
    equity_curve_from_pnl,
    equity_curve_from_returns,
    final_equity,
    normalize_curve,
    pnl_to_returns,
    returns_to_pnl,
)


def _close(a, b, tol=1e-9):
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# equity_curve_from_returns
# ---------------------------------------------------------------------------

def test_returns_curve_hand_computed():
    # 1000 -> +10% -> 1100 -> -50% -> 550 -> +0% -> 550
    curve = equity_curve_from_returns(1000.0, [0.10, -0.50, 0.0])
    assert len(curve) == 4
    assert _close(curve[0], 1000.0)
    assert _close(curve[1], 1100.0)
    assert _close(curve[2], 550.0)
    assert _close(curve[3], 550.0)


def test_returns_curve_empty_is_single_point():
    assert equity_curve_from_returns(1000.0, []) == [1000.0]


def test_returns_curve_compounds():
    # two consecutive +100% returns quadruple capital
    curve = equity_curve_from_returns(50.0, [1.0, 1.0])
    assert _close(curve[-1], 200.0)


def test_returns_curve_can_go_to_zero():
    curve = equity_curve_from_returns(100.0, [-1.0])
    assert _close(curve[-1], 0.0)


# ---------------------------------------------------------------------------
# equity_curve_from_pnl
# ---------------------------------------------------------------------------

def test_pnl_curve_hand_computed():
    # start 1000, +50, -30, +200 -> 1050, 1020, 1220
    curve = equity_curve_from_pnl(1000.0, [50.0, -30.0, 200.0])
    assert curve == [1000.0, 1050.0, 1020.0, 1220.0]


def test_pnl_curve_empty_is_single_point():
    assert equity_curve_from_pnl(500.0, []) == [500.0]


def test_pnl_curve_can_go_negative():
    curve = equity_curve_from_pnl(100.0, [-150.0])
    assert _close(curve[-1], -50.0)


# ---------------------------------------------------------------------------
# returns_to_pnl  /  pnl_to_returns  (round-trips)
# ---------------------------------------------------------------------------

def test_returns_to_pnl_hand_computed():
    # 1000 -> +10% (=+100) -> 1100 -> -50% (=-550) -> 550
    pnl = returns_to_pnl(1000.0, [0.10, -0.50])
    assert len(pnl) == 2
    assert _close(pnl[0], 100.0)
    assert _close(pnl[1], -550.0)


def test_returns_to_pnl_empty():
    assert returns_to_pnl(1000.0, []) == []


def test_pnl_to_returns_hand_computed():
    # start 1000: +100 over 1000 = 0.10 ; then -550 over 1100 = -0.50
    rets = pnl_to_returns(1000.0, [100.0, -550.0])
    assert len(rets) == 2
    assert _close(rets[0], 0.10)
    assert _close(rets[1], -0.50)


def test_pnl_to_returns_empty():
    assert pnl_to_returns(1000.0, []) == []


def test_pnl_to_returns_fails_closed_at_zero_equity():
    # equity reaches exactly zero after the first PnL; next period -> 0.0 return
    rets = pnl_to_returns(100.0, [-100.0, 25.0])
    assert _close(rets[0], -1.0)
    assert _close(rets[1], 0.0)  # would divide by zero -> fail closed


def test_returns_pnl_roundtrip():
    returns = [0.05, -0.02, 0.13, -0.30, 0.08]
    pnl = returns_to_pnl(10000.0, returns)
    back = pnl_to_returns(10000.0, pnl)
    assert len(back) == len(returns)
    for r, b in zip(returns, back):
        assert _close(r, b)


def test_pnl_returns_roundtrip():
    pnl = [120.0, -45.0, 300.0, -10.0]
    returns = pnl_to_returns(5000.0, pnl)
    back = returns_to_pnl(5000.0, returns)
    assert len(back) == len(pnl)
    for p, b in zip(pnl, back):
        assert _close(p, b, tol=1e-7)


# ---------------------------------------------------------------------------
# normalize_curve
# ---------------------------------------------------------------------------

def test_normalize_curve_rebases_to_one():
    norm = normalize_curve([1000.0, 1100.0, 550.0])
    assert _close(norm[0], 1.0)
    assert _close(norm[1], 1.1)
    assert _close(norm[2], 0.55)


def test_normalize_curve_empty():
    assert normalize_curve([]) == []


def test_normalize_curve_zero_base_unchanged():
    # cannot rebase off zero -> return values unchanged (fail closed)
    assert normalize_curve([0.0, 5.0, 10.0]) == [0.0, 5.0, 10.0]


# ---------------------------------------------------------------------------
# final_equity
# ---------------------------------------------------------------------------

def test_final_equity_matches_curve_tail():
    returns = [0.10, -0.50, 0.0, 0.25]
    assert _close(
        final_equity(1000.0, returns),
        equity_curve_from_returns(1000.0, returns)[-1],
    )


def test_final_equity_empty_is_initial():
    assert _close(final_equity(1234.5, []), 1234.5)


# ---------------------------------------------------------------------------
# integer / int-input handling (returns should be floats)
# ---------------------------------------------------------------------------

def test_int_inputs_produce_floats():
    curve = equity_curve_from_returns(1000, [1, 1])  # ints
    assert all(isinstance(v, float) for v in curve)
    assert _close(curve[-1], 4000.0)


def test_no_nan_on_normal_inputs():
    curve = equity_curve_from_returns(1000.0, [0.01, -0.01, 0.02])
    assert all(math.isfinite(v) for v in curve)
