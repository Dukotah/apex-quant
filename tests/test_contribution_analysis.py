"""Tests for apex.analytics.contribution_analysis.

Hand-computed known values plus edge cases. Pure and fast.
"""

from __future__ import annotations

import math

import pytest

from apex.analytics.contribution_analysis import (
    contribution_fractions,
    geometric_total_return,
    multi_period_contributions,
    portfolio_return,
    single_period_contributions,
)

# --------------------------------------------------------------------------
# single_period_contributions / portfolio_return
# --------------------------------------------------------------------------


def test_single_period_basic():
    weights = {"AAA": 0.6, "BBB": 0.4}
    returns = {"AAA": 0.10, "BBB": -0.05}
    contribs = single_period_contributions(weights, returns)
    # 0.6 * 0.10 = 0.06 ; 0.4 * -0.05 = -0.02
    assert contribs["AAA"] == pytest.approx(0.06)
    assert contribs["BBB"] == pytest.approx(-0.02)


def test_portfolio_return_is_sum_of_contributions():
    weights = {"AAA": 0.6, "BBB": 0.4}
    returns = {"AAA": 0.10, "BBB": -0.05}
    pr = portfolio_return(weights, returns)
    assert pr == pytest.approx(0.06 - 0.02)  # = 0.04
    assert pr == pytest.approx(math.fsum(single_period_contributions(weights, returns).values()))


def test_missing_return_treated_as_zero():
    weights = {"AAA": 0.5, "BBB": 0.5}
    returns = {"AAA": 0.20}  # BBB missing
    contribs = single_period_contributions(weights, returns)
    assert contribs["AAA"] == pytest.approx(0.10)
    assert contribs["BBB"] == 0.0


def test_empty_weights():
    assert single_period_contributions({}, {"AAA": 0.1}) == {}
    assert portfolio_return({}, {"AAA": 0.1}) == 0.0


# --------------------------------------------------------------------------
# multi_period_contributions + Cariño reconciliation
# --------------------------------------------------------------------------


def test_single_asset_reconciles_to_geometric_total():
    # One asset, full weight each period. Returns +10% then -5%.
    weights = [{"X": 1.0}, {"X": 1.0}]
    returns = [{"X": 0.10}, {"X": -0.05}]
    total = geometric_total_return(weights, returns)
    # 1.10 * 0.95 - 1 = 0.045
    assert total == pytest.approx(0.045)
    contribs = multi_period_contributions(weights, returns)
    # Single asset => its contribution must equal the whole geometric total.
    assert contribs["X"] == pytest.approx(0.045)


def test_multi_asset_contributions_sum_to_geometric_total():
    weights = [
        {"AAA": 0.5, "BBB": 0.5},
        {"AAA": 0.7, "BBB": 0.3},
    ]
    returns = [
        {"AAA": 0.08, "BBB": -0.02},
        {"AAA": -0.04, "BBB": 0.06},
    ]
    total = geometric_total_return(weights, returns)
    contribs = multi_period_contributions(weights, returns)
    # The Cariño linking guarantees exact reconciliation.
    assert math.fsum(contribs.values()) == pytest.approx(total)
    # Both assets present.
    assert set(contribs) == {"AAA", "BBB"}


def test_zero_total_return_degenerate():
    # +10% then a return that exactly undoes it: 1.10 * x = 1 => x = 1/1.10 - 1
    r2 = 1.0 / 1.10 - 1.0
    weights = [{"X": 1.0}, {"X": 1.0}]
    returns = [{"X": 0.10}, {"X": r2}]
    total = geometric_total_return(weights, returns)
    assert total == pytest.approx(0.0, abs=1e-12)
    contribs = multi_period_contributions(weights, returns)
    assert math.fsum(contribs.values()) == pytest.approx(0.0, abs=1e-12)


def test_asset_appearing_in_only_one_period():
    weights = [{"AAA": 1.0}, {"AAA": 0.5, "NEW": 0.5}]
    returns = [{"AAA": 0.05}, {"AAA": 0.02, "NEW": 0.10}]
    contribs = multi_period_contributions(weights, returns)
    total = geometric_total_return(weights, returns)
    assert set(contribs) == {"AAA", "NEW"}
    assert math.fsum(contribs.values()) == pytest.approx(total)


def test_multi_period_empty_and_mismatched():
    assert multi_period_contributions([], []) == {}
    assert multi_period_contributions([{"X": 1.0}], []) == {}
    assert multi_period_contributions([{"X": 1.0}], [{"X": 0.1}, {"X": 0.2}]) == {}
    assert geometric_total_return([], []) == 0.0
    assert geometric_total_return([{"X": 1.0}], []) == 0.0


def test_single_period_via_multi_matches_arithmetic():
    # With one period, Cariño scale is 1.0, so contributions equal arithmetic.
    weights = [{"AAA": 0.6, "BBB": 0.4}]
    returns = [{"AAA": 0.10, "BBB": -0.05}]
    multi = multi_period_contributions(weights, returns)
    single = single_period_contributions(weights[0], returns[0])
    assert multi["AAA"] == pytest.approx(single["AAA"])
    assert multi["BBB"] == pytest.approx(single["BBB"])


# --------------------------------------------------------------------------
# contribution_fractions
# --------------------------------------------------------------------------


def test_contribution_fractions_basic():
    fracs = contribution_fractions({"AAA": 0.06, "BBB": 0.02})
    assert fracs["AAA"] == pytest.approx(0.75)
    assert fracs["BBB"] == pytest.approx(0.25)
    assert math.fsum(fracs.values()) == pytest.approx(1.0)


def test_contribution_fractions_empty():
    assert contribution_fractions({}) == {}


def test_contribution_fractions_zero_total():
    # Contributions cancel to zero -> shares undefined -> all zero (fail closed).
    fracs = contribution_fractions({"AAA": 0.05, "BBB": -0.05})
    assert fracs == {"AAA": 0.0, "BBB": 0.0}


def test_contribution_fractions_with_negative():
    fracs = contribution_fractions({"AAA": 0.10, "BBB": -0.04})
    # total = 0.06 ; AAA share > 1, BBB share negative, still sums to 1.
    assert fracs["AAA"] == pytest.approx(0.10 / 0.06)
    assert fracs["BBB"] == pytest.approx(-0.04 / 0.06)
    assert math.fsum(fracs.values()) == pytest.approx(1.0)
