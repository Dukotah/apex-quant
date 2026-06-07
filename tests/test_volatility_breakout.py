"""
Tests for apex.strategy.library.volatility_breakout (UNVALIDATED research candidate).

This strategy is LONG-ONLY and POSITION-AWARE: each bar it computes a target
state (long iff a fresh ATR breakout / still in trade) and emits the delta against
what it ACTUALLY holds, read from the (broker-reconciled) StrategyContext. So the
tests drive it through a context and simulate fills the way the engine / run_once
do — that is the real contract.

Where possible the breakout level is hand-computed:
  With high == low == close == c on every bar, the true range simplifies to
  TR[i] = |c[i] - c[i-1]|, and ATR(period=2) Wilder = avg of the first two TRs,
  then smoothed. We use that to assert exact entry timing.

Covered:
  - constructor validation,
  - warmup: no signal until ATR is computable,
  - hand-computed breakout entry (close > prior_close + k*ATR),
  - no entry when the move is below the breakout threshold,
  - every BUY carries a suggested_stop_loss (ATR-based when warm, % fallback),
  - position-awareness: no pyramiding while held; full SELL on exit,
  - TIME stop and ATR-trailing exit,
  - restart safety: long with no recorded entry skips the time stop (no bogus exit),
  - unknown symbol ignored, determinism.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.volatility_breakout import VolatilityBreakoutStrategy

SYM = Symbol("BRK", AssetClass.ETF)


def _bar(sym: Symbol, t: datetime, close: float, high: float = None, low: float = None) -> Bar:
    c = Decimal(str(close))
    h = Decimal(str(high)) if high is not None else c
    lo = Decimal(str(low)) if low is not None else c
    return Bar(symbol=sym, timestamp=t, open=c, high=h, low=lo, close=c, volume=Decimal("1000"))


class _Harness:
    """
    Minimal stand-in for the engine: binds a context, refreshes it from a simulated
    portfolio before each bar, then applies emitted signals as IMMEDIATE fills so the
    next bar sees the updated holding (mirrors engine fill-before-dispatch ordering).
    """

    def __init__(self, strat: VolatilityBreakoutStrategy):
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

    def step(self, sym: Symbol, close: float, high: float = None, low: float = None):
        self._refresh()
        sigs = self.strat.on_bar(_bar(sym, self.t + timedelta(days=self.i), close, high, low))
        self.i += 1
        for s in sigs:
            self.held[sym.ticker] = Decimal("1") if s.side == OrderSide.BUY else Decimal("0")
        return sigs

    def feed(self, sym: Symbol, closes: list[float]):
        out = []
        for c in closes:
            out.extend(self.step(sym, c))
        return out


# ---- constructor validation -------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"atr_period": 0},
        {"k": 0},
        {"k": -1.0},
        {"stop_atr_mult": 0},
        {"stop_loss_pct": Decimal("0")},
    ],
)
def test_constructor_validation(kwargs):
    with pytest.raises(ValueError):
        VolatilityBreakoutStrategy("s", [SYM], **kwargs)


# ---- warmup -----------------------------------------------------------------


def test_no_signal_during_atr_warmup():
    # atr_period=2 needs 3 bars before ATR exists; first two bars must be silent.
    h = _Harness(VolatilityBreakoutStrategy("s", [SYM], atr_period=2, k=1.0))
    assert h.step(SYM, 100.0) == []
    assert h.step(SYM, 101.0) == []
    assert h.held.get("BRK", Decimal("0")) == 0


# ---- hand-computed breakout entry ------------------------------------------


def test_hand_computed_breakout_entry():
    """
    closes: 100, 102, 101, 110  (high==low==close so TR=|dc|).
      TR: -, 2, 1, 9
      ATR(2) at bar index 2 (0-based, the 3rd bar, close=101) = (2+1)/2 = 1.5.
      Bar 3 (close=110): prior_close=101, ATR Wilder updated:
        ATR3 = (ATR2*(2-1) + TR3)/2 = (1.5 + 9)/2 = 5.25.
      breakout_level = 101 + 1.0*5.25 = 106.25. close 110 > 106.25 -> BUY.
    The 3rd bar (close=101) cannot break out: prior=102, level=102+1.5=103.5,
    close 101 < 103.5 -> no signal.
    """
    h = _Harness(VolatilityBreakoutStrategy("s", [SYM], atr_period=2, k=1.0))
    sigs = h.feed(SYM, [100.0, 102.0, 101.0, 110.0])
    buys = [s for s in sigs if s.side == OrderSide.BUY]
    assert len(buys) == 1
    assert buys[0].timestamp == h.t + timedelta(days=3)
    assert buys[0].side == OrderSide.BUY
    assert h.held["BRK"] > 0


def test_no_entry_below_threshold():
    """
    closes: 100, 102, 101, 102. Bar 3 (close=102): prior=101, TR3=|102-101|=1,
    ATR3 = (1.5*(2-1) + 1)/2 = 1.25, level = 101 + 1.0*1.25 = 102.25;
    close 102 < 102.25 -> NO breakout, stay flat.
    """
    h = _Harness(VolatilityBreakoutStrategy("s", [SYM], atr_period=2, k=1.0))
    sigs = h.feed(SYM, [100.0, 102.0, 101.0, 102.0])
    assert [s for s in sigs if s.side == OrderSide.BUY] == []
    assert h.held.get("BRK", Decimal("0")) == 0


def test_larger_k_suppresses_marginal_breakout():
    # Same path as the entry test but k=3 -> level=101+3*5.25=116.75 > 110: no entry.
    h = _Harness(VolatilityBreakoutStrategy("s", [SYM], atr_period=2, k=3.0))
    sigs = h.feed(SYM, [100.0, 102.0, 101.0, 110.0])
    assert [s for s in sigs if s.side == OrderSide.BUY] == []


# ---- stop attachment --------------------------------------------------------


def test_buy_has_atr_based_stop():
    h = _Harness(VolatilityBreakoutStrategy("s", [SYM], atr_period=2, k=1.0, stop_atr_mult=2.0))
    sigs = h.feed(SYM, [100.0, 102.0, 101.0, 110.0])
    buy = next(s for s in sigs if s.side == OrderSide.BUY)
    # stop = entry_close - 2*ATR = 110 - 2*5.25 = 99.5
    assert buy.suggested_stop_loss == Decimal("99.5")
    assert Decimal("0") < buy.suggested_stop_loss < Decimal("110")


def test_buy_always_has_a_stop():
    """Every BUY carries a stop; the ATR-warm path is exercised above. Here we just
    assert the invariant holds for the emitted entry."""
    h = _Harness(VolatilityBreakoutStrategy("s", [SYM], atr_period=2, k=1.0))
    sigs = h.feed(SYM, [100.0, 102.0, 101.0, 110.0])
    for s in sigs:
        if s.side == OrderSide.BUY:
            assert s.suggested_stop_loss is not None
            assert s.suggested_stop_loss > Decimal("0")


def test_percentage_stop_fallback_used_when_atr_degenerate():
    """
    Unit-level check of the private stop sizer: with no ATR (warmup) the fixed
    percentage fallback is used, so a stop always exists.
    """
    strat = VolatilityBreakoutStrategy("s", [SYM], stop_loss_pct=Decimal("0.05"))
    # ATR not available -> 100 * (1 - 0.05) = 95.
    assert strat._suggested_stop(Decimal("100"), None) == Decimal("95.00")
    # ATR so large the ATR stop would go non-positive -> falls back to %.
    assert strat._suggested_stop(Decimal("100"), 1000.0) == Decimal("95.00")


# ---- position awareness -----------------------------------------------------


def test_no_pyramiding_while_held():
    # Enter on bar 3, then keep ripping higher: must NOT emit further BUYs.
    h = _Harness(
        VolatilityBreakoutStrategy(
            "s", [SYM], atr_period=2, k=1.0, max_hold_bars=0, exit_atr_mult=0
        )
    )
    sigs = h.feed(SYM, [100.0, 102.0, 101.0, 110.0, 120.0, 130.0, 140.0])
    buys = [s for s in sigs if s.side == OrderSide.BUY]
    assert len(buys) == 1


def test_time_stop_exit():
    # max_hold_bars=2, trailing disabled. Enter on bar 3 (idx 4, 1-based),
    # exit 2 bars later regardless of price.
    h = _Harness(
        VolatilityBreakoutStrategy(
            "s", [SYM], atr_period=2, k=1.0, max_hold_bars=2, exit_atr_mult=0
        )
    )
    sigs = h.feed(SYM, [100.0, 102.0, 101.0, 110.0, 111.0, 112.0, 113.0])
    sells = [s for s in sigs if s.side == OrderSide.SELL]
    assert len(sells) == 1
    assert sells[0].strength == Decimal("1.0")
    assert "time stop" in sells[0].reason
    assert h.held["BRK"] == 0


def test_atr_trailing_exit():
    """
    Time stop disabled; a give-back from the peak triggers the ATR trail exit.
      closes: 100,102,101,110,112,108. TR: 2,1,9,2,4.
      ATR(2): 1.5 -> 5.25 -> 3.625 -> 3.8125.
      Enter on the 110 breakout (bar idx 4), peak rises to 112 on the next bar.
      Bar close=108: peak=112, ATR=3.8125, trail_level=112-1.0*3.8125=108.1875;
      close 108 <= 108.1875 -> EXIT.
    """
    h = _Harness(
        VolatilityBreakoutStrategy(
            "s", [SYM], atr_period=2, k=1.0, max_hold_bars=0, exit_atr_mult=1.0
        )
    )
    sigs = h.feed(SYM, [100.0, 102.0, 101.0, 110.0, 112.0, 108.0])
    sides = [s.side for s in sigs]
    assert OrderSide.BUY in sides and OrderSide.SELL in sides
    assert sides.index(OrderSide.BUY) < sides.index(OrderSide.SELL)
    sells = [s for s in sigs if s.side == OrderSide.SELL]
    assert "ATR trail" in sells[0].reason
    assert h.held["BRK"] == 0


def test_restart_long_without_entry_skips_time_stop():
    """
    Restart safety: the context says we are LONG but the strategy has no recorded
    entry index (fresh process). The time stop must be SKIPPED (no bogus immediate
    exit); only the ATR trail can exit. Here price holds, so we stay long.
    """
    strat = VolatilityBreakoutStrategy(
        "s", [SYM], atr_period=2, k=1.0, max_hold_bars=1, exit_atr_mult=2.0
    )
    ctx = StrategyContext()
    strat.bind_context(ctx)
    ctx.sync_state(positions={"BRK": SimpleNamespace(quantity=Decimal("1"))})
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    out = []
    for i, c in enumerate([100.0, 101.0, 102.0, 103.0]):
        out.extend(strat.on_bar(_bar(SYM, t + timedelta(days=i), c)))
    # No exit: time stop skipped (no entry idx) and price never gave back to trail.
    assert [s for s in out if s.side == OrderSide.SELL] == []


# ---- misc -------------------------------------------------------------------


def test_ignores_unknown_symbol():
    h = _Harness(VolatilityBreakoutStrategy("s", [SYM], atr_period=2, k=1.0))
    other = Symbol("NOPE", AssetClass.ETF)
    assert h.step(other, 100.0) == []


def test_deterministic():
    closes = [100.0, 102.0, 101.0, 110.0, 115.0, 90.0, 120.0, 121.0]
    h1 = _Harness(
        VolatilityBreakoutStrategy(
            "s", [SYM], atr_period=2, k=1.0, max_hold_bars=3, exit_atr_mult=2.0
        )
    )
    h2 = _Harness(
        VolatilityBreakoutStrategy(
            "s", [SYM], atr_period=2, k=1.0, max_hold_bars=3, exit_atr_mult=2.0
        )
    )
    s1 = h1.feed(SYM, closes)
    s2 = h2.feed(SYM, closes)
    assert [(s.side, s.strength, s.reason) for s in s1] == [
        (s.side, s.strength, s.reason) for s in s2
    ]
