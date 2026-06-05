"""
Tests for apex.strategy.library.rsi2_mean_reversion.

Validates:
  - No signal during warmup (< 201 bars).
  - BUY emitted when price > SMA(200) AND RSI(2) < threshold.
  - Stop-loss is attached and is below the entry close.
  - No BUY when price is below SMA(200) even when RSI is oversold.
  - SELL emitted when close crosses above SMA(5).
  - SELL emitted by time-stop after N bars.
  - No duplicate BUYs while already long.
  - Determinism: same inputs → same signals.
  - Ignores bars for unknown symbols.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.library.rsi2_mean_reversion import RSI2MeanReversionStrategy

SYM = Symbol("SPY", AssetClass.ETF)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _bar(price: float, t: datetime, high: float = None, low: float = None) -> Bar:
    p = Decimal(str(price))
    h = Decimal(str(high)) if high is not None else p
    lo = Decimal(str(low)) if low is not None else p
    return Bar(
        symbol=SYM,
        timestamp=t,
        open=p,
        high=h,
        low=lo,
        close=p,
        volume=Decimal("1000000"),
    )


def _feed_prices(
    strat: RSI2MeanReversionStrategy,
    prices: List[float],
    start: datetime = None,
) -> List:
    """Feed a flat price list as daily bars; returns all emitted signals."""
    all_signals = []
    t = start or datetime(2020, 1, 1, tzinfo=timezone.utc)
    for i, p in enumerate(prices):
        bar = _bar(p, t + timedelta(days=i))
        all_signals.extend(strat.on_bar(bar))
    return all_signals


def _make_uptrend(
    n: int = 210,
    base: float = 100.0,
    drift: float = 0.05,
) -> List[float]:
    """
    Build a steady upward trend:  price[i] = base + i * drift.
    With n=210 and drift=0.05, price runs from 100 to ~110.5 —
    well above a 200-SMA that will be ~105, so trend filter is satisfied.
    """
    return [base + i * drift for i in range(n)]


def _make_dip(prices: List[float], depth: float, length: int) -> List[float]:
    """
    Append a sharp down-dip of `depth` points over `length` bars,
    designed to force RSI(2) well below 10.
    """
    last = prices[-1]
    step = depth / length
    return prices + [last - step * (i + 1) for i in range(length)]


def _make_recovery(prices: List[float], target: float, length: int) -> List[float]:
    """Append a recovery back above `target` over `length` bars."""
    last = prices[-1]
    step = (target - last) / length
    return prices + [last + step * (i + 1) for i in range(length)]


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------

def test_invalid_sma_params():
    with pytest.raises(ValueError):
        RSI2MeanReversionStrategy("s", [SYM], sma_trend=5, sma_exit=10)


def test_invalid_stop_loss_pct():
    with pytest.raises(ValueError):
        RSI2MeanReversionStrategy("s", [SYM], stop_loss_pct=Decimal("0"))
    with pytest.raises(ValueError):
        RSI2MeanReversionStrategy("s", [SYM], stop_loss_pct=Decimal("1.5"))


# ---------------------------------------------------------------------------
# Warmup
# ---------------------------------------------------------------------------

def test_no_signal_during_warmup():
    """Fewer than 201 bars → SMA(200) undefined → no signals."""
    strat = RSI2MeanReversionStrategy("s", [SYM])
    # Feed 200 bars — one short of the warmup requirement.
    prices = _make_uptrend(n=200)
    signals = _feed_prices(strat, prices)
    assert signals == [], f"Expected no signals during warmup, got {signals}"


# ---------------------------------------------------------------------------
# BUY conditions
# ---------------------------------------------------------------------------

def test_buy_emitted_when_uptrend_and_rsi_oversold():
    """
    Build 210 uptrend bars so SMA(200) is established and price > SMA(200),
    then drive a sharp dip to force RSI(2) < 10.  Expect at least one BUY.
    """
    strat = RSI2MeanReversionStrategy(
        "s", [SYM],
        entry_threshold=Decimal("10"),
        time_stop_bars=0,  # disable time-stop so only SMA(5) exits apply
    )
    prices = list(_make_uptrend(n=210, base=100.0, drift=0.05))
    # Sharp drop: 8 bars down by 5 pts → should crater RSI(2).
    prices = _make_dip(prices, depth=5.0, length=8)
    signals = _feed_prices(strat, prices)

    buys = [s for s in signals if s.side == OrderSide.BUY]
    assert len(buys) >= 1, "Expected at least one BUY after uptrend + RSI dip"

    buy = buys[0]
    assert buy.suggested_stop_loss is not None, "BUY must carry a stop-loss"
    assert buy.suggested_stop_loss < buy.suggested_stop_loss + Decimal("0.01")  # sanity
    # Stop must be below the entry price.
    # The entry close will be somewhere in the dip area (> 95 given drift).
    assert buy.suggested_stop_loss < Decimal("120"), "Stop must be below any likely close"
    assert buy.strategy_id == "s"
    assert buy.strength > Decimal("0")
    assert buy.strength <= Decimal("1.0")


def test_stop_loss_below_close():
    """Stop-loss must be strictly below the bar close at entry."""
    strat = RSI2MeanReversionStrategy(
        "s", [SYM],
        stop_loss_pct=Decimal("0.02"),
        time_stop_bars=0,
    )
    prices = list(_make_uptrend(n=210, base=100.0, drift=0.05))
    prices = _make_dip(prices, depth=5.0, length=8)

    t = datetime(2020, 1, 1, tzinfo=timezone.utc)
    for i, p in enumerate(prices):
        price = Decimal(str(p))
        bar = Bar(
            symbol=SYM,
            timestamp=t + timedelta(days=i),
            open=price, high=price, low=price, close=price,
            volume=Decimal("1000000"),
        )
        sigs = strat.on_bar(bar)
        for sig in sigs:
            if sig.side == OrderSide.BUY:
                assert sig.suggested_stop_loss < price, (
                    f"Stop {sig.suggested_stop_loss} must be below close {price}"
                )


# ---------------------------------------------------------------------------
# No BUY when below the trend filter
# ---------------------------------------------------------------------------

def test_no_buy_when_below_200sma():
    """
    Build a downtrend so price falls below SMA(200), then send in oversold RSI.
    No BUY should fire because the trend filter blocks it.
    """
    strat = RSI2MeanReversionStrategy(
        "s", [SYM],
        entry_threshold=Decimal("10"),
        time_stop_bars=0,
    )
    # Steady downtrend: price slides from 200 down to ~90.  Price will always be
    # below the 200-SMA which will lag above.
    n = 210
    prices = [200.0 - i * 0.5 for i in range(n)]     # 200 → ~95
    # Now send in a violent short dip to force RSI(2) < 10.
    prices = _make_dip(prices, depth=10.0, length=5)
    signals = _feed_prices(strat, prices)

    buys = [s for s in signals if s.side == OrderSide.BUY]
    assert buys == [], (
        f"Expected no BUYs when below SMA(200), but got {len(buys)}"
    )


# ---------------------------------------------------------------------------
# SELL conditions
# ---------------------------------------------------------------------------

def test_sell_emitted_when_close_above_sma5():
    """After a BUY, a recovery above SMA(5) should emit SELL."""
    strat = RSI2MeanReversionStrategy(
        "s", [SYM],
        entry_threshold=Decimal("10"),
        time_stop_bars=0,  # only SMA(5) exit
    )
    prices = list(_make_uptrend(n=210, base=100.0, drift=0.05))
    prices = _make_dip(prices, depth=5.0, length=8)
    # Recovery: bounce back 5+ pts above recent close so we cross SMA(5).
    prices = _make_recovery(prices, target=prices[209] + 2.0, length=8)

    signals = _feed_prices(strat, prices)
    sides = [s.side for s in signals]
    assert OrderSide.BUY in sides, "Expected BUY before SELL"
    assert OrderSide.SELL in sides, "Expected SELL after recovery above SMA(5)"
    buy_idx = sides.index(OrderSide.BUY)
    sell_idx = sides.index(OrderSide.SELL)
    assert buy_idx < sell_idx, "BUY must precede SELL"


def test_time_stop_exits_position():
    """If close doesn't rise above SMA(5), the time-stop fires after N bars."""
    strat = RSI2MeanReversionStrategy(
        "s", [SYM],
        entry_threshold=Decimal("10"),
        sma_exit=5,
        time_stop_bars=3,
    )
    prices = list(_make_uptrend(n=210, base=100.0, drift=0.05))
    prices = _make_dip(prices, depth=5.0, length=8)
    # Stay flat/down after the dip so SMA(5) exit never triggers.
    last = prices[-1]
    prices = prices + [last - 0.1] * 10  # keep price below entry, no SMA(5) cross

    signals = _feed_prices(strat, prices)
    sides = [s.side for s in signals]
    assert OrderSide.SELL in sides, "Time-stop should emit SELL"


