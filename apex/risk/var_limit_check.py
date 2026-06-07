"""
apex.risk.var_limit_check
=========================
A PURE pass/fail check that compares a portfolio's Value-at-Risk (VaR) estimate
against a configured VaR budget.

VaR answers "how much could this book lose, at a given confidence, over the
horizon?" A risk policy typically caps that number — e.g. "the 95% 1-day VaR may
not exceed 3% of equity." This module is the guardrail that enforces such a cap:
given a VaR estimate (as a positive LOSS fraction of equity, mirroring how
apex.validation.var_cvar and apex.validation.metrics report losses) it decides
whether the book is WITHIN budget and, if not, by how much it breaches.

It is deliberately ADVISORY/diagnostic: like apex.risk.liquidity_caps it does NOT
place or reject orders. The RiskManager remains the only producer of OrderEvents;
a caller may run this check to decide whether to de-risk, but the function itself
only reports a verdict and the supporting detail.

Design invariants (mirror apex.risk.portfolio / risk_manager / stop_levels):
  - All risk quantities are Decimal — never float. VaR here is money-adjacent (a
    loss budget against equity) and is compared against Decimal limits, so the
    whole comparison stays in exact Decimal arithmetic.
  - Pure and deterministic: same inputs -> same verdict, every call. No I/O, no
    wall-clock time, no randomness.
  - FAIL CLOSED. On missing/invalid data (a None or negative VaR estimate, a
    non-positive limit) the check returns a BREACH verdict rather than a pass —
    the default outcome of any uncertainty is "not within budget", never "fine".
    This matches Golden Rule 6: risk checks fail closed.

Both the VaR estimate and the limit are expressed as the SAME unit — a positive
fraction of equity (0.03 = a 3% loss). Convert a dollar VaR to a fraction by
dividing by equity before calling, or use `var_fraction_of_equity` to do it
safely (it fails closed to None on a non-positive equity).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

_ZERO = Decimal("0")


def _as_decimal(value) -> Optional[Decimal]:
    """
    Coerce a numeric input to Decimal, going through str so floats don't drag in
    binary-float noise (mirrors apex.risk.stop_levels._as_decimal). Returns None
    for None or anything non-numeric so callers can fail closed instead of raising.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None


@dataclass(frozen=True)
class VarLimitResult:
    """
    Verdict of a VaR-vs-limit check. Frozen so a caller cannot mutate the result.

    Attributes:
        within_limit: True only when the VaR estimate is valid AND <= the limit.
            False on any breach OR on invalid/missing data (fail closed).
        var_estimate: The VaR used for the comparison, as a positive loss
            fraction of equity, or None if the supplied estimate was invalid.
        limit: The configured VaR budget (positive loss fraction), or None if the
            supplied limit was invalid (non-positive).
        utilization: var_estimate / limit — fraction of the budget consumed
            (1.0 = exactly at the cap, > 1.0 = breach). None when either input is
            invalid so it cannot be computed.
        breach_amount: How far VaR exceeds the limit (var_estimate - limit) when
            breaching, else ZERO. ZERO when inputs are invalid (the magnitude is
            unknown — `within_limit` already says it failed).
        reason: Human-readable explanation of the verdict (for logging).
    """

    within_limit: bool
    var_estimate: Optional[Decimal]
    limit: Optional[Decimal]
    utilization: Optional[Decimal]
    breach_amount: Decimal
    reason: str

    def summary(self) -> str:
        status = "WITHIN" if self.within_limit else "BREACH"
        ve = "n/a" if self.var_estimate is None else f"{self.var_estimate:.4%}"
        lim = "n/a" if self.limit is None else f"{self.limit:.4%}"
        util = "n/a" if self.utilization is None else f"{self.utilization:.2%}"
        return f"VaR limit {status}: var={ve} limit={lim} utilization={util}"


