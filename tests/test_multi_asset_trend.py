"""
Tests for apex.strategy.library.multi_asset_trend.

This strategy is POSITION-AWARE: each bar it targets "long iff in an uptrend" and
emits the delta against what it actually holds, read from the (broker-reconciled)
StrategyContext. So the tests drive it through a context and simulate fills, the
way the engine / run_once do — that's the real contract.

Covered:
  - the four transitions (flat+uptrend -> BUY, held+uptrend -> nothing,
    held+downtrend -> SELL, flat+downtrend -> nothing),
  - cold start: enters an ALREADY-established uptrend with no fresh cross (the bug
    this strategy was built to fix),
  - inverse-volatility sizing: a calmer sleeve earns more conviction than a wilder
    one entering at the same time, and the calmest hits the full cap (1.0).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.multi_asset_trend import MultiAssetTrendStrategy
from apex.core.models import Bar, Symbol, AssetClass, OrderSide


CALM = Symbol("CALM", AssetClass.ETF)
WILD = Symbol("WILD", AssetClass.ETF)


def _bar(sym: Symbol, t: datetime, price: float) -> Bar:
    p = Decimal(str(price))
    return Bar(symbol=sym, timestamp=t, open=p, high=p, low=p, close=p,
               volume=Decimal("1000"))


class _Harness:
    """
    Minimal stand-in for the engine: binds a context, refreshes it from a simulated
    portfolio before each bar, and applies emitted signals as IMMEDIATE fills so the
    next bar sees the updated holding (mirrors engine fill-before-dispatch ordering).
    """
    def __init__(self, strat: MultiAssetTrendStrategy):
        self.strat = strat
        self.ctx = StrategyContext()
        strat.bind_context(self.ctx)
        self.held: dict[str, Decimal] = {}   # ticker -> qty
        self.t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.i = 0

    def _refresh(self):
        self.ctx.sync_state(positions={
            k: SimpleNamespace(quantity=q) for k, q in self.held.items() if q > 0
        })

    def step(self, sym: Symbol, price: float):
        self._refresh()
        sigs = self.strat.on_bar(_bar(sym, self.t + timedelta(days=self.i), price))
        self.i += 1
        for s in sigs:                       # simulate immediate fills
            self.held[sym.ticker] = Decimal("1") if s.side == OrderSide.BUY else Decimal("0")
        return sigs

    def feed(self, sym: Symbol, prices: list[float]):
        out = []
        for p in prices:
            out.extend(self.step(sym, p))
        return out


def test_fast_must_be_less_than_slow():
    with pytest.raises(ValueError):
        MultiAssetTrendStrategy("s", [CALM], fast_period=200, slow_period=20)


def test_vol_window_floor():
    with pytest.raises(ValueError):
        MultiAssetTrendStrategy("s", [CALM], vol_window=1)


def test_no_signal_during_warmup():
    h = _Harness(MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3))
    assert h.feed(CALM, [10, 11, 12]) == []


def test_uptrend_emits_buy_with_stop():
    h = _Harness(MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3))
    prices = [20, 19, 18, 17, 16, 15, 16, 18, 21, 25, 30]
    buys = [s for s in h.feed(CALM, prices) if s.side == OrderSide.BUY]
    assert len(buys) == 1                     # enters once, then holds (no pyramiding)
    assert buys[0].suggested_stop_loss is not None
    assert buys[0].suggested_stop_loss < Decimal("30")
    assert Decimal("0") < buys[0].strength <= Decimal("1")


def test_cold_start_enters_established_uptrend():
    """
    THE BUG FIX. Warm the strategy over a window in which the cross happened in the
    distant past (price is already well above its SMAs the whole time, so there is
    NO fresh crossover). A position-aware strategy must still enter, because it is
    flat and the trend is up — it does not need to witness the cross.
    """
    h = _Harness(MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3))
    # Steadily rising series: fast > slow on every computable bar, never a cross.
    prices = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
    sigs = h.feed(CALM, prices)
    buys = [s for s in sigs if s.side == OrderSide.BUY]
    assert len(buys) == 1, "should enter the established uptrend exactly once"
    assert h.held["CALM"] > 0


def test_exit_on_trend_break():
    h = _Harness(MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3))
    prices = [16, 15, 14, 13, 12, 13, 15, 18, 22, 26, 30, 28, 24, 20, 16, 12, 8]
    sides = [s.side for s in h.feed(CALM, prices)]
    assert OrderSide.BUY in sides and OrderSide.SELL in sides
    assert sides.index(OrderSide.BUY) < sides.index(OrderSide.SELL)
    # The exit is full-conviction and leaves us flat.
    assert h.held["CALM"] == 0


def test_sell_is_full_conviction():
    h = _Harness(MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3))
    prices = [16, 15, 14, 13, 12, 13, 15, 18, 22, 26, 30, 28, 24, 20, 16, 12, 8]
    sells = [s for s in h.feed(CALM, prices) if s.side == OrderSide.SELL]
    assert sells and sells[0].strength == Decimal("1.0")


def test_no_duplicate_buys_while_held():
    h = _Harness(MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3))
    prices = [16, 15, 14, 13, 12, 13, 15, 18, 22, 26, 30, 34, 38, 42, 46]
    buys = [s for s in h.feed(CALM, prices) if s.side == OrderSide.BUY]
    assert len(buys) == 1


def test_flat_in_downtrend_stays_flat():
    h = _Harness(MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3))
    # Monotonic decline: never an uptrend, never held → no signals at all.
    prices = [30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20]
    assert h.feed(CALM, prices) == []


def test_ignores_unknown_symbol():
    h = _Harness(MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3))
    other = Symbol("NOPE", AssetClass.ETF)
    assert h.step(other, 1.0) == []


def test_inverse_vol_calm_outweighs_wild():
    """
    The risk-parity property. Two sleeves are fed the SAME shape in lockstep — a
    CALM sleeve (gentle) and a WILD sleeve (amplified, higher realized vol) — both
    ending in an uptrend. The calmer sleeve must earn MORE conviction, and the
    calmest sleeve overall must hit the full-conviction cap of 1.0.
    """
    strat = MultiAssetTrendStrategy("s", [CALM, WILD], fast_period=3, slow_period=5, vol_window=5)
    ctx = StrategyContext()
    strat.bind_context(ctx)
    held: dict[str, Decimal] = {}

    # Trough-then-rally so the entry triggers LATE — after the vol window has
    # filled — otherwise both sleeves enter on the vol-warmup fallback (1.0) and
    # the weighting can't differentiate them.
    base = [20, 19, 18, 17, 16, 15, 16, 18, 21, 25, 30]
    calm_path = base
    wild_path = [20 + (p - 20) * 3 for p in base]   # same trend, 3x the wiggle

    buys: dict[str, Decimal] = {}
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(len(base)):
        for sym, path in ((CALM, calm_path), (WILD, wild_path)):
            ctx.sync_state(positions={
                k: SimpleNamespace(quantity=q) for k, q in held.items() if q > 0})
            for sig in strat.on_bar(_bar(sym, t + timedelta(days=i), path[i])):
                if sig.side == OrderSide.BUY:
                    buys[sym.ticker] = sig.strength
                    held[sym.ticker] = Decimal("1")

    assert "CALM" in buys and "WILD" in buys, "both sleeves should enter long"
    assert buys["CALM"] > buys["WILD"], "calmer sleeve must earn more conviction"
    assert buys["CALM"] == Decimal("1.0"), "calmest sleeve gets the full cap"
    assert buys["WILD"] >= strat.min_strength


def test_deterministic():
    prices = [16, 15, 14, 13, 12, 13, 15, 18, 22, 26, 30, 28, 24, 20]
    h1 = _Harness(MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3))
    h2 = _Harness(MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3))
    sig1 = h1.feed(CALM, prices)
    sig2 = h2.feed(CALM, prices)
    assert [(s.side, s.strength) for s in sig1] == [(s.side, s.strength) for s in sig2]
