"""
apex.risk.position_sizing
=========================
Advisory position-size calculators. These turn a risk budget into a suggested
quantity using three classic schemes:

  - **fixed-fractional**: risk a fixed fraction of equity per trade, sized by
    the distance from entry to stop (the per-unit dollar risk).
  - **volatility-target**: size so the position's own (annualized) volatility
    contributes a target fraction of equity volatility — de-risk volatile
    instruments, up-size calm ones.
  - **ATR-risk**: a fixed-fractional variant where the per-unit risk is an ATR
    multiple (the standard turtle-style stop distance), rather than an explicit
    stop price.

IMPORTANT — these are ADVISORY ONLY. The RiskManager remains the single, sole
sizer and gatekeeper of real orders (CLAUDE.md golden rules 2, 3). Nothing here
places, sizes, or approves a live trade; a strategy may use these to inform the
`strength` it emits, but the RiskManager always has the final word and its hard
caps cannot be bypassed by anything computed here.

Design invariants:
  - All money/price/quantity math uses Decimal — never float (golden rule 14).
  - Pure & deterministic: same inputs -> same outputs, no I/O, no wall clock,
    no randomness.
  - Fail closed: insufficient / nonsensical inputs return Decimal("0") (no
    position), never garbage or an exception for a normal "can't size" case.
  - Fractional vs whole-unit rounding mirrors the RiskManager: fractionable
    instruments quantize to 1e-4, others floor to whole units.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from apex.core.models import Symbol

_ZERO = Decimal("0")
_QUANTUM = Decimal("0.0001")  # fractional sizing precision (mirrors RiskManager)


def _to_decimal(value) -> Optional[Decimal]:
    """
    Coerce a numeric input to Decimal via str (avoids binary float artifacts).
    Returns None if the value cannot be represented as a finite Decimal.
    """
    if value is None:
        return None
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None
    if not d.is_finite():
        return None
    return d


def round_quantity(raw_qty: Decimal, symbol: Symbol) -> Decimal:
    """
    Apply the project's standard quantity rounding for a symbol.

    Fractionable instruments (crypto / fractional shares) quantize to 1e-4;
    everything else floors to whole units. Negative or non-finite inputs
    collapse to zero (fail closed). This mirrors RiskManager._size_position so
    advisory sizes line up with what the gate would actually allow.
    """
    if raw_qty is None or not raw_qty.is_finite() or raw_qty <= _ZERO:
        return _ZERO
    if symbol.fractionable:
        return raw_qty.quantize(_QUANTUM)
    return Decimal(int(raw_qty))


def fixed_fractional_size(
    equity,
    entry_price,
    stop_price,
    symbol: Symbol,
    risk_fraction=Decimal("0.01"),
) -> Decimal:
    """
    Fixed-fractional sizing: risk `risk_fraction` of equity per trade, sized by
    the per-unit dollar risk implied by the stop distance.

        risk_dollars = equity * risk_fraction
        per_unit_risk = |entry_price - stop_price| * contract_multiplier
        raw_qty = risk_dollars / per_unit_risk

    Args:
        equity: Account equity (must be > 0).
        entry_price: Intended entry price (must be > 0).
        stop_price: Protective stop price (must differ from entry).
        symbol: Instrument (drives multiplier + rounding).
        risk_fraction: Fraction of equity to put at risk (0 < f <= 1).

    Returns the suggested quantity, rounded for the symbol. Returns Decimal("0")
    when inputs are insufficient or nonsensical (fail closed).
    """
    eq = _to_decimal(equity)
    entry = _to_decimal(entry_price)
    stop = _to_decimal(stop_price)
    frac = _to_decimal(risk_fraction)
    if eq is None or entry is None or stop is None or frac is None:
        return _ZERO
    if eq <= _ZERO or entry <= _ZERO or frac <= _ZERO or frac > Decimal("1"):
        return _ZERO

    multiplier = symbol.contract_multiplier
    per_unit_risk = abs(entry - stop) * multiplier
    if per_unit_risk <= _ZERO:
        return _ZERO

    risk_dollars = eq * frac
    raw_qty = risk_dollars / per_unit_risk
    return round_quantity(raw_qty, symbol)


def atr_risk_size(
    equity,
    entry_price,
    atr,
    symbol: Symbol,
    risk_fraction=Decimal("0.01"),
    atr_multiple=Decimal("2"),
) -> Decimal:
    """
    ATR-risk sizing: a fixed-fractional scheme whose per-unit risk is an ATR
    multiple (the classic turtle stop distance) instead of an explicit stop.

        stop_distance = atr * atr_multiple
        per_unit_risk = stop_distance * contract_multiplier
        raw_qty = (equity * risk_fraction) / per_unit_risk

    `atr` is an average-true-range value in price units (e.g. from
    apex.strategy.indicators). It is treated as a float-derived magnitude but
    coerced to Decimal for the money math.

    Returns the suggested quantity, rounded for the symbol, or Decimal("0") on
    insufficient / nonsensical input (fail closed).
    """
    eq = _to_decimal(equity)
    entry = _to_decimal(entry_price)
    atr_d = _to_decimal(atr)
    frac = _to_decimal(risk_fraction)
    mult = _to_decimal(atr_multiple)
    if eq is None or entry is None or atr_d is None or frac is None or mult is None:
        return _ZERO
    if eq <= _ZERO or entry <= _ZERO or atr_d <= _ZERO or mult <= _ZERO:
        return _ZERO
    if frac <= _ZERO or frac > Decimal("1"):
        return _ZERO

    stop_distance = atr_d * mult
    per_unit_risk = stop_distance * symbol.contract_multiplier
    if per_unit_risk <= _ZERO:
        return _ZERO

    risk_dollars = eq * frac
    raw_qty = risk_dollars / per_unit_risk
    return round_quantity(raw_qty, symbol)


def volatility_target_size(
    equity,
    entry_price,
    instrument_volatility,
    symbol: Symbol,
    target_volatility=Decimal("0.10"),
    max_fraction=Decimal("1.0"),
) -> Decimal:
    """
    Volatility-target sizing: allocate notional so the position's own annualized
    volatility contributes `target_volatility` of equity. Calm instruments get
    a bigger allocation; volatile ones get trimmed.

        weight = target_volatility / instrument_volatility   (capped at max_fraction)
        notional = equity * weight
        raw_qty = notional / (entry_price * contract_multiplier)

    `instrument_volatility` is the instrument's annualized return volatility
    (e.g. 0.20 = 20%), a float-derived statistic coerced to Decimal here.

    Returns the suggested quantity, rounded for the symbol, or Decimal("0") on
    insufficient / nonsensical input (fail closed). The weight is clamped to
    [0, max_fraction] so this never advises more than `max_fraction` of equity.
    """
    eq = _to_decimal(equity)
    entry = _to_decimal(entry_price)
    inst_vol = _to_decimal(instrument_volatility)
    target = _to_decimal(target_volatility)
    cap = _to_decimal(max_fraction)
    if eq is None or entry is None or inst_vol is None or target is None or cap is None:
        return _ZERO
    if eq <= _ZERO or entry <= _ZERO or inst_vol <= _ZERO or target <= _ZERO or cap <= _ZERO:
        return _ZERO

    weight = target / inst_vol
    if weight > cap:
        weight = cap
    if weight <= _ZERO:
        return _ZERO

    notional = eq * weight
    denom = entry * symbol.contract_multiplier
    if denom <= _ZERO:
        return _ZERO
    raw_qty = notional / denom
    return round_quantity(raw_qty, symbol)


def kelly_fraction(
    win_rate,
    win_loss_ratio,
    cap=Decimal("1.0"),
) -> Decimal:
    """
    The Kelly-optimal fraction of capital to risk, given an edge.

        f* = p - (1 - p) / b

    where p = win_rate (0..1) and b = win_loss_ratio (avg win / avg loss, > 0).
    Returned clamped to [0, cap]; a non-positive edge advises 0 (fail closed).
    This is a helper that callers may feed into `risk_fraction` above; it is NOT
    a sizer itself and produces no quantity.
    """
    p = _to_decimal(win_rate)
    b = _to_decimal(win_loss_ratio)
    c = _to_decimal(cap)
    if p is None or b is None or c is None:
        return _ZERO
    if p <= _ZERO or p > Decimal("1") or b <= _ZERO or c <= _ZERO:
        return _ZERO

    f = p - (Decimal("1") - p) / b
    if f <= _ZERO:
        return _ZERO
    return min(f, c)