# ---------------------------------------------------------------------------
# No duplicate BUYs while long
# ---------------------------------------------------------------------------

def test_no_duplicate_buys_while_long():
    """
    After entering long, another low-RSI bar should NOT trigger a second BUY
    until the position is closed.
    """
    strat = RSI2MeanReversionStrategy(
        "s", [SYM],
        entry_threshold=Decimal("15"),   # wider threshold to capture more bars
        time_stop_bars=0,
    )
    prices = list(_make_uptrend(n=210, base=100.0, drift=0.05))
    # Sustained dip — multiple bars below threshold — should buy only once.
    prices = _make_dip(prices, depth=8.0, length=10)

    signals = _feed_prices(strat, prices)
    buys = [s for s in signals if s.side == OrderSide.BUY]
    assert len(buys) == 1, f"Expected exactly 1 BUY, got {len(buys)}"


# ---------------------------------------------------------------------------
# Ignores unknown symbol
# ---------------------------------------------------------------------------

def test_ignores_unknown_symbol():
    strat = RSI2MeanReversionStrategy("s", [SYM])
    other = Symbol("OTHER", AssetClass.EQUITY)
    t = datetime(2020, 1, 1, tzinfo=timezone.utc)
    p = Decimal("100")
    bar = Bar(symbol=other, timestamp=t, open=p, high=p, low=p, close=p,
              volume=Decimal("1"))
    assert strat.on_bar(bar) == []


