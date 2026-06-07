"""Tests for apex.data.anchored_vwap — anchored & rolling VWAP over Bar series."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from apex.core.models import AssetClass, Bar, Symbol
from apex.data.anchored_vwap import (
    anchored_vwap,
    anchored_vwap_series,
    close_price,
    rolling_vwap,
    rolling_vwap_series,
    typical_price,
)

SYM = Symbol(ticker="TEST", asset_class=AssetClass.EQUITY)
T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def make_bar(i: int, o, h, lo, c, v) -> Bar:
    return Bar(
        symbol=SYM,
        timestamp=T0 + timedelta(days=i),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(lo)),
        close=Decimal(str(c)),
        volume=Decimal(str(v)),
    )


# --------------------------------------------------------------- price hooks


def test_typical_price_is_hlc_over_3():
    bar = make_bar(0, 10, 12, 9, 11, 100)
    # (12 + 9 + 11) / 3 = 32/3
    assert typical_price(bar) == Decimal("32") / Decimal("3")


def test_close_price_hook():
    bar = make_bar(0, 10, 12, 9, 11, 100)
    assert close_price(bar) == Decimal("11")


# ------------------------------------------------------------- rolling_vwap


def test_rolling_vwap_known_value_close_price():
    # Two bars, close prices 10 & 20, volumes 100 & 300.
    bars = [
        make_bar(0, 10, 10, 10, 10, 100),
        make_bar(1, 20, 20, 20, 20, 300),
    ]
    # (10*100 + 20*300) / (100+300) = 7000/400 = 17.5
    assert rolling_vwap(bars, 2, price_fn=close_price) == Decimal("17.5")


def test_rolling_vwap_uses_only_last_window():
    bars = [
        make_bar(0, 1, 1, 1, 1, 1000),  # excluded by window=2
        make_bar(1, 10, 10, 10, 10, 100),
        make_bar(2, 20, 20, 20, 20, 300),
    ]
    assert rolling_vwap(bars, 2, price_fn=close_price) == Decimal("17.5")


def test_rolling_vwap_insufficient_data_returns_none():
    bars = [make_bar(0, 10, 10, 10, 10, 100)]
    assert rolling_vwap(bars, 2, price_fn=close_price) is None


def test_rolling_vwap_non_positive_window_returns_none():
    bars = [make_bar(0, 10, 10, 10, 10, 100)]
    assert rolling_vwap(bars, 0, price_fn=close_price) is None
    assert rolling_vwap(bars, -1, price_fn=close_price) is None


def test_rolling_vwap_zero_volume_window_returns_none():
    bars = [
        make_bar(0, 10, 10, 10, 10, 0),
        make_bar(1, 20, 20, 20, 20, 0),
    ]
    assert rolling_vwap(bars, 2, price_fn=close_price) is None


def test_rolling_vwap_skips_zero_volume_bars():
    bars = [
        make_bar(0, 10, 10, 10, 10, 0),  # zero vol → ignored
        make_bar(1, 20, 20, 20, 20, 300),
    ]
    # only the second bar contributes → VWAP == 20
    assert rolling_vwap(bars, 2, price_fn=close_price) == Decimal("20")


# ------------------------------------------------------------ anchored_vwap


def test_anchored_vwap_from_start():
    bars = [
        make_bar(0, 10, 10, 10, 10, 100),
        make_bar(1, 20, 20, 20, 20, 300),
    ]
    # all bars: 7000/400 = 17.5
    assert anchored_vwap(bars, 0, price_fn=close_price) == Decimal("17.5")


def test_anchored_vwap_mid_anchor():
    bars = [
        make_bar(0, 1, 1, 1, 1, 9999),  # before anchor → excluded
        make_bar(1, 10, 10, 10, 10, 100),
        make_bar(2, 20, 20, 20, 20, 300),
    ]
    assert anchored_vwap(bars, 1, price_fn=close_price) == Decimal("17.5")


def test_anchored_vwap_negative_anchor():
    bars = [
        make_bar(0, 1, 1, 1, 1, 9999),
        make_bar(1, 10, 10, 10, 10, 100),
        make_bar(2, 20, 20, 20, 20, 300),
    ]
    # anchor_index=-2 → starts at index 1
    assert anchored_vwap(bars, -2, price_fn=close_price) == Decimal("17.5")


def test_anchored_vwap_out_of_range_returns_none():
    bars = [make_bar(0, 10, 10, 10, 10, 100)]
    assert anchored_vwap(bars, 5, price_fn=close_price) is None
    assert anchored_vwap(bars, -5, price_fn=close_price) is None


def test_anchored_vwap_empty_returns_none():
    assert anchored_vwap([], 0) is None


def test_anchored_vwap_default_typical_price():
    # single bar HLC=(12,8,10) -> typical 30/3=10, vol 50 -> vwap 10
    bars = [make_bar(0, 9, 12, 8, 10, 50)]
    assert anchored_vwap(bars, 0) == Decimal("10")


# --------------------------------------------------------- rolling_vwap_series


def test_rolling_vwap_series_alignment_and_values():
    bars = [
        make_bar(0, 10, 10, 10, 10, 100),
        make_bar(1, 20, 20, 20, 20, 300),
        make_bar(2, 30, 30, 30, 30, 100),
    ]
    series = rolling_vwap_series(bars, 2, price_fn=close_price)
    assert len(series) == 3
    assert series[0] is None  # not enough history
    assert series[1] == Decimal("17.5")  # (10*100+20*300)/400
    # (20*300 + 30*100)/(300+100) = 9000/400 = 22.5
    assert series[2] == Decimal("22.5")


def test_rolling_vwap_series_non_positive_window_all_none():
    bars = [make_bar(0, 10, 10, 10, 10, 100), make_bar(1, 20, 20, 20, 20, 100)]
    assert rolling_vwap_series(bars, 0, price_fn=close_price) == [None, None]


def test_rolling_vwap_series_empty():
    assert rolling_vwap_series([], 3) == []


# -------------------------------------------------------- anchored_vwap_series


def test_anchored_vwap_series_running_cumulative():
    bars = [
        make_bar(0, 10, 10, 10, 10, 100),
        make_bar(1, 20, 20, 20, 20, 300),
        make_bar(2, 30, 30, 30, 30, 100),
    ]
    series = anchored_vwap_series(bars, 0, price_fn=close_price)
    assert len(series) == 3
    assert series[0] == Decimal("10")  # 1000/100
    assert series[1] == Decimal("17.5")  # 7000/400
    # (7000 + 30*100)/(400+100) = 10000/500 = 20
    assert series[2] == Decimal("20")


def test_anchored_vwap_series_before_anchor_is_none():
    bars = [
        make_bar(0, 1, 1, 1, 1, 100),
        make_bar(1, 10, 10, 10, 10, 100),
        make_bar(2, 20, 20, 20, 20, 300),
    ]
    series = anchored_vwap_series(bars, 1, price_fn=close_price)
    assert series[0] is None
    assert series[1] == Decimal("10")
    assert series[2] == Decimal("17.5")


def test_anchored_vwap_series_matches_scalar():
    bars = [
        make_bar(0, 10, 12, 8, 11, 120),
        make_bar(1, 11, 14, 10, 13, 80),
        make_bar(2, 13, 15, 12, 14, 200),
    ]
    series = anchored_vwap_series(bars, 0)
    # final running value must equal the scalar anchored_vwap over all bars
    assert series[-1] == anchored_vwap(bars, 0)


def test_anchored_vwap_series_out_of_range_all_none():
    bars = [make_bar(0, 10, 10, 10, 10, 100)]
    assert anchored_vwap_series(bars, 5) == [None]


def test_anchored_vwap_series_empty():
    assert anchored_vwap_series([], 0) == []


def test_anchored_vwap_series_zero_volume_leading_none():
    bars = [
        make_bar(0, 10, 10, 10, 10, 0),  # zero vol → still None
        make_bar(1, 20, 20, 20, 20, 100),
    ]
    series = anchored_vwap_series(bars, 0, price_fn=close_price)
    assert series[0] is None  # no volume accumulated yet
    assert series[1] == Decimal("20")


def test_results_are_decimal():
    bars = [make_bar(0, 10, 10, 10, 10, 100), make_bar(1, 20, 20, 20, 20, 300)]
    assert isinstance(anchored_vwap(bars, 0), Decimal)
    assert isinstance(rolling_vwap(bars, 2), Decimal)
