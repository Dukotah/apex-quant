"""
apex.strategy.ind_adx
=====================
Directional movement indicators: +DI, -DI, and ADX (Average Directional Index)
using Wilder's smoothing. A trend-strength companion to the moving-average and
oscillator indicators in `apex.strategy.indicators`.

Like the rest of the indicator layer this is stateless and pure: it works in
float internally (indicators are comparative, not accounting — money math stays
Decimal elsewhere) and returns lists the SAME LENGTH as the input, with None for
positions inside the warmup period. None means "not enough history, don't trade
on this" — never garbage.

Wilder's directional movement (the classic definition):
  up_move   = high[i] - high[i-1]
  down_move = low[i-1] - low[i]
  +DM = up_move   if up_move > down_move and up_move > 0   else 0
  -DM = down_move if down_move > up_move and down_move > 0  else 0
  TR  = max(high-low, |high-prev_close|, |low-prev_close|)

+DM, -DM and TR are Wilder-smoothed over `period`. Then:
  +DI = 100 * smoothed(+DM) / smoothed(TR)
  -DI = 100 * smoothed(-DM) / smoothed(TR)
  DX  = 100 * |+DI - -DI| / (+DI + -DI)
  ADX = Wilder-smoothed average of DX over `period`.

Warmup:
  - +DI / -DI become available at index `period` (need `period` DM/TR values,
    which need the prior bar, so the first lands at index `period`).
  - DX is available wherever +DI / -DI are.
  - ADX needs a further `period` DX values to seed, so the first ADX lands at
    index `2*period - 1`.

Deterministic: same input → same output, always. No I/O, no wall-clock, no
randomness. Tested in tests/test_ind_adx.py against hand-computed values.
"""

from __future__ import annotations

from typing import Optional, Sequence


def _to_floats(data: Sequence) -> list[float]:
    return [float(x) for x in data]


def directional_indicators(
    high: Sequence, low: Sequence, close: Sequence, period: int = 14
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    """
    Wilder's +DI and -DI. Returns (plus_di, minus_di), each the same length as
    the input. None until index `period` (need `period` smoothed DM/TR values,
    each requiring the prior bar).

    Values are in 0-100 range. If smoothed true range is zero (a flat,
    no-movement window) both DIs are 0.0 for that bar — fail closed, no signal.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    highs, lows, closes = _to_floats(high), _to_floats(low), _to_floats(close)
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high, low, close must be the same length")

    plus_di: list[Optional[float]] = [None] * n
    minus_di: list[Optional[float]] = [None] * n
    if n < period + 1:
        return plus_di, minus_di

    # Per-bar raw +DM, -DM and TR (index 0 has no prior bar → undefined, left 0).
    plus_dm: list[float] = [0.0] * n
    minus_dm: list[float] = [0.0] * n
    true_range: list[float] = [0.0] * n
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        true_range[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    # Wilder seed = simple sum of the first `period` values (indices 1..period).
    sm_plus_dm = sum(plus_dm[1 : period + 1])
    sm_minus_dm = sum(minus_dm[1 : period + 1])
    sm_tr = sum(true_range[1 : period + 1])
    plus_di[period] = _di(sm_plus_dm, sm_tr)
    minus_di[period] = _di(sm_minus_dm, sm_tr)

    # Wilder smoothing: subtract one period-average, add the new raw value.
    for i in range(period + 1, n):
        sm_plus_dm = sm_plus_dm - (sm_plus_dm / period) + plus_dm[i]
        sm_minus_dm = sm_minus_dm - (sm_minus_dm / period) + minus_dm[i]
        sm_tr = sm_tr - (sm_tr / period) + true_range[i]
        plus_di[i] = _di(sm_plus_dm, sm_tr)
        minus_di[i] = _di(sm_minus_dm, sm_tr)

    return plus_di, minus_di


def _di(smoothed_dm: float, smoothed_tr: float) -> float:
    if smoothed_tr == 0:
        return 0.0
    return 100.0 * smoothed_dm / smoothed_tr


def _dx(plus: float, minus: float) -> float:
    total = plus + minus
    if total == 0:
        return 0.0
    return 100.0 * abs(plus - minus) / total


def adx(high: Sequence, low: Sequence, close: Sequence, period: int = 14) -> list[Optional[float]]:
    """
    Average Directional Index (Wilder). Measures TREND STRENGTH (not direction):
    higher = stronger trend, regardless of up/down. Same length as input, None
    during warmup. The first ADX lands at index `2*period - 1`.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    plus_di, minus_di = directional_indicators(high, low, close, period)
    n = len(plus_di)
    out: list[Optional[float]] = [None] * n
    if n < 2 * period:
        return out

    # DX is defined wherever both DIs are (from index `period` onward).
    dx_vals: list[float] = []
    for i in range(period, n):
        dx_vals.append(_dx(plus_di[i], minus_di[i]))  # type: ignore[arg-type]

    # Seed ADX = simple average of the first `period` DX values, landing at the
    # bar where the `period`-th DX exists: index `period + (period - 1)`.
    seed = sum(dx_vals[:period]) / period
    out[2 * period - 1] = seed
    prev = seed
    for j in range(period, len(dx_vals)):
        prev = (prev * (period - 1) + dx_vals[j]) / period
        out[period + j] = prev
    return out


def adx_components(
    high: Sequence, low: Sequence, close: Sequence, period: int = 14
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    """
    Convenience: return (plus_di, minus_di, adx) in one call, each the same
    length as the input. Avoids recomputing the directional indicators twice
    when a strategy wants all three.
    """
    plus_di, minus_di = directional_indicators(high, low, close, period)
    adx_line = adx(high, low, close, period)
    return plus_di, minus_di, adx_line
