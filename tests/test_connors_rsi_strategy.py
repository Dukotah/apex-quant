"""
Tests for apex.strategy.library.connors_rsi_strategy.ConnorsRSIStrategy.

Pure, fast, deterministic. Covers:
  - private helpers (streak series, percent-rank) against hand-computed values
  - warmup returns no signals
  - a deep oversold decline fires a BUY that ALWAYS carries a stop
  - ATR-based stop vs. percentage fallback during ATR warmup
  - position-awareness: held + oversold does NOT pyramid; held + recovery exits
  - long-only: never emits a naked short
  - off-universe symbols are ignored
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Position, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.connors_rsi_strategy import ConnorsRSIStrategy

SYM = Symbol(ticker="TEST", asset_class=AssetClass.EQUITY)
OTHER = Symbol(ticker="NOPE", asset_class=AssetClass.EQUITY)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bar(close: float, i: int, high: Optional[float] = None,
         low: Optional[float] = None, symbol: Symbol = SYM) -> Bar:
    c = Decimal(str(close))
    h = Decimal(str(high)) if high is not None else c
    lo = Decimal(str(low)) if low is not None else c
    return Bar(
        symbol=symbol,
        timestamp=T0 + timedelta(days=i),
        open=c,
        high=h,
        low=lo,
        close=c,
        volume=Decimal("1000"),
        timeframe="1Day",
    )


def _ctx_holding(qty: float) -> StrategyContext:
    ctx = StrategyContext()
    if qty != 0:
        pos = Position(
            symbol=SYM,
            quantity=Decimal(str(qty)),
            avg_entry_price=Decimal("100"),
            current_price=Decimal("100"),
        )
        ctx.sync_state(positions={SYM.ticker: pos})
    return ctx


def _feed(strat: ConnorsRSIStrategy, closes: List[float],
          highs: Optional[List[float]] = None,
          lows: Optional[List[float]] = None) -> List:
    """Feed a price path; return the signals from the LAST bar only."""
    sigs: List = []
    for i, c in enumerate(closes):
        h = highs[i] if highs is not None else None
        lo = lows[i] if lows is not None else None
        sigs = strat.on_bar(_bar(c, i, high=h, low=lo))
    return sigs


# ---- private helpers -------------------------------------------------------

def test_streak_series_hand_computed():
    s = ConnorsRSIStrategy("t", [SYM])
    # closes: 10,11,12,11,11,13  -> diffs: +,+,-,0,+
    closes = [10.0, 11.0, 12.0, 11.0, 11.0, 13.0]
    streak = s._streak_series(closes)
    assert streak == [0.0, 1.0, 2.0, -1.0, 0.0, 1.0]


def test_streak_flips_sign_not_accumulate():
    s = ConnorsRSIStrategy("t", [SYM])
    # down, down, up -> -1, -2, then flip to +1
    closes = [10.0, 9.0, 8.0, 9.0]
    assert s._streak_series(closes) == [0.0, -1.0, -2.0, 1.0]


def test_percent_rank_hand_computed():
    s = ConnorsRSIStrategy("t", [SYM], rank_window=10)
    # returns where the last value is the smallest -> rank 0
    rets = [0.05, 0.02, -0.01, 0.03, -0.10]
    # priors = [0.05,0.02,-0.01,0.03]; none < -0.10 -> 0.0
    assert s._percent_rank(rets) == pytest.approx(0.0)
    # last value largest -> all 4 priors < it -> 100
    rets2 = [0.05, 0.02, -0.01, 0.03, 0.99]
    assert s._percent_rank(rets2) == pytest.approx(100.0)
    # last value mid: priors [0.0,0.10,-0.10,0.50], current 0.05 -> 2 of 4 < -> 50
    rets3 = [0.0, 0.10, -0.10, 0.50, 0.05]
    assert s._percent_rank(rets3) == pytest.approx(50.0)


def test_percent_rank_insufficient_data_returns_none():
    s = ConnorsRSIStrategy("t", [SYM])
    assert s._percent_rank([]) is None
    assert s._percent_rank([0.01]) is None


# ---- warmup ----------------------------------------------------------------

def test_warmup_returns_no_signals():
    s = ConnorsRSIStrategy("t", [SYM], rsi_period=3, rank_window=20)
    s.bind_context(_ctx_holding(0))
    # Far fewer bars than rank_window+rsi needs -> composite is None -> no signal.
    sigs = _feed(s, [100.0, 99.0, 98.0])
    assert sigs == []


# ---- entry -----------------------------------------------------------------

def test_deep_decline_fires_buy_with_stop():
    s = ConnorsRSIStrategy(
        "t", [SYM], rsi_period=3, streak_rsi_period=2,
        rank_window=15, entry_threshold=10.0,
    )
    s.bind_context(_ctx_holding(0))
    # Strictly monotonic decline: price RSI -> ~0, streak RSI -> ~0, and the last
    # return is among the lowest -> composite deeply oversold -> BUY.
    closes = [100.0 - i for i in range(25)]
    sigs = _feed(s, closes)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.side == OrderSide.BUY
    assert sig.strategy_id == "t"
    # MANDATORY stop attached, positive, and below entry.
    assert sig.suggested_stop_loss is not None
    assert sig.suggested_stop_loss > 0
    assert sig.suggested_stop_loss < closes[-1]
    assert Decimal("0") < sig.strength <= Decimal("1")
    assert sig.timestamp is not None


def test_no_entry_when_not_oversold():
    s = ConnorsRSIStrategy(
        "t", [SYM], rsi_period=3, rank_window=15, entry_threshold=10.0,
    )
    s.bind_context(_ctx_holding(0))
    # Strictly rising path -> composite high (not oversold) -> no BUY.
    closes = [100.0 + i for i in range(25)]
    sigs = _feed(s, closes)
    assert sigs == []


def test_atr_stop_used_after_warmup_else_pct_fallback():
    # With atr_period large relative to bars, ATR is None -> pct fallback.
    s_pct = ConnorsRSIStrategy(
        "t", [SYM], rsi_period=3, streak_rsi_period=2, rank_window=15,
        entry_threshold=10.0, atr_period=50, stop_loss_pct=Decimal("0.05"),
    )
    s_pct.bind_context(_ctx_holding(0))
    closes = [100.0 - i for i in range(25)]
    sigs = _feed(s_pct, closes)
    assert len(sigs) == 1
    entry = Decimal(str(closes[-1]))
    expected_pct_stop = entry * (Decimal("1") - Decimal("0.05"))
    assert sigs[0].suggested_stop_loss == expected_pct_stop

    # With a small atr_period and a wide H-L range, the ATR stop should be used
    # (different from the pct fallback) and still positive/below entry.
    s_atr = ConnorsRSIStrategy(
        "t", [SYM], rsi_period=3, streak_rsi_period=2, rank_window=15,
        entry_threshold=10.0, atr_period=5, atr_mult=Decimal("2.0"),
        stop_loss_pct=Decimal("0.05"),
    )
    s_atr.bind_context(_ctx_holding(0))
    highs = [c + 1.5 for c in closes]
    lows = [c - 1.5 for c in closes]
    sigs2 = _feed(s_atr, closes, highs=highs, lows=lows)
    assert len(sigs2) == 1
    stop = sigs2[0].suggested_stop_loss
    assert stop is not None and stop > 0 and stop < Decimal(str(closes[-1]))
    assert stop != expected_pct_stop  # ATR-derived, not the pct fallback


# ---- position awareness ----------------------------------------------------

def test_held_and_oversold_does_not_pyramid():
    s = ConnorsRSIStrategy(
        "t", [SYM], rsi_period=3, streak_rsi_period=2, rank_window=15,
        entry_threshold=10.0,
    )
    s.bind_context(_ctx_holding(10))  # already long
    closes = [100.0 - i for i in range(25)]  # deeply oversold
    sigs = _feed(s, closes)
    assert sigs == []  # no second BUY — never pyramids


def test_held_and_recovered_exits():
    s = ConnorsRSIStrategy(
        "t", [SYM], rsi_period=3, streak_rsi_period=2, rank_window=15,
        entry_threshold=10.0, exit_threshold=70.0,
    )
    s.bind_context(_ctx_holding(10))  # already long
    # Strong rally -> price RSI high (>=70) -> SELL exit.
    closes = [100.0 + i for i in range(25)]
    sigs = _feed(s, closes)
    assert len(sigs) == 1
    assert sigs[0].side == OrderSide.SELL
    assert sigs[0].strength == Decimal("1.0")
    assert sigs[0].suggested_stop_loss is None  # exits don't carry a stop


def test_flat_and_recovered_does_nothing():
    s = ConnorsRSIStrategy(
        "t", [SYM], rsi_period=3, rank_window=15, exit_threshold=70.0,
    )
    s.bind_context(_ctx_holding(0))  # flat
    closes = [100.0 + i for i in range(25)]  # recovered but we hold nothing
    sigs = _feed(s, closes)
    assert sigs == []  # no naked short, nothing to exit


def test_long_only_never_shorts():
    s = ConnorsRSIStrategy("t", [SYM], rsi_period=3, rank_window=15)
    s.bind_context(_ctx_holding(0))
    # Feed a long noisy path; assert no BUY/SELL is ever a short open.
    closes = [100.0, 98.0, 101.0, 97.0, 103.0, 95.0, 99.0, 104.0,
              96.0, 100.0, 92.0, 105.0, 90.0, 101.0, 88.0, 102.0,
              94.0, 99.0, 91.0, 100.0, 93.0, 98.0, 95.0, 97.0, 96.0]
    for i, c in enumerate(closes):
        held = s._held(SYM)
        for sig in s.on_bar(_bar(c, i)):
            if sig.side == OrderSide.SELL:
                # A SELL is only ever an exit of an existing long.
                assert held, "SELL emitted while flat = naked short (forbidden)"


# ---- restart / idempotency -------------------------------------------------

def test_idempotent_when_already_long():
    """Re-dispatching the same oversold bar while long yields nothing both times."""
    s = ConnorsRSIStrategy(
        "t", [SYM], rsi_period=3, rank_window=15, entry_threshold=10.0,
    )
    s.bind_context(_ctx_holding(5))
    closes = [100.0 - i for i in range(25)]
    first = _feed(s, closes)
    # feed the final bar again
    again = s.on_bar(_bar(closes[-1] - 1.0, len(closes)))
    assert first == []
    assert again == []


# ---- universe filtering ----------------------------------------------------

def test_off_universe_symbol_ignored():
    s = ConnorsRSIStrategy("t", [SYM], rsi_period=3, rank_window=15)
    s.bind_context(_ctx_holding(0))
    sigs = s.on_bar(_bar(50.0, 0, symbol=OTHER))
    assert sigs == []


# ---- validation ------------------------------------------------------------

def test_invalid_params_raise():
    with pytest.raises(ValueError):
        ConnorsRSIStrategy("t", [SYM], rsi_period=0)
    with pytest.raises(ValueError):
        ConnorsRSIStrategy("t", [SYM], entry_threshold=150.0)
    with pytest.raises(ValueError):
        ConnorsRSIStrategy("t", [SYM], atr_mult=Decimal("0"))
    with pytest.raises(ValueError):
        ConnorsRSIStrategy("t", [SYM], stop_loss_pct=Decimal("1.5"))
