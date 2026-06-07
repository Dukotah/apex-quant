"""
Tests for apex.data.outlier_filter.

The filter drops bars whose close jumps beyond ``threshold * rolling-volatility``
from the last accepted bar. These tests pin down the contract: a lone spike in
an otherwise smooth series is rejected; ordinary bars pass; warmup bars always
pass; the scale is computed over accepted bars only (so a spike cannot widen its
own band); both ATR and MAD methods agree on the obvious cases; empty in → empty
out; and bad parameters fail loud. Pure/offline: no I/O, no clock, no randomness.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apex.core.models import AssetClass, Bar, Symbol
from apex.data.outlier_filter import (
    FilterResult,
    RejectedBar,
    filter_outliers,
)

SPY = Symbol("SPY", AssetClass.ETF)
_EPOCH = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _bar(index: int, close: float, *, spread: float = 0.5) -> Bar:
    """
    Build a tidy OHLCV bar at day ``index`` with the given close. High/low hug
    the close by ``spread`` so the intrabar range (and thus ATR) is small and
    predictable, isolating close-to-close jumps as the thing under test.
    """
    c = Decimal(str(close))
    s = Decimal(str(spread))
    return Bar(
        symbol=SPY,
        timestamp=_EPOCH + timedelta(days=index),
        open=c,
        high=c + s,
        low=c - s,
        close=c,
        volume=Decimal("1000"),
        timeframe="1Day",
    )


def _smooth_series(n: int, start: float = 400.0, step: float = 0.25) -> list[Bar]:
    """A gently rising, low-volatility series — no bar should ever be rejected."""
    return [_bar(i, start + i * step) for i in range(n)]


# --------------------------------------------------------------- empty / shape

def test_empty_input_returns_empty():
    result = filter_outliers([])
    assert isinstance(result, FilterResult)
    assert result.cleaned == []
    assert result.rejected == []


def test_result_preserves_order_and_objects_for_clean_series():
    bars = _smooth_series(40)
    result = filter_outliers(bars, threshold=5.0, window=14, warmup=14, method="atr")
    assert result.rejected == []
    # Same objects, same order — nothing mutated, nothing reordered.
    assert result.cleaned == bars
    assert all(a is b for a, b in zip(result.cleaned, bars))


# ------------------------------------------------------------------- warmup

def test_warmup_bars_always_pass_even_if_spiked():
    # Spike lands inside the warmup window → must pass through untouched.
    bars = _smooth_series(20)
    spiked = list(bars)
    spiked[3] = _bar(3, 9999.0)  # absurd close, but index 3 < warmup
    result = filter_outliers(spiked, threshold=3.0, window=5, warmup=10, method="mad")
    assert result.rejected == []
    assert result.cleaned[3].close == Decimal("9999.0")


def test_bars_pass_until_window_of_accepted_bars_exists():
    # warmup=0 but window=14: indices 0..13 have <14 accepted predecessors, so
    # they pass regardless. A spike at index 5 therefore still passes.
    bars = _smooth_series(30)
    spiked = list(bars)
    spiked[5] = _bar(5, 8000.0)
    result = filter_outliers(spiked, threshold=3.0, window=14, warmup=0, method="atr")
    assert all(rb.bar.close != Decimal("8000.0") for rb in result.rejected)
    assert spiked[5] in result.cleaned


# --------------------------------------------------------------- core rejection

@pytest.mark.parametrize("method", ["atr", "mad"])
def test_single_spike_rejected_normal_bars_pass(method):
    bars = _smooth_series(40)
    spiked = list(bars)
    # Index 25 is well past warmup/window. Jump from ~406.25 to 800 dwarfs the
    # ~0.25 close-to-close / ~1.0 ATR scale of the smooth series.
    spiked[25] = _bar(25, 800.0)
    result = filter_outliers(spiked, threshold=5.0, window=14, warmup=14, method=method)

    assert len(result.rejected) == 1
    rej = result.rejected[0]
    assert isinstance(rej, RejectedBar)
    assert rej.bar is spiked[25]
    assert rej.bar.close == Decimal("800.0")
    assert "exceeds" in rej.reason
    assert method.upper() in rej.reason
    # Everything else survived, in order.
    assert spiked[25] not in result.cleaned
    assert len(result.cleaned) == len(bars) - 1


@pytest.mark.parametrize("method", ["atr", "mad"])
def test_spike_does_not_inflate_band_for_following_bar(method):
    # Two spikes back-to-back. Because the scale uses ACCEPTED bars only, the
    # first rejected spike must not widen the band enough to let the second pass.
    bars = _smooth_series(40)
    spiked = list(bars)
    spiked[25] = _bar(25, 800.0)
    spiked[26] = _bar(26, 760.0)  # still ~350 above the smooth ~406.5 level
    result = filter_outliers(spiked, threshold=5.0, window=14, warmup=14, method=method)
    rejected_closes = {rb.bar.close for rb in result.rejected}
    assert Decimal("800.0") in rejected_closes
    assert Decimal("760.0") in rejected_closes


def test_downward_spike_rejected():
    bars = _smooth_series(40)
    spiked = list(bars)
    spiked[30] = _bar(30, 5.0)  # crash to near-zero
    result = filter_outliers(spiked, threshold=5.0, window=14, warmup=14, method="atr")
    assert len(result.rejected) == 1
    assert result.rejected[0].bar.close == Decimal("5.0")


def test_modest_jump_within_threshold_passes():
    # A jump that is large-ish but under threshold*scale must NOT be rejected.
    bars = _smooth_series(40)
    nudged = list(bars)
    # Smooth close-to-close is 0.25, ATR ~1.0. A +2.0 jump < 5*~1.0 band.
    nudged[25] = _bar(25, float(bars[24].close) + 2.0)
    result = filter_outliers(nudged, threshold=5.0, window=14, warmup=14, method="atr")
    assert result.rejected == []


def test_threshold_controls_strictness():
    bars = _smooth_series(40)
    spiked = list(bars)
    spiked[25] = _bar(25, float(bars[24].close) + 8.0)  # ~8x the ~1.0 ATR scale

    strict = filter_outliers(spiked, threshold=3.0, window=14, warmup=14, method="atr")
    lenient = filter_outliers(spiked, threshold=50.0, window=14, warmup=14, method="atr")

    assert len(strict.rejected) == 1
    assert lenient.rejected == []


# --------------------------------------------------------------- flat window

def test_flat_window_zero_scale_accepts_everything():
    # A perfectly flat series has zero volatility → no meaningful band → accept,
    # even a later jump (no scale to compare against). Must not divide-by-zero
    # or reject on a degenerate band.
    flat = [_bar(i, 100.0, spread=0.0) for i in range(20)]
    flat.append(_bar(20, 150.0, spread=0.0))
    result = filter_outliers(flat, threshold=5.0, window=10, warmup=10, method="mad")
    assert result.rejected == []
    assert len(result.cleaned) == 21


# --------------------------------------------------------------- determinism

def test_deterministic_repeated_runs():
    bars = _smooth_series(50)
    spiked = list(bars)
    spiked[40] = _bar(40, 1200.0)
    a = filter_outliers(spiked, threshold=4.0, window=14, warmup=14, method="atr")
    b = filter_outliers(spiked, threshold=4.0, window=14, warmup=14, method="atr")
    assert [x.close for x in a.cleaned] == [x.close for x in b.cleaned]
    assert [r.reason for r in a.rejected] == [r.reason for r in b.rejected]


def test_rejected_bar_timestamp_property():
    bars = _smooth_series(40)
    spiked = list(bars)
    spiked[25] = _bar(25, 800.0)
    result = filter_outliers(spiked, threshold=5.0, window=14, warmup=14, method="atr")
    assert result.rejected[0].timestamp == spiked[25].timestamp
    assert result.rejected[0].timestamp.tzinfo == timezone.utc


# --------------------------------------------------------------- bad params

@pytest.mark.parametrize(
    "kwargs",
    [
        {"threshold": 0.0},
        {"threshold": -1.0},
        {"window": 0},
        {"window": -5},
        {"warmup": -1},
        {"method": "bogus"},
    ],
)
def test_bad_parameters_raise(kwargs):
    with pytest.raises(ValueError):
        filter_outliers(_smooth_series(20), **kwargs)


# --------------------------------------------------------------- bars unmutated

def test_input_bars_not_mutated():
    bars = _smooth_series(40)
    spiked = list(bars)
    spiked[25] = _bar(25, 800.0)
    closes_before = [b.close for b in spiked]
    filter_outliers(spiked, threshold=5.0, window=14, warmup=14, method="atr")
    closes_after = [b.close for b in spiked]
    assert closes_before == closes_after  # frozen bars untouched
