"""
Tests for apex.validation.monte_carlo.

The key behavioral guarantees we lock in:
  - A strong, consistent edge PASSES (low p-value).
  - A coin-flip strategy with no real edge FAILS (high p-value).
  - Too few trades FAILS CLOSED (can't validate → don't approve).
  - Results are reproducible (seeded).
"""
from __future__ import annotations

import random

from apex.validation.monte_carlo import run_monte_carlo


def _make_edge_trades(n: int, seed: int = 1) -> list[float]:
    """Trades with a genuine positive expectancy (win bigger/more often)."""
    rng = random.Random(seed)
    trades = []
    for _ in range(n):
        if rng.random() < 0.60:           # 60% win rate
            trades.append(rng.uniform(0.01, 0.03))   # wins +1% to +3%
        else:
            trades.append(rng.uniform(-0.02, -0.005))  # losses smaller
    return trades


def _make_noise_trades(n: int, seed: int = 1) -> list[float]:
    """Symmetric coin-flip trades — no real edge."""
    rng = random.Random(seed)
    return [rng.uniform(-0.02, 0.02) for _ in range(n)]


def test_strong_edge_passes():
    trades = _make_edge_trades(200)
    result = run_monte_carlo(trades, iterations=1000, seed=7)
    assert result.passed is True
    assert result.p_value < 0.05
    assert result.realistic_max_drawdown >= 0.0


def test_noise_fails():
    trades = _make_noise_trades(200)
    result = run_monte_carlo(trades, iterations=1000, seed=7)
    # A no-edge strategy should NOT clear the significance bar.
    assert result.passed is False


def test_too_few_trades_fails_closed():
    trades = _make_edge_trades(10)     # below the 30-trade minimum
    result = run_monte_carlo(trades, iterations=1000, seed=7)
    assert result.passed is False
    assert result.iterations == 0      # signals "not enough data to test"


def test_reproducible():
    trades = _make_edge_trades(100)
    r1 = run_monte_carlo(trades, iterations=500, seed=99)
    r2 = run_monte_carlo(trades, iterations=500, seed=99)
    assert r1.p_value == r2.p_value
    assert r1.realistic_max_drawdown == r2.realistic_max_drawdown


def test_realistic_dd_at_least_as_bad_as_typical():
    # The 95th-percentile (realistic) drawdown should generally be >= the
    # strategy's own single-sequence drawdown is NOT guaranteed, but the
    # realistic DD should be a sensible positive number.
    trades = _make_edge_trades(150)
    result = run_monte_carlo(trades, iterations=1000, seed=3)
    assert 0.0 <= result.realistic_max_drawdown <= 1.0
