"""
Tests for apex.data.dollar_volume_bars.

Pins the deterministic aggregation of finer Bars into volume / dollar-volume
bars: OHLC stitching (first open, max high, min low, last close), volume summing,
last-bar timestamp, the >= threshold trigger, single-bar overflow, partial-tail
handling, multi-symbol rejection, and graceful empty/insufficient input. Pure and
offline: no I/O, no clock, no randomness. Values are hand-computed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apex.core.models import AssetClass, Bar, Symbol
from apex.data.dollar_volume_bars import (
    BarMetric,
    aggregate_bars,
    aggregate_dollar_volume_bars,
    aggregate_volume_bars,
    bar_dollar_volume,
    total_metric,
)

NVDA = Symbol("NVDA", AssetClass.EQUITY)
SPY = Symbol("SPY", AssetClass.ETF)

T0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _bar(
    minute: int,
    o: str,
    h: str,
    lo: str,
    c: str,
    v: str,
    symbol: Symbol = NVDA,
) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=T0 + timedelta(minutes=minute),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=Decimal(v),
        timeframe="1Min",
    )


# ----------------------------------------------------------------- helpers


def test_bar_dollar_volume_is_close_times_volume():
    b = _bar(0, "10", "11", "9", "10", "100")
    assert bar_dollar_volume(b) == Decimal("1000")


def test_total_metric_volume_and_dollar():
    bars = [_bar(0, "10", "10", "10", "10", "100"), _bar(1, "20", "20", "20", "20", "50")]
    assert total_metric(bars, BarMetric.VOLUME) == Decimal("150")
    # 10*100 + 20*50 = 1000 + 1000 = 2000
    assert total_metric(bars, BarMetric.DOLLAR_VOLUME) == Decimal("2000")


def test_total_metric_empty_is_zero():
    assert total_metric([]) == Decimal("0")


# ------------------------------------------------------------- volume bars


def test_volume_bars_basic_ohlc_and_timestamp():
    # Three 1Min bars; volume threshold 250 → first two (100+150=250) form bar 1.
    bars = [
        _bar(0, "10", "12", "9", "11", "100"),
        _bar(1, "11", "15", "10", "14", "150"),
        _bar(2, "14", "16", "13", "15", "300"),
    ]
    out = aggregate_volume_bars(bars, 250)
    assert len(out) == 2

    b1 = out[0]
    assert b1.open == Decimal("10")  # first bar's open
    assert b1.high == Decimal("15")  # max(12, 15)
    assert b1.low == Decimal("9")  # min(9, 10)
    assert b1.close == Decimal("14")  # last contributing close
    assert b1.volume == Decimal("250")  # 100 + 150
    assert b1.timestamp == T0 + timedelta(minutes=1)  # last contributor's ts

    b2 = out[1]
    assert b2.open == Decimal("14")
    assert b2.high == Decimal("16")
    assert b2.low == Decimal("13")
    assert b2.close == Decimal("15")
    assert b2.volume == Decimal("300")


def test_threshold_trigger_is_inclusive_ge():
    # Exactly hitting the threshold on a single bar emits immediately.
    bars = [_bar(0, "10", "10", "10", "10", "100")]
    out = aggregate_volume_bars(bars, 100)
    assert len(out) == 1
    assert out[0].volume == Decimal("100")


def test_single_bar_exceeding_threshold_emits_alone():
    # One source bar with volume 1000 against threshold 250 -> its own bar (never split).
    bars = [_bar(0, "10", "12", "9", "11", "1000")]
    out = aggregate_volume_bars(bars, 250)
    assert len(out) == 1
    assert out[0].volume == Decimal("1000")
    assert out[0].close == Decimal("11")


# ------------------------------------------------------- dollar-volume bars


def test_dollar_volume_bars_threshold():
    # dollar volume per bar: 10*50=500, 20*30=600 -> cumulative 1100 >= 1000 after 2.
    bars = [
        _bar(0, "10", "10", "10", "10", "50"),
        _bar(1, "20", "21", "19", "20", "30"),
        _bar(2, "20", "20", "20", "20", "10"),  # 20*10=200, leftover (<1000)
    ]
    out = aggregate_dollar_volume_bars(bars, 1000)
    assert len(out) == 1  # trailing 200 dropped by default
    assert out[0].open == Decimal("10")
    assert out[0].close == Decimal("20")
    assert out[0].volume == Decimal("80")  # 50 + 30


def test_default_metric_is_dollar_volume():
    bars = [_bar(0, "10", "10", "10", "10", "50"), _bar(1, "20", "20", "20", "20", "30")]
    via_default = aggregate_bars(bars, 1000)
    via_explicit = aggregate_dollar_volume_bars(bars, 1000)
    assert [b.volume for b in via_default] == [b.volume for b in via_explicit]
    assert len(via_default) == 1


# ----------------------------------------------------------- partial tail


def test_partial_tail_dropped_by_default():
    bars = [_bar(0, "10", "10", "10", "10", "100")]
    out = aggregate_volume_bars(bars, 250)  # never reaches 250
    assert out == []


def test_partial_tail_emitted_when_requested():
    bars = [
        _bar(0, "10", "12", "9", "11", "100"),
        _bar(1, "11", "13", "10", "12", "80"),
    ]
    out = aggregate_volume_bars(bars, 250, emit_partial=True)
    assert len(out) == 1
    assert out[0].volume == Decimal("180")
    assert out[0].open == Decimal("10")
    assert out[0].high == Decimal("13")
    assert out[0].low == Decimal("9")
    assert out[0].close == Decimal("12")


# --------------------------------------------------- edge cases / failures


def test_empty_input_yields_empty():
    assert aggregate_bars([], 100) == []
    assert aggregate_volume_bars([], 100, emit_partial=True) == []


def test_zero_volume_bar_folds_into_ohlc_without_advancing():
    # A zero-volume bar still contributes price action but not to the running total.
    bars = [
        _bar(0, "10", "12", "9", "11", "0"),  # vol 0
        _bar(1, "11", "20", "8", "15", "250"),
    ]
    out = aggregate_volume_bars(bars, 250)
    assert len(out) == 1
    assert out[0].open == Decimal("10")  # zero-vol bar's open is kept
    assert out[0].high == Decimal("20")
    assert out[0].low == Decimal("8")
    assert out[0].close == Decimal("15")
    assert out[0].volume == Decimal("250")


@pytest.mark.parametrize("bad", [0, -1, "0", "-5", "abc", None])
def test_bad_threshold_raises(bad):
    with pytest.raises(ValueError):
        aggregate_bars([_bar(0, "10", "10", "10", "10", "1")], bad)


def test_multiple_symbols_rejected():
    bars = [
        _bar(0, "10", "10", "10", "10", "100", symbol=NVDA),
        _bar(1, "20", "20", "20", "20", "100", symbol=SPY),
    ]
    with pytest.raises(ValueError):
        aggregate_volume_bars(bars, 1000)


def test_custom_timeframe_label_used():
    bars = [_bar(0, "10", "10", "10", "10", "100")]
    out = aggregate_volume_bars(bars, 100, timeframe="vbar")
    assert out[0].timeframe == "vbar"


def test_default_timeframe_label_describes_metric_and_threshold():
    bars = [_bar(0, "10", "10", "10", "10", "100")]
    out = aggregate_volume_bars(bars, 100)
    assert out[0].timeframe == "volume@100"
    out2 = aggregate_dollar_volume_bars(bars, 500)
    assert out2[0].timeframe == "dollar@500"


def test_output_bars_are_valid_frozen_bars():
    bars = [_bar(0, "10", "12", "9", "11", "300")]
    out = aggregate_volume_bars(bars, 250)
    b = out[0]
    assert b.timestamp.tzinfo == timezone.utc
    with pytest.raises(Exception):
        b.close = Decimal("999")  # frozen
