"""Tests for apex.data.returns_builder — simple/log return construction."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from apex.core.models import AssetClass, Bar, Symbol
from apex.data.returns_builder import (
    log_returns,
    returns_from_bars,
    simple_returns,
    to_closes,
)

SYM = Symbol(ticker="TEST", asset_class=AssetClass.EQUITY)


def _bar(close: str, day: int = 1) -> Bar:
    """Build a flat OHLCV Bar with the given close on a distinct UTC day."""
    c = Decimal(close)
    return Bar(
        symbol=SYM,
        timestamp=datetime(2026, 1, day, tzinfo=timezone.utc),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=Decimal("100"),
        timeframe="1Day",
    )


# --------------------------------------------------------------- simple returns

def test_simple_returns_hand_computed():
    # closes: 100 -> 110 -> 99
    # r1 = 110/100 - 1 = 0.10 ; r2 = 99/110 - 1 = -0.10
    out = simple_returns([100.0, 110.0, 99.0])
    assert out == pytest.approx([0.10, -0.10])


def test_simple_returns_length_is_n_minus_one():
    out = simple_returns([10.0, 20.0, 30.0, 40.0])
    assert len(out) == 3


def test_simple_returns_flat_series_is_zeros():
    assert simple_returns([50.0, 50.0, 50.0]) == pytest.approx([0.0, 0.0])


# ------------------------------------------------------------------ log returns

def test_log_returns_hand_computed():
    # ln(110/100) and ln(99/110)
    out = log_returns([100.0, 110.0, 99.0])
    assert out == pytest.approx([math.log(1.1), math.log(99 / 110)])


def test_log_returns_additive_property():
    # Sum of log returns == log of total growth factor (200/100).
    closes = [100.0, 125.0, 160.0, 200.0]
    assert sum(log_returns(closes)) == pytest.approx(math.log(200.0 / 100.0))


def test_log_and_simple_agree_for_tiny_moves():
    # For small returns, log ~ simple. Use a single 0.1% step.
    s = simple_returns([1000.0, 1001.0])
    lo = log_returns([1000.0, 1001.0])
    assert s[0] == pytest.approx(0.001)
    assert lo[0] == pytest.approx(s[0], abs=1e-6)


# ----------------------------------------------------------- insufficient data

@pytest.mark.parametrize("series", [[], [42.0]])
def test_empty_for_fewer_than_two_prices(series):
    assert simple_returns(series) == []
    assert log_returns(series) == []


# ---------------------------------------------------------------- input shapes

def test_accepts_decimal_prices_exactly():
    out = simple_returns([Decimal("100"), Decimal("110")])
    assert out == pytest.approx([0.10])


def test_accepts_numeric_strings():
    out = simple_returns(["100", "110"])
    assert out == pytest.approx([0.10])


def test_to_closes_coerces_to_float():
    closes = to_closes([Decimal("1.5"), 2, "3.0", 4.0])
    assert closes == [1.5, 2.0, 3.0, 4.0]
    assert all(isinstance(c, float) for c in closes)


# ------------------------------------------------------------------ from bars

def test_returns_from_bars_simple():
    bars = [_bar("100", 1), _bar("110", 2), _bar("99", 3)]
    out = returns_from_bars(bars)
    assert out == pytest.approx([0.10, -0.10])


def test_returns_from_bars_log():
    bars = [_bar("100", 1), _bar("110", 2)]
    out = returns_from_bars(bars, log=True)
    assert out == pytest.approx([math.log(1.1)])


def test_bars_used_in_given_order_not_sorted():
    # Reverse-chronological input is honored as-is (caller controls order).
    bars = [_bar("110", 2), _bar("100", 1)]
    out = simple_returns(bars)
    # 100/110 - 1 = -0.0909...
    assert out == pytest.approx([100 / 110 - 1.0])


# ------------------------------------------------------------- failure modes

def test_zero_price_rejected():
    with pytest.raises(ValueError):
        simple_returns([100.0, 0.0])


def test_negative_price_rejected():
    with pytest.raises(ValueError):
        log_returns([100.0, -5.0])


def test_bool_rejected():
    with pytest.raises(ValueError):
        to_closes([True])


def test_garbage_string_rejected():
    with pytest.raises(ValueError):
        to_closes(["not-a-number"])


def test_error_names_offending_index():
    with pytest.raises(ValueError, match="index 1"):
        to_closes([100.0, "bad"])


def test_nan_rejected():
    with pytest.raises(ValueError):
        to_closes([float("nan")])
