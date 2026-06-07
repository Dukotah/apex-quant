"""
apex.risk.drawdown_throttle
===========================
A PURE, standalone computation of the drawdown sizing throttle: a multiplier in
[floor, 1] (a subset of [0, 1]) that de-risks NEW entries as equity bleeds away
from its peak.

This is the standard managed-futures answer to a strategy whose realistic
drawdown is large: you bet smaller WHILE losing, so a bad run is survivable. It
protects the equity PATH, and is independent of (and complementary to) the hard
`max_drawdown_pct` halt, which remains the catastrophe backstop.

The shape is piecewise-linear in current drawdown-from-peak:

      multiplier
        1.0  ┤───────────────●
             │                 \\
             │                  \\
       floor ┤                   ●──────────────
             └────────┬──────────┬──────────────▶ drawdown
                    start        full

  - drawdown <= start          → 1.0           (full size; no down-sizing yet)
  - start  < drawdown < full   → linear ramp    (1.0 → floor)
  - drawdown >= full           → floor          (held at the smallest size)

Design notes (mirrors the risk layer's conventions):
  - Decimal throughout — this is the money/risk layer, like risk_manager.py.
  - PURE and deterministic: no I/O, no clock, no randomness. Same inputs →
    same output, every time.
  - Fails to the SAFE full-throttle value (1) only for the *structural* cases
    (disabled, or no peak yet). A malformed ramp (e.g. full <= start) collapses
    to `floor` past `start` rather than amplifying — de-risking, never the
    reverse. Inputs are validated up front so garbage cannot silently produce a
    nonsensical multiplier.
  - The returned multiplier is always clamped into [floor, 1] ⊆ [0, 1].

This duplicates no live state and imports nothing from sibling risk modules; it
is a self-contained kernel the RiskManager (or a backtest report) can call.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional, Union

Number = Union[Decimal, int, float, str]

_ZERO = Decimal("0")
_ONE = Decimal("1")


def _to_decimal(value: Number) -> Decimal:
    """Coerce any supported numeric input to Decimal (str() guards float noise)."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def drawdown_from_equity(
    equity: Number,
    peak_equity: Number,
) -> Optional[Decimal]:
    """
    Current drawdown as a fraction of peak equity, in [0, 1].

    0   => at (or above) the peak.
    1   => total loss.

    Returns None when there is no usable peak yet (peak <= 0), signalling
    "insufficient data" rather than fabricating a number. Never returns a
    negative value: if equity has risen above the recorded peak the drawdown
    is clamped to 0.
    """
    peak = _to_decimal(peak_equity)
    if peak <= _ZERO:
        return None
    eq = _to_decimal(equity)
    dd = (peak - eq) / peak
    return max(_ZERO, dd)


def throttle_factor(
    drawdown: Number,
    *,
    start: Optional[Number],
    full: Number = Decimal("0.30"),
    floor: Number = Decimal("0.30"),
) -> Decimal:
    """
    Map a current drawdown-from-peak to a sizing multiplier in [floor, 1].

    Args:
        drawdown: Current drawdown as a fraction (0.10 = 10% off peak). Values
            below 0 are treated as 0 (at peak); the multiplier is non-increasing
            in drawdown.
        start: Drawdown at which down-sizing begins. ``None`` disables the
            throttle entirely and returns 1 (full size) — matching the default
            in RiskConfig, so an unset throttle is a true no-op.
        full: Drawdown at which the multiplier reaches ``floor`` and is held
            there for all deeper drawdowns.
        floor: The smallest multiplier (the most de-risked size). Clamped into
            (0, 1]: it must be positive (a 0 floor would mean "stop trading",
            which is the halt's job, not the throttle's) and never above 1.

    Behaviour:
        drawdown <= start          → 1
        start < drawdown < full    → linear interpolation from 1 down to floor
        drawdown >= full           → floor
        full <= start (degenerate) → floor for any drawdown past start (no ramp)

    Returns a Decimal in [floor, 1] ⊆ [0, 1]. Fails SAFE: any structural
    disable returns 1; any malformed ramp collapses to floor (de-risking),
    never to an amplifying multiplier.
    """
    if start is None:
        return _ONE

    start_d = _to_decimal(start)
    full_d = _to_decimal(full)

    # Clamp floor into (0, 1]. A non-positive or absent floor would either zero
    # out sizing (the halt's responsibility) or be meaningless; cap at 1 so the
    # throttle can only ever reduce, never amplify.
    floor_d = _to_decimal(floor)
    if floor_d <= _ZERO:
        floor_d = _ZERO  # degenerate but still a valid lower bound
    if floor_d > _ONE:
        floor_d = _ONE

    dd = max(_ZERO, _to_decimal(drawdown))

    # Below the trigger: full size.
    if dd <= start_d:
        return _ONE

    # At/over the bottom of the ramp, or a degenerate ramp (full <= start):
    # hold the floor. Collapsing a bad config to floor de-risks rather than
    # amplifies.
    if dd >= full_d or full_d <= start_d:
        return floor_d

    # Strictly inside the ramp: linear interpolation 1 → floor.
    frac = (dd - start_d) / (full_d - start_d)          # 0..1 across the ramp
    return _ONE - frac * (_ONE - floor_d)


def equity_throttle_factor(
    equity: Number,
    peak_equity: Number,
    *,
    start: Optional[Number],
    full: Number = Decimal("0.30"),
    floor: Number = Decimal("0.30"),
) -> Decimal:
    """
    Convenience kernel: compute drawdown from (equity, peak_equity) then map it
    through :func:`throttle_factor`.

    Returns 1 (full size) when the throttle is disabled (``start is None``) or
    while there is no usable peak yet (peak <= 0) — the same SAFE full-throttle
    default the inline RiskManager path uses, so an unconfigured or
    not-yet-warmed portfolio is never down-sized.
    """
    if start is None:
        return _ONE
    dd = drawdown_from_equity(equity, peak_equity)
    if dd is None:
        return _ONE
    return throttle_factor(dd, start=start, full=full, floor=floor)
