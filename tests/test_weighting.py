"""
tests.test_weighting
=====================
Tests for apex.strategy.weighting — pure portfolio-weighting helpers.

Math is checked against hand-computed values and the contract is enforced:
non-negative Decimal weights summing EXACTLY to the cap, deterministic output,
and graceful edge-case handling (empty / single / zero-None-NaN vol).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apex.core.models import AssetClass, Symbol
from apex.strategy.weighting import (
    correlation_down_weight,
    equal_weight,
    inverse_vol_weight,
    risk_parity_weight,
)


def _sym(ticker: str) -> Symbol:
    return Symbol(ticker=ticker, asset_class=AssetClass.EQUITY)


def _assert_sums_to(weights, cap: Decimal) -> None:
    assert sum(weights.values(), Decimal("0")) == cap
    assert all(w >= 0 for w in weights.values())


# --------------------------------------------------------------------------- #
# equal_weight
# --------------------------------------------------------------------------- #


def test_equal_weight_sums_to_one():
    w = equal_weight([_sym("SPY"), _sym("TLT"), _sym("GLD"), _sym("DBC")])
    _assert_sums_to(w, Decimal("1"))
    assert set(w) == {"SPY", "TLT", "GLD", "DBC"}


def test_equal_weight_exact_with_indivisible_count():
    # 1/3 is non-terminating; the contract still requires an EXACT sum of 1.
    w = equal_weight([_sym("A"), _sym("B"), _sym("C")])
    _assert_sums_to(w, Decimal("1"))


def test_equal_weight_accepts_plain_strings():
    w = equal_weight(["SPY", "TLT"])
    assert w == {"SPY": Decimal("0.5"), "TLT": Decimal("0.5")}


def test_equal_weight_empty_is_empty():
    assert equal_weight([]) == {}


def test_equal_weight_single_gets_full_cap():
    assert equal_weight([_sym("SPY")]) == {"SPY": Decimal("1")}


def test_equal_weight_dedupes_tickers():
    w = equal_weight([_sym("SPY"), _sym("SPY"), _sym("TLT")])
    assert set(w) == {"SPY", "TLT"}
    _assert_sums_to(w, Decimal("1"))


def test_equal_weight_respects_cap():
    w = equal_weight([_sym("A"), _sym("B")], cap=Decimal("0.5"))
    _assert_sums_to(w, Decimal("0.5"))
    assert w == {"A": Decimal("0.25"), "B": Decimal("0.25")}


# --------------------------------------------------------------------------- #
# inverse_vol_weight
# --------------------------------------------------------------------------- #


def test_inverse_vol_known_values():
    # vols 0.1 and 0.2 -> raw 10 and 5 -> normalized 2/3 and 1/3.
    w = inverse_vol_weight({"LOW": 0.1, "HIGH": 0.2})
    _assert_sums_to(w, Decimal("1"))
    # LOW should be ~double HIGH (the exact-sum residue lands on the larger weight,
    # so the ratio is 2 up to a sub-quantum rounding remainder).
    assert abs(w["LOW"] - 2 * w["HIGH"]) < Decimal("1e-20")
    assert w["LOW"] > w["HIGH"]


def test_inverse_vol_higher_vol_gets_strictly_lower_weight():
    w = inverse_vol_weight({"CALM": 0.05, "MID": 0.10, "WILD": 0.30})
    _assert_sums_to(w, Decimal("1"))
    assert w["CALM"] > w["MID"] > w["WILD"]


def test_inverse_vol_skips_zero_none_and_nan():
    w = inverse_vol_weight(
        {"GOOD": 0.1, "ZERO": 0.0, "NONE": None, "NAN": float("nan"), "NEG": -0.2}
    )
    # Only the usable sleeve survives and takes the full cap.
    assert w == {"GOOD": Decimal("1")}


def test_inverse_vol_empty_is_empty():
    assert inverse_vol_weight({}) == {}


def test_inverse_vol_single_gets_full_cap():
    assert inverse_vol_weight({"SPY": 0.15}) == {"SPY": Decimal("1")}


def test_inverse_vol_all_unusable_falls_back_to_equal():
    w = inverse_vol_weight({"A": 0.0, "B": None})
    _assert_sums_to(w, Decimal("1"))
    assert w == {"A": Decimal("0.5"), "B": Decimal("0.5")}


def test_inverse_vol_respects_cap():
    w = inverse_vol_weight({"LOW": 0.1, "HIGH": 0.2}, cap=Decimal("0.6"))
    _assert_sums_to(w, Decimal("0.6"))


def test_inverse_vol_deterministic():
    vols = {"A": 0.11, "B": 0.07, "C": 0.23}
    assert inverse_vol_weight(vols) == inverse_vol_weight(vols)


# --------------------------------------------------------------------------- #
# risk_parity_weight (diagonal == inverse vol)
# --------------------------------------------------------------------------- #


def test_risk_parity_equals_inverse_vol():
    vols = {"A": 0.1, "B": 0.2, "C": 0.4}
    assert risk_parity_weight(vols) == inverse_vol_weight(vols)


def test_risk_parity_sums_to_one():
    _assert_sums_to(risk_parity_weight({"A": 0.1, "B": 0.2}), Decimal("1"))


# --------------------------------------------------------------------------- #
# correlation_down_weight
# --------------------------------------------------------------------------- #


def test_corr_down_weight_trims_correlated_keeps_diversifier():
    base = {"SPY": Decimal("0.5"), "TLT": Decimal("0.5")}
    # SPY fully correlated (penalized), TLT uncorrelated (kept).
    w = correlation_down_weight(base, {"SPY": 1.0, "TLT": 0.0})
    _assert_sums_to(w, Decimal("1"))
    # The diversifier should end up with strictly more weight than the crowded one.
    assert w["TLT"] > w["SPY"]


def test_corr_down_weight_known_values():
    base = {"A": Decimal("0.5"), "B": Decimal("0.5")}
    # max_penalty 0.5: A corr 1 -> factor 0.5 (raw 0.25); B corr 0 -> factor 1 (raw 0.5).
    # normalized: A = 0.25/0.75 = 1/3, B = 0.5/0.75 = 2/3.
    w = correlation_down_weight(base, {"A": 1.0, "B": 0.0}, max_penalty=Decimal("0.5"))
    _assert_sums_to(w, Decimal("1"))
    assert abs(w["B"] - 2 * w["A"]) < Decimal("1e-20")
    assert w["B"] > w["A"]


def test_corr_down_weight_negative_corr_no_penalty():
    base = {"A": Decimal("0.5"), "B": Decimal("0.5")}
    # Negative correlation clamps to 0 -> no haircut -> weights unchanged.
    w = correlation_down_weight(base, {"A": -0.8, "B": -0.5})
    assert w == {"A": Decimal("0.5"), "B": Decimal("0.5")}


def test_corr_down_weight_missing_corr_treated_uncorrelated():
    base = {"A": Decimal("0.5"), "B": Decimal("0.5")}
    w = correlation_down_weight(base, {"A": 1.0})  # B missing
    _assert_sums_to(w, Decimal("1"))
    assert w["B"] > w["A"]


def test_corr_down_weight_preserves_input_cap_by_default():
    base = {"A": Decimal("0.3"), "B": Decimal("0.3")}  # sum 0.6
    w = correlation_down_weight(base, {"A": 0.5, "B": 0.2})
    _assert_sums_to(w, Decimal("0.6"))


def test_corr_down_weight_explicit_cap():
    base = {"A": Decimal("0.5"), "B": Decimal("0.5")}
    w = correlation_down_weight(base, {"A": 0.5, "B": 0.2}, cap=Decimal("0.4"))
    _assert_sums_to(w, Decimal("0.4"))


def test_corr_down_weight_empty_is_empty():
    assert correlation_down_weight({}, {}) == {}


def test_corr_down_weight_max_penalty_clamped():
    base = {"A": Decimal("0.5"), "B": Decimal("0.5")}
    # max_penalty > 1 clamps to 1; A corr 1 -> factor 0 -> all weight to B.
    w = correlation_down_weight(base, {"A": 1.0, "B": 0.0}, max_penalty=Decimal("5"))
    _assert_sums_to(w, Decimal("1"))
    assert w["B"] == Decimal("1")
    assert w["A"] == Decimal("0")


def test_corr_down_weight_chains_with_inverse_vol():
    iv = inverse_vol_weight({"LOW": 0.1, "HIGH": 0.2})
    w = correlation_down_weight(iv, {"LOW": 1.0, "HIGH": 0.0})
    _assert_sums_to(w, Decimal("1"))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
