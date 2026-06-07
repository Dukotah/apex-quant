"""
Tests for apex.data.resample.

Resampling rolls finer single-symbol bars up into coarser ones; it must be
``Decimal``-correct, deterministic, calendar-aligned, and fail loud on bad input
(unsorted / mixed-symbol) rather than emit garbage. OHLCV aggregation is checked
against hand-computed values. Pure/offline: no I/O, no clock, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apex.core.models import AssetClass, Bar, Symbol
from apex.data.resample import parse_timeframe, resample_bars

NVDA = Symbol("NVDA", AssetClass.EQUITY)
SPY = Symbol("SPY", AssetClass.ETF)

# A clean epoch-aligned anchor: 2024-01-01T00:00:00Z falls on a 5-minute boundary.
BASE = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _bar(
    minute: int,
    o: str,
    h: str,
    lo: str,
    c: str,
    v: str,
    *,
    symbol: Symbol = NVDA,
) -> Bar:
    """One 1-minute bar whose close timestamp is BASE + ``minute`` minutes."""
    return Bar(
        symbol=symbol,
        timestamp=BASE + timedelta(minutes=minute),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=Decimal(v),
        timeframe="1Min",
    )


# ----------------------------------------------------------- parse_timeframe


def test_parse_timeframe_known_spellings():
    assert parse_timeframe("1Min") == timedelta(minutes=1)
    assert parse_timeframe("5Min") == timedelta(minutes=5)
    assert parse_timeframe("1Hour") == timedelta(hours=1)
    assert parse_timeframe("1Day") == timedelta(days=1)


def test_parse_timeframe_case_insensitive_and_implicit_one():
    assert parse_timeframe("15min") == timedelta(minutes=15)
    assert parse_timeframe("Day") == timedelta(days=1)


def test_parse_timeframe_rejects_garbage_and_zero():
    with pytest.raises(ValueError):
        parse_timeframe("5Fortnights")
    with pytest.raises(ValueError):
        parse_timeframe("0Min")
    with pytest.raises(ValueError):
        parse_timeframe("")


# ---------------------------------------------------------- core aggregation


def test_five_one_minute_bars_into_one_five_minute_bar():
    # Five 1-min bars filling exactly the 00:00–00:05 bucket (closes at +1..+5).
    bars = [
        _bar(1, "10", "12", "9", "11", "100"),
        _bar(2, "11", "15", "10", "14", "200"),
        _bar(3, "14", "14", "8", "9", "150"),  # contains the bucket low (8)
        _bar(4, "9", "16", "9", "13", "250"),  # contains the bucket high (16)
        _bar(5, "13", "13", "12", "12", "300"),
    ]
    out = resample_bars(bars, "5Min")
    assert len(out) == 1
    bar = out[0]

    # OHLC: open=first open, high=max, low=min, close=last close.
    assert bar.open == Decimal("10")
    assert bar.high == Decimal("16")
    assert bar.low == Decimal("8")
    assert bar.close == Decimal("12")
    # Volume = sum, and stays Decimal.
    assert bar.volume == Decimal("1000")
    assert isinstance(bar.volume, Decimal)
    # Timestamp = bucket close time; timeframe = target.
    assert bar.timestamp == BASE + timedelta(minutes=5)
    assert bar.timeframe == "5Min"
    assert bar.symbol == NVDA


def test_decimal_precision_preserved_no_float_drift():
    # 0.1 + 0.2 must stay exact in Decimal land (would be 0.30000000000000004 as float).
    bars = [
        _bar(1, "1.1", "1.1", "1.1", "1.1", "0.1"),
        _bar(2, "1.2", "1.2", "1.2", "1.2", "0.2"),
    ]
    # Only a partial bucket forms here (two of five minutes), so keep it.
    out = resample_bars(bars, "5Min", keep_partial=True)
    assert out[0].volume == Decimal("0.3")


def test_multiple_buckets_aligned_to_epoch():
    # Ten 1-min bars → two complete 5-min buckets. Flat bar at value m keeps each
    # bar valid (o=h=lo=c=m) while preserving the per-bucket close/volume math.
    bars = [_bar(m, str(m), str(m), str(m), str(m), str(m)) for m in range(1, 11)]
    out = resample_bars(bars, "5Min")
    assert len(out) == 2
    assert out[0].timestamp == BASE + timedelta(minutes=5)
    assert out[1].timestamp == BASE + timedelta(minutes=10)
    # First bucket = closes at +1..+5, second = +6..+10.
    assert out[0].close == Decimal("5")
    assert out[1].close == Decimal("10")
    assert out[0].volume == Decimal("15")  # 1+2+3+4+5
    assert out[1].volume == Decimal("40")  # 6+7+8+9+10


def test_resample_to_same_timeframe_is_identity_ohlcv():
    bars = [_bar(1, "10", "12", "9", "11", "100")]
    out = resample_bars(bars, "1Min")
    assert len(out) == 1
    assert out[0].open == Decimal("10")
    assert out[0].close == Decimal("11")
    assert out[0].timestamp == BASE + timedelta(minutes=1)


# --------------------------------------------------------- partial buckets


def test_partial_trailing_bucket_dropped_by_default():
    # Six 1-min bars: first five complete the 00:00–00:05 bucket, the sixth
    # (close at +6) only begins the next bucket → trailing partial, dropped.
    bars = [_bar(m, "10", "10", "10", "10", "10") for m in range(1, 7)]
    out = resample_bars(bars, "5Min")
    assert len(out) == 1
    assert out[0].timestamp == BASE + timedelta(minutes=5)


def test_partial_trailing_bucket_kept_when_requested():
    bars = [_bar(m, "10", "10", "10", "10", "10") for m in range(1, 7)]
    out = resample_bars(bars, "5Min", keep_partial=True)
    assert len(out) == 2
    # The partial bucket still carries its (would-be) close boundary timestamp.
    assert out[1].timestamp == BASE + timedelta(minutes=10)
    assert out[1].volume == Decimal("10")  # only the single bar so far


def test_exactly_full_trailing_bucket_is_not_partial():
    bars = [_bar(m, "10", "10", "10", "10", "10") for m in range(1, 6)]
    out = resample_bars(bars, "5Min")
    assert len(out) == 1  # complete, kept even with keep_partial=False


# ------------------------------------------------------------- edge cases


def test_empty_input_returns_empty():
    assert resample_bars([], "5Min") == []


def test_unsorted_input_raises():
    bars = [
        _bar(2, "10", "10", "10", "10", "10"),
        _bar(1, "10", "10", "10", "10", "10"),
    ]
    with pytest.raises(ValueError):
        resample_bars(bars, "5Min")


def test_duplicate_timestamp_raises_strictly_ascending():
    bars = [
        _bar(1, "10", "10", "10", "10", "10"),
        _bar(1, "11", "11", "11", "11", "11"),
    ]
    with pytest.raises(ValueError):
        resample_bars(bars, "5Min")


def test_mixed_symbols_raises():
    bars = [
        _bar(1, "10", "10", "10", "10", "10", symbol=NVDA),
        _bar(2, "10", "10", "10", "10", "10", symbol=SPY),
    ]
    with pytest.raises(ValueError):
        resample_bars(bars, "5Min")


def test_bad_target_timeframe_raises():
    bars = [_bar(1, "10", "10", "10", "10", "10")]
    with pytest.raises(ValueError):
        resample_bars(bars, "notatimeframe")


def test_hourly_rollup_from_minutes():
    # 60 one-minute bars (closes +6..+65) fill the 00:00–01:00 hour bucket.
    # close = 5 + m keeps every bar valid (low 5 stays the series minimum).
    bars = [_bar(m, "10", str(10 + m), "5", str(5 + m), "1") for m in range(1, 61)]
    out = resample_bars(bars, "1Hour")
    assert len(out) == 1
    bar = out[0]
    assert bar.timestamp == BASE + timedelta(hours=1)
    assert bar.timeframe == "1Hour"
    assert bar.open == Decimal("10")
    assert bar.close == Decimal("65")  # 5 + 60
    assert bar.high == Decimal("70")  # 10 + 60
    assert bar.low == Decimal("5")
    assert bar.volume == Decimal("60")
