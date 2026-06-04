"""
Tests for apex.data.normalizer.

The normalizer is the single raw→model translation boundary, so these tests pin
down every dialect it must accept (datetime/ISO/Zulu/epoch timestamps; float/
str/Decimal prices; dict rows; SDK-style attribute objects) and confirm it fails
loud — never silently — on bad input. Pure/offline: no I/O, no clock, no network.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from apex.core.models import AssetClass, Bar, Symbol, Tick
from apex.data import normalizer as norm

NVDA = Symbol("NVDA", AssetClass.EQUITY)


# ------------------------------------------------------------------- to_utc

def test_naive_datetime_assumed_utc():
    dt = norm.to_utc(datetime(2024, 1, 1, 12, 0, 0))
    assert dt.tzinfo == timezone.utc
    assert dt.hour == 12


def test_aware_datetime_converted_to_utc():
    from datetime import timedelta
    aware = datetime(2024, 1, 1, 5, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    dt = norm.to_utc(aware)
    assert dt == datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


def test_iso_string_and_zulu_resolve_to_same_instant():
    a = norm.to_utc("2024-01-01T00:00:00Z")
    b = norm.to_utc("2024-01-01T05:00:00+05:00")
    assert a == b
    assert a.tzinfo == timezone.utc


def test_epoch_seconds_and_millis():
    secs = norm.to_utc(1704067200)          # 2024-01-01T00:00:00Z
    millis = norm.to_utc(1704067200000)     # same instant in ms
    assert secs == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert secs == millis


@pytest.mark.parametrize("bad", [None, "", "not-a-date", True])
def test_bad_timestamp_raises(bad):
    with pytest.raises(ValueError):
        norm.to_utc(bad)


# ---------------------------------------------------------------- to_decimal

def test_to_decimal_from_float_str_decimal():
    assert norm.to_decimal(10.5) == Decimal("10.5")
    assert norm.to_decimal("10.5") == Decimal("10.5")
    assert norm.to_decimal(Decimal("10.5")) == Decimal("10.5")


def test_to_decimal_avoids_binary_float_artifact():
    # str() path means we get exactly what the source intended, not 0.1+0.2 noise.
    assert norm.to_decimal("0.30") == Decimal("0.30")


@pytest.mark.parametrize("bad", [None, "", "abc"])
def test_to_decimal_bad_raises_with_field_name(bad):
    with pytest.raises(ValueError) as exc:
        norm.to_decimal(bad, field="close")
    assert "close" in str(exc.value)


# ------------------------------------------------------------------ make_bar

def test_make_bar_normalizes_types():
    bar = norm.make_bar(NVDA, "2024-01-01", 10.0, 11.0, 9.0, 10.5, 1000)
    assert isinstance(bar, Bar)
    assert bar.timestamp.tzinfo == timezone.utc
    assert isinstance(bar.close, Decimal)
    assert bar.close == Decimal("10.5")


def test_make_bar_rejects_high_below_low():
    # The frozen Bar's own validation fires through the normalizer.
    with pytest.raises(ValueError):
        norm.make_bar(NVDA, "2024-01-01", 10, 9, 11, 10, 100)  # high < low


def test_make_bar_rejects_negative_price():
    with pytest.raises(ValueError):
        norm.make_bar(NVDA, "2024-01-01", -1, 11, 9, 10, 100)


# -------------------------------------------------------------- bar_from_mapping

def test_bar_from_mapping_canonical_headers():
    row = {"timestamp": "2024-01-01", "open": 10, "high": 11,
           "low": 9, "close": 10.5, "volume": 100}
    bar = norm.bar_from_mapping(row, NVDA)
    assert bar.close == Decimal("10.5")


def test_bar_from_mapping_aliased_and_mixed_case_headers():
    row = {"Date": "2024-01-01", "O": 10, "H": 11, "L": 9, "C": 10.5, "Vol": 100}
    bar = norm.bar_from_mapping(row, NVDA)
    assert bar.open == Decimal("10")
    assert bar.volume == Decimal("100")


def test_bar_from_mapping_missing_field_names_it():
    row = {"timestamp": "2024-01-01", "open": 10, "high": 11, "low": 9, "close": 10.5}
    with pytest.raises(ValueError) as exc:
        norm.bar_from_mapping(row, NVDA)
    assert "volume" in str(exc.value)


def test_bar_from_mapping_missing_timestamp_raises():
    row = {"open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100}
    with pytest.raises(ValueError) as exc:
        norm.bar_from_mapping(row, NVDA)
    assert "timestamp" in str(exc.value)


# ----------------------------------------------------------------- bar_from_obj

class _FakeAlpacaBar:
    """Mimics alpaca.data.models.Bar — attributes, not dict keys."""
    def __init__(self, timestamp, open, high, low, close, volume):
        self.timestamp = timestamp
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


def test_bar_from_obj_attribute_access():
    raw = _FakeAlpacaBar(
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        open=10.0, high=11.0, low=9.0, close=10.5, volume=1000,
    )
    bar = norm.bar_from_obj(raw, NVDA)
    assert bar.close == Decimal("10.5")
    assert bar.timestamp == datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_bar_from_obj_missing_attribute_raises():
    class Partial:
        timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
        open = 10
        high = 11
        low = 9
        close = 10
        # volume missing
    with pytest.raises(ValueError):
        norm.bar_from_obj(Partial(), NVDA)


# -------------------------------------------------------------------- make_tick

def test_make_tick_with_and_without_quote():
    t1 = norm.make_tick(NVDA, "2024-01-01T00:00:00Z", 10.5, 100)
    assert isinstance(t1, Tick)
    assert t1.bid is None and t1.ask is None

    t2 = norm.make_tick(NVDA, "2024-01-01T00:00:00Z", 10.5, 100, bid=10.4, ask=10.6)
    assert t2.bid == Decimal("10.4")
    assert t2.ask == Decimal("10.6")


def test_make_tick_rejects_nonpositive_price():
    with pytest.raises(ValueError):
        norm.make_tick(NVDA, "2024-01-01T00:00:00Z", 0, 100)
