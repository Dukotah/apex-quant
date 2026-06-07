"""
Tests for apex.strategy.library.stochastic_reversal (UNVALIDATED research candidate).

This strategy is POSITION-AWARE: each bar it computes a target (long iff a fresh
oversold %K-over-%D cross fired while flat; flat iff overbought) and emits the delta
against what it actually holds, read from the broker-reconciled StrategyContext.
So the tests drive it through a context and simulate fills, the way the engine /
run_once do — that's the real contract.

Covered:
  - constructor validation,
  - the private oscillator math against hand-computed %K/%D values,
  - warmup safety (no signal, no crash on insufficient data),
  - entry on an oversold %K-over-%D cross, with a stop attached (ATR + pct fallback),
  - no entry when the cross happens but it was NOT oversold,
  - exit on overbought (incl. a cold-start mid-trade exit with no fresh cross),
  - no pyramiding while held,
  - unknown symbols ignored,
  - determinism.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.stochastic_reversal import StochasticReversalStrategy

SYM = Symbol("X", AssetClass.ETF)


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
    Minimal engine stand-in: binds a context, refreshes it from a simulated
    portfolio before each bar, and applies emitted signals as IMMEDIATE fills so
    the next bar sees the updated holding (mirrors fill-before-dispatch ordering).
    """

    def __init__(self, strat: StochasticReversalStrategy):
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

    def step(self, sym: Symbol, hlc: tuple[float, float, float]):
        self._refresh()
        high, low, close = hlc
        sigs = self.strat.on_bar(_bar(sym, self.t + timedelta(days=self.i), high, low, close))
        self.i += 1
        for s in sigs:
            self.held[sym.ticker] = Decimal("1") if s.side == OrderSide.BUY else Decimal("0")
        return sigs

    def feed(self, sym: Symbol, bars: list[tuple[float, float, float]]):
        out = []
        for hlc in bars:
            out.extend(self.step(sym, hlc))
        return out


# ---- constructor validation ----------------------------------------------


def test_bad_levels_rejected():
    with pytest.raises(ValueError):
        StochasticReversalStrategy("s", [SYM], oversold=80, overbought=20)


def test_bad_overbought_above_100_rejected():
    with pytest.raises(ValueError):
        StochasticReversalStrategy("s", [SYM], overbought=120)


def test_bad_periods_rejected():
    with pytest.raises(ValueError):
        StochasticReversalStrategy("s", [SYM], k_period=0)
    with pytest.raises(ValueError):
        StochasticReversalStrategy("s", [SYM], d_period=0)


def test_bad_atr_mult_rejected():
    with pytest.raises(ValueError):
        StochasticReversalStrategy("s", [SYM], atr_mult=0)


# ---- oscillator math (hand-computed) -------------------------------------


def test_stochastic_known_values():
    st = StochasticReversalStrategy("s", [SYM], k_period=3, d_period=2)
    highs = [10, 11, 12, 9, 8, 7, 12]
    lows = [8, 9, 10, 7, 6, 5, 10]
    closes = [9, 10, 11, 8, 7, 6, 11]
    k, d = st._stochastic(highs, lows, closes)
    # %K[2]: hi=12 lo=8 close=11 -> 100*(11-8)/(12-8) = 75.0
    assert k[0] is None and k[1] is None
    assert k[2] == pytest.approx(75.0)
    assert k[3] == pytest.approx(20.0)
    assert k[4] == pytest.approx(100.0 * (7 - 6) / (12 - 6))  # 16.666...
    assert k[6] == pytest.approx(100.0 * (11 - 5) / (12 - 5))  # 85.714...
    # %D = SMA(%K, 2); first defined at index 3 = (75+20)/2 = 47.5
    assert d[2] is None
    assert d[3] == pytest.approx(47.5)


