"""Tests for apex.strategy.ind_donchian_channels — hand-computed values + edges."""
from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_donchian_channels import (
    donchian_channels,
    donchian_lower,
    donchian_middle,
    donchian_upper,
)


def test_known_values_period_3():
    # highs/lows hand-picked so the rolling windows are easy to verify.
    high = [10, 12, 11, 15, 14, 9]
    low = [5, 6, 4, 8, 7, 3]
    upper, middle, lower = donchian_channels(high, low, period=3)

    # warmup: first period-1 = 2 positions are None
    assert upper[0] is None and upper[1] is None
    assert middle[0] is None and middle[1] is None
    assert lower[0] is None and lower[1] is None

    # i=2: window highs [10,12,11] -> 12 ; lows [5,6,4] -> 4
    assert upper[2] == 12.0
    assert lower[2] == 4.0
    assert middle[2] == (12.0 + 4.0) / 2.0
    # i=3: highs [12,11,15] -> 15 ; lows [6,4,8] -> 4
    assert upper[3] == 15.0
    assert lower[3] == 4.0
    assert middle[3] == 9.5
    # i=4: highs [11,15,14] -> 15 ; lows [4,8,7] -> 4
    assert upper[4] == 15.0
    assert lower[4] == 4.0
    # i=5: highs [15,14,9] -> 15 ; lows [8,7,3] -> 3
    assert upper[5] == 15.0
    assert lower[5] == 3.0
    assert middle[5] == 9.0


def test_length_preserved():
    high = list(range(50))
    low = list(range(50))
    upper, middle, lower = donchian_channels(high, low, period=20)
    assert len(upper) == len(middle) == len(lower) == 50


def test_insufficient_data_all_none():
    high = [1, 2, 3]
    low = [0, 1, 2]
    upper, middle, lower = donchian_channels(high, low, period=5)
    assert upper == [None, None, None]
    assert middle == [None, None, None]
    assert lower == [None, None, None]


def test_empty_input():
    assert donchian_channels([], [], period=20) == ([], [], [])


def test_exact_window_length():
    high = [4, 7, 2, 9]
    low = [1, 3, 0, 5]
    upper, middle, lower = donchian_channels(high, low, period=4)
    assert upper == [None, None, None, 9.0]
    assert lower == [None, None, None, 0.0]
    assert middle == [None, None, None, 4.5]


def test_decimal_inputs_supported():
    high = [Decimal("10.5"), Decimal("11.0"), Decimal("9.0")]
    low = [Decimal("8.0"), Decimal("7.5"), Decimal("6.0")]
    upper, middle, lower = donchian_channels(high, low, period=2)
    assert upper[1] == 11.0
    assert lower[1] == 7.5
    assert upper[2] == 11.0
    assert lower[2] == 6.0


def test_helper_functions_match_combined():
    high = [10, 12, 11, 15, 14, 9]
    low = [5, 6, 4, 8, 7, 3]
    upper, middle, lower = donchian_channels(high, low, period=3)
    assert donchian_upper(high, period=3) == upper
    assert donchian_lower(low, period=3) == lower
    assert donchian_middle(high, low, period=3) == middle


def test_period_must_be_positive():
    for fn_args in (
        lambda: donchian_channels([1, 2], [1, 2], 0),
        lambda: donchian_upper([1, 2], 0),
        lambda: donchian_lower([1, 2], -1),
        lambda: donchian_middle([1, 2], [1, 2], 0),
    ):
        with pytest.raises(ValueError):
            fn_args()


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        donchian_channels([1, 2, 3], [1, 2], period=2)


def test_period_one_tracks_each_bar():
    high = [3, 7, 2]
    low = [1, 5, 0]
    upper, middle, lower = donchian_channels(high, low, period=1)
    assert upper == [3.0, 7.0, 2.0]
    assert lower == [1.0, 5.0, 0.0]
    assert middle == [2.0, 6.0, 1.0]
