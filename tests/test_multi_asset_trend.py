"""
Tests for apex.strategy.library.multi_asset_trend.

Covers the trend pipeline (entries/exits identical in shape to sma_crossover) and
the distinguishing feature: INVERSE-VOLATILITY sizing via SignalEvent.strength —
a calmer sleeve must earn more conviction than a wilder one entering at the same
time, and the calmest sleeve must hit the full-conviction cap (1.0).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apex.strategy.library.multi_asset_trend import MultiAssetTrendStrategy
from apex.core.models import Bar, Symbol, AssetClass, OrderSide


CALM = Symbol("CALM", AssetClass.ETF)
WILD = Symbol("WILD", AssetClass.ETF)


def _bar(sym: Symbol, t: datetime, price: float) -> Bar:
    p = Decimal(str(price))
    return Bar(symbol=sym, timestamp=t, open=p, high=p, low=p, close=p,
               volume=Decimal("1000"))


def _feed_single(strat: MultiAssetTrendStrategy, sym: Symbol, prices: list[float]):
    """Feed one symbol a price path; collect signals."""
    out = []
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i, p in enumerate(prices):
        out.extend(strat.on_bar(_bar(sym, t + timedelta(days=i), p)))
    return out


def test_fast_must_be_less_than_slow():
    with pytest.raises(ValueError):
        MultiAssetTrendStrategy("s", [CALM], fast_period=200, slow_period=20)


def test_vol_window_floor():
    with pytest.raises(ValueError):
        MultiAssetTrendStrategy("s", [CALM], vol_window=1)


def test_no_signal_during_warmup():
    strat = MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3)
    assert _feed_single(strat, CALM, [10, 11, 12]) == []


def test_golden_cross_emits_buy_with_stop():
    strat = MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3)
    prices = [20, 19, 18, 17, 16, 15, 16, 18, 21, 25, 30]
    buys = [s for s in _feed_single(strat, CALM, prices) if s.side == OrderSide.BUY]
    assert len(buys) >= 1
    assert buys[0].suggested_stop_loss is not None
    assert buys[0].suggested_stop_loss < Decimal("30")
    # strength is a valid conviction in (0, 1].
    assert Decimal("0") < buys[0].strength <= Decimal("1")


def test_death_cross_exits_after_entry():
    strat = MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3)
    prices = [16, 15, 14, 13, 12, 13, 15, 18, 22, 26, 30, 28, 24, 20, 16, 12, 8]
    sides = [s.side for s in _feed_single(strat, CALM, prices)]
    assert OrderSide.BUY in sides and OrderSide.SELL in sides
    assert sides.index(OrderSide.BUY) < sides.index(OrderSide.SELL)
    # An exit is always full-conviction.
    sells = [s for s in _feed_single(
        MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3),
        CALM, prices) if s.side == OrderSide.SELL]
    assert sells[0].strength == Decimal("1.0")


def test_no_duplicate_buys_while_long():
    strat = MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3)
    prices = [16, 15, 14, 13, 12, 13, 15, 18, 22, 26, 30, 34, 38, 42, 46]
    buys = [s for s in _feed_single(strat, CALM, prices) if s.side == OrderSide.BUY]
    assert len(buys) == 1


def test_ignores_unknown_symbol():
    strat = MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3)
    other = Symbol("NOPE", AssetClass.ETF)
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert strat.on_bar(_bar(other, t, 1.0)) == []


def test_inverse_vol_calm_outweighs_wild():
    """
    The core risk-parity property. Feed two sleeves the SAME bars in lockstep:
    a CALM sleeve (small wiggle) and a WILD sleeve (large wiggle), both ending in
    an identical golden cross. The calmer sleeve must receive MORE conviction, and
    the calmest sleeve overall must hit the full-conviction cap of 1.0.
    """
    strat = MultiAssetTrendStrategy(
        "s", [CALM, WILD], fast_period=3, slow_period=5, vol_window=5
    )
    # A trough-then-rally shape that produces a golden cross on the last bar.
    base = [20, 19, 18, 17, 16, 15, 16, 18, 21, 25, 30]
    # CALM: gentle path. WILD: same trend but amplified deviations around it →
    # higher realized volatility, hence lower inverse-vol conviction.
    calm_path = base
    wild_path = [20 + (p - 20) * 3 for p in base]

    buys: dict[str, Decimal] = {}
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(len(base)):
        # Feed both symbols on the same timestamp (lockstep), CALM then WILD.
        for sig in strat.on_bar(_bar(CALM, t + timedelta(days=i), calm_path[i])):
            if sig.side == OrderSide.BUY:
                buys["CALM"] = sig.strength
        for sig in strat.on_bar(_bar(WILD, t + timedelta(days=i), wild_path[i])):
            if sig.side == OrderSide.BUY:
                buys["WILD"] = sig.strength

    assert "CALM" in buys and "WILD" in buys, "both sleeves should enter long"
    assert buys["CALM"] > buys["WILD"], "calmer sleeve must earn more conviction"
    # The calmest sleeve across the universe earns the full position cap.
    assert buys["CALM"] == Decimal("1.0")
    # The wilder sleeve is scaled down but still tradeable (>= floor).
    assert buys["WILD"] >= strat.min_strength


def test_deterministic():
    prices = [16, 15, 14, 13, 12, 13, 15, 18, 22, 26, 30, 28, 24, 20]
    s1 = MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3)
    s2 = MultiAssetTrendStrategy("s", [CALM], fast_period=3, slow_period=5, vol_window=3)
    sig1 = _feed_single(s1, CALM, prices)
    sig2 = _feed_single(s2, CALM, prices)
    assert [(s.side, s.strength) for s in sig1] == [(s.side, s.strength) for s in sig2]