def test_flat_range_maps_to_neutral_50():
    st = StochasticReversalStrategy("s", [SYM], k_period=2, d_period=1)
    # Identical high==low across the window -> degenerate range -> 50.0, no crash.
    k, _ = st._stochastic([5, 5, 5], [5, 5, 5], [5, 5, 5])
    assert k[1] == pytest.approx(50.0)
    assert k[2] == pytest.approx(50.0)


# ---- warmup --------------------------------------------------------------


def test_no_signal_during_warmup():
    h = _Harness(StochasticReversalStrategy("s", [SYM], k_period=3, d_period=2))
    # Two bars: oscillator not yet computable -> no signals, no crash.
    assert h.feed(SYM, [(10, 8, 9), (11, 9, 10)]) == []


# ---- entry ---------------------------------------------------------------


def test_entry_on_oversold_cross_with_stop():
    """
    Build a path that dips so %K & %D go oversold, then %K crosses up through %D.
    Expect exactly one BUY carrying a suggested stop strictly below price.
    """
    h = _Harness(
        StochasticReversalStrategy(
            "s",
            [SYM],
            k_period=3,
            d_period=2,
            oversold=30,
            overbought=80,
            atr_period=3,
            atr_mult=2.0,
        )
    )
    # Decline into oversold, then a bounce that lifts %K above %D from the bottom.
    bars = [
        (20, 18, 19),
        (19, 17, 18),
        (18, 16, 17),
        (17, 15, 15.5),  # near range lows -> oversold
        (16, 14, 14.5),  # deeper -> %K & %D oversold
        (16.5, 14, 16.2),  # snaps up toward the top of the window -> %K crosses up
    ]
    sigs = h.feed(SYM, bars)
    buys = [s for s in sigs if s.side == OrderSide.BUY]
    assert len(buys) == 1, f"expected one entry, got sides {[s.side for s in sigs]}"
    buy = buys[0]
    assert buy.suggested_stop_loss is not None
    assert buy.suggested_stop_loss < buy_price(bars)
    assert buy.suggested_stop_loss > Decimal("0")
    assert buy.strength == Decimal("1.0")
    assert h.held["X"] > 0


def buy_price(bars):
    return Decimal(str(bars[-1][2]))


def test_no_entry_when_cross_not_oversold():
    """A %K-over-%D cross that happens with both lines well above oversold must NOT
    trigger an entry (this is a reversal-from-oversold candidate)."""
    h = _Harness(
        StochasticReversalStrategy("s", [SYM], k_period=3, d_period=2, oversold=20, overbought=95)
    )
    # Price oscillates near the TOP of its range: %K/%D stay high, never oversold.
    bars = [
        (10, 0, 9),
        (10, 0, 9.2),
        (10, 0, 8.5),
        (10, 0, 9.5),  # bounce up but %K ~ high, not oversold
        (10, 0, 8.8),
        (10, 0, 9.7),  # another up-cross, still not oversold
    ]
    sigs = h.feed(SYM, bars)
    assert all(s.side != OrderSide.BUY for s in sigs)
    assert h.held.get("X", Decimal("0")) == Decimal("0")


def test_atr_fallback_to_pct_stop_during_warmup():
    """When ATR has not warmed up, the BUY must still carry a stop = pct fallback."""
    pct = Decimal("0.05")
    h = _Harness(
        StochasticReversalStrategy(
            "s",
            [SYM],
            k_period=3,
            d_period=2,
            oversold=30,
            overbought=80,
            atr_period=50,
            atr_mult=2.0,
            stop_loss_pct=pct,
        )
    )  # atr_period huge -> warmup
    bars = [
        (20, 18, 19),
        (19, 17, 18),
        (18, 16, 17),
        (17, 15, 15.5),
        (16, 14, 14.5),
        (16.5, 14, 16.2),
    ]
    buys = [s for s in h.feed(SYM, bars) if s.side == OrderSide.BUY]
    assert len(buys) == 1
    price = Decimal("16.2")
    assert buys[0].suggested_stop_loss == price * (Decimal("1") - pct)


# ---- exit ----------------------------------------------------------------


