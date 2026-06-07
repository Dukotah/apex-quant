"""Tests for apex.validation.rolling_sharpe.

Hand-computed known values plus edge cases. Pure and fast.
"""
from __future__ import annotations

import math

import pytest

from apex.validation.rolling_sharpe import (
    latest_rolling_sharpe,
    rolling_sharpe,
    rolling_sharpe_stats,
    window_sharpe,
)


def _manual_sharpe(window, rf=0.0, ppy=252):
    """Independent reference implementation for cross-checking."""
    per = rf / ppy
    excess = [r - per for r in window]
    mean = sum(excess) / len(excess)
    var = sum((x - mean) ** 2 for x in excess) / len(excess)  # population
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return (mean / sd) * math.sqrt(ppy)


# --- window_sharpe -------------------------------------------------------

def test_window_sharpe_hand_computed():
    # Returns [0.01, 0.02, 0.03]: mean=0.02, pop var = 2/3*0.0001
    # pstdev = 0.01 * sqrt(2/3); Sharpe = (0.02 / sd) * sqrt(252)
    window = [0.01, 0.02, 0.03]
    sd = 0.01 * math.sqrt(2.0 / 3.0)
    expected = (0.02 / sd) * math.sqrt(252)
    got = window_sharpe(window)
    assert got is not None
    assert got == pytest.approx(expected, rel=1e-12)


def test_window_sharpe_matches_reference():
    window = [0.005, -0.01, 0.02, 0.0, 0.013]
    assert window_sharpe(window) == pytest.approx(_manual_sharpe(window), rel=1e-12)


def test_window_sharpe_zero_variance_returns_none():
    assert window_sharpe([0.01, 0.01, 0.01]) is None


def test_window_sharpe_too_few_points_returns_none():
    assert window_sharpe([]) is None
    assert window_sharpe([0.01]) is None


def test_window_sharpe_risk_free_rate_applied():
    window = [0.01, 0.02, 0.03]
    # With rf, mean excess shifts down but sd is unchanged.
    rf = 0.0252  # per-period = 0.0001
    sd = 0.01 * math.sqrt(2.0 / 3.0)
    expected = ((0.02 - 0.0001) / sd) * math.sqrt(252)
    assert window_sharpe(window, risk_free_rate=rf) == pytest.approx(expected, rel=1e-12)


# --- rolling_sharpe ------------------------------------------------------

def test_rolling_sharpe_length_and_values():
    returns = [0.01, 0.02, 0.03, 0.04]
    out = rolling_sharpe(returns, window=3)
    # N=4, W=3 -> 2 windows: [0.01,0.02,0.03] and [0.02,0.03,0.04]
    assert len(out) == 2
    assert out[0] == pytest.approx(_manual_sharpe([0.01, 0.02, 0.03]), rel=1e-12)
    assert out[1] == pytest.approx(_manual_sharpe([0.02, 0.03, 0.04]), rel=1e-12)


def test_rolling_sharpe_higher_mean_same_sd_higher_sharpe():
    # Both windows have identical sd (evenly spaced by 0.01) but the second has
    # a higher mean, so its Sharpe must be strictly larger.
    returns = [0.01, 0.02, 0.03, 0.04]
    out = rolling_sharpe(returns, window=3)
    assert out[1] > out[0] > 0


def test_rolling_sharpe_insufficient_data_returns_empty():
    assert rolling_sharpe([0.01, 0.02], window=3) == []
    assert rolling_sharpe([], window=2) == []


def test_rolling_sharpe_window_equals_length():
    returns = [0.01, -0.02, 0.03]
    out = rolling_sharpe(returns, window=3)
    assert len(out) == 1
    assert out[0] == pytest.approx(_manual_sharpe(returns), rel=1e-12)


def test_rolling_sharpe_none_for_flat_window():
    # A flat window has zero variance -> None, surrounding windows defined.
    returns = [0.05, 0.05, 0.05, 0.01]
    out = rolling_sharpe(returns, window=3)
    assert out[0] is None  # [0.05, 0.05, 0.05]
    assert out[1] is not None  # [0.05, 0.05, 0.01]


def test_rolling_sharpe_window_too_small_raises():
    with pytest.raises(ValueError):
        rolling_sharpe([0.01, 0.02, 0.03], window=1)
    with pytest.raises(ValueError):
        rolling_sharpe([0.01, 0.02, 0.03], window=0)


# --- latest_rolling_sharpe ----------------------------------------------

def test_latest_rolling_sharpe_matches_last_of_series():
    returns = [0.01, 0.02, 0.03, 0.04, 0.05]
    series = rolling_sharpe(returns, window=3)
    assert latest_rolling_sharpe(returns, window=3) == pytest.approx(series[-1], rel=1e-12)


def test_latest_rolling_sharpe_insufficient_returns_none():
    assert latest_rolling_sharpe([0.01], window=3) is None
    assert latest_rolling_sharpe([0.01, 0.02], window=2) is not None


def test_latest_rolling_sharpe_window_too_small_none():
    assert latest_rolling_sharpe([0.01, 0.02], window=1) is None


# --- rolling_sharpe_stats ------------------------------------------------

def test_rolling_sharpe_stats_basic():
    returns = [0.01, 0.02, 0.03, 0.04]
    stats = rolling_sharpe_stats(returns, window=3)
    series = rolling_sharpe(returns, window=3)
    defined = [s for s in series if s is not None]
    assert stats["count"] == float(len(defined))
    assert stats["mean"] == pytest.approx(sum(defined) / len(defined), rel=1e-12)
    assert stats["min"] == pytest.approx(min(defined), rel=1e-12)
    assert stats["max"] == pytest.approx(max(defined), rel=1e-12)
    assert stats["last"] == pytest.approx(series[-1], rel=1e-12)
    assert stats["positive_fraction"] == pytest.approx(1.0, rel=1e-12)


def test_rolling_sharpe_stats_empty_series():
    stats = rolling_sharpe_stats([0.01, 0.02], window=3)
    assert stats["count"] == 0.0
    assert stats["mean"] is None
    assert stats["min"] is None
    assert stats["max"] is None
    assert stats["last"] is None
    assert stats["positive_fraction"] is None


def test_rolling_sharpe_stats_all_windows_undefined():
    # Every window flat -> all None; count 0 but last (None) reflects series end.
    returns = [0.02, 0.02, 0.02, 0.02]
    stats = rolling_sharpe_stats(returns, window=3)
    assert stats["count"] == 0.0
    assert stats["mean"] is None
    assert stats["last"] is None  # last window was undefined


def test_rolling_sharpe_stats_mixed_positive_fraction():
    # Construct windows with mixed Sharpe signs.
    # Negative-drift window then positive-drift window.
    returns = [0.0, -0.01, -0.02, 0.02, 0.03]
    stats = rolling_sharpe_stats(returns, window=3)
    series = rolling_sharpe(returns, window=3)
    defined = [s for s in series if s is not None]
    positives = sum(1 for s in defined if s > 0)
    assert stats["positive_fraction"] == pytest.approx(positives / len(defined), rel=1e-12)
    assert 0.0 < stats["positive_fraction"] < 1.0
