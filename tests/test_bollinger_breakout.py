"""
Tests for apex.strategy.library.bollinger_breakout.

Pure, fast, deterministic. Hand-computed band values where feasible plus
position-awareness / warmup / stop-fallback edge cases. The strategy is
imported by full path so no package __init__ edit is needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Position, Symbol
from apex.strategy import indicators as ind
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.bollinger_breakout import BollingerBreakoutStrategy

SYM = Symbol(ticker="TEST", asset_class=AssetClass.ETF)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_bar(
    close: float, *, i: int, high: Optional[float] = None, low: Optional[float] = None
) -> Bar:
    """A 1-day bar at day i. high/low default to a small band around close."""
    c = Decimal(str(close))
    h = Decimal(str(high)) if high is not None else c + Decimal("0.5")
    lo = Decimal(str(low)) if low is not None else c - Decimal("0.5")
    return Bar(
        symbol=SYM,
        timestamp=T0 + timedelta(days=i),
        open=c,
        high=h,
        low=lo,
        close=c,
        volume=Decimal("1000"),
    )


def feed(strat: BollingerBreakoutStrategy, closes: List[float]) -> List:
    """Feed a sequence of closes; return signals from the LAST bar."""
    sig: List = []
    for i, c in enumerate(closes):
        sig = strat.on_bar(make_bar(c, i=i))
    return sig


# --------------------------------------------------------------------------
# warmup
# --------------------------------------------------------------------------


def test_warmup_returns_no_signals():
    strat = BollingerBreakoutStrategy("bb", [SYM], period=3, num_std=1.0)
    # Only 2 closes < period=3 -> still warming up.
    assert strat.on_bar(make_bar(10.0, i=0)) == []
    assert strat.on_bar(make_bar(10.0, i=1)) == []


# --------------------------------------------------------------------------
# entry: break above the upper band (hand-computed)
# --------------------------------------------------------------------------


def test_break_above_upper_band_emits_buy():
    # period=3, num_std=1.0. Baseline flat [10,10,10] -> upper==middle==10.
    # 4th close 11: window [10,10,11], mean=10.3333, pstdev computed below,
    # upper ~10.804 -> close 11 breaks above -> BUY.
    strat = BollingerBreakoutStrategy("bb", [SYM], period=3, num_std=1.0)
    # confirm the hand math against the indicator source of truth
    upper, middle, _ = ind.bollinger_bands([10.0, 10.0, 11.0], 3, 1.0)
    assert upper[-1] is not None and 11.0 > upper[-1]

    sig = feed(strat, [10.0, 10.0, 10.0, 11.0])
    assert len(sig) == 1
    assert sig[0].side is OrderSide.BUY
    assert sig[0].strategy_id == "bb"
    # every BUY must carry a stop strictly below entry
    assert sig[0].suggested_stop_loss is not None
    assert sig[0].suggested_stop_loss < Decimal("11")


def test_no_break_no_signal_when_flat():
    # Flat baseline then a close equal to baseline -> not above upper -> no signal.
    strat = BollingerBreakoutStrategy("bb", [SYM], period=3, num_std=1.0)
    sig = feed(strat, [10.0, 10.0, 10.0, 10.0])
    assert sig == []


# --------------------------------------------------------------------------
# position awareness: never pyramid; exit at middle band
# --------------------------------------------------------------------------


def _ctx_holding() -> StrategyContext:
    ctx = StrategyContext()
    pos = Position(
        symbol=SYM,
        quantity=Decimal("10"),
        avg_entry_price=Decimal("11"),
        current_price=Decimal("11"),
    )
    ctx.sync_state(positions={SYM.ticker: pos})
    return ctx


def test_no_pyramid_when_already_held():
    # Already long; another breakout bar must NOT emit a second BUY.
    strat = BollingerBreakoutStrategy("bb", [SYM], period=3, num_std=1.0)
    strat.bind_context(_ctx_holding())
    sig = feed(strat, [10.0, 10.0, 10.0, 12.0])
    assert sig == []


def test_exit_when_reverts_to_middle_band():
    # Held; close falls back to/below the middle band -> SELL (full exit).
    strat = BollingerBreakoutStrategy("bb", [SYM], period=3, num_std=1.0)
    strat.bind_context(_ctx_holding())
    # window [10,11,9]: mean=10 -> close 9 < middle 10 -> exit.
    _, middle, _ = ind.bollinger_bands([10.0, 11.0, 9.0], 3, 1.0)
    assert middle[-1] == pytest.approx(10.0)
    sig = feed(strat, [10.0, 10.0, 11.0, 9.0])
    assert len(sig) == 1
    assert sig[0].side is OrderSide.SELL
    assert sig[0].strength == Decimal("1.0")


def test_held_above_middle_holds_no_signal():
    # Held; close stays above middle band -> hold, no signal (no pyramid, no exit).
    strat = BollingerBreakoutStrategy("bb", [SYM], period=3, num_std=1.0)
    strat.bind_context(_ctx_holding())
    # window [10,10,10.5]: middle ~10.1667, close 10.5 > middle -> hold.
    _, middle, _ = ind.bollinger_bands([10.0, 10.0, 10.5], 3, 1.0)
    assert 10.5 > middle[-1]
    sig = feed(strat, [10.0, 10.0, 10.0, 10.5])
    assert sig == []


def test_flat_context_treated_as_not_held():
    # No context bound -> treated as flat -> a breakout produces a BUY.
    strat = BollingerBreakoutStrategy("bb", [SYM], period=3, num_std=1.0)
    sig = feed(strat, [10.0, 10.0, 10.0, 11.0])
    assert len(sig) == 1 and sig[0].side is OrderSide.BUY


# --------------------------------------------------------------------------
# stop loss: ATR-based once warmed, percentage fallback during warmup
# --------------------------------------------------------------------------


def test_stop_uses_percentage_fallback_during_atr_warmup():
    # period=3 (bands warm fast) but atr_period large so ATR is not ready at entry.
    strat = BollingerBreakoutStrategy(
        "bb",
        [SYM],
        period=3,
        num_std=1.0,
        atr_period=14,
        stop_loss_pct=Decimal("0.05"),
    )
    sig = feed(strat, [10.0, 10.0, 10.0, 11.0])
    assert len(sig) == 1
    entry = Decimal("11")
    expected = entry * (Decimal("1") - Decimal("0.05"))
    assert sig[0].suggested_stop_loss == expected


def test_stop_uses_atr_once_warmed():
    # Small atr_period so ATR is ready by the breakout bar; stop should be
    # ATR-based (entry - atr_mult*ATR), differing from the pct fallback.
    strat = BollingerBreakoutStrategy(
        "bb",
        [SYM],
        period=3,
        num_std=1.0,
        atr_period=2,
        atr_mult=Decimal("2.0"),
        stop_loss_pct=Decimal("0.05"),
    )
    closes = [10.0, 10.0, 10.0, 11.0]
    sig = feed(strat, closes)
    assert len(sig) == 1
    stop = sig[0].suggested_stop_loss
    assert stop is not None and stop < Decimal("11")
    # It must NOT equal the percentage fallback (ATR path was taken).
    pct_fallback = Decimal("11") * (Decimal("1") - Decimal("0.05"))
    assert stop != pct_fallback


# --------------------------------------------------------------------------
# untracked symbol & validation
# --------------------------------------------------------------------------


def test_untracked_symbol_ignored():
    strat = BollingerBreakoutStrategy("bb", [SYM], period=3, num_std=1.0)
    other = Symbol(ticker="OTHER", asset_class=AssetClass.ETF)
    bar = Bar(
        symbol=other,
        timestamp=T0,
        open=Decimal("1"),
        high=Decimal("2"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("1"),
    )
    assert strat.on_bar(bar) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"period": 1},
        {"num_std": 0.0},
        {"atr_period": 0},
        {"atr_mult": Decimal("0")},
        {"stop_loss_pct": Decimal("0")},
        {"stop_loss_pct": Decimal("1")},
    ],
)
def test_invalid_params_raise(kwargs):
    with pytest.raises(ValueError):
        BollingerBreakoutStrategy("bb", [SYM], **kwargs)