def test_exit_on_overbought():
    h = _Harness(
        StochasticReversalStrategy(
            "s",
            [SYM],
            k_period=3,
            d_period=2,
            oversold=30,
            overbought=80,
            atr_period=3,
            atr_mult=2.0,
        )
    )
    bars = [
        (20, 18, 19),
        (19, 17, 18),
        (18, 16, 17),
        (17, 15, 15.5),
        (16, 14, 14.5),
        (16.5, 14, 16.2),  # entry
        (20, 16, 19.8),  # rallies hard -> %K overbought -> exit
        (22, 18, 21.9),
    ]
    sides = [s.side for s in h.feed(SYM, bars)]
    assert OrderSide.BUY in sides and OrderSide.SELL in sides
    assert sides.index(OrderSide.BUY) < sides.index(OrderSide.SELL)
    assert h.held["X"] == Decimal("0")


def test_cold_start_exit_when_already_overbought():
    """
    Cold start mid-trade: we already hold (context says so) and the oscillator is
    overbought on the very first computable bar — no fresh cross witnessed. A
    position-aware exit must still SELL because the exit is a persistent STATE.
    """
    st = StochasticReversalStrategy("s", [SYM], k_period=3, d_period=2, oversold=20, overbought=70)
    ctx = StrategyContext()
    st.bind_context(ctx)
    ctx.sync_state(positions={"X": SimpleNamespace(quantity=Decimal("1"))})
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # Rising closes pinned to the top of the range -> %K high (overbought).
    bars = [(10, 0, 8), (10, 0, 9), (10, 0, 9.9), (10, 0, 10)]
    sells = []
    for i, (h, lo, c) in enumerate(bars):
        for s in st.on_bar(_bar(SYM, t + timedelta(days=i), h, lo, c)):
            sells.append(s)
    assert sells, "should emit an exit once overbought, even with no fresh cross"
    assert all(s.side == OrderSide.SELL for s in sells)
    assert sells[0].strength == Decimal("1.0")


def test_no_pyramiding_while_held():
    """While held, repeated oversold crosses must NOT add to the position."""
    h = _Harness(
        StochasticReversalStrategy(
            "s",
            [SYM],
            k_period=3,
            d_period=2,
            oversold=30,
            overbought=95,
            atr_period=3,
            atr_mult=2.0,
        )
    )
    bars = [
        (20, 18, 19),
        (19, 17, 18),
        (18, 16, 17),
        (17, 15, 15.5),
        (16, 14, 14.5),
        (16.5, 14, 16.2),  # entry
        (16, 14, 14.6),  # dip again (would be another oversold cross setup)
        (16.5, 14, 16.3),  # would-be second cross while held -> ignored
    ]
    buys = [s for s in h.feed(SYM, bars) if s.side == OrderSide.BUY]
    assert len(buys) == 1


# ---- misc ----------------------------------------------------------------


def test_ignores_unknown_symbol():
    h = _Harness(StochasticReversalStrategy("s", [SYM], k_period=3, d_period=2))
    other = Symbol("NOPE", AssetClass.ETF)
    assert h.step(other, (10, 8, 9)) == []


def test_deterministic():
    bars = [
        (20, 18, 19),
        (19, 17, 18),
        (18, 16, 17),
        (17, 15, 15.5),
        (16, 14, 14.5),
        (16.5, 14, 16.2),
        (20, 16, 19.8),
        (22, 18, 21.9),
    ]
    h1 = _Harness(
        StochasticReversalStrategy("s", [SYM], k_period=3, d_period=2, oversold=30, overbought=80)
    )
    h2 = _Harness(
        StochasticReversalStrategy("s", [SYM], k_period=3, d_period=2, oversold=30, overbought=80)
    )
    s1 = h1.feed(SYM, bars)
    s2 = h2.feed(SYM, bars)
    assert [(s.side, s.strength, s.suggested_stop_loss) for s in s1] == [
        (s.side, s.strength, s.suggested_stop_loss) for s in s2
    ]
