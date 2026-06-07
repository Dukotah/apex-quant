"""
Tests for apex.data.spread_estimator (Corwin-Schultz spread estimation).

Imported by full path so no package __init__ edits are needed. Values are
hand-/reference-computed from the Corwin-Schultz (2012) formulas; edge cases
exercise the degenerate-pair and insufficient-data handling.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from apex.core.models import AssetClass, Bar, Symbol
from apex.data.spread_estimator import (
    alpha_to_spread,
    corwin_schultz_spreads,
    mean_spread,
    median_spread,
    spreads_from_bars,
)

# ----------------------------------------------------------------- helpers

def _ref_alpha(h0: float, l0: float, h1: float, l1: float) -> float:
    """Independent re-implementation of the CS alpha, for cross-checking."""
    r0 = math.log(h0 / l0)
    r1 = math.log(h1 / l1)
    beta = r0 * r0 + r1 * r1
    gamma = math.log(max(h0, h1) / min(l0, l1)) ** 2
    k = 3.0 - 2.0 * math.sqrt(2.0)
    return (math.sqrt(2 * beta) - math.sqrt(beta)) / k - math.sqrt(gamma / k)


def _ref_spread(h0: float, l0: float, h1: float, l1: float) -> float:
    a = _ref_alpha(h0, l0, h1, l1)
    s = 2.0 * (math.exp(a) - 1.0) / (1.0 + math.exp(a))
    return max(s, 0.0)


# ----------------------------------------------------------------- alpha_to_spread

def test_alpha_zero_gives_zero_spread():
    assert alpha_to_spread(0.0) == 0.0


def test_alpha_positive_known_value():
    # 2*(e^0.5 - 1)/(1 + e^0.5)
    assert alpha_to_spread(0.5) == pytest.approx(0.48983732480741826)


def test_negative_alpha_floored_to_zero():
    # Negative alpha -> negative raw spread -> floored to 0.0.
    assert alpha_to_spread(-1.0) == 0.0


# ------------------------------------------------- corwin_schultz_spreads core

def test_identical_wide_bars_known_spread():
    # Two identical bars (H=110, L=90). Reference: alpha ~= 0.20067, spread = 0.2.
    spreads = corwin_schultz_spreads([110.0, 110.0], [90.0, 90.0])
    assert spreads[0] is None                      # no preceding bar
    assert spreads[1] == pytest.approx(0.2, abs=1e-9)


def test_matches_independent_reference_three_bars():
    highs = [110.0, 108.0, 111.0]
    lows = [90.0, 95.0, 92.0]
    spreads = corwin_schultz_spreads(highs, lows)
    assert spreads[0] is None
    assert spreads[1] == pytest.approx(_ref_spread(110, 90, 108, 95))
    assert spreads[2] == pytest.approx(_ref_spread(108, 95, 111, 92))


def test_trending_market_floors_negative_to_zero():
    # A strong overnight gap inflates the 2-day range -> negative alpha -> 0.
    spreads = corwin_schultz_spreads([100.0, 200.0], [90.0, 180.0])
    assert spreads[1] == 0.0


def test_output_same_length_as_input():
    highs = [10.0, 11.0, 12.0, 13.0]
    lows = [9.0, 10.0, 11.0, 12.0]
    assert len(corwin_schultz_spreads(highs, lows)) == len(highs)


# ------------------------------------------------- edge / degenerate cases

def test_single_bar_yields_all_none():
    assert corwin_schultz_spreads([100.0], [90.0]) == [None]


def test_empty_input_yields_empty_list():
    assert corwin_schultz_spreads([], []) == []


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        corwin_schultz_spreads([1.0, 2.0], [1.0])


def test_non_positive_price_pair_is_none():
    # A zero low in one bar makes that pair's log undefined -> None, not garbage.
    spreads = corwin_schultz_spreads([100.0, 100.0, 100.0], [0.0, 90.0, 95.0])
    assert spreads[1] is None      # pair (bar0 with low=0, bar1)
    assert spreads[2] is not None  # pair (bar1, bar2) is fine


def test_high_below_low_pair_is_none():
    # Degenerate bar (high < low) -> that pair has no usable estimate.
    spreads = corwin_schultz_spreads([100.0, 80.0, 100.0], [90.0, 95.0, 92.0])
    assert spreads[1] is None      # bar1 has high 80 < low 95
    assert spreads[2] is None      # bar1 again on the left of this pair


def test_zero_range_bars_give_zero_spread():
    # H == L on both bars -> beta=gamma=0 -> alpha=0 -> spread 0 (not None).
    spreads = corwin_schultz_spreads([100.0, 100.0], [100.0, 100.0])
    assert spreads[1] == 0.0


# ------------------------------------------------- mean / median collapse

def test_mean_and_median_none_when_no_usable_pairs():
    assert mean_spread([100.0], [90.0]) is None
    assert median_spread([100.0], [90.0]) is None
    assert mean_spread([], []) is None


def test_mean_spread_matches_manual_average():
    highs = [110.0, 108.0, 111.0]
    lows = [90.0, 95.0, 92.0]
    s1 = _ref_spread(110, 90, 108, 95)
    s2 = _ref_spread(108, 95, 111, 92)
    assert mean_spread(highs, lows) == pytest.approx((s1 + s2) / 2.0)


def test_median_ignores_degenerate_pairs():
    # Middle bar degenerate -> only one usable pair survives; median == it.
    highs = [110.0, 80.0, 111.0, 109.0]
    lows = [90.0, 95.0, 92.0, 94.0]
    spreads = corwin_schultz_spreads(highs, lows)
    usable = [s for s in spreads if s is not None]
    assert len(usable) == 1
    assert median_spread(highs, lows) == pytest.approx(usable[0])


# ------------------------------------------------- Decimal inputs / Bar adapter

def test_decimal_inputs_match_float_inputs():
    f = corwin_schultz_spreads([110.0, 108.0], [90.0, 95.0])
    d = corwin_schultz_spreads([Decimal("110"), Decimal("108")],
                               [Decimal("90"), Decimal("95")])
    assert d[1] == pytest.approx(f[1])


def _bar(high: str, low: str) -> Bar:
    sym = Symbol(ticker="TST", asset_class=AssetClass.EQUITY)
    # open/close kept inside [low, high] so the frozen Bar validates.
    return Bar(
        symbol=sym,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        open=Decimal(low),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(high),
        volume=Decimal("1000"),
    )


def test_spreads_from_bars_matches_raw_arrays():
    bars = [_bar("110", "90"), _bar("108", "95"), _bar("111", "92")]
    from_bars = spreads_from_bars(bars)
    raw = corwin_schultz_spreads([110.0, 108.0, 111.0], [90.0, 95.0, 92.0])
    assert from_bars[0] is None
    assert from_bars[1] == pytest.approx(raw[1])
    assert from_bars[2] == pytest.approx(raw[2])
