"""
Tests for apex.risk.var_limit_check — pure VaR-vs-limit guardrail.

Hand-computed values plus the fail-closed edge cases that the risk layer cares
about. Imported by full path so no package __init__ edits are needed.
"""
from __future__ import annotations

from decimal import Decimal

from apex.risk.var_limit_check import (
    VarLimitResult,
    check_var_dollars_limit,
    check_var_limit,
    var_fraction_of_equity,
)

# --------------------------------------------------------------------------
# check_var_limit — the core verdict
# --------------------------------------------------------------------------

def test_within_limit_strict_pass():
    # VaR 2% against a 3% budget -> within, utilization 2/3.
    res = check_var_limit(Decimal("0.02"), Decimal("0.03"))
    assert isinstance(res, VarLimitResult)
    assert res.within_limit is True
    assert res.var_estimate == Decimal("0.02")
    assert res.limit == Decimal("0.03")
    assert res.utilization == Decimal("0.02") / Decimal("0.03")
    assert res.breach_amount == Decimal("0")


def test_exactly_at_limit_is_within():
    # At-the-cap is inclusive: utilization is exactly 1.
    res = check_var_limit(Decimal("0.03"), Decimal("0.03"))
    assert res.within_limit is True
    assert res.utilization == Decimal("1")
    assert res.breach_amount == Decimal("0")


def test_breach_reports_amount_and_utilization():
    # VaR 5% against a 3% budget -> breach by 2 points, utilization 5/3.
    res = check_var_limit(Decimal("0.05"), Decimal("0.03"))
    assert res.within_limit is False
    assert res.breach_amount == Decimal("0.02")
    assert res.utilization == Decimal("0.05") / Decimal("0.03")
    assert "exceeds" in res.reason


def test_zero_var_is_within():
    # A flat / hedged book with zero VaR is always within any positive budget.
    res = check_var_limit(Decimal("0"), Decimal("0.03"))
    assert res.within_limit is True
    assert res.utilization == Decimal("0")
    assert res.breach_amount == Decimal("0")


def test_float_inputs_are_coerced_via_str():
    # Floats are accepted and coerced exactly (no binary-float noise).
    res = check_var_limit(0.02, 0.03)
    assert res.var_estimate == Decimal("0.02")
    assert res.limit == Decimal("0.03")
    assert res.within_limit is True


# --------------------------------------------------------------------------
# Fail-closed behaviour
# --------------------------------------------------------------------------

def test_none_var_estimate_fails_closed():
    res = check_var_limit(None, Decimal("0.03"))
    assert res.within_limit is False
    assert res.var_estimate is None
    assert res.limit == Decimal("0.03")
    assert res.utilization is None
    assert "invalid/missing VaR estimate" in res.reason


def test_negative_var_estimate_fails_closed():
    res = check_var_limit(Decimal("-0.01"), Decimal("0.03"))
    assert res.within_limit is False
    assert res.var_estimate is None


def test_non_numeric_var_fails_closed():
    res = check_var_limit("not-a-number", Decimal("0.03"))
    assert res.within_limit is False
    assert res.var_estimate is None


def test_none_limit_fails_closed():
    res = check_var_limit(Decimal("0.01"), None)
    assert res.within_limit is False
    assert res.limit is None
    assert res.utilization is None
    assert "invalid VaR limit" in res.reason
    # A valid VaR is still echoed back for diagnostics.
    assert res.var_estimate == Decimal("0.01")


def test_zero_limit_fails_closed():
    res = check_var_limit(Decimal("0.01"), Decimal("0"))
    assert res.within_limit is False
    assert res.limit is None


def test_negative_limit_fails_closed():
    res = check_var_limit(Decimal("0.01"), Decimal("-0.03"))
    assert res.within_limit is False
    assert res.limit is None


def test_invalid_var_echoed_as_none_even_with_bad_limit():
    res = check_var_limit(None, None)
    assert res.within_limit is False
    assert res.var_estimate is None
    assert res.limit is None


# --------------------------------------------------------------------------
# var_fraction_of_equity
# --------------------------------------------------------------------------

def test_var_fraction_basic():
    # $300 VaR on $10,000 equity -> 0.03 fraction.
    frac = var_fraction_of_equity(Decimal("300"), Decimal("10000"))
    assert frac == Decimal("0.03")


def test_var_fraction_zero_var():
    frac = var_fraction_of_equity(Decimal("0"), Decimal("10000"))
    assert frac == Decimal("0")


def test_var_fraction_non_positive_equity_is_none():
    assert var_fraction_of_equity(Decimal("300"), Decimal("0")) is None
    assert var_fraction_of_equity(Decimal("300"), Decimal("-10000")) is None


def test_var_fraction_negative_var_is_none():
    assert var_fraction_of_equity(Decimal("-1"), Decimal("10000")) is None


def test_var_fraction_none_inputs_are_none():
    assert var_fraction_of_equity(None, Decimal("10000")) is None
    assert var_fraction_of_equity(Decimal("300"), None) is None


# --------------------------------------------------------------------------
# check_var_dollars_limit — convenience wrapper
# --------------------------------------------------------------------------

def test_dollars_within_limit():
    # $200 VaR on $10k = 2% <= 3% budget.
    res = check_var_dollars_limit(Decimal("200"), Decimal("10000"), Decimal("0.03"))
    assert res.within_limit is True
    assert res.var_estimate == Decimal("0.02")


def test_dollars_breach():
    # $500 VaR on $10k = 5% > 3% budget.
    res = check_var_dollars_limit(Decimal("500"), Decimal("10000"), Decimal("0.03"))
    assert res.within_limit is False
    assert res.breach_amount == Decimal("0.02")


def test_dollars_bad_equity_fails_closed():
    # Non-positive equity -> conversion None -> fail-closed breach.
    res = check_var_dollars_limit(Decimal("500"), Decimal("0"), Decimal("0.03"))
    assert res.within_limit is False
    assert res.var_estimate is None
    assert "invalid/missing VaR estimate" in res.reason


def test_result_is_frozen():
    res = check_var_limit(Decimal("0.02"), Decimal("0.03"))
    try:
        res.within_limit = False  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("VarLimitResult should be frozen/immutable")


def test_summary_strings_render():
    within = check_var_limit(Decimal("0.02"), Decimal("0.03")).summary()
    assert "WITHIN" in within
    breach = check_var_limit(Decimal("0.05"), Decimal("0.03")).summary()
    assert "BREACH" in breach
    bad = check_var_limit(None, None).summary()
    assert "n/a" in bad
