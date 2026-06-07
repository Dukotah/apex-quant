"""Tests for apex.strategy.ind_obv — On-Balance Volume."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_obv import obv


def test_known_sequence_hand_computed():
    # closes:   10, 11, 10, 10, 12
    # volumes:  100, 200, 150, 300, 250
    # bar 0: seed              -> 0
    # bar 1: 11 > 10  +200     -> 200
    # bar 2: 10 < 11  -150     -> 50
    # bar 3: 10 == 10 unchanged-> 50
    # bar 4: 12 > 10  +250     -> 300
    closes = [10, 11, 10, 10, 12]
    volumes = [100, 200, 150, 300, 250]
    assert obv(closes, volumes) == [0.0, 200.0, 50.0, 50.0, 300.0]


def test_explicit_expected_list():
    closes = [10, 11, 10, 10, 12]
    volumes = [100, 200, 150, 300, 250]
    assert obv(closes, volumes) == [0.0, 200.0, 50.0, 50.0, 300.0]


def test_all_rising():
    closes = [1, 2, 3, 4]
    volumes = [10, 20, 30, 40]
    assert obv(closes, volumes) == [0.0, 20.0, 50.0, 90.0]


def test_all_falling():
    closes = [4, 3, 2, 1]
    volumes = [10, 20, 30, 40]
    assert obv(closes, volumes) == [0.0, -20.0, -50.0, -90.0]


def test_all_flat():
    closes = [5, 5, 5, 5]
    volumes = [10, 20, 30, 40]
    assert obv(closes, volumes) == [0.0, 0.0, 0.0, 0.0]


def test_single_bar():
    assert obv([42], [99]) == [0.0]


def test_empty():
    assert obv([], []) == []


def test_same_length_as_input():
    closes = [1, 2, 3, 2, 1, 1, 5]
    volumes = [5, 5, 5, 5, 5, 5, 5]
    result = obv(closes, volumes)
    assert len(result) == len(closes)
    assert all(v is not None for v in result)


def test_decimal_inputs_match_floats():
    closes = [Decimal("10"), Decimal("11"), Decimal("10.5")]
    volumes = [Decimal("100"), Decimal("200"), Decimal("50")]
    # bar1: up +200 -> 200; bar2: down -50 -> 150
    assert obv(closes, volumes) == [0.0, 200.0, 150.0]


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        obv([1, 2, 3], [10, 20])


def test_determinism():
    closes = [3, 1, 4, 1, 5, 9, 2, 6]
    volumes = [7, 1, 8, 2, 8, 1, 8, 2]
    assert obv(closes, volumes) == obv(closes, volumes)
