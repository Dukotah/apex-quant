"""
apex.strategy.indicators
========================
The technical indicator library. Stateless, pure functions. Every strategy reads
its signals from here rather than recomputing inline — one tested source of truth.

CONTRACT for every function:
  - Input: a sequence of values (floats or Decimals — we work in float internally
    for speed, since indicators are comparative, not accounting; money math stays
    Decimal elsewhere).
  - Output: a list the SAME LENGTH as the input, with None for positions where
    there isn't enough data yet (the "warmup" period). NEVER returns garbage for
    insufficient data — None means "not enough history, don't trade on this."
  - Deterministic: same input → same output, always.

All functions tested in tests/test_indicators.py against hand-computed values.
"""
from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def sma(data: Sequence, period: int) -> list[Optional[float]]:
    """Simple Moving Average. None until `period` values are available."""
    if period <= 0:
        raise ValueError("period must be positive")
    values = _to_floats(data)
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    window_sum = sum(values[:period])
    out[period - 1] = window_sum / period
    for i in range(period, len(values)):
        window_sum += values[i] - values[i - period]
        out[i] = window_sum / period
    return out


def ema(data: Sequence, period: int) -> list[Optional[float]]:
    """
    Exponential Moving Average. Seeded with the SMA of the first `period` values
    (standard convention), then smoothed with alpha = 2/(period+1).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    values = _to_floats(data)
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (values[i] - prev) * alpha + prev
        out[i] = prev
    return out


def rsi(data: Sequence, period: int) -> list[Optional[float]]:
    """
    Relative Strength Index using Wilder's smoothing. Returns 0-100.
    None until `period`+1 values exist (need `period` price changes).

    RSI(2) is the Connors mean-reversion signal; RSI(14) is the classic.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    values = _to_floats(data)
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out

    # Initial average gain/loss over the first `period` changes.
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from_avgs(avg_gain, avg_loss)

    # Wilder's smoothing for the rest.
    for i in range(period + 1, n):
        change = values[i] - values[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from_avgs(avg_gain, avg_loss)
    return out


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(
    data: Sequence,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    """
    MACD. Returns (macd_line, signal_line, histogram), each same length as input.
    macd_line = EMA(fast) - EMA(slow); signal = EMA(macd_line); hist = macd - signal.
    """
    fast = ema(data, fast_period)
    slow = ema(data, slow_period)
    n = len(data)
    macd_line: list[Optional[float]] = [None] * n
    for i in range(n):
        if fast[i] is not None and slow[i] is not None:
            macd_line[i] = fast[i] - slow[i]

    # Signal line = EMA of the macd_line's non-None portion.
    valid = [(i, v) for i, v in enumerate(macd_line) if v is not None]
    signal_line: list[Optional[float]] = [None] * n
    if len(valid) >= signal_period:
        macd_vals = [v for _, v in valid]
        sig = ema(macd_vals, signal_period)
        for (orig_i, _), s in zip(valid, sig):
            signal_line[orig_i] = s

    histogram: list[Optional[float]] = [None] * n
    for i in range(n):
        if macd_line[i] is not None and signal_line[i] is not None:
            histogram[i] = macd_line[i] - signal_line[i]
    return macd_line, signal_line, histogram


def bollinger_bands(
    data: Sequence, period: int = 20, num_std: float = 2.0
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    """
    Bollinger Bands. Returns (upper, middle, lower). Middle is the SMA; the bands
    are num_std population standard deviations away.
    """
    values = _to_floats(data)
    middle = sma(values, period)
    n = len(values)
    upper: list[Optional[float]] = [None] * n
    lower: list[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        window = values[i - period + 1: i + 1]
        mean = middle[i]
        variance = sum((x - mean) ** 2 for x in window) / period
        sd = variance ** 0.5
        upper[i] = mean + num_std * sd
        lower[i] = mean - num_std * sd
    return upper, middle, lower


def atr(
    high: Sequence, low: Sequence, close: Sequence, period: int = 14
) -> list[Optional[float]]:
    """
    Average True Range (Wilder). Measures volatility. Used by the vol-filtered
    RSI(2) strategy and for volatility-scaled position sizing.
    None until `period`+1 bars (true range needs the prior close).
    """
    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high, low, close must be the same length")
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out

    true_ranges: list[float] = [0.0] * n
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges[i] = tr

    # First ATR = simple average of the first `period` true ranges (indices 1..period).
    first_atr = sum(true_ranges[1: period + 1]) / period
    out[period] = first_atr
    prev = first_atr
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + true_ranges[i]) / period
        out[i] = prev
    return out


def rolling_return(data: Sequence, period: int) -> list[Optional[float]]:
    """
    Trailing total return over `period` bars, as a fraction. Used by momentum
    strategies (e.g. 12-month return for dual momentum, 3-month for rotation).
    None until `period`+1 values exist.
    """
    values = _to_floats(data)
    n = len(values)
    out: list[Optional[float]] = [None] * n
    for i in range(period, n):
        past = values[i - period]
        if past != 0:
            out[i] = values[i] / past - 1.0
    return out


def crosses_above(series_a: Sequence[Optional[float]], series_b: Sequence[Optional[float]]) -> list[bool]:
    """
    True at each index where series_a crosses ABOVE series_b (was <=, now >).
    series_b may be a list or a constant-equivalent list. None values → no cross.
    """
    n = min(len(series_a), len(series_b))
    out = [False] * n
    for i in range(1, n):
        a_prev, a_cur = series_a[i - 1], series_a[i]
        b_prev, b_cur = series_b[i - 1], series_b[i]
        if None in (a_prev, a_cur, b_prev, b_cur):
            continue
        if a_prev <= b_prev and a_cur > b_cur:
            out[i] = True
    return out


def crosses_below(series_a: Sequence[Optional[float]], series_b: Sequence[Optional[float]]) -> list[bool]:
    """True where series_a crosses BELOW series_b (was >=, now <)."""
    n = min(len(series_a), len(series_b))
    out = [False] * n
    for i in range(1, n):
        a_prev, a_cur = series_a[i - 1], series_a[i]
        b_prev, b_cur = series_b[i - 1], series_b[i]
        if None in (a_prev, a_cur, b_prev, b_cur):
            continue
        if a_prev >= b_prev and a_cur < b_cur:
            out[i] = True
    return out
