"""
Tests for apex.data.rolling_zscore.

Hand-computed known values plus edge cases (insufficient data, flat windows,
population vs sample std, bad arguments).
"""
from __future__ import annotations

import math

import pytest

from apex.data.rolling_zscore import latest_zscore, rolling_zscore


def test_warmup_positions_are_none() -> None:
    # First window-1 positions can't have a full window → None.
    result = rolling_zscore([1.0, 2.0, 3.0, 4.0], window=3)
    assert result[0] is None
    assert result[1] is None
    assert result[2] is not None
    assert result[3] is not None
    assert len(result) == 4


def test_known_population_zscore() -> None:
    # Window [2, 4, 6]: mean = 4, population variance = ((4)+(0)+(4))/3 = 8/3,
    # pstdev = sqrt(8/3). Latest value 6 → z = (6 - 4) / sqrt(8/3).
    result = rolling_zscore([2.0, 4.0, 6.0], window=3)
    expected = (6.0 - 4.0) / math.sqrt(8.0 / 3.0)
    assert result[0] is None
    assert result[1] is None
    assert result[2] == pytest.approx(expected)


def test_known_sample_zscore() -> None:
    # Same window [2, 4, 6] but sample std: variance = 8/2 = 4, stdev = 2.
    # z = (6 - 4) / 2 = 1.0.
    result = rolling_zscore([2.0, 4.0, 6.0], window=3, ddof=1)
    assert result[2] == pytest.approx(1.0)


def test_rolling_advances_window() -> None:
    # series 1..5, window 3 (sample std for clean numbers).
    # window [3,4,5]: mean 4, stdev 1 → z = (5-4)/1 = 1.
    result = rolling_zscore([1.0, 2.0, 3.0, 4.0, 5.0], window=3, ddof=1)
    assert result[4] == pytest.approx(1.0)
    # window [2,3,4]: mean 3, stdev 1 → z = (4-3)/1 = 1.
    assert result[3] == pytest.approx(1.0)


def test_flat_window_returns_none() -> None:
    # Constant window → zero std → undefined z → None (not inf/nan).
    result = rolling_zscore([5.0, 5.0, 5.0, 5.0], window=3)
    assert result[2] is None
    assert result[3] is None


def test_empty_series() -> None:
    assert rolling_zscore([], window=3) == []


def test_series_shorter_than_window_all_none() -> None:
    result = rolling_zscore([1.0, 2.0], window=5)
    assert result == [None, None]


def test_window_one_population_always_none() -> None:
    # A single-element population window has zero std → always None.
    result = rolling_zscore([1.0, 2.0, 3.0], window=1)
    assert result == [None, None, None]


def test_negative_zscore() -> None:
    # window [10, 6, 2]: mean 6, stdev (sample) = sqrt(((16)+(0)+(16))/2)=4.
    # latest 2 → z = (2 - 6)/4 = -1.0.
    result = rolling_zscore([10.0, 6.0, 2.0], window=3, ddof=1)
    assert result[2] == pytest.approx(-1.0)


def test_accepts_ints() -> None:
    # Integers should be coerced to float and behave identically.
    result = rolling_zscore([2, 4, 6], window=3, ddof=1)
    assert result[2] == pytest.approx(1.0)


# ----------------------------------------------------------------- latest_zscore

def test_latest_matches_last_rolling() -> None:
    series = [1.0, 2.0, 3.0, 4.0, 5.0]
    full = rolling_zscore(series, window=3, ddof=1)
    assert latest_zscore(series, window=3, ddof=1) == pytest.approx(full[-1])


def test_latest_insufficient_data_is_none() -> None:
    assert latest_zscore([1.0, 2.0], window=3) is None


def test_latest_flat_is_none() -> None:
    assert latest_zscore([7.0, 7.0, 7.0], window=3) is None


# ----------------------------------------------------------------- bad arguments

@pytest.mark.parametrize("bad", [0, -1, 2.5, "3", True])
def test_invalid_window_raises(bad: object) -> None:
    with pytest.raises(ValueError):
        rolling_zscore([1.0, 2.0, 3.0], window=bad)  # type: ignore[arg-type]


def test_invalid_ddof_raises() -> None:
    with pytest.raises(ValueError):
        rolling_zscore([1.0, 2.0, 3.0], window=3, ddof=2)


def test_sample_std_window_too_small_raises() -> None:
    # window=1 with sample std needs 2 points → reject.
    with pytest.raises(ValueError):
        rolling_zscore([1.0, 2.0], window=1, ddof=1)


def test_latest_invalid_window_raises() -> None:
    with pytest.raises(ValueError):
        latest_zscore([1.0, 2.0], window=0)
