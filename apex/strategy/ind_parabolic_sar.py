"""
apex.strategy.ind_parabolic_sar
===============================
Parabolic SAR (Stop And Reverse) — J. Welles Wilder's trailing-stop / trend
indicator. A standalone addition to the indicator library; same contract as
``apex.strategy.indicators``: a pure, stateless, deterministic function that
returns a list the SAME LENGTH as the input, with ``None`` for warmup positions.

How it works (Wilder's rules):
  - The SAR trails price by an accelerating amount. Each bar:
        SAR_next = SAR_prev + AF * (EP - SAR_prev)
    where EP (the "extreme point") is the highest high reached during the
    current uptrend (or the lowest low during a downtrend), and AF is the
    "acceleration factor".
  - AF starts at ``af_start`` (classic 0.02), increments by ``af_step`` each
    time a NEW extreme point is made, and is capped at ``af_max`` (classic 0.20).
  - When price penetrates the SAR, the trend flips ("stop and reverse"): the
    SAR becomes the prior EP, AF resets to ``af_start``, and EP resets to the
    current bar's extreme in the new direction.
  - A SAR is never allowed to move into the prior two bars' price range; if it
    would, it is clamped to that range (Wilder's penetration rule).

Determinism: pure function of its inputs, no I/O, no clock, no randomness.
Insufficient data (fewer than 2 bars) returns all ``None`` — never garbage.
We work in float internally to match the indicator layer's convention
(comparative math, not accounting); money math stays Decimal elsewhere.

Tested in tests/test_ind_parabolic_sar.py against hand-computed values.
"""

from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def parabolic_sar(
    high: Sequence,
    low: Sequence,
    af_start: float = 0.02,
    af_step: float = 0.02,
    af_max: float = 0.20,
) -> list[Optional[float]]:
    """
    Parabolic SAR (Wilder). Returns a list of SAR values, same length as input.

    Parameters
    ----------
    high, low : sequences of the bar highs and lows (same length).
    af_start  : initial acceleration factor (classic 0.02).
    af_step   : increment applied each time a new extreme point is made (0.02).
    af_max    : maximum acceleration factor (classic 0.20).

    Returns
    -------
    list[Optional[float]] of length == len(high). Index 0 is always ``None``
    (no prior bar to seed direction). From index 1 onward each entry is the
    SAR for that bar. Fewer than 2 bars → all ``None``.

    Raises
    ------
    ValueError if high/low lengths differ, if any high < low, or if the AF
    parameters are non-positive / inconsistent (fail closed on bad config).
    """
    highs = _to_floats(high)
    lows = _to_floats(low)
    n = len(highs)
    if len(lows) != n:
        raise ValueError("high and low must be the same length")
    if af_start <= 0 or af_step <= 0 or af_max <= 0:
        raise ValueError("af_start, af_step, af_max must be positive")
    if af_max < af_start:
        raise ValueError("af_max must be >= af_start")
    for i in range(n):
        if highs[i] < lows[i]:
            raise ValueError(f"high < low at index {i}")

    out: list[Optional[float]] = [None] * n
    if n < 2:
        return out

    # Seed the trend direction from the first two bars. If the second bar's
    # close-equivalent (we use highs/lows only) is rising, start long; else
    # short. We compare the second high/low to the first to pick a direction.
    rising = (highs[1] >= highs[0]) or (lows[1] >= lows[0])

    if rising:
        # Uptrend: SAR seeds at the prior low, EP is the prior high.
        sar = lows[0]
        ep = highs[0]
    else:
        # Downtrend: SAR seeds at the prior high, EP is the prior low.
        sar = highs[0]
        ep = lows[0]
    af = af_start
    long = rising

    for i in range(1, n):
        prev_sar = sar
        # Advance the SAR toward the extreme point.
        sar = prev_sar + af * (ep - prev_sar)

        if long:
            # SAR must not exceed the prior two bars' lows.
            sar = min(sar, lows[i - 1])
            if i >= 2:
                sar = min(sar, lows[i - 2])

            if lows[i] < sar:
                # Trend flips to down ("stop and reverse").
                long = False
                sar = ep  # new SAR = prior extreme point
                ep = lows[i]  # new EP = current low
                af = af_start
            else:
                # Trend continues; extend EP / accelerate if a new high made.
                if highs[i] > ep:
                    ep = highs[i]
                    af = min(af + af_step, af_max)
        else:
            # Downtrend: SAR must not fall below the prior two bars' highs.
            sar = max(sar, highs[i - 1])
            if i >= 2:
                sar = max(sar, highs[i - 2])

            if highs[i] > sar:
                # Trend flips to up.
                long = True
                sar = ep
                ep = highs[i]
                af = af_start
            else:
                if lows[i] < ep:
                    ep = lows[i]
                    af = min(af + af_step, af_max)

        out[i] = sar

    return out
