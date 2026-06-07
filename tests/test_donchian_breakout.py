"""
Tests for apex.strategy.library.donchian_breakout.

This strategy is POSITION-AWARE: each bar it derives a target long/flat state
purely from rolling OHLC history (Donchian channels) and emits the DELTA against
what it actually holds, read from the (broker-reconciled) StrategyContext. So the
tests drive it through a context and simulate fills, the way the engine / run_once
do — that's the real contract.

Covered:
  - constructor validation,
  - warmup -> no signals,
  - N-day high breakout -> BUY carrying a protective stop,
  - M-day low breakdown -> SELL (full exit), with hand-computed transition points,
  - no pyramiding while held; stays flat in a downtrend,
  - cold start: enters an ALREADY-broken-out trend with no fresh breakout in the
    replay window (restart correctness),
  - ATR-based stop when warm, percentage fallback during ATR warmup,
  - unknown symbol ignored,
  - determinism.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.donchian_breakout import DonchianBreakoutStrategy

SYM = Symbol("SYM", AssetClass.ETF)


def _bar(
    sym: Symbol, t: datetime, price: float, high: float | None = None, low: float | None = None
) -> Bar:
    c = Decimal(str(price))
    h = Decimal(str(high)) if high is not None else c
    lo = Decimal(str(low)) if low is not None else c
    return Bar(symbol=sym, timestamp=t, open=c, high=h, low=lo, close=c, volume=Decimal("1000"))


class _Harness:
    """
    Minimal stand-in for the engine: binds a context, refreshes it from a simulated
    portfolio before each bar, and applies emitted signals as IMMEDIATE fills so the
    next bar sees the updated holding (mirrors engine fill-before-dispatch ordering).
    """

    def __init__(self, strat: DonchianBreakoutStrategy):
        self.strat = strat
        self.ctx = StrategyContext()
        strat.bind_context(self.ctx)
        self.held: dict[str, Decimal] = {}
        self.t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.i = 0

    def _refresh(self):
        self.ctx.sync_state(
            positions={k: SimpleNamespace(quantity=q) for k, q in self.held.items() if q > 0}
        )

    def step(self, sym: Symbol, price: float, high: float | None = None, low: float | None = None):
        self._refresh()
        sigs = self.strat.on_bar(_bar(sym, self.t + timedelta(days=self.i), price, high, low))
        self.i += 1
        for s in sigs:
            self.held[sym.ticker] = Decimal("1") if s.side == OrderSide.BUY else Decimal("0")
        return sigs

    def feed(self, sym: Symbol, prices: list[float]):
        out = []
        for p in prices:
            out.extend(self.step(sym, p))
        return out


def _strat(**kw) -> DonchianBreakoutStrategy:
    base = dict(entry_period=3, exit_period=2, atr_period=3, stop_loss_pct=Decimal("0.05"))
    base.update(kw)
    return DonchianBreakoutStrategy("s", [SYM], **base)


# ---- validation ----------------------------------------------------------


def test_exit_must_not_exceed_entry():
    with pytest.raises(ValueError):
        DonchianBreakoutStrategy("s", [SYM], entry_period=10, exit_period=20)


def test_periods_must_be_positive():
    with pytest.raises(ValueError):
        DonchianBreakoutStrategy("s", [SYM], entry_period=0)


def test_atr_mult_must_be_positive():
    with pytest.raises(ValueError):
        DonchianBreakoutStrategy("s", [SYM], atr_mult=Decimal("0"))


def test_stop_pct_must_be_positive():
    with pytest.raises(ValueError):
        DonchianBreakoutStrategy("s", [SYM], stop_loss_pct=Decimal("0"))


# ---- warmup --------------------------------------------------------------


def test_no_signal_during_warmup():
    h = _Harness(_strat())
    # entry_period=3 -> need 4 bars before any opinion; first 3 produce nothing.
    assert h.feed(SYM, [10, 10, 10]) == []


# ---- breakout / breakdown with hand-computed transitions -----------------


def test_breakout_emits_buy_then_breakdown_sells():
    """
    Hand-computed with entry_period=3, exit_period=2 on flat (H=L=C) bars.
    Path: [10,10,10,10,15,15,15,8,8]
      i=4: close 15 > max(prior 3 highs=10) -> BUY
      i=7: close 8 < min(prior 2 lows=15)   -> SELL
    """
    h = _Harness(_strat())
    sigs = h.feed(SYM, [10, 10, 10, 10, 15, 15, 15, 8, 8])
    sides = [s.side for s in sigs]
    assert sides == [OrderSide.BUY, OrderSide.SELL]
    assert h.held["SYM"] == 0


def test_buy_carries_stop_below_price():
    h = _Harness(_strat())
    buys = [s for s in h.feed(SYM, [10, 10, 10, 10, 15, 15, 15, 8, 8]) if s.side == OrderSide.BUY]
    assert len(buys) == 1
    assert buys[0].suggested_stop_loss is not None
    assert Decimal("0") < buys[0].suggested_stop_loss < Decimal("15")


def test_sell_is_full_conviction():
    h = _Harness(_strat())
    sells = [s for s in h.feed(SYM, [10, 10, 10, 10, 15, 15, 15, 8, 8]) if s.side == OrderSide.SELL]
    assert sells and sells[0].strength == Decimal("1.0")


def test_no_pyramiding_while_held():
    h = _Harness(_strat())
    # Break out, then keep making higher highs: must enter exactly once.
    prices = [10, 10, 10, 10, 15, 20, 25, 30, 35, 40]
    buys = [s for s in h.feed(SYM, prices) if s.side == OrderSide.BUY]
    assert len(buys) == 1
    assert h.held["SYM"] > 0


def test_flat_in_downtrend_stays_flat():
    h = _Harness(_strat())
    # Monotonic decline: never a high breakout, never held -> no signals.
    prices = [30, 29, 28, 27, 26, 25, 24, 23, 22, 21]
    assert h.feed(SYM, prices) == []


def test_cold_start_enters_established_breakout():
    """
    Restart correctness. A steadily rising series: the most recent bar's close
    keeps exceeding the prior N-high, so the target is long throughout. A flat
    strategy waking up mid-trend must enter exactly once without needing to have
    witnessed the original breakout bar.
    """
    h = _Harness(_strat())
    prices = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
    buys = [s for s in h.feed(SYM, prices) if s.side == OrderSide.BUY]
    assert len(buys) == 1
    assert h.held["SYM"] > 0


# ---- stop placement: ATR vs percentage fallback --------------------------


def test_percentage_fallback_during_atr_warmup():
    """
    Force the BUY to fire before ATR has enough bars (atr_period large). The stop
    must then be exactly the percentage fallback: close * (1 - stop_loss_pct).
    """
    h = _Harness(_strat(atr_period=50, stop_loss_pct=Decimal("0.05")))
    buys = [s for s in h.feed(SYM, [10, 10, 10, 10, 15]) if s.side == OrderSide.BUY]
    assert len(buys) == 1
    # close at entry is 15 -> 15 * 0.95 = 14.25
    assert buys[0].suggested_stop_loss == Decimal("15") * Decimal("0.95")


def test_atr_stop_used_when_warm():
    """
    With a small atr_period and ranged bars, ATR is available at the breakout, so
    the stop should be close - atr_mult*ATR, which differs from the 5% fallback.
    """
    strat = _strat(atr_period=3, atr_mult=Decimal("2.0"), stop_loss_pct=Decimal("0.05"))
    ctx = StrategyContext()
    strat.bind_context(ctx)
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    held: dict[str, Decimal] = {}
    # Bars with a real high/low range so ATR > 0; breakout on the last bar.
    rows = [
        (10, 11, 9),
        (10, 11, 9),
        (10, 11, 9),
        (10, 11, 9),
        (15, 16, 14),  # close 15 > prior-3 high (11) -> BUY here, ATR warm
    ]
    captured = None
    for i, (c, hi, lo) in enumerate(rows):
        ctx.sync_state(positions={k: SimpleNamespace(quantity=q) for k, q in held.items() if q > 0})
        for s in strat.on_bar(_bar(SYM, t + timedelta(days=i), c, hi, lo)):
            if s.side == OrderSide.BUY:
                captured = s
                held[SYM.ticker] = Decimal("1")
    assert captured is not None
    fallback = Decimal("15") * Decimal("0.95")
    # ATR-derived stop should be present and NOT equal to the pct fallback.
    assert captured.suggested_stop_loss != fallback
    assert Decimal("0") < captured.suggested_stop_loss < Decimal("15")


# ---- misc ----------------------------------------------------------------


def test_ignores_unknown_symbol():
    h = _Harness(_strat())
    other = Symbol("NOPE", AssetClass.ETF)
    assert h.step(other, 1.0) == []


def test_deterministic():
    prices = [10, 10, 10, 10, 15, 15, 15, 8, 8, 9, 12, 20, 25]
    h1 = _Harness(_strat())
    h2 = _Harness(_strat())
    s1 = h1.feed(SYM, prices)
    s2 = h2.feed(SYM, prices)
    assert [(s.side, s.suggested_stop_loss) for s in s1] == [
        (s.side, s.suggested_stop_loss) for s in s2
    ]
