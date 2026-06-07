"""Tests for apex.risk.beta_hedge — beta estimation and hedge sizing."""

from __future__ import annotations

import math

import pytest

from apex.risk.beta_hedge import BetaHedge, beta, beta_hedge, hedge_ratio

# ----------------------------------------------------------------------
# beta()
# ----------------------------------------------------------------------


def test_beta_perfect_one_to_one():
    # Asset moves exactly with the benchmark => beta 1.0.
    bench = [0.01, -0.02, 0.03, -0.01]
    asset = list(bench)
    assert beta(asset, bench) == pytest.approx(1.0)


def test_beta_double_sensitivity():
    # Asset moves exactly 2x the benchmark => beta 2.0.
    bench = [0.01, -0.02, 0.03, -0.01]
    asset = [2 * x for x in bench]
    assert beta(asset, bench) == pytest.approx(2.0)


def test_beta_inverse():
    # Asset moves opposite the benchmark => negative beta.
    bench = [0.01, -0.02, 0.03, -0.01]
    asset = [-0.5 * x for x in bench]
    assert beta(asset, bench) == pytest.approx(-0.5)


def test_beta_hand_computed():
    # cov / var, hand-verified.
    # bench: [1, 2, 3], mean 2 -> dev [-1, 0, 1], var = 2
    # asset: [1, 3, 2], mean 2 -> dev [-1, 1, 0]
    # cov = (-1*-1) + (1*0) + (0*1) = 1
    # beta = 1 / 2 = 0.5
    asset = [1.0, 3.0, 2.0]
    bench = [1.0, 2.0, 3.0]
    assert beta(asset, bench) == pytest.approx(0.5)


def test_beta_aligns_to_shorter_length():
    # Extra trailing benchmark points are ignored (aligned oldest-first).
    bench = [0.01, -0.02, 0.03, 0.99, 0.99]
    asset = [0.01, -0.02, 0.03]
    assert beta(asset, bench) == pytest.approx(1.0)


def test_beta_insufficient_data_returns_none():
    assert beta([0.01], [0.01]) is None
    assert beta([], []) is None


def test_beta_flat_benchmark_returns_none():
    # Zero benchmark variance => slope undefined => None (fail closed).
    assert beta([0.01, -0.02, 0.03], [0.0, 0.0, 0.0]) is None


# ----------------------------------------------------------------------
# hedge_ratio()
# ----------------------------------------------------------------------


def test_hedge_ratio_full_neutral():
    # Default target 0 => short the full beta.
    assert hedge_ratio(1.3) == pytest.approx(1.3)


def test_hedge_ratio_partial_target():
    # Hedge from beta 1.3 down to a residual target of 0.3.
    assert hedge_ratio(1.3, target_beta=0.3) == pytest.approx(1.0)


def test_hedge_ratio_negative_means_add_exposure():
    # Net-short book (beta -0.5) needs to BUY benchmark exposure.
    assert hedge_ratio(-0.5) == pytest.approx(-0.5)


# ----------------------------------------------------------------------
# beta_hedge()
# ----------------------------------------------------------------------


def test_beta_hedge_full_neutralization():
    bench = [0.01, -0.02, 0.03, -0.01]
    asset = [2 * x for x in bench]  # beta 2.0
    result = beta_hedge(asset, bench, portfolio_value=100_000.0)
    assert isinstance(result, BetaHedge)
    assert result.beta == pytest.approx(2.0)
    assert result.hedge_ratio == pytest.approx(2.0)
    # Short 2x the book value to neutralize beta-2 exposure.
    assert result.hedge_notional == pytest.approx(200_000.0)
    assert result.hedge_units is None


def test_beta_hedge_target_beta():
    bench = [0.01, -0.02, 0.03, -0.01]
    asset = [2 * x for x in bench]  # beta 2.0
    result = beta_hedge(asset, bench, portfolio_value=100_000.0, target_beta=0.5)
    # Hedge from 2.0 down to residual 0.5 => short 1.5x.
    assert result.hedge_ratio == pytest.approx(1.5)
    assert result.hedge_notional == pytest.approx(150_000.0)


def test_beta_hedge_units_from_price():
    bench = [0.01, -0.02, 0.03, -0.01]
    asset = list(bench)  # beta 1.0
    result = beta_hedge(asset, bench, portfolio_value=50_000.0, benchmark_price=500.0)
    assert result.hedge_notional == pytest.approx(50_000.0)
    # 50_000 notional / 500 price = 100 units to short.
    assert result.hedge_units == pytest.approx(100.0)


def test_beta_hedge_negative_beta_adds_exposure():
    bench = [0.01, -0.02, 0.03, -0.01]
    asset = [-1 * x for x in bench]  # beta -1.0
    result = beta_hedge(asset, bench, portfolio_value=10_000.0, benchmark_price=100.0)
    assert result.hedge_ratio == pytest.approx(-1.0)
    # Negative notional/units => LONG the benchmark.
    assert result.hedge_notional == pytest.approx(-10_000.0)
    assert result.hedge_units == pytest.approx(-100.0)


def test_beta_hedge_zero_portfolio_value():
    bench = [0.01, -0.02, 0.03, -0.01]
    asset = list(bench)
    result = beta_hedge(asset, bench, portfolio_value=0.0)
    assert result is not None
    assert result.hedge_notional == 0.0


def test_beta_hedge_negative_portfolio_value_returns_none():
    bench = [0.01, -0.02, 0.03, -0.01]
    asset = list(bench)
    assert beta_hedge(asset, bench, portfolio_value=-1.0) is None


def test_beta_hedge_insufficient_data_returns_none():
    assert beta_hedge([0.01], [0.01], portfolio_value=100.0) is None


def test_beta_hedge_flat_benchmark_returns_none():
    assert beta_hedge([0.01, -0.02, 0.03], [0.0, 0.0, 0.0], portfolio_value=100.0) is None


def test_beta_hedge_zero_price_skips_units():
    bench = [0.01, -0.02, 0.03, -0.01]
    asset = list(bench)
    result = beta_hedge(asset, bench, portfolio_value=1_000.0, benchmark_price=0.0)
    assert result.hedge_units is None


def test_beta_hedge_is_frozen():
    bench = [0.01, -0.02, 0.03, -0.01]
    asset = list(bench)
    result = beta_hedge(asset, bench, portfolio_value=1_000.0)
    with pytest.raises(Exception):
        result.beta = 99.0  # type: ignore[misc]


def test_beta_hedge_deterministic():
    bench = [0.012, -0.008, 0.021, -0.015, 0.004]
    asset = [0.018, -0.011, 0.030, -0.020, 0.006]
    r1 = beta_hedge(asset, bench, portfolio_value=250_000.0, benchmark_price=420.0)
    r2 = beta_hedge(asset, bench, portfolio_value=250_000.0, benchmark_price=420.0)
    assert r1 == r2
    assert math.isfinite(r1.beta)
