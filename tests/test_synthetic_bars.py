"""Tests for apex.data.synthetic_bars — seeded GBM bar generator."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pytest

from apex.core.events import MarketEvent
from apex.core.models import AssetClass, Bar, Symbol
from apex.data.synthetic_bars import (
    generate_bars,
    generate_market_events,
    generate_multi_symbol_bars,
    generate_prices,
)

SYM = Symbol(ticker="TEST", asset_class=AssetClass.EQUITY)
SPY = Symbol(ticker="SPY", asset_class=AssetClass.ETF)


# ------------------------------------------------------------------- prices


def test_prices_match_independent_gbm_formula():
    """generate_prices reproduces the hand-derived GBM recurrence exactly."""
    n, seed, sp, mu, sigma = 5, 7, 100.0, 0.08, 0.20
    got = generate_prices(n, seed=seed, start_price=sp, mu=mu, sigma=sigma)

    # Independent re-derivation (same formula, same seeded generator).
    dt = 1.0 / 252.0
    rng = np.random.default_rng(seed)
    shocks = rng.standard_normal(n)
    log_returns = (mu - 0.5 * sigma * sigma) * dt + sigma * math.sqrt(dt) * shocks
    expected, lp = [], math.log(sp)
    for r in log_returns:
        lp += float(r)
        expected.append(math.exp(lp))

    assert got == pytest.approx(expected, rel=1e-12)
    assert len(got) == n


def test_prices_are_deterministic_per_seed():
    a = generate_prices(20, seed=42)
    b = generate_prices(20, seed=42)
    c = generate_prices(20, seed=43)
    assert a == b
    assert a != c  # different seed → different path


def test_prices_strictly_positive():
    # High vol over many bars must never go non-positive (GBM is multiplicative).
    prices = generate_prices(500, seed=1, sigma=0.9)
    assert all(p > 0 for p in prices)


def test_zero_or_negative_n_returns_empty():
    assert generate_prices(0, seed=1) == []
    assert generate_prices(-5, seed=1) == []
    assert generate_bars(SYM, 0, seed=1) == []
    assert generate_bars(SYM, -3, seed=1) == []


def test_prices_reject_bad_params():
    with pytest.raises(ValueError):
        generate_prices(5, seed=1, start_price=0.0)
    with pytest.raises(ValueError):
        generate_prices(5, seed=1, sigma=-0.1)
    with pytest.raises(ValueError):
        generate_prices(5, seed=1, timeframe="3Day")


# -------------------------------------------------------------------- bars


def test_bars_count_and_types():
    bars = generate_bars(SYM, 10, seed=5)
    assert len(bars) == 10
    assert all(isinstance(b, Bar) for b in bars)
    assert all(isinstance(b.close, Decimal) for b in bars)
    assert all(b.symbol is SYM for b in bars)


def test_bar_ohlc_invariants_hold():
    """For every bar: low <= min(o,c) <= max(o,c) <= high, all positive."""
    bars = generate_bars(SYM, 200, seed=11, sigma=0.5, intrabar_range=0.03)
    for b in bars:
        assert b.low <= min(b.open, b.close)
        assert max(b.open, b.close) <= b.high
        assert b.high >= b.low
        assert b.low > 0
        assert b.volume >= 0


def test_first_bar_opens_at_start_price():
    bars = generate_bars(SYM, 3, seed=9, start_price=100.0)
    assert bars[0].open == Decimal("100.00")
    # Each subsequent bar opens where the previous one closed.
    for prev, cur in zip(bars, bars[1:]):
        assert cur.open == prev.close


def test_timestamps_advance_by_timeframe_and_are_utc():
    start = datetime(2021, 6, 1, tzinfo=timezone.utc)
    bars = generate_bars(SYM, 4, seed=2, timeframe="1Day", start=start)
    assert bars[0].timestamp == start
    for i, b in enumerate(bars):
        assert b.timestamp == start + timedelta(days=i)
        assert b.timestamp.tzinfo is not None


def test_naive_start_assumed_utc():
    naive = datetime(2022, 1, 3, 9, 30)
    bars = generate_bars(SYM, 2, seed=3, start=naive)
    assert bars[0].timestamp == naive.replace(tzinfo=timezone.utc)


def test_hourly_timeframe_steps_one_hour():
    start = datetime(2023, 3, 1, tzinfo=timezone.utc)
    bars = generate_bars(SYM, 3, seed=4, timeframe="1Hour", start=start)
    assert bars[1].timestamp - bars[0].timestamp == timedelta(hours=1)


def test_bars_fully_deterministic():
    a = generate_bars(SYM, 50, seed=99, start=datetime(2020, 1, 1, tzinfo=timezone.utc))
    b = generate_bars(SYM, 50, seed=99, start=datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert a == b


def test_prices_decoupled_from_geometry_seed_stream():
    """Changing intrabar_range must not move the close path (separate RNG stream)."""
    a = generate_bars(SYM, 30, seed=8, intrabar_range=0.0)
    b = generate_bars(SYM, 30, seed=8, intrabar_range=0.05)
    assert [x.close for x in a] == [x.close for x in b]


def test_zero_intrabar_range_makes_high_low_bracket_body_exactly():
    bars = generate_bars(SYM, 20, seed=6, intrabar_range=0.0)
    for b in bars:
        assert b.high == max(b.open, b.close)
        assert b.low == min(b.open, b.close)


def test_bars_reject_bad_geometry_params():
    with pytest.raises(ValueError):
        generate_bars(SYM, 5, seed=1, intrabar_range=-0.01)
    with pytest.raises(ValueError):
        generate_bars(SYM, 5, seed=1, base_volume=-1.0)


# ----------------------------------------------------------------- events


def test_market_events_wrap_bars():
    events = generate_market_events(SYM, 7, seed=1, mu=0.1)
    bars = generate_bars(SYM, 7, seed=1, mu=0.1)
    assert len(events) == 7
    assert all(isinstance(e, MarketEvent) for e in events)
    assert [e.bar for e in events] == bars


# ----------------------------------------------------------- multi-symbol


def test_multi_symbol_merged_and_sorted():
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    merged = generate_multi_symbol_bars([SYM, SPY], 5, seed=1, start=start)
    assert len(merged) == 10
    # Chronologically non-decreasing.
    ts = [b.timestamp for b in merged]
    assert ts == sorted(ts)
    # Both symbols present.
    tickers = {b.symbol.ticker for b in merged}
    assert tickers == {"TEST", "SPY"}


def test_multi_symbol_paths_differ_but_deterministic():
    a = generate_multi_symbol_bars([SYM, SPY], 10, seed=5)
    b = generate_multi_symbol_bars([SYM, SPY], 10, seed=5)
    assert a == b
    test_closes = [x.close for x in a if x.symbol.ticker == "TEST"]
    spy_closes = [x.close for x in a if x.symbol.ticker == "SPY"]
    assert test_closes != spy_closes  # distinct seeds → distinct paths
