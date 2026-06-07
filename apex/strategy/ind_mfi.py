"""
apex.strategy.ind_mfi
=====================
Money Flow Index (MFI) — a volume-weighted momentum oscillator, often called the
"volume-weighted RSI." It uses the typical price (HLC/3) and volume to gauge
buying vs. selling pressure over a rolling window. Returns 0-100; readings above
80 are considered overbought, below 20 oversold.

Follows the same CONTRACT as apex.strategy.indicators:
  - Input: high, low, close, volume sequences (floats or Decimals — converted to
    float internally for speed, since this is a comparative oscillator, not
    accounting; money math stays Decimal elsewhere).
  - Output: a list the SAME LENGTH as the input, with None for positions where
    there isn't enough data yet (the warmup period). NEVER returns garbage for
    insufficient data — None means "not enough history, don't trade on this."
  - Deterministic: same input → same output, always.

Tested in tests/test_ind_mfi.py against hand-computed values.
"""
from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def typical_price(
    high: Sequence, low: Sequence, close: Sequence
) -> list[float]:
    """Typical price per bar: (high + low + close) / 3."""
    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high, low, close must be the same length")
    return [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)]


def mfi(
    high: Sequence,
    low: Sequence,
    close: Sequence,
    volume: Sequence,
    period: int = 14,
) -> list[Optional[float]]:
    """
    Money Flow Index over `period` bars. Returns 0-100.

    For each bar:
      typical price  = (high + low + close) / 3
      raw money flow = typical price * volume
    Money flow is "positive" when the typical price rises vs. the prior bar,
    "negative" when it falls; an unchanged typical price contributes to neither
    (standard convention). Over the trailing `period` bars:
      money ratio = sum(positive money flow) / sum(negative money flow)
      MFI         = 100 - 100 / (1 + money ratio)
    If negative money flow is zero, MFI is 100 (pure buying pressure).

    None until `period`+1 bars exist (need `period` typical-price changes).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    volumes = _to_floats(volume)
    n = len(closes)
    if not (len(highs) == len(lows) == n == len(volumes)):
        raise ValueError("high, low, close, volume must be the same length")

    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out

    tp = typical_price(highs, lows, closes)
    raw_flow = [tp[i] * volumes[i] for i in range(n)]

    # Signed money flow per bar (index 0 has no prior bar → 0 for both).
    positive = [0.0] * n
    negative = [0.0] * n
    for i in range(1, n):
        if tp[i] > tp[i - 1]:
            positive[i] = raw_flow[i]
        elif tp[i] < tp[i - 1]:
            negative[i] = raw_flow[i]
        # Unchanged typical price contributes to neither.

    # First MFI uses the `period` changes spanning indices 1..period.
    pos_sum = sum(positive[1: period + 1])
    neg_sum = sum(negative[1: period + 1])
    out[period] = _mfi_from_sums(pos_sum, neg_sum)

    # Slide the window one bar at a time.
    for i in range(period + 1, n):
        pos_sum += positive[i] - positive[i - period]
        neg_sum += negative[i] - negative[i - period]
        out[i] = _mfi_from_sums(pos_sum, neg_sum)
    return out


def _mfi_from_sums(pos_sum: float, neg_sum: float) -> float:
    if neg_sum == 0:
        # No selling pressure in the window → maximum reading.
        return 100.0
    money_ratio = pos_sum / neg_sum
    return 100.0 - (100.0 / (1.0 + money_ratio))