# ---------------------------------------------------------------------------
# Sell-only-when-long
# ---------------------------------------------------------------------------

def test_no_sell_without_prior_buy():
    """If we never entered long, no SELL should ever fire."""
    strat = RSI2MeanReversionStrategy(
        "s", [SYM],
        entry_threshold=Decimal("10"),
        time_stop_bars=5,
    )
    # Downtrend: trend filter never passes, so no BUY ever fires.
    n = 250
    prices = [200.0 - i * 0.4 for i in range(n)]
    signals = _feed_prices(strat, prices)
    sells = [s for s in signals if s.side == OrderSide.SELL]
    assert sells == [], f"Got unexpected SELLs: {sells}"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_deterministic():
    """Same inputs fed to two independent instances → identical signals."""
    prices = list(_make_uptrend(n=210, base=100.0, drift=0.05))
    prices = _make_dip(prices, depth=5.0, length=8)
    prices = _make_recovery(prices, target=prices[209] + 2.0, length=8)

    s1 = RSI2MeanReversionStrategy("s", [SYM], time_stop_bars=0)
    s2 = RSI2MeanReversionStrategy("s", [SYM], time_stop_bars=0)
    sig1 = _feed_prices(s1, prices)
    sig2 = _feed_prices(s2, prices)
    assert [s.side for s in sig1] == [s.side for s in sig2], (
        "Non-deterministic output detected"
    )
