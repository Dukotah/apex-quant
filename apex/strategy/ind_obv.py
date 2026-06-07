"""
apex.strategy.ind_obv
=====================
On-Balance Volume (OBV) — a cumulative volume momentum indicator.

OBV adds the bar's volume to a running total when price closes higher than the
prior bar, subtracts it when price closes lower, and leaves it unchanged when the
close is flat. Rising OBV confirms buying pressure; divergence from price hints at
weakening trends.

Follows the indicator-library contract (see apex.strategy.indicators):
  - Input: parallel sequences of closes and volumes (floats or Decimals — we work
    in float internally for speed; money/accounting math stays Decimal elsewhere).
  - Output: a list the SAME LENGTH as the input. The first bar has no prior close,
    so its OBV is the seed value 0.0; positions before that (an empty input) yield
    an empty list. NEVER returns garbage for insufficient data.
  - Deterministic: same input -> same output, always.

Tested in tests/test_ind_obv.py against hand-computed values.
"""

from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def obv(close: Sequence, volume: Sequence) -> list[Optional[float]]:
    """
    On-Balance Volume cumulative series.

    Starts at 0.0 on the first bar (no prior close to compare). For each later
    bar: add the volume if the close rose, subtract it if the close fell, carry
    the running total unchanged if the close was flat.

    Returns a list the same length as the inputs. An empty input returns [].
    """
    closes = _to_floats(close)
    volumes = _to_floats(volume)
    n = len(closes)
    if len(volumes) != n:
        raise ValueError("close and volume must be the same length")

    out: list[Optional[float]] = [None] * n
    if n == 0:
        return out

    running = 0.0
    out[0] = running
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            running += volumes[i]
        elif closes[i] < closes[i - 1]:
            running -= volumes[i]
        # flat close: running unchanged
        out[i] = running
    return out
