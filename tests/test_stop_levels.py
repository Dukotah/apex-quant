"""
Tests for apex.risk.stop_levels.

Hand-computed known values plus edge cases (insufficient/garbage data must
fail closed to None, never produce a wrong level).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.core.models import OrderSide
from apex.risk.stop_levels import atr_stop, chandelier_stop, percent_stop

# --------------------------------------------------------------------------
# percent_stop
# --------------------------------------------------------------------------


def test_percent_stop_long_known_value():
    # 100 with a 5% stop -> 95.
    assert percent_stop(Decimal("100"), Decimal("0.05"), OrderSide.BUY) == Decimal("95.00")


def test_percent_stop_short_known_value():
    # 100 with a 5% stop on a short -> 105 (above entry).
    assert percent_stop(Decimal("100"), Decimal("0.05"), OrderSide.SELL) == Decimal("105.00")


def test_percent_stop_default_side_is_long():
    assert percent_stop(Decimal("200"), Decimal("0.10")) == Decimal("180.0")


def test_percent_stop_accepts_float_pct_via_str():
    # Float 0.1 routed through str -> exact Decimal('0.1'); 50 * 0.9 = 45.0.
    assert percent_stop(Decimal("50"), 0.1, OrderSide.BUY) == Decimal("45.0")


def test_percent_stop_rejects_non_positive_entry():
    assert percent_stop(Decimal("0"), Decimal("0.05")) is None
    assert percent_stop(Decimal("-10"), Decimal("0.05")) is None


def test_percent_stop_rejects_non_positive_pct():
    assert percent_stop(Decimal("100"), Decimal("0")) is None
    assert percent_stop(Decimal("100"), Decimal("-0.05")) is None


def test_percent_stop_long_rejects_pct_at_or_above_one():
    # A >=100% stop on a long would land at/below zero — meaningless.
    assert percent_stop(Decimal("100"), Decimal("1")) is None
    assert percent_stop(Decimal("100"), Decimal("1.5")) is None


def test_percent_stop_short_allows_large_pct():
    # On a short the stop is above entry, so a large pct is fine.
    assert percent_stop(Decimal("100"), Decimal("1.5"), OrderSide.SELL) == Decimal("250.0")


def test_percent_stop_none_inputs():
    assert percent_stop(None, Decimal("0.05")) is None
    assert percent_stop(Decimal("100"), None) is None


# --------------------------------------------------------------------------
# atr_stop
# --------------------------------------------------------------------------


def test_atr_stop_long_known_value():
    # entry 100, ATR 2, multiplier 3 -> 100 - 6 = 94.
    assert atr_stop(Decimal("100"), Decimal("2"), Decimal("3"), OrderSide.BUY) == Decimal("94")


def test_atr_stop_short_known_value():
    # entry 100, ATR 2, multiplier 3 -> 100 + 6 = 106.
    assert atr_stop(Decimal("100"), Decimal("2"), Decimal("3"), OrderSide.SELL) == Decimal("106")


def test_atr_stop_default_multiplier_is_three():
    assert atr_stop(Decimal("50"), Decimal("1")) == Decimal("47")


def test_atr_stop_accepts_floats():
    # entry 100.0, ATR 1.5, mult 2 -> 100 - 3 = 97.0
    assert atr_stop(100.0, 1.5, 2.0, OrderSide.BUY) == Decimal("97.0")


def test_atr_stop_rejects_non_positive_inputs():
    assert atr_stop(Decimal("0"), Decimal("2"), Decimal("3")) is None
    assert atr_stop(Decimal("100"), Decimal("0"), Decimal("3")) is None
    assert atr_stop(Decimal("100"), Decimal("2"), Decimal("0")) is None
    assert atr_stop(Decimal("100"), Decimal("2"), Decimal("-1")) is None


def test_atr_stop_long_rejects_level_at_or_below_zero():
    # ATR * mult >= entry -> long stop would be <= 0.
    assert atr_stop(Decimal("10"), Decimal("5"), Decimal("3"), OrderSide.BUY) is None


def test_atr_stop_short_allows_large_distance():
    # Short stop is above entry, so a large distance is always valid.
    assert atr_stop(Decimal("10"), Decimal("5"), Decimal("3"), OrderSide.SELL) == Decimal("25")


def test_atr_stop_none_inputs():
    assert atr_stop(None, Decimal("2"), Decimal("3")) is None
    assert atr_stop(Decimal("100"), None, Decimal("3")) is None
    assert atr_stop(Decimal("100"), Decimal("2"), None) is None


# --------------------------------------------------------------------------
# chandelier_stop
# --------------------------------------------------------------------------


def test_chandelier_long_known_value():
    # highest high = 120, ATR 2, mult 3 -> 120 - 6 = 114.
    highs = [Decimal("100"), Decimal("120"), Decimal("110")]
    lows = [Decimal("95"), Decimal("112"), Decimal("105")]
    assert chandelier_stop(highs, lows, Decimal("2"), Decimal("3"), OrderSide.BUY) == Decimal("114")


def test_chandelier_short_known_value():
    # lowest low = 90, ATR 2, mult 3 -> 90 + 6 = 96.
    highs = [Decimal("105"), Decimal("100"), Decimal("98")]
    lows = [Decimal("100"), Decimal("90"), Decimal("94")]
    assert chandelier_stop(highs, lows, Decimal("2"), Decimal("3"), OrderSide.SELL) == Decimal("96")


def test_chandelier_long_trails_up_with_new_highs():
    # Adding a higher high raises the stop (ratchets in the favourable direction).
    base = chandelier_stop(
        [Decimal("110")], [Decimal("100")], Decimal("2"), Decimal("3"), OrderSide.BUY
    )
    higher = chandelier_stop(
        [Decimal("110"), Decimal("130")],
        [Decimal("100"), Decimal("120")],
        Decimal("2"),
        Decimal("3"),
        OrderSide.BUY,
    )
    assert base == Decimal("104")
    assert higher == Decimal("124")
    assert higher > base


def test_chandelier_skips_none_entries():
    highs = [None, Decimal("120"), None, Decimal("115")]
    lows = [Decimal("110"), None, Decimal("100"), None]
    # max high ignoring None = 120 -> 120 - 6 = 114.
    assert chandelier_stop(highs, lows, Decimal("2"), Decimal("3"), OrderSide.BUY) == Decimal("114")


def test_chandelier_accepts_floats():
    assert chandelier_stop([100.0, 105.0], [98.0, 101.0], 1.0, 2.0, OrderSide.BUY) == Decimal(
        "103.0"
    )


def test_chandelier_empty_series_returns_none():
    assert chandelier_stop([], [Decimal("100")], Decimal("2"), Decimal("3"), OrderSide.BUY) is None
    assert chandelier_stop([Decimal("100")], [], Decimal("2"), Decimal("3"), OrderSide.SELL) is None


def test_chandelier_all_none_series_returns_none():
    assert chandelier_stop([None, None], [None], Decimal("2"), Decimal("3"), OrderSide.BUY) is None


def test_chandelier_rejects_non_positive_atr_or_multiplier():
    highs = [Decimal("120")]
    lows = [Decimal("90")]
    assert chandelier_stop(highs, lows, Decimal("0"), Decimal("3"), OrderSide.BUY) is None
    assert chandelier_stop(highs, lows, Decimal("2"), Decimal("0"), OrderSide.BUY) is None
    assert chandelier_stop(highs, lows, None, Decimal("3"), OrderSide.BUY) is None


def test_chandelier_long_rejects_level_at_or_below_zero():
    # highest high 5, distance 6 -> level -1 <= 0.
    assert (
        chandelier_stop([Decimal("5")], [Decimal("4")], Decimal("2"), Decimal("3"), OrderSide.BUY)
        is None
    )


def test_all_stops_return_decimal_type():
    p = percent_stop(Decimal("100"), Decimal("0.05"))
    a = atr_stop(Decimal("100"), Decimal("2"))
    c = chandelier_stop([Decimal("120")], [Decimal("90")], Decimal("2"))
    assert isinstance(p, Decimal)
    assert isinstance(a, Decimal)
    assert isinstance(c, Decimal)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
