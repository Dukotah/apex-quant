"""
Tests for apex.strategy.library.sma_crossover.
Validates the full strategy-hook pipeline: bars → indicator → signal emission.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.library.sma_crossover import SMACrossoverStrategy

SYM = Symbol("TEST", AssetClass.EQUITY)


def _feed_prices(strat: SMACrossoverStrategy, prices: list[float]):
    """Feed a list of closing prices as bars; collect all emitted signals."""
    all_signals = []
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i, p in enumerate(prices):
        price = Decimal(str(p))
        bar = Bar(symbol=SYM, timestamp=t + timedelta(days=i),
                  open=price, high=price, low=price, close=price,
                  volume=Decimal("1000"))
        all_signals.extend(strat.on_bar(bar))
    return all_signals


def test_fast_must_be_less_than_slow():
    with pytest.raises(ValueError):
        SMACrossoverStrategy("s", [SYM], fast_period=50, slow_period=20)


def test_no_signal_during_warmup():
    strat = SMACrossoverStrategy("s", [SYM], fast_period=3, slow_period=5)
    # Fewer than slow_period+1 bars → no signals possible.
    signals = _feed_prices(strat, [10, 11, 12])
    assert signals == []


def test_golden_cross_emits_buy():
    strat = SMACrossoverStrategy("s", [SYM], fast_period=3, slow_period=5)
    # Start flat/declining so fast < slow, then rally so fast crosses above slow.
    prices = [20, 19, 18, 17, 16, 15, 16, 18, 21, 25, 30]
    signals = _feed_prices(strat, prices)
    buys = [s for s in signals if s.side == OrderSide.BUY]
    assert len(buys) >= 1
    assert buys[0].suggested_stop_loss is not None
    # Stop must be below the entry price (a protective stop for a long).
    assert buys[0].suggested_stop_loss < Decimal("30")


def test_death_cross_emits_sell_after_buy():
    strat = SMACrossoverStrategy("s", [SYM], fast_period=3, slow_period=5)
    # Rally (triggers buy), then collapse (triggers sell).
    prices = [16, 15, 14, 13, 12, 13, 15, 18, 22, 26, 30, 28, 24, 20, 16, 12, 8]
    signals = _feed_prices(strat, prices)
    sides = [s.side for s in signals]
    assert OrderSide.BUY in sides
    assert OrderSide.SELL in sides
    # Buy must come before sell.
    assert sides.index(OrderSide.BUY) < sides.index(OrderSide.SELL)


def test_no_duplicate_buys_while_long():
    strat = SMACrossoverStrategy("s", [SYM], fast_period=3, slow_period=5)
    # Sustained uptrend — should buy once, then stay long (no repeated buys).
    prices = [16, 15, 14, 13, 12, 13, 15, 18, 22, 26, 30, 34, 38, 42, 46]
    signals = _feed_prices(strat, prices)
    buys = [s for s in signals if s.side == OrderSide.BUY]
    assert len(buys) == 1   # entered long once, no duplicates


def test_ignores_unknown_symbol():
    strat = SMACrossoverStrategy("s", [SYM], fast_period=3, slow_period=5)
    other = Symbol("OTHER", AssetClass.EQUITY)
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bar = Bar(symbol=other, timestamp=t, open=Decimal("1"), high=Decimal("1"),
              low=Decimal("1"), close=Decimal("1"), volume=Decimal("1"))
    assert strat.on_bar(bar) == []


def test_deterministic():
    prices = [16, 15, 14, 13, 12, 13, 15, 18, 22, 26, 30, 28, 24, 20]
    s1 = SMACrossoverStrategy("s", [SYM], fast_period=3, slow_period=5)
    s2 = SMACrossoverStrategy("s", [SYM], fast_period=3, slow_period=5)
    sig1 = _feed_prices(s1, prices)
    sig2 = _feed_prices(s2, prices)
    # Same inputs → same number and side of signals.
    assert [s.side for s in sig1] == [s.side for s in sig2]
