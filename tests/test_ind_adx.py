"""
Tests for apex.strategy.ind_adx — +DI, -DI, ADX (Wilder smoothing).

We verify:
  - Warmup/None placement matches the documented contract.
  - +DI/-DI/ADX against an independent, plain-loop reference implementation.
  - A hand-built pure-uptrend series: +DI >> -DI and a strong, rising ADX.
  - A pure-downtrend series: -DI >> +DI.
  - Degenerate (flat) windows fail closed to 0.0, not garbage.
  - Edge cases: insufficient data, bad period, mismatched lengths, Decimal input.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.strategy.ind_adx import (
    adx,
    adx_components,
    directional_indicators,
)

# --- An independent reference implementation (deliberately written differently) ---


def _ref_di_adx(highs, lows, closes, period):
    n = len(closes)
    pdm = [0.0] * n
    mdm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))

    pdi = [None] * n
    mdi = [None] * n
    if n < period + 1:
        return pdi, mdi, [None] * n

    sp = sum(pdm[1 : period + 1])
    sm = sum(mdm[1 : period + 1])
    st = sum(tr[1 : period + 1])

    def di(d, t):
        return 0.0 if t == 0 else 100.0 * d / t

    pdi[period] = di(sp, st)
    mdi[period] = di(sm, st)
    for i in range(period + 1, n):
        sp += pdm[i] - sp / period
        sm += mdm[i] - sm / period
        st += tr[i] - st / period
        pdi[i] = di(sp, st)
        mdi[i] = di(sm, st)

    dxs = []
    for i in range(period, n):
        tot = pdi[i] + mdi[i]
        dxs.append(0.0 if tot == 0 else 100.0 * abs(pdi[i] - mdi[i]) / tot)

    a = [None] * n
    if n >= 2 * period:
        prev = sum(dxs[:period]) / period
        a[2 * period - 1] = prev
        for j in range(period, len(dxs)):
            prev = (prev * (period - 1) + dxs[j]) / period
            a[period + j] = prev
    return pdi, mdi, a


def _series(seed=7, n=80):
    """Deterministic pseudo-random OHLC series (seeded, no global RNG)."""
    import random

    rng = random.Random(seed)
    highs, lows, closes = [], [], []
    price = 100.0
    for _ in range(n):
        price += rng.uniform(-1.5, 1.6)
        price = max(price, 1.0)
        spread = rng.uniform(0.2, 2.0)
        h = price + spread
        lo = price - spread
        c = lo + rng.uniform(0, 1) * (h - lo)
        highs.append(h)
        lows.append(lo)
        closes.append(c)
    return highs, lows, closes


def test_matches_reference_implementation():
    highs, lows, closes = _series()
    period = 14
    pdi, mdi, a = adx_components(highs, lows, closes, period)
    rpdi, rmdi, ra = _ref_di_adx(highs, lows, closes, period)
    for x, y in zip(pdi, rpdi):
        if x is None or y is None:
            assert x is y
        else:
            assert x == pytest.approx(y, rel=1e-12, abs=1e-12)
    for x, y in zip(mdi, rmdi):
        if x is None or y is None:
            assert x is y
        else:
            assert x == pytest.approx(y, rel=1e-12, abs=1e-12)
    for x, y in zip(a, ra):
        if x is None or y is None:
            assert x is y
        else:
            assert x == pytest.approx(y, rel=1e-12, abs=1e-12)


def test_warmup_placement():
    period = 14
    n = 60
    highs, lows, closes = _series(seed=3, n=n)
    pdi, mdi = directional_indicators(highs, lows, closes, period)
    a = adx(highs, lows, closes, period)
    # +DI / -DI: None before index `period`, defined at and after.
    assert all(v is None for v in pdi[:period])
    assert pdi[period] is not None
    assert all(v is not None for v in pdi[period:])
    assert all(v is None for v in mdi[:period])
    assert mdi[period] is not None
    # ADX: first value at index 2*period - 1.
    assert all(v is None for v in a[: 2 * period - 1])
    assert a[2 * period - 1] is not None
    assert all(v is not None for v in a[2 * period - 1 :])


def test_pure_uptrend():
    # Strictly rising highs and lows → +DM dominates, -DM ~ 0.
    period = 5
    highs = [10 + i for i in range(40)]
    lows = [9 + i for i in range(40)]
    closes = [9.5 + i for i in range(40)]
    pdi, mdi = directional_indicators(highs, lows, closes, period)
    a = adx(highs, lows, closes, period)
    # In a clean uptrend +DM = 1/bar dominates while -DM = 0, but TR includes
    # the gap to the prior close, so +DI = 100 * 1 / 1.5 = 66.67 exactly, and
    # -DI = 0. ADX → 100 because +DI and -DI never disagree (DX is always 100).
    assert pdi[period] == pytest.approx(200.0 / 3.0)
    assert mdi[period] == 0.0
    last_adx = a[-1]
    assert last_adx is not None
    assert last_adx == pytest.approx(100.0)


def test_pure_downtrend():
    period = 5
    highs = [50 - i for i in range(40)]
    lows = [49 - i for i in range(40)]
    closes = [49.5 - i for i in range(40)]
    pdi, mdi = directional_indicators(highs, lows, closes, period)
    assert mdi[period] == pytest.approx(200.0 / 3.0)
    assert pdi[period] == 0.0


def test_flat_series_fails_closed():
    # No movement at all → smoothed TR is 0 → DIs are 0.0 (not NaN/garbage),
    # and DX total is 0 → ADX is 0.0.
    period = 5
    highs = [20.0] * 30
    lows = [20.0] * 30
    closes = [20.0] * 30
    pdi, mdi = directional_indicators(highs, lows, closes, period)
    a = adx(highs, lows, closes, period)
    assert pdi[period] == 0.0
    assert mdi[period] == 0.0
    assert a[2 * period - 1] == 0.0


def test_di_bounds():
    highs, lows, closes = _series(seed=11, n=70)
    pdi, mdi = directional_indicators(highs, lows, closes, 14)
    for v in pdi:
        if v is not None:
            assert 0.0 <= v <= 100.0
    for v in mdi:
        if v is not None:
            assert 0.0 <= v <= 100.0
    a = adx(highs, lows, closes, 14)
    for v in a:
        if v is not None:
            assert 0.0 <= v <= 100.0


def test_insufficient_data_all_none():
    period = 14
    highs = [1.0] * 5
    lows = [0.5] * 5
    closes = [0.75] * 5
    pdi, mdi = directional_indicators(highs, lows, closes, period)
    assert pdi == [None] * 5
    assert mdi == [None] * 5
    # Not enough for any ADX either (need 2*period).
    short = [1.0] * (2 * period - 1)
    a = adx(short, [0.5] * (2 * period - 1), [0.75] * (2 * period - 1), period)
    assert all(v is None for v in a)


def test_decimal_input_accepted():
    # Money/price models use Decimal; the indicator must accept it (float internally).
    period = 5
    highs = [Decimal(10 + i) for i in range(30)]
    lows = [Decimal(9 + i) for i in range(30)]
    closes = [Decimal("9.5") + i for i in range(30)]
    pdi, mdi = directional_indicators(highs, lows, closes, period)
    assert isinstance(pdi[period], float)
    assert pdi[period] == pytest.approx(200.0 / 3.0)
    assert mdi[period] == 0.0


def test_invalid_period_raises():
    with pytest.raises(ValueError):
        directional_indicators([1, 2, 3], [0, 1, 2], [0.5, 1.5, 2.5], 0)
    with pytest.raises(ValueError):
        adx([1, 2, 3], [0, 1, 2], [0.5, 1.5, 2.5], -1)


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        directional_indicators([1, 2, 3], [0, 1], [0.5, 1.5, 2.5], 2)


def test_determinism():
    highs, lows, closes = _series(seed=42, n=50)
    a1 = adx(highs, lows, closes, 14)
    a2 = adx(highs, lows, closes, 14)
    assert a1 == a2
