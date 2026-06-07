"""Tests for apex.validation.kelly_criterion (hand-computed values + edges)."""
from __future__ import annotations

import math

import pytest

from apex.validation.kelly_criterion import (
    DEFAULT_KELLY_FRACTION,
    KellyResult,
    full_kelly_fraction,
    kelly_from_returns,
    kelly_from_win_rate,
)

# --- full_kelly_fraction: classic f* = p - q/b ----------------------------------

def test_full_kelly_known_value():
    # p=0.6, q=0.4, b=2.0 -> 0.6 - 0.4/2 = 0.6 - 0.2 = 0.4
    assert full_kelly_fraction(0.6, 2.0) == pytest.approx(0.4)


def test_full_kelly_even_money_coin_no_edge():
    # Fair 50/50 at 1:1 payoff -> 0.5 - 0.5/1 = 0.0 (no edge, don't bet)
    assert full_kelly_fraction(0.5, 1.0) == pytest.approx(0.0)


def test_full_kelly_negative_edge():
    # p=0.4, q=0.6, b=1.0 -> 0.4 - 0.6 = -0.2 (negative expectancy)
    assert full_kelly_fraction(0.4, 1.0) == pytest.approx(-0.2)


def test_full_kelly_big_payoff():
    # p=0.5, q=0.5, b=3.0 -> 0.5 - 0.5/3 = 0.3333...
    assert full_kelly_fraction(0.5, 3.0) == pytest.approx(1.0 / 3.0)


@pytest.mark.parametrize("wr", [-0.01, 1.01, float("nan"), float("inf")])
def test_full_kelly_rejects_bad_win_rate(wr):
    assert full_kelly_fraction(wr, 2.0) is None


@pytest.mark.parametrize("b", [0.0, -1.0, float("nan"), float("inf")])
def test_full_kelly_rejects_bad_payoff(b):
    assert full_kelly_fraction(0.6, b) is None


# --- kelly_from_win_rate: full + fractional + clamping --------------------------

def test_kelly_from_win_rate_default_half():
    r = kelly_from_win_rate(0.6, 2.0)
    assert isinstance(r, KellyResult)
    assert r.full_kelly == pytest.approx(0.4)
    assert r.kelly_fraction == pytest.approx(DEFAULT_KELLY_FRACTION)
    # half-Kelly: 0.4 * 0.5 = 0.2
    assert r.fractional_kelly == pytest.approx(0.2)
    assert r.edge is True


def test_kelly_from_win_rate_custom_fraction():
    r = kelly_from_win_rate(0.6, 2.0, kelly_fraction=0.25)
    # quarter-Kelly: 0.4 * 0.25 = 0.1
    assert r.fractional_kelly == pytest.approx(0.1)
    assert r.kelly_fraction == pytest.approx(0.25)


def test_kelly_negative_edge_deploys_zero():
    r = kelly_from_win_rate(0.4, 1.0)  # f* = -0.2
    assert r.full_kelly == pytest.approx(-0.2)
    assert r.fractional_kelly == 0.0  # clamped, never recommend a negative-edge bet
    assert r.edge is False


def test_kelly_fraction_clamped_to_unit():
    # kelly_fraction > 1 is refused -> clamped to 1.0
    r = kelly_from_win_rate(0.6, 2.0, kelly_fraction=5.0)
    assert r.kelly_fraction == 1.0
    assert r.fractional_kelly == pytest.approx(0.4)


def test_kelly_deployed_never_exceeds_one():
    # From win/loss stats f* = p - q/b <= p <= 1, so it can't exceed 1, but it
    # CAN approach 1 with a strong edge; deployed must never exceed 1.0.
    r = kelly_from_win_rate(0.99, 100.0, kelly_fraction=1.0)
    assert r.full_kelly == pytest.approx(0.99 - 0.01 / 100.0)
    assert 0.0 <= r.fractional_kelly <= 1.0


def test_kelly_from_win_rate_bad_input_returns_none():
    assert kelly_from_win_rate(1.5, 2.0) is None
    assert kelly_from_win_rate(0.6, 0.0) is None


# --- kelly_from_returns: continuous mean/variance form -------------------------

def test_kelly_from_returns_known_value():
    # returns [0.1, -0.05, 0.1, -0.05]
    # mean = 0.025 ; population variance:
    #   deviations from mean: 0.075, -0.075, 0.075, -0.075 -> sq = 0.005625 each
    #   pvariance = mean of those = 0.005625
    # f* = 0.025 / 0.005625 = 4.4444...
    rets = [0.1, -0.05, 0.1, -0.05]
    r = kelly_from_returns(rets)
    assert r.full_kelly == pytest.approx(0.025 / 0.005625)
    assert r.edge is True
    # deployed clamped to 1.0 (full_kelly >> 1)
    assert r.fractional_kelly == 1.0


def test_kelly_from_returns_small_edge_within_unit():
    # symmetric small returns -> mean/var modest enough to stay < 1
    rets = [0.01, -0.005, 0.01, -0.005, 0.01, -0.005]
    r = kelly_from_returns(rets, kelly_fraction=1.0)
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / len(rets)
    assert r.full_kelly == pytest.approx(mean / var)


def test_kelly_from_returns_negative_mean_no_edge():
    rets = [-0.02, 0.01, -0.02, 0.01]
    r = kelly_from_returns(rets)
    assert r.full_kelly < 0.0
    assert r.edge is False
    assert r.fractional_kelly == 0.0


def test_kelly_from_returns_too_few_points():
    assert kelly_from_returns([]) is None
    assert kelly_from_returns([0.05]) is None


def test_kelly_from_returns_zero_variance():
    # constant returns -> variance 0 -> can't divide -> None (fail closed)
    assert kelly_from_returns([0.03, 0.03, 0.03]) is None


def test_kelly_from_returns_rejects_non_finite():
    assert kelly_from_returns([0.01, float("nan"), 0.02]) is None
    assert kelly_from_returns([0.01, float("inf"), 0.02]) is None


# --- determinism ----------------------------------------------------------------

def test_determinism_same_inputs_same_outputs():
    a = kelly_from_returns([0.1, -0.05, 0.1, -0.05], kelly_fraction=0.5)
    b = kelly_from_returns([0.1, -0.05, 0.1, -0.05], kelly_fraction=0.5)
    assert a == b


def test_summary_string():
    r = kelly_from_win_rate(0.6, 2.0)
    s = r.summary()
    assert "EDGE" in s
    assert "deploy" in s
    no_edge = kelly_from_win_rate(0.4, 1.0).summary()
    assert "NO EDGE" in no_edge


def test_result_is_frozen():
    r = kelly_from_win_rate(0.6, 2.0)
    with pytest.raises(Exception):
        r.full_kelly = 0.9  # type: ignore[misc]


def test_all_results_finite():
    r = kelly_from_win_rate(0.6, 2.0)
    assert math.isfinite(r.full_kelly)
    assert math.isfinite(r.fractional_kelly)
