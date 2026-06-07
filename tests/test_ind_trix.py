"""Tests for apex.strategy.ind_trix — TRIX (triple-smoothed EMA rate of change).

Hand-computed against the existing apex.strategy.indicators.ema (which seeds with
the SMA of the first `period` values, then smooths with alpha = 2/(period+1)).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_trix import triple_ema, trix, trix_signal


def _approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


def test_triple_ema_known_values_period2():
    # With period=2 and a linear ramp, the three EMA passes resolve cleanly.
    # e1 = [.,1.5,2.5,3.5,4.5,5.5,6.5,7.5]; e2 = [2,3,4,5,6,7]; e3 = [2.5,3.5,4.5,5.5,6.5]
    data = [1, 2, 3, 4, 5, 6, 7, 8]
    out = triple_ema(data, 2)
    assert len(out) == len(data)
    assert out[0] is None and out[1] is None and out[2] is None
    expected = [2.5, 3.5, 4.5, 5.5, 6.5]
    for got, exp in zip(out[3:], expected):
        assert got is not None and _approx(got, exp)


def test_trix_known_values_period2():
    data = [1, 2, 3, 4, 5, 6, 7, 8]
    out = trix(data, 2)
    assert len(out) == len(data)
    # First valid triple-ema is at index 3, so TRIX is None through index 3.
    assert all(out[i] is None for i in range(4))
    assert _approx(out[4], 3.5 / 2.5 - 1.0)  # 0.4
    assert _approx(out[5], 4.5 / 3.5 - 1.0)  # 0.285714...
    assert _approx(out[6], 5.5 / 4.5 - 1.0)  # 0.222222...
    assert _approx(out[7], 6.5 / 5.5 - 1.0)  # 0.181818...


def test_insufficient_data_returns_all_none():
    # Need enough data to seed three successive EMAs of length `period`.
    short = [10, 11, 12, 13]
    out = trix(short, 5)
    assert out == [None] * len(short)
    assert triple_ema(short, 5) == [None] * len(short)


def test_empty_input():
    assert trix([], 15) == []
    assert triple_ema([], 15) == []
    line, sig = trix_signal([], 15, 9)
    assert line == [] and sig == []


def test_constant_series_trix_is_zero():
    # A flat price → triple EMA is flat → rate of change is exactly 0 (not None).
    data = [50.0] * 30
    out = trix(data, 4)
    valid = [v for v in out if v is not None]
    assert valid, "expected some non-None TRIX values"
    assert all(_approx(v, 0.0) for v in valid)


def test_decimal_input_matches_float():
    data_f = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    data_d = [Decimal(x) for x in (1, 2, 3, 4, 5, 6, 7, 8)]
    out_f = trix(data_f, 2)
    out_d = trix(data_d, 2)
    assert len(out_f) == len(out_d)
    for a, b in zip(out_f, out_d):
        if a is None:
            assert b is None
        else:
            assert b is not None and _approx(a, b)


def test_output_length_always_matches_input():
    data = list(range(40))
    for p in (1, 3, 5, 15):
        assert len(trix(data, p)) == len(data)
        assert len(triple_ema(data, p)) == len(data)


def test_uptrend_positive_downtrend_negative():
    up = [float(x) for x in range(1, 31)]
    down = [float(x) for x in range(30, 0, -1)]
    up_out = [v for v in trix(up, 3) if v is not None]
    down_out = [v for v in trix(down, 3) if v is not None]
    assert up_out and all(v > 0 for v in up_out)
    assert down_out and all(v < 0 for v in down_out)


def test_trix_signal_lengths_and_alignment():
    data = [float(x) for x in range(60)]
    line, sig = trix_signal(data, 5, 9)
    assert len(line) == len(data) == len(sig)
    # Signal line is None wherever the TRIX line is None (can't smooth nothing).
    for ln, sg in zip(line, sig):
        if ln is None:
            assert sg is None
    # And there should be at least one valid signal point on a long ramp.
    assert any(s is not None for s in sig)


def test_trix_signal_too_short_for_signal_period():
    # Enough data for a TRIX line but fewer valid TRIX points than signal_period.
    data = [float(x) for x in range(12)]
    line, sig = trix_signal(data, 3, 50)
    assert any(v is not None for v in line)  # TRIX line exists
    assert sig == [None] * len(data)  # but signal can't form


def test_zero_prior_triple_ema_is_none():
    # If a prior triple-EMA value is exactly 0, rate of change is undefined → None.
    # Construct via a constant-zero prefix transitioning; simplest is all-zero start
    # which keeps triple-EMA at 0, making division skip (None) rather than error.
    data = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    out = trix(data, 2)
    # No ZeroDivisionError, and zero-prior positions are None.
    assert all(v is None for v in out)


def test_invalid_period_raises():
    with pytest.raises(ValueError):
        trix([1, 2, 3], 0)
    with pytest.raises(ValueError):
        triple_ema([1, 2, 3], -1)
    with pytest.raises(ValueError):
        trix_signal([1, 2, 3], 2, 0)
