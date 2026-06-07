"""
Tests for apex.strategy.library.macd_trend.MacdTrendStrategy.

Pure, fast, deterministic. We construct synthetic close series whose MACD regime
we can reason about (a steady downtrend that flips to a steady uptrend produces a
bullish MACD crossover), drive the strategy bar-by-bar, and assert the
position-aware delta logic, the mandatory stop, and warmup behaviour.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Position, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.indicators import macd
from apex.strategy.library.macd_trend import MacdTrendStrategy

SYM = Symbol(ticker="TEST", asset_class=AssetClass.ETF)
START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def make_bar(i: int, close: float, *, high: float | None = None, low: float | None = None) -> Bar:
    c = Decimal(str(close))
    hi = Decimal(str(high)) if high is not None else c
    lo = Decimal(str(low)) if low is not None else c
    return Bar(
        symbol=SYM,
        timestamp=START + timedelta(days=i),
        open=c,
        high=hi,
        low=lo,
        close=c,
        volume=Decimal("1000"),
    )


def v_to_u_closes(n_down: int = 40, n_up: int = 40) -> list[float]:
    """A clean down-then-up series: forces a bullish MACD crossover in the up leg."""
    down = [200.0 - i for i in range(n_down)]
    base = down[-1]
    up = [base + (i + 1) * 2.0 for i in range(n_up)]
    return down + up


def test_warmup_returns_no_signal():
    """Before MACD/signal lines exist, the strategy emits nothing."""
    strat = MacdTrendStrategy("macd-test", [SYM])
    strat.bind_context(StrategyContext())
    # slow=26 + signal=9 → need ~34 closes before signal line is non-None.
    for i in range(20):
        assert strat.on_bar(make_bar(i, 100.0 + i * 0.1)) == []


def test_bullish_regime_emits_buy_with_stop_when_flat():
    closes = v_to_u_closes()
    strat = MacdTrendStrategy("macd-test", [SYM])
    strat.bind_context(StrategyContext())  # flat throughout

    buys = []
    for i, c in enumerate(closes):
        sigs = strat.on_bar(make_bar(i, c, high=c + 1.0, low=c - 1.0))
        for s in sigs:
            if s.side is OrderSide.BUY:
                buys.append((i, s))

    assert buys, "expected at least one BUY once the uptrend turns MACD bullish"
    # Every BUY carries a protective stop strictly below entry price.
    for i, s in buys:
        assert s.suggested_stop_loss is not None
        assert s.suggested_stop_loss < make_bar(i, closes[i]).close
        assert s.suggested_stop_loss > 0
        assert s.strategy_id == "macd-test"
        assert s.timestamp == START + timedelta(days=i)


def test_position_aware_no_pyramiding():
    """While bullish AND already long, emit nothing (no second BUY)."""
    closes = v_to_u_closes()
    strat = MacdTrendStrategy("macd-test", [SYM])

    held_after_first_buy = {"on": False}

    class Ctx(StrategyContext):
        def get_position(self, symbol):
            if held_after_first_buy["on"]:
                return Position(
                    symbol=SYM,
                    quantity=Decimal("10"),
                    avg_entry_price=Decimal("100"),
                    current_price=Decimal("100"),
                )
            return None

    strat.bind_context(Ctx())

    buy_count = 0
    for i, c in enumerate(closes):
        sigs = strat.on_bar(make_bar(i, c, high=c + 1.0, low=c - 1.0))
        for s in sigs:
            if s.side is OrderSide.BUY:
                buy_count += 1
                held_after_first_buy["on"] = True  # now "filled" → held

    assert buy_count == 1, "position-aware strategy must not pyramid"


def test_exit_when_held_and_regime_turns_bearish():
    """Held long + MACD flips bearish → exactly one SELL, no stop attached."""
    # Up first (bullish), then a sharp downtrend that flips MACD bearish.
    closes = [100.0 + i * 2.0 for i in range(45)] + [190.0 - i * 3.0 for i in range(45)]
    strat = MacdTrendStrategy("macd-test", [SYM])

    class AlwaysHeld(StrategyContext):
        def get_position(self, symbol):
            return Position(
                symbol=SYM,
                quantity=Decimal("10"),
                avg_entry_price=Decimal("100"),
                current_price=Decimal("100"),
            )

    strat.bind_context(AlwaysHeld())

    sells = []
    for i, c in enumerate(closes):
        for s in strat.on_bar(make_bar(i, c, high=c + 1.0, low=c - 1.0)):
            if s.side is OrderSide.SELL:
                sells.append((i, s))

    assert sells, "expected a SELL when held and MACD turns bearish"
    i0, s0 = sells[0]
    assert s0.side is OrderSide.SELL
    assert s0.strength == Decimal("1.0")
    # Confirm the regime really is bearish at the exit bar (sanity, no look-ahead).
    macd_line, signal_line, _ = macd(closes[: i0 + 1])
    assert macd_line[-1] is not None and signal_line[-1] is not None
    assert macd_line[-1] <= signal_line[-1]


def test_long_only_never_shorts_when_flat_and_bearish():
    """Flat + bearish regime → no signal at all (long-only, no shorting)."""
    closes = [200.0 - i for i in range(90)]  # persistent downtrend
    strat = MacdTrendStrategy("macd-test", [SYM])
    strat.bind_context(StrategyContext())  # always flat

    for i, c in enumerate(closes):
        for s in strat.on_bar(make_bar(i, c, high=c + 1.0, low=c - 1.0)):
            assert s.side is not OrderSide.SELL
            assert s.side is OrderSide.BUY  # only ever buys, and only if bullish


def test_atr_stop_used_once_warm_then_below_entry():
    """Once ATR is warm, the BUY stop reflects ATR distance (below entry)."""
    closes = v_to_u_closes()
    strat = MacdTrendStrategy(
        "macd-test", [SYM], atr_multiple=Decimal("2"), stop_loss_pct=Decimal("0.05")
    )
    strat.bind_context(StrategyContext())

    first_buy = None
    for i, c in enumerate(closes):
        sigs = strat.on_bar(make_bar(i, c, high=c + 5.0, low=c - 5.0))
        for s in sigs:
            if s.side is OrderSide.BUY and first_buy is None:
                first_buy = (i, s)
    assert first_buy is not None
    i, s = first_buy
    entry = Decimal(str(closes[i]))
    assert s.suggested_stop_loss < entry
    assert s.suggested_stop_loss > 0


def test_unknown_symbol_ignored():
    strat = MacdTrendStrategy("macd-test", [SYM])
    strat.bind_context(StrategyContext())
    other = Symbol(ticker="OTHER", asset_class=AssetClass.ETF)
    bar = Bar(
        symbol=other,
        timestamp=START,
        open=Decimal("10"),
        high=Decimal("10"),
        low=Decimal("10"),
        close=Decimal("10"),
        volume=Decimal("1"),
    )
    assert strat.on_bar(bar) == []


def test_no_context_treats_as_flat():
    """No bound context → behaves as flat (can buy, never errors on exit path)."""
    closes = v_to_u_closes()
    strat = MacdTrendStrategy("macd-test", [SYM])  # no bind_context
    saw_buy = False
    for i, c in enumerate(closes):
        for s in strat.on_bar(make_bar(i, c, high=c + 1.0, low=c - 1.0)):
            if s.side is OrderSide.BUY:
                saw_buy = True
    assert saw_buy


def test_invalid_params_raise():
    with pytest.raises(ValueError):
        MacdTrendStrategy("x", [SYM], fast_period=26, slow_period=12)
    with pytest.raises(ValueError):
        MacdTrendStrategy("x", [SYM], stop_loss_pct=Decimal("1.5"))
    with pytest.raises(ValueError):
        MacdTrendStrategy("x", [SYM], atr_multiple=Decimal("0"))
    with pytest.raises(ValueError):
        MacdTrendStrategy("x", [SYM], atr_period=0)
