"""
Tests for apex.strategy.indicators.
Indicator math verified against hand-computed / reference values. A wrong
indicator silently corrupts every strategy that uses it, so these are strict.
"""
from __future__ import annotations

from apex.strategy import indicators as ind


def test_sma_basic():
    data = [1, 2, 3, 4, 5]
    out = ind.sma(data, 3)
    assert out[0] is None and out[1] is None
    assert out[2] == 2.0    # (1+2+3)/3
    assert out[3] == 3.0    # (2+3+4)/3
    assert out[4] == 4.0    # (3+4+5)/3


def test_sma_insufficient_data():
    assert all(x is None for x in ind.sma([1, 2], 5))


def test_ema_seeds_with_sma():
    data = [1, 2, 3, 4, 5, 6, 7, 8]
    out = ind.ema(data, 3)
    # First EMA value = SMA of first 3 = 2.0
    assert out[2] == 2.0
    # Next = (4 - 2)*0.5 + 2 = 3.0  (alpha = 2/(3+1) = 0.5)
    assert out[3] == 3.0


def test_rsi_all_gains_is_100():
    # Monotonic increase → no losses → RSI 100.
    data = list(range(1, 20))
    out = ind.rsi(data, 14)
    assert out[14] == 100.0


def test_rsi_all_losses_near_zero():
    data = list(range(20, 1, -1))  # monotonic decrease
    out = ind.rsi(data, 14)
    assert out[14] == 0.0


def test_rsi_range_bounded():
    data = [44, 44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
            45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.0, 46.03]
    out = ind.rsi(data, 14)
    for v in out:
        if v is not None:
            assert 0.0 <= v <= 100.0


def test_rsi_insufficient():
    assert all(x is None for x in ind.rsi([1, 2, 3], 14))


def test_macd_shapes():
    data = [float(i) for i in range(60)]
    macd_line, signal, hist = ind.macd(data)
    assert len(macd_line) == len(signal) == len(hist) == 60
    # Steady uptrend → macd line should be positive once warmed up.
    last = [v for v in macd_line if v is not None][-1]
    assert last > 0


def test_bollinger_bands_ordering():
    data = [float(i % 10) for i in range(50)]
    upper, middle, lower = ind.bollinger_bands(data, 20, 2.0)
    for u, m, low in zip(upper, middle, lower):
        if None not in (u, m, low):
            assert low <= m <= u


def test_atr_positive():
    n = 30
    high = [10 + i * 0.5 for i in range(n)]
    low = [9 + i * 0.5 for i in range(n)]
    close = [9.5 + i * 0.5 for i in range(n)]
    out = ind.atr(high, low, close, 14)
    valid = [v for v in out if v is not None]
    assert valid and all(v > 0 for v in valid)


def test_atr_length_mismatch_raises():
    try:
        ind.atr([1, 2, 3], [1, 2], [1, 2, 3], 2)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_rolling_return():
    data = [100, 110, 121]
    out = ind.rolling_return(data, 1)
    assert out[0] is None
    assert abs(out[1] - 0.10) < 1e-9
    assert abs(out[2] - 0.10) < 1e-9


def test_crosses_above():
    fast = [1, 2, 3, 4, 5]
    slow = [3, 3, 3, 3, 3]
    crosses = ind.crosses_above(fast, slow)
    # fast goes 3->4 above slow 3 at index 3
    assert crosses[3] is True
    assert crosses[0] is False


def test_crosses_below():
    fast = [5, 4, 3, 2, 1]
    slow = [3, 3, 3, 3, 3]
    crosses = ind.crosses_below(fast, slow)
    # fast goes 3->2 below slow at index 3
    assert crosses[3] is True


def test_crosses_ignore_none():
    a = [None, None, 1.0, 2.0]
    b = [0.5, 0.5, 0.5, 0.5]
    # No crash on None; first valid comparison only.
    result = ind.crosses_above(a, b)
    assert len(result) == 4
