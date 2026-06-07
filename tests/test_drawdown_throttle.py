"""
Tests for apex.risk.drawdown_throttle — the pure piecewise-linear sizing
throttle. Hand-computed known values plus edge / degenerate cases.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.risk.drawdown_throttle import (
    drawdown_from_equity,
    equity_throttle_factor,
    throttle_factor,
)

_ONE = Decimal("1")
_ZERO = Decimal("0")


# ---------------------------------------------------------------------------
# drawdown_from_equity
# ---------------------------------------------------------------------------


def test_drawdown_at_peak_is_zero():
    assert drawdown_from_equity(100, 100) == _ZERO


def test_drawdown_known_value():
    # peak 100, equity 90 -> 10% drawdown.
    assert drawdown_from_equity(90, 100) == Decimal("0.10")


def test_drawdown_clamps_above_peak_to_zero():
    # Equity exceeding peak should never produce negative drawdown.
    assert drawdown_from_equity(120, 100) == _ZERO


def test_drawdown_no_peak_returns_none():
    assert drawdown_from_equity(50, 0) is None
    assert drawdown_from_equity(50, -10) is None


def test_drawdown_total_loss():
    assert drawdown_from_equity(0, 100) == _ONE


# ---------------------------------------------------------------------------
# throttle_factor — disabled / boundaries
# ---------------------------------------------------------------------------


def test_disabled_when_start_none():
    assert throttle_factor(Decimal("0.50"), start=None) == _ONE


def test_full_size_below_start():
    assert throttle_factor(Decimal("0.05"), start=Decimal("0.10")) == _ONE


def test_full_size_exactly_at_start():
    # drawdown == start is still full size (down-sizing begins PAST start).
    assert throttle_factor(Decimal("0.10"), start=Decimal("0.10")) == _ONE


def test_floor_at_and_beyond_full():
    f = throttle_factor(
        Decimal("0.30"),
        start=Decimal("0.10"),
        full=Decimal("0.30"),
        floor=Decimal("0.40"),
    )
    assert f == Decimal("0.40")
    deeper = throttle_factor(
        Decimal("0.99"),
        start=Decimal("0.10"),
        full=Decimal("0.30"),
        floor=Decimal("0.40"),
    )
    assert deeper == Decimal("0.40")


# ---------------------------------------------------------------------------
# throttle_factor — linear ramp (hand-computed)
# ---------------------------------------------------------------------------


def test_ramp_midpoint():
    # start=0.10, full=0.30, floor=0.40.
    # At drawdown 0.20 (halfway through ramp): frac=0.5,
    # multiplier = 1 - 0.5*(1-0.40) = 1 - 0.30 = 0.70.
    f = throttle_factor(
        Decimal("0.20"),
        start=Decimal("0.10"),
        full=Decimal("0.30"),
        floor=Decimal("0.40"),
    )
    assert f == Decimal("0.70")


def test_ramp_quarter():
    # drawdown 0.15 -> frac=0.25, mult = 1 - 0.25*0.60 = 1 - 0.15 = 0.85.
    f = throttle_factor(
        Decimal("0.15"),
        start=Decimal("0.10"),
        full=Decimal("0.30"),
        floor=Decimal("0.40"),
    )
    assert f == Decimal("0.85")


def test_ramp_is_monotonic_non_increasing():
    kw = dict(start=Decimal("0.05"), full=Decimal("0.25"), floor=Decimal("0.30"))
    vals = [
        throttle_factor(Decimal(str(dd)), **kw)
        for dd in ("0.0", "0.05", "0.10", "0.15", "0.20", "0.25", "0.30")
    ]
    for a, b in zip(vals, vals[1:]):
        assert b <= a
    assert vals[0] == _ONE
    assert vals[-1] == Decimal("0.30")


# ---------------------------------------------------------------------------
# throttle_factor — degenerate / fail-safe configs
# ---------------------------------------------------------------------------


def test_degenerate_full_le_start_collapses_to_floor():
    # full <= start has no valid ramp: past start it must de-risk to floor,
    # never amplify.
    f = throttle_factor(
        Decimal("0.20"),
        start=Decimal("0.30"),
        full=Decimal("0.20"),
        floor=Decimal("0.50"),
    )
    # drawdown 0.20 <= start 0.30 -> still full size.
    assert f == _ONE
    f2 = throttle_factor(
        Decimal("0.35"),
        start=Decimal("0.30"),
        full=Decimal("0.20"),
        floor=Decimal("0.50"),
    )
    # drawdown 0.35 > start -> degenerate ramp collapses to floor.
    assert f2 == Decimal("0.50")


def test_negative_drawdown_treated_as_zero():
    assert throttle_factor(Decimal("-0.10"), start=Decimal("0.05")) == _ONE


def test_floor_clamped_above_one():
    # A floor > 1 must never amplify; clamp to 1.
    f = throttle_factor(
        Decimal("0.99"),
        start=Decimal("0.10"),
        full=Decimal("0.30"),
        floor=Decimal("1.5"),
    )
    assert f == _ONE


def test_floor_clamped_at_or_below_zero():
    f = throttle_factor(
        Decimal("0.99"),
        start=Decimal("0.10"),
        full=Decimal("0.30"),
        floor=Decimal("-0.2"),
    )
    assert f == _ZERO
    # And the ramp toward a zero floor stays within bounds.
    mid = throttle_factor(
        Decimal("0.20"),
        start=Decimal("0.10"),
        full=Decimal("0.30"),
        floor=_ZERO,
    )
    # frac=0.5 -> 1 - 0.5*(1-0) = 0.5
    assert mid == Decimal("0.5")


def test_result_always_in_unit_interval():
    kw = dict(start=Decimal("0.02"), full=Decimal("0.40"), floor=Decimal("0.25"))
    for dd in ("0", "0.01", "0.02", "0.10", "0.21", "0.40", "0.80", "1.0"):
        f = throttle_factor(Decimal(dd), **kw)
        assert _ZERO <= f <= _ONE


def test_accepts_float_and_str_inputs():
    a = throttle_factor(0.20, start=0.10, full=0.30, floor=0.40)
    b = throttle_factor("0.20", start="0.10", full="0.30", floor="0.40")
    assert a == b == Decimal("0.70")


# ---------------------------------------------------------------------------
# equity_throttle_factor — end-to-end kernel
# ---------------------------------------------------------------------------


def test_equity_kernel_disabled():
    assert equity_throttle_factor(50, 100, start=None) == _ONE


def test_equity_kernel_no_peak_full_size():
    assert equity_throttle_factor(50, 0, start=Decimal("0.10")) == _ONE


def test_equity_kernel_matches_throttle():
    # equity 80, peak 100 -> drawdown 0.20.
    # start=0.10, full=0.30, floor=0.40 -> 0.70 (see test_ramp_midpoint).
    f = equity_throttle_factor(
        80,
        100,
        start=Decimal("0.10"),
        full=Decimal("0.30"),
        floor=Decimal("0.40"),
    )
    assert f == Decimal("0.70")


def test_equity_kernel_above_peak_full_size():
    assert (
        equity_throttle_factor(
            120,
            100,
            start=Decimal("0.10"),
        )
        == _ONE
    )


def test_returns_decimal_type():
    assert isinstance(throttle_factor(Decimal("0.20"), start=Decimal("0.10")), Decimal)
    assert isinstance(equity_throttle_factor(80, 100, start=Decimal("0.10")), Decimal)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
