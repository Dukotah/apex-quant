"""
apex.risk.stop_levels
=====================
Stop-loss LEVEL calculators — pure functions that turn an entry price (and,
for the volatility-based variants, an ATR and/or a price extreme) into a
concrete protective stop price.

These produce the `suggested_stop_loss` a strategy attaches to a SignalEvent;
the RiskManager (apex.risk.risk_manager) then validates that stop is on the
correct side and far enough away before it will approve an order. Computing the
level and validating it are deliberately separate concerns — this module only
computes.

Three families are provided:
  - percent_stop:      a fixed fraction below (long) / above (short) entry.
  - atr_stop:          entry minus/plus `multiplier * ATR` — a volatility-aware
                       stop that widens in turbulent markets and tightens in calm
                       ones (the standard volatility-normalised exit distance).
  - chandelier_stop:   a TRAILING stop anchored to the highest high (long) /
                       lowest low (short) since entry, offset by `multiplier *
                       ATR` (Chuck LeBeau's Chandelier Exit). Ratchets in the
                       favourable direction only — it never loosens.

Design invariants (mirrors apex.risk.portfolio / apex.risk.risk_manager):
  - All prices/distances are Decimal — never float. Stops are money, and the
    RiskManager compares them against Decimal reference prices.
  - Pure and deterministic: same inputs → same output, every call. No I/O, no
    wall-clock time, no randomness.
  - Fail closed on insufficient / nonsensical inputs: return None rather than a
    garbage level. A None stop is something the caller can detect and reject;
    a wrong number is something it would silently trade on.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional, Sequence

from apex.core.models import OrderSide

_ZERO = Decimal("0")
_ONE = Decimal("1")


def _as_decimal(value) -> Optional[Decimal]:
    """
    Coerce a numeric input to Decimal, going through str so floats don't drag
    in binary-float noise. Returns None for None or anything non-numeric, so the
    callers can fail closed instead of raising.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None


def percent_stop(
    entry_price,
    pct: Decimal | float,
    side: OrderSide = OrderSide.BUY,
) -> Optional[Decimal]:
    """
    Fixed-percentage stop.

    For a long (BUY):  stop = entry * (1 - pct)   (below entry).
    For a short (SELL): stop = entry * (1 + pct)   (above entry).

    `pct` is a fraction (0.05 = 5%). Returns None if the entry price is
    non-positive or `pct` is not a positive fraction below 1 (a >=100% stop on a
    long would be <= 0, which is meaningless).
    """
    entry = _as_decimal(entry_price)
    fraction = _as_decimal(pct)
    if entry is None or fraction is None:
        return None
    if entry <= _ZERO or fraction <= _ZERO:
        return None
    if side == OrderSide.BUY:
        if fraction >= _ONE:
            return None  # would put the stop at or below zero
        return entry * (_ONE - fraction)
    return entry * (_ONE + fraction)


def atr_stop(
    entry_price,
    atr_value,
    multiplier: Decimal | float = Decimal("3"),
    side: OrderSide = OrderSide.BUY,
) -> Optional[Decimal]:
    """
    Volatility-based stop a fixed number of ATRs away from entry.

    For a long (BUY):  stop = entry - multiplier * ATR.
    For a short (SELL): stop = entry + multiplier * ATR.

    `atr_value` is the current Average True Range (same price units as entry;
    pass the latest non-None value from apex.strategy.indicators.atr). Returns
    None on non-positive entry, non-positive ATR, non-positive multiplier, or a
    None/garbage input. For a long, also returns None if the computed level is
    <= 0 (ATR larger than the price — no sensible protective stop exists).
    """
    entry = _as_decimal(entry_price)
    atr = _as_decimal(atr_value)
    mult = _as_decimal(multiplier)
    if entry is None or atr is None or mult is None:
        return None
    if entry <= _ZERO or atr <= _ZERO or mult <= _ZERO:
        return None
    distance = mult * atr
    if side == OrderSide.BUY:
        level = entry - distance
        if level <= _ZERO:
            return None
        return level
    return entry + distance


def chandelier_stop(
    highs: Sequence,
    lows: Sequence,
    atr_value,
    multiplier: Decimal | float = Decimal("3"),
    side: OrderSide = OrderSide.BUY,
) -> Optional[Decimal]:
    """
    Chandelier Exit — a trailing stop anchored to the price extreme since entry.

    For a long (BUY):  stop = highest_high - multiplier * ATR.
    For a short (SELL): stop = lowest_low  + multiplier * ATR.

    `highs` / `lows` are the bar highs / lows over the lookback window since
    entry (the caller slices the window; this function takes the extreme over
    everything it is given). The stop trails the favourable extreme: as a long
    makes new highs the level rises, locking in gains, and never falls unless the
    caller feeds it a window with a lower extreme.

    Returns None if the relevant series is empty, the ATR / multiplier are not
    positive, any required input is None/garbage, or (for a long) the computed
    level is <= 0.
    """
    atr = _as_decimal(atr_value)
    mult = _as_decimal(multiplier)
    if atr is None or mult is None or atr <= _ZERO or mult <= _ZERO:
        return None
    distance = mult * atr

    if side == OrderSide.BUY:
        extreme = _extreme(highs, want_max=True)
        if extreme is None:
            return None
        level = extreme - distance
        if level <= _ZERO:
            return None
        return level

    extreme = _extreme(lows, want_max=False)
    if extreme is None:
        return None
    return extreme + distance


def _extreme(series: Sequence, want_max: bool) -> Optional[Decimal]:
    """
    Max (want_max=True) or min of a numeric series as a Decimal, skipping None
    entries. Returns None if the series is empty or yields no usable values, so
    the caller fails closed instead of trading on an undefined extreme.
    """
    if series is None:
        return None
    best: Optional[Decimal] = None
    for raw in series:
        value = _as_decimal(raw)
        if value is None:
            continue
        if best is None or (value > best if want_max else value < best):
            best = value
    return best