def var_fraction_of_equity(
    var_dollars,
    equity,
) -> Optional[Decimal]:
    """
    Convert a dollar VaR into a positive loss fraction of equity for use with
    `check_var_limit`.

    fraction = var_dollars / equity

    A negative `var_dollars` is invalid (VaR is a loss magnitude, reported
    positive) and yields None. FAILS CLOSED to None on a non-positive equity or
    any non-numeric input — the caller then treats None as "cannot assess" and
    `check_var_limit` will breach on it.

    Args:
        var_dollars: VaR as a non-negative dollar loss amount.
        equity: Account equity the VaR is measured against (must be > 0).

    Returns:
        VaR as a non-negative Decimal fraction of equity, or None if it cannot be
        computed safely.
    """
    var = _as_decimal(var_dollars)
    eq = _as_decimal(equity)
    if var is None or eq is None:
        return None
    if var < _ZERO or eq <= _ZERO:
        return None
    return var / eq


def check_var_limit(
    var_estimate,
    limit,
) -> VarLimitResult:
    """
    Compare a VaR estimate against a configured VaR budget.

    Both arguments are positive LOSS fractions of equity (0.03 = a 3% loss). The
    check PASSES (within_limit=True) only when the VaR estimate is a valid,
    non-negative number AND is <= the limit. It is at-the-cap-inclusive: a VaR
    exactly equal to the limit is within budget (utilization 1.0).

    FAILS CLOSED on bad input — a None / non-numeric / negative VaR estimate, or a
    None / non-numeric / non-positive limit — returning within_limit=False with a
    reason. The default outcome of any uncertainty is "not within budget".

    Args:
        var_estimate: Portfolio VaR as a non-negative loss fraction of equity.
            (Use `var_fraction_of_equity` to derive this from a dollar VaR.)
        limit: The VaR budget as a positive loss fraction of equity.

    Returns:
        A VarLimitResult describing the verdict and supporting detail.
    """
    var = _as_decimal(var_estimate)
    lim = _as_decimal(limit)

    # Fail closed on an invalid limit: with no valid budget we cannot pass.
    if lim is None or lim <= _ZERO:
        return VarLimitResult(
            within_limit=False,
            var_estimate=var if (var is not None and var >= _ZERO) else None,
            limit=None,
            utilization=None,
            breach_amount=_ZERO,
            reason="invalid VaR limit (must be a positive fraction)",
        )

    # Fail closed on an invalid VaR estimate: cannot assess => not within budget.
    if var is None or var < _ZERO:
        return VarLimitResult(
            within_limit=False,
            var_estimate=None,
            limit=lim,
            utilization=None,
            breach_amount=_ZERO,
            reason="invalid/missing VaR estimate (must be a non-negative fraction)",
        )

    utilization = var / lim

    if var <= lim:
        return VarLimitResult(
            within_limit=True,
            var_estimate=var,
            limit=lim,
            utilization=utilization,
            breach_amount=_ZERO,
            reason=f"VaR {var} within limit {lim}",
        )

    return VarLimitResult(
        within_limit=False,
        var_estimate=var,
        limit=lim,
        utilization=utilization,
        breach_amount=var - lim,
        reason=f"VaR {var} exceeds limit {lim} by {var - lim}",
    )


def check_var_dollars_limit(
    var_dollars,
    equity,
    limit,
) -> VarLimitResult:
    """
    Convenience wrapper: check a DOLLAR VaR against a fraction-of-equity limit.

    Converts the dollar VaR to a fraction via `var_fraction_of_equity` (which
    fails closed to None on a non-positive equity or negative VaR) and then defers
    to `check_var_limit`. A None conversion propagates as a fail-closed breach.

    Args:
        var_dollars: VaR as a non-negative dollar loss amount.
        equity: Account equity (must be > 0).
        limit: The VaR budget as a positive loss fraction of equity.

    Returns:
        A VarLimitResult, identical in shape to `check_var_limit`.
    """
    fraction = var_fraction_of_equity(var_dollars, equity)
    # If the conversion failed, pass None straight through so check_var_limit
    # produces the fail-closed "invalid VaR estimate" verdict.
    return check_var_limit(fraction, limit)
