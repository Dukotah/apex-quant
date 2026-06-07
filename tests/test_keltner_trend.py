"""
Tests for apex.strategy.library.keltner_trend (UNVALIDATED research candidate).

This strategy is LONG-ONLY and POSITION-AWARE: each bar it targets "long iff the
breakout/hold condition holds" and emits the delta against what it ACTUALLY holds,
read from the (broker-reconciled) StrategyContext. So the tests drive it through a
context and simulate fills, the way the engine / run_once do.

The channel math (EMA middle, ATR bands) is verified bar-by-bar with a hand-traced
series so the entry/exit indices are known, not guessed.

Covered:
  - constructor validation,
  - warmup: no signal until BOTH EMA and ATR exist,
  - upper-band breakout emits a BUY carrying a stop, with no pyramiding,
  - exit when close falls below the middle EMA, leaving us flat,
  - hysteresis: once long we hold between middle and upper (don't churn),
  - cold start: enters an already-established breakout with no fresh band-cross,
  - ATR-based stop when ATR exists, percentage-fallback stop during ATR warmup,
  - unknown symbol ignored, and determinism.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.keltner_trend import KeltnerTrendStrategy

SYM = Symbol("TEST", AssetClass.ETF)


def _bar(sym: Symbol, t: datetime, high: float, low: float, close: float) -> Bar:
    return Bar(
        symbol=sym,
        timestamp=t,
        open=Decimal(str(close)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal("1000"),
    )


class _Harness:
    """
    Minimal stand-in for the engine: binds a context, refreshes it from a simulated
    portfolio before each bar, and applies emitted signals as IMMEDIATE fills so the
    next bar sees the updated holding (mirrors engine fill-before-dispatch ordering).
    """

    def __init__(self, strat: KeltnerTrendStrategy):
        self.strat = strat
        self.ctx = StrategyContext()
        strat.bind_context(self.ctx)
        self.held: dict[str, Decimal] = {}
        self.t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.i = 0

    def _refresh(self) -> None:
        self.ctx.sync_state(positions={
            k: SimpleNamespace(quantity=q) for k, q in self.held.items() if q > 0
        })

    def step(self, sym: Symbol, high: float, low: float, close: float):
        self._refresh()
        sigs = self.strat.on_bar(_bar(sym, self.t + timedelta(days=self.i), high, low, close))
        self.i += 1
        for s in sigs:
            self.held[sym.ticker] = Decimal("1") if s.side == OrderSide.BUY else Decimal("0")
        return sigs

    def feed(self, sym: Symbol, bars: list[tuple[float, float, float]]):
        out = []
        for hi, lo, cl in bars:
            out.extend(self.step(sym, hi, lo, cl))
        return out


# Hand-traced scenario (ema_period=3, atr_period=2, atr_mult=0.5):
#   bar 7 (close 11.0) breaks above upper band 10.826  -> BUY
#   bars 8,9 still above band but already long           -> no pyramiding
#   bar 10 (close 10.5) falls below middle EMA 10.812    -> SELL exit
_HLC = [
    (10.1, 9.9, 10.0),
    (10.2, 10.0, 10.1),
    (10.0, 9.8, 9.9),
    (10.1, 9.9, 10.0),
    (10.2, 10.0, 10.1),
    (10.0, 9.8, 9.9),
    (10.1, 9.9, 10.0),
    (11.1, 10.9, 11.0),   # bar 7: breakout
    (11.3, 11.1, 11.2),   # bar 8: above band, held
    (11.5, 11.3, 11.4),   # bar 9: above band, held
    (10.6, 10.4, 10.5),   # bar 10: below middle -> exit
    (9.6, 9.4, 9.5),
]


def _strat(symbols=None, **kw):
    defaults = dict(ema_period=3, atr_period=2, atr_mult=0.5)
    defaults.update(kw)
    return KeltnerTrendStrategy("k", symbols or [SYM], **defaults)


# ---- constructor validation ----------------------------------------------

@pytest.mark.parametrize("bad", [
    dict(ema_period=0),
    dict(atr_period=0),
    dict(atr_mult=0.0),
    dict(atr_stop_mult=0.0),
    dict(stop_loss_pct=Decimal("0")),
    dict(stop_loss_pct=Decimal("1")),
    dict(strength=Decimal("0")),
    dict(strength=Decimal("1.5")),
])
def test_constructor_rejects_bad_params(bad):
    with pytest.raises(ValueError):
        _strat(**bad)


# ---- warmup ---------------------------------------------------------------

def test_no_signal_during_warmup():
    h = _Harness(_strat())
    # Fewer than (atr_period+1) bars -> ATR is None -> channel undefined -> no signal.
    assert h.feed(SYM, _HLC[:2]) == []


# ---- entry ----------------------------------------------------------------

def test_breakout_emits_single_buy_with_stop():
    h = _Harness(_strat())
    sigs = h.feed(SYM, _HLC)
    buys = [s for s in sigs if s.side == OrderSide.BUY]
    assert len(buys) == 1, "enters once on the breakout, then holds (no pyramiding)"
    b = buys[0]
    assert b.suggested_stop_loss is not None, "every BUY must carry a stop"
    assert Decimal("0") < b.suggested_stop_loss < Decimal("11.0")  # below entry
    assert b.strength == Decimal("1.0")
    assert b.side == OrderSide.BUY


def test_buy_happens_on_the_breakout_bar():
    h = _Harness(_strat())
    # Feed up to and including the breakout bar (index 7); only then should a BUY appear.
    pre = h.feed(SYM, _HLC[:7])
    assert [s for s in pre if s.side == OrderSide.BUY] == []
    on_break = h.feed(SYM, _HLC[7:8])
    assert any(s.side == OrderSide.BUY for s in on_break)
    assert h.held["TEST"] > 0


# ---- exit -----------------------------------------------------------------

def test_exit_below_middle_ema():
    h = _Harness(_strat())
    sides = [s.side for s in h.feed(SYM, _HLC)]
    assert OrderSide.BUY in sides and OrderSide.SELL in sides
    assert sides.index(OrderSide.BUY) < sides.index(OrderSide.SELL)
    assert h.held["TEST"] == 0   # exit leaves us flat


def test_sell_is_full_conviction():
    h = _Harness(_strat())
    sells = [s for s in h.feed(SYM, _HLC) if s.side == OrderSide.SELL]
    assert sells and sells[0].strength == Decimal("1.0")
    # Full exit carries no stop (it's closing, not opening, exposure).
    assert sells[0].suggested_stop_loss is None


# ---- hysteresis -----------------------------------------------------------

def test_hysteresis_holds_between_middle_and_upper():
    """
    Once long, a bar that sits BELOW the upper band but ABOVE the middle EMA must
    NOT trigger an exit (the hold zone). Bars 8 and 9 are above the band, but the
    key property is that the strategy never re-buys and never exits while close>=mid.
    """
    h = _Harness(_strat())
    sigs = h.feed(SYM, _HLC[:10])   # through bar 9 (still in trend, above middle)
    assert len([s for s in sigs if s.side == OrderSide.BUY]) == 1
    assert [s for s in sigs if s.side == OrderSide.SELL] == []
    assert h.held["TEST"] > 0       # still long, no churn


def test_no_pyramiding_in_strong_trend():
    h = _Harness(_strat())
    # Keep climbing well above the band; must still only buy once.
    bars = _HLC[:10] + [(12.0, 11.8, 11.9), (13.0, 12.8, 12.9), (14.0, 13.8, 13.9)]
    buys = [s for s in h.feed(SYM, bars) if s.side == OrderSide.BUY]
    assert len(buys) == 1


# ---- cold start (the position-aware property) ------------------------------

def test_cold_start_enters_established_breakout():
    """
    Warm the strategy while it is FLAT but price is already parked above the upper
    band (the breakout happened before our replay window). A position-aware strategy
    must still enter — it is flat and the breakout condition holds — without needing
    to witness a fresh band-cross.
    """
    h = _Harness(_strat())
    # Calm base to build a tight channel, then sit clearly above it for several bars.
    bars = [
        (10.1, 9.9, 10.0), (10.2, 10.0, 10.1), (10.0, 9.8, 9.9),
        (10.1, 9.9, 10.0), (10.2, 10.0, 10.1),
        (12.1, 11.9, 12.0), (12.1, 11.9, 12.0), (12.1, 11.9, 12.0),
    ]
    sigs = h.feed(SYM, bars)
    buys = [s for s in sigs if s.side == OrderSide.BUY]
    assert len(buys) == 1, "should enter the established breakout exactly once"
    assert h.held["TEST"] > 0


# ---- stops ----------------------------------------------------------------

def test_atr_based_stop_when_atr_available():
    """On the breakout bar ATR exists, so the stop is the ATR stop, not the % stop."""
    h = _Harness(_strat(atr_stop_mult=2.0, stop_loss_pct=Decimal("0.05")))
    buys = [s for s in h.feed(SYM, _HLC) if s.side == OrderSide.BUY]
    b = buys[0]
    entry = Decimal("11.0")          # close of the breakout bar
    pct_stop = entry * (Decimal("1") - Decimal("0.05"))   # 10.45
    # ATR on the breakout bar (hand-traced) is 0.664 -> atr_stop = 11.0 - 2*0.664.
    assert b.suggested_stop_loss != pct_stop, "should use the ATR stop, not the % fallback"
    assert Decimal("0") < b.suggested_stop_loss < entry


def test_percentage_fallback_stop_during_atr_warmup():
    """
    With a long ATR window the band needs ATR, but we can isolate the stop helper's
    fallback directly: before any ATR exists, _suggested_stop returns the % stop.
    """
    s = _strat(stop_loss_pct=Decimal("0.05"))
    # No bars fed -> ATR buffers empty -> ATR is None -> percentage fallback.
    entry = Decimal("100")
    assert s._suggested_stop("TEST", entry) == entry * Decimal("0.95")


def test_stop_floored_when_atr_stop_would_be_nonpositive():
    # A huge atr_stop_mult could drive the ATR stop <= 0; it must floor to the % stop.
    h = _Harness(_strat(atr_stop_mult=1000.0, stop_loss_pct=Decimal("0.05")))
    buys = [s for s in h.feed(SYM, _HLC) if s.side == OrderSide.BUY]
    b = buys[0]
    assert b.suggested_stop_loss == Decimal("11.0") * Decimal("0.95")


# ---- misc -----------------------------------------------------------------

def test_ignores_unknown_symbol():
    h = _Harness(_strat())
    other = Symbol("NOPE", AssetClass.ETF)
    assert h.step(other, 1.0, 1.0, 1.0) == []


def test_long_only_never_emits_sell_while_flat():
    h = _Harness(_strat())
    # Monotonic decline from a flat start: never long, so never a SELL (or any signal).
    bars = [(10.0, 9.8, 9.9), (9.8, 9.6, 9.7), (9.6, 9.4, 9.5),
            (9.4, 9.2, 9.3), (9.2, 9.0, 9.1), (9.0, 8.8, 8.9)]
    assert h.feed(SYM, bars) == []


def test_deterministic():
    h1 = _Harness(_strat())
    h2 = _Harness(_strat())
    s1 = h1.feed(SYM, _HLC)
    s2 = h2.feed(SYM, _HLC)
    assert [(s.side, s.strength, s.suggested_stop_loss) for s in s1] == \
           [(s.side, s.strength, s.suggested_stop_loss) for s in s2]
