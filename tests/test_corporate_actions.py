"""Tests for apex.data.corporate_actions — back-adjustment for splits & dividends."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from apex.core.models import AssetClass, Bar, Symbol
from apex.data.corporate_actions import (
    CorporateAction,
    CorporateActionType,
    adjust_bars,
)

SYM = Symbol(ticker="AAPL", asset_class=AssetClass.EQUITY)


def _dt(day: int) -> datetime:
    return datetime(2026, 1, day, tzinfo=timezone.utc)


def _bar(day: int, close: str, *, volume: str = "100") -> Bar:
    """A flat OHLC bar (open=high=low=close) for clean factor arithmetic."""
    c = Decimal(close)
    return Bar(
        symbol=SYM,
        timestamp=_dt(day),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=Decimal(volume),
        timeframe="1Day",
    )


# ------------------------------------------------------------------ edge cases

def test_empty_bars_returns_empty():
    assert adjust_bars([], [CorporateAction.split(_dt(2), 2)]) == []


def test_no_actions_returns_copy_unchanged():
    bars = [_bar(1, "10"), _bar(2, "11")]
    out = adjust_bars(bars, [])
    assert out == bars
    assert out is not bars  # new list, inputs untouched


def test_action_outside_window_is_noop():
    bars = [_bar(5, "10"), _bar(6, "11")]
    # ex-date before the whole series → nothing is "before" it
    out = adjust_bars(bars, [CorporateAction.split(_dt(1), 2)])
    assert out == bars


# ----------------------------------------------------------------------- splits

def test_split_4_for_1_halves_prior_prices_and_quadruples_volume():
    bars = [_bar(1, "400", volume="100"), _bar(2, "100", volume="50")]
    # ex-date day 2: day-1 bar (before) divided by 4, volume *4; day-2 untouched.
    out = adjust_bars(bars, [CorporateAction.split(_dt(2), 4)])
    assert out[0].close == Decimal("100")
    assert out[0].open == Decimal("100")
    assert out[0].high == Decimal("100")
    assert out[0].low == Decimal("100")
    assert out[0].volume == Decimal("400")
    # on/after ex-date is real-world, untouched
    assert out[1].close == Decimal("100")
    assert out[1].volume == Decimal("50")


def test_reverse_split_1_for_10_multiplies_prior_prices():
    bars = [_bar(1, "5"), _bar(2, "50")]
    # 1-for-10 reverse split → ratio 0.1; prior price /0.1 = *10
    out = adjust_bars(bars, [CorporateAction.split(_dt(2), 1, 10)])
    assert out[0].close == Decimal("50")
    assert out[0].volume == Decimal("10")  # 100 * 0.1
    assert out[1].close == Decimal("50")


def test_split_factory_new_for_old():
    a = CorporateAction.split("2026-01-02", 3, 2)  # 3-for-2
    assert a.action_type is CorporateActionType.SPLIT
    assert a.value == Decimal("1.5")
    assert a.effective_date == _dt(2)


# -------------------------------------------------------------------- dividends

def test_dividend_proportional_adjustment():
    # close_before = 100, dividend = 2 → factor = 1 - 2/100 = 0.98
    bars = [_bar(1, "100", volume="100"), _bar(2, "98")]
    out = adjust_bars(bars, [CorporateAction.dividend(_dt(2), "2")])
    assert out[0].close == Decimal("98.00")
    assert out[0].volume == Decimal("100")  # dividends never touch volume
    assert out[1].close == Decimal("98")    # ex-date bar untouched


def test_dividend_predating_series_is_noop():
    # no bar strictly before the ex-date → close_before is None → unchanged
    bars = [_bar(2, "100"), _bar(3, "101")]
    out = adjust_bars(bars, [CorporateAction.dividend(_dt(2), "5")])
    assert out == bars


def test_dividend_larger_than_close_fails_closed():
    bars = [_bar(1, "3"), _bar(2, "1")]
    out = adjust_bars(bars, [CorporateAction.dividend(_dt(2), "5")])
    assert out == bars  # would produce non-positive price → left unchanged


# ------------------------------------------------------------------ compounding

def test_split_then_dividend_compound():
    # day1=400, day2 (ex-split 2-for-1)=200, day3 (ex-div 1.00 off 200 close)=199
    bars = [_bar(1, "400"), _bar(2, "200"), _bar(3, "199")]
    actions = [
        CorporateAction.split(_dt(2), 2),
        CorporateAction.dividend(_dt(3), "1"),
    ]
    out = adjust_bars(bars, actions)
    # day1: split halves → 200, then dividend factor 1 - 1/200 = 0.995 → 199
    assert out[0].close == Decimal("199.000")
    # day2: only the dividend applies (split ex-date is not "before" day2)
    #       factor 0.995 → 200 * 0.995 = 199
    assert out[1].close == Decimal("199.000")
    # day3: ex-div bar, untouched
    assert out[2].close == Decimal("199")


def test_actions_applied_in_date_order_regardless_of_input_order():
    bars = [_bar(1, "400"), _bar(2, "200"), _bar(3, "199")]
    actions = [
        CorporateAction.dividend(_dt(3), "1"),  # later ex-date, given first
        CorporateAction.split(_dt(2), 2),
    ]
    out = adjust_bars(bars, actions)
    assert out[0].close == Decimal("199.000")
    assert out[1].close == Decimal("199.000")


def test_inputs_not_mutated():
    bars = [_bar(1, "400"), _bar(2, "100")]
    original_close = bars[0].close
    adjust_bars(bars, [CorporateAction.split(_dt(2), 4)])
    assert bars[0].close == original_close  # frozen + new-list contract


# ----------------------------------------------------------------- validation

def test_split_zero_ratio_rejected():
    with pytest.raises(ValueError):
        CorporateAction.split(_dt(2), 0)


def test_negative_dividend_rejected():
    with pytest.raises(ValueError):
        CorporateAction.dividend(_dt(2), "-1")


def test_naive_effective_date_rejected():
    with pytest.raises(ValueError):
        CorporateAction(
            CorporateActionType.SPLIT,
            datetime(2026, 1, 2),  # naive
            Decimal("2"),
        )


def test_garbage_dividend_amount_rejected():
    with pytest.raises(ValueError):
        CorporateAction.dividend(_dt(2), "not-a-number")


def test_str_parsing_avoids_float_artifact():
    # 0.1 passed as float would be 0.1000000000000000055...; via str() it's exact.
    a = CorporateAction.dividend(_dt(2), 0.1)
    assert a.value == Decimal("0.1")
