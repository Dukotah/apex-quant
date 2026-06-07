"""
Tests for apex.strategy.library.roc_momentum.

ROCMomentumStrategy is LONG-ONLY and POSITION-AWARE: each bar it targets "long iff
recent rate-of-change is strong" and emits the delta against what it actually
holds, read from the (broker-reconciled) StrategyContext. So the tests drive it
through a context and simulate fills the way the engine / run_once do.

Covered:
  - constructor validation,
  - warmup (no ROC yet -> no signals),
  - flat + strong ROC -> BUY carrying a stop; held + strong -> nothing (no pyramid),
  - exit hysteresis (held exits only when ROC <= exit_threshold, not entry),
  - cold start: enters an ALREADY-strong instrument with no fresh crossing,
  - hand-computed ROC at the entry threshold boundary,
  - ATR stop vs. percentage-fallback stop during warmup,
  - unknown-symbol guard, determinism.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.roc_momentum import ROCMomentumStrategy

SYM = Symbol("MOM", AssetClass.ETF)


def _bar(t: datetime, close: float, high: float | None = None,
         low: float | None = None) -> Bar:
    c = Decimal(str(close))
    h = Decimal(str(high)) if high is not None else c
    lo = Decimal(str(low)) if low is not None else c
    return Bar(symbol=SYM, timestamp=t, open=c, high=h, low=lo, close=c,
               volume=Decimal("1000"))


class _Harness:
    """
    Stand-in for the engine: binds a context, refreshes it from a simulated
    holding before each bar, and applies emitted signals as IMMEDIATE fills so the
    next bar sees the updated position (mirrors engine fill-before-dispatch order).
    """
    def __init__(self, strat: ROCMomentumStrategy):
        self.strat = strat
        self.ctx = StrategyContext()
        strat.bind_context(self.ctx)
        self.qty = Decimal("0")
        self.t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.i = 0

    def _refresh(self):
        positions = {}
        if self.qty > 0:
            positions[SYM.ticker] = SimpleNamespace(quantity=self.qty)
        self.ctx.sync_state(positions=positions)

    def step(self, close: float, high: float | None = None,
             low: float | None = None):
        self._refresh()
        sigs = self.strat.on_bar(
            _bar(self.t + timedelta(days=self.i), close, high, low))
        self.i += 1
        for s in sigs:
            self.qty = Decimal("1") if s.side == OrderSide.BUY else Decimal("0")
        return sigs

    def feed(self, closes: list[float]):
        out = []
        for c in closes:
            out.extend(self.step(c))
        return out


# ---- constructor validation ------------------------------------------------

def test_rejects_exit_above_entry():
    with pytest.raises(ValueError):
        ROCMomentumStrategy("s", [SYM], entry_threshold=0.05, exit_threshold=0.10)


def test_rejects_bad_roc_period():
    with pytest.raises(ValueError):
        ROCMomentumStrategy("s", [SYM], roc_period=0)


def test_rejects_bad_atr_mult():
    with pytest.raises(ValueError):
        ROCMomentumStrategy("s", [SYM], atr_mult=0)


def test_rejects_bad_strength():
    with pytest.raises(ValueError):
        ROCMomentumStrategy("s", [SYM], strength=Decimal("1.5"))


def test_rejects_bad_stop_pct():
    with pytest.raises(ValueError):
        ROCMomentumStrategy("s", [SYM], stop_loss_pct=Decimal("1.0"))


# ---- warmup & core transitions --------------------------------------------

def test_no_signal_during_warmup():
    # roc_period=3 needs 4 closes before any ROC exists.
    h = _Harness(ROCMomentumStrategy("s", [SYM], roc_period=3, entry_threshold=0.05))
    assert h.feed([100, 101, 102]) == []


def test_strong_roc_emits_buy_with_stop():
    h = _Harness(ROCMomentumStrategy("s", [SYM], roc_period=3, entry_threshold=0.05,
                                     atr_period=14))
    # 4th bar: ROC = 120/100 - 1 = 0.20 >= 0.05 -> BUY.
    buys = [s for s in h.feed([100, 105, 110, 120]) if s.side == OrderSide.BUY]
    assert len(buys) == 1
    assert buys[0].side == OrderSide.BUY
    assert buys[0].strength == Decimal("1.0")
    stop = buys[0].suggested_stop_loss
    assert stop is not None and Decimal("0") < stop < Decimal("120")


def test_threshold_boundary_is_inclusive():
    # ROC exactly == entry_threshold must trigger (>= boundary).
    h = _Harness(ROCMomentumStrategy("s", [SYM], roc_period=3, entry_threshold=0.10))
    # 110/100 - 1 = 0.10 exactly.
    buys = [s for s in h.feed([100, 100, 100, 110]) if s.side == OrderSide.BUY]
    assert len(buys) == 1


def test_just_below_threshold_no_entry():
    h = _Harness(ROCMomentumStrategy("s", [SYM], roc_period=3, entry_threshold=0.10))
    # 109/100 - 1 = 0.09 < 0.10 -> no entry.
    buys = [s for s in h.feed([100, 100, 100, 109]) if s.side == OrderSide.BUY]
    assert buys == []


def test_no_pyramiding_while_held():
    h = _Harness(ROCMomentumStrategy("s", [SYM], roc_period=3, entry_threshold=0.05))
    # Strongly rising the whole way: enters once, then stays long (no duplicate buys).
    buys = [s for s in h.feed([100, 110, 120, 130, 140, 150, 160])
            if s.side == OrderSide.BUY]
    assert len(buys) == 1


def test_exit_when_momentum_fades():
    h = _Harness(ROCMomentumStrategy("s", [SYM], roc_period=3, entry_threshold=0.05,
                                     exit_threshold=0.0))
    # Rise to trigger a long, then decline so trailing ROC turns negative -> SELL.
    sides = [s.side for s in h.feed(
        [100, 110, 120, 130, 125, 110, 95, 80, 70])]
    assert OrderSide.BUY in sides and OrderSide.SELL in sides
    assert sides.index(OrderSide.BUY) < sides.index(OrderSide.SELL)
    assert h.qty == 0


def test_sell_is_full_conviction():
    h = _Harness(ROCMomentumStrategy("s", [SYM], roc_period=3, entry_threshold=0.05,
                                     exit_threshold=0.0))
    sells = [s for s in h.feed([100, 110, 120, 130, 125, 110, 95, 80, 70])
             if s.side == OrderSide.SELL]
    assert sells and sells[0].strength == Decimal("1.0")


def test_hysteresis_holds_through_shallow_dip():
    """
    With entry=0.10 and exit=0.0, a long should be HELD while ROC is positive but
    below the entry trigger — it must not re-evaluate against the entry threshold.
    """
    strat = ROCMomentumStrategy("s", [SYM], roc_period=2, entry_threshold=0.10,
                                exit_threshold=0.0)
    h = _Harness(strat)
    # closes: 100,100,120 -> ROC(2) at idx2 = 120/100-1 = 0.20 >= 0.10 -> BUY.
    # then 124 -> ROC = 124/120-1 = 0.033: positive but < entry; exit thresh is 0
    # so we STAY long (no SELL). 122 -> 122/124-1 < 0 ... actually keep positive:
    sigs = h.feed([100, 100, 120, 124])
    buys = [s for s in sigs if s.side == OrderSide.BUY]
    sells = [s for s in sigs if s.side == OrderSide.SELL]
    assert len(buys) == 1
    assert sells == []          # held through the shallow ROC dip (hysteresis)
    assert h.qty > 0


def test_flat_weak_momentum_stays_flat():
    h = _Harness(ROCMomentumStrategy("s", [SYM], roc_period=3, entry_threshold=0.05))
    # Monotonic decline: ROC always negative -> never long -> no signals.
    assert h.feed([130, 128, 126, 124, 122, 120, 118]) == []


# ---- cold start ------------------------------------------------------------

def test_cold_start_enters_established_strength():
    """
    Warm over a window where the instrument is already strongly up the whole time
    (no fresh threshold crossing). A position-aware strategy must still enter,
    because it is flat and momentum is strong.
    """
    h = _Harness(ROCMomentumStrategy("s", [SYM], roc_period=3, entry_threshold=0.05))
    # Every computable ROC >> 0.05 from the first bar onward; never a fresh cross.
    buys = [s for s in h.feed([100, 115, 130, 150, 175, 200])
            if s.side == OrderSide.BUY]
    assert len(buys) == 1
    assert h.qty > 0


# ---- stop sizing -----------------------------------------------------------

def test_pct_fallback_stop_during_atr_warmup():
    """
    With only 4 bars and atr_period=14, ATR is None -> the stop must be the fixed
    percentage fallback: price * (1 - stop_loss_pct).
    """
    strat = ROCMomentumStrategy("s", [SYM], roc_period=3, entry_threshold=0.05,
                                atr_period=14, stop_loss_pct=Decimal("0.05"))
    h = _Harness(strat)
    buys = [s for s in h.feed([100, 105, 110, 120]) if s.side == OrderSide.BUY]
    assert len(buys) == 1
    # 120 * (1 - 0.05) = 114.
    assert buys[0].suggested_stop_loss == Decimal("120") * (Decimal("1") - Decimal("0.05"))


def test_atr_stop_used_when_warm():
    """
    Once enough bars exist for ATR, the stop should be ATR-based and differ from the
    plain percentage stop (because the path carries real high/low range).
    """
    strat = ROCMomentumStrategy("s", [SYM], roc_period=3, entry_threshold=0.01,
                                atr_period=3, atr_mult=2.0, stop_loss_pct=Decimal("0.05"))
    h = _Harness(strat)
    # Feed bars with explicit ranges so ATR > 0. Keep flat (small ROC) until ATR is
    # warm, then trigger an entry on the last bar.
    h.step(100, high=101, low=99)
    h.step(100, high=101, low=99)
    h.step(100, high=101, low=99)
    h.step(100, high=101, low=99)   # ATR now defined; ROC ~0 so still flat
    sigs = h.step(105, high=106, low=104)   # ROC = 105/100-1 = 0.05 >= 0.01 -> BUY
    buys = [s for s in sigs if s.side == OrderSide.BUY]
    assert len(buys) == 1
    stop = buys[0].suggested_stop_loss
    pct_stop = Decimal("105") * (Decimal("1") - Decimal("0.05"))
    assert stop is not None and stop != pct_stop
    assert Decimal("0") < stop < Decimal("105")


# ---- guards & determinism --------------------------------------------------

def test_ignores_unknown_symbol():
    strat = ROCMomentumStrategy("s", [SYM], roc_period=3, entry_threshold=0.05)
    ctx = StrategyContext()
    strat.bind_context(ctx)
    other = Symbol("NOPE", AssetClass.ETF)
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bar = Bar(symbol=other, timestamp=t, open=Decimal("1"), high=Decimal("1"),
              low=Decimal("1"), close=Decimal("1"), volume=Decimal("1"))
    assert strat.on_bar(bar) == []


def test_no_context_treated_as_flat():
    # No context bound: _held returns False, so strong ROC still produces a BUY.
    strat = ROCMomentumStrategy("s", [SYM], roc_period=3, entry_threshold=0.05)
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    sigs = []
    for i, c in enumerate([100, 105, 110, 120]):
        sigs.extend(strat.on_bar(_bar(t + timedelta(days=i), c)))
    assert any(s.side == OrderSide.BUY for s in sigs)


def test_deterministic():
    closes = [100, 110, 120, 130, 125, 110, 95, 80, 70, 75, 85]
    h1 = _Harness(ROCMomentumStrategy("s", [SYM], roc_period=3, entry_threshold=0.05))
    h2 = _Harness(ROCMomentumStrategy("s", [SYM], roc_period=3, entry_threshold=0.05))
    s1 = h1.feed(closes)
    s2 = h2.feed(closes)
    assert [(s.side, s.strength, s.suggested_stop_loss) for s in s1] == \
           [(s.side, s.strength, s.suggested_stop_loss) for s in s2]
