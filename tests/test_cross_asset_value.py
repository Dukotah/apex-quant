"""
Tests for apex.strategy.library.cross_asset_value.

Position-aware like the trend/cross-sectional strategies, so driven through a context
that simulates fills. Covers validation, the value (long-horizon reversal) ranking, the
optional trend-trap filter, and determinism.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.cross_asset_value import CrossAssetValueStrategy

A = Symbol("AAA", AssetClass.ETF)
B = Symbol("BBB", AssetClass.ETF)
C = Symbol("CCC", AssetClass.ETF)


def _bar(sym, t, price):
    p = Decimal(str(price))
    return Bar(symbol=sym, timestamp=t, open=p, high=p, low=p, close=p, volume=Decimal("1000"))


class _Harness:
    def __init__(self, strat):
        self.strat = strat
        self.ctx = StrategyContext()
        strat.bind_context(self.ctx)
        self.held: dict[str, Decimal] = {}
        self.t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.i = 0

    def step_all(self, prices: dict):
        """Feed one bar per symbol (same day) and apply fills; return all signals."""
        self.ctx.sync_state(
            positions={k: SimpleNamespace(quantity=q) for k, q in self.held.items() if q > 0}
        )
        out = []
        for sym, px in prices.items():
            for s in self.strat.on_bar(_bar(sym, self.t + timedelta(days=self.i), px)):
                out.append(s)
                self.held[sym.ticker] = Decimal("1") if s.side == OrderSide.BUY else Decimal("0")
        self.i += 1
        return out


def test_validation():
    with pytest.raises(ValueError):
        CrossAssetValueStrategy("s", [A], value_period=1)
    with pytest.raises(ValueError):
        CrossAssetValueStrategy("s", [A], value_period=10, skip_recent=10)  # skip >= window
    with pytest.raises(ValueError):
        CrossAssetValueStrategy("s", [A], skip_recent=-1)
    with pytest.raises(ValueError):
        CrossAssetValueStrategy("s", [A], top_k=0)


def test_warmup_no_signals():
    # value_period+1 bars are required before any score exists.
    h = _Harness(
        CrossAssetValueStrategy(
            "s", [A, B, C], value_period=8, skip_recent=2, top_k=1, vol_window=3
        )
    )
    out = []
    for _ in range(8):  # only 8 bars < value_period+1 = 9
        out += h.step_all({A: 10, B: 10, C: 10})
    assert out == []


def test_buys_the_cheapest_laggard_not_the_winner():
    # value = long-horizon reversal: the worst LONG-RUN performer is the "cheapest".
    # AAA falls over the window, CCC rises -> AAA should be the value pick, never CCC.
    h = _Harness(
        CrossAssetValueStrategy(
            "s", [A, B, C], value_period=8, skip_recent=0, top_k=1, vol_window=3
        )
    )
    buys = []
    # AAA declines, BBB flat-ish, CCC rises: AAA is the long-run laggard = cheapest.
    for d in range(16):
        for s in h.step_all({A: 30 - d, B: 20, C: 10 + d}):
            if s.side == OrderSide.BUY:
                buys.append(s.symbol.ticker)
    assert "AAA" in buys  # the cheap laggard is bought (value)
    assert "CCC" not in buys  # the expensive winner is never bought
    assert h.held["AAA"] > 0


def test_opposite_of_momentum():
    # Same price paths, top_k=1: value picks the LAGGARD where momentum would pick the
    # LEADER. AAA ramps up hardest, CCC falls -> value must settle holding CCC (cheap),
    # NOT the momentum leader AAA. (Asserts the steady state: the very first scored bar
    # has a one-bar cross-symbol transient where the first-processed symbol is trivially
    # top-K before the others are scored; it self-corrects on the next bar.)
    h = _Harness(
        CrossAssetValueStrategy(
            "s", [A, B, C], value_period=8, skip_recent=0, top_k=1, vol_window=3
        )
    )
    for d in range(16):
        h.step_all({A: 10 + d * 2, B: 18, C: 30 - d})
    assert h.held["CCC"] > 0  # value settles on the cheap faller
    assert h.held.get("AAA", 0) == 0  # not the momentum leader


def test_trend_filter_blocks_falling_value_trap():
    # With the trend filter ON, the cheapest sleeve is also in a downtrend (a value
    # trap) -> must stay flat rather than catch the falling knife.
    h = _Harness(
        CrossAssetValueStrategy(
            "s",
            [A, B, C],
            value_period=8,
            skip_recent=0,
            top_k=2,
            use_trend_filter=True,
            trend_period=5,
            vol_window=3,
        )
    )
    out = []
    for d in range(18):
        out += h.step_all({A: 40 - d, B: 40 - d * 1.3, C: 40 - d * 1.6})
    assert all(s.side != OrderSide.BUY for s in out)  # never buy into a downtrend


def test_deterministic():
    def run():
        h = _Harness(
            CrossAssetValueStrategy(
                "s", [A, B, C], value_period=8, skip_recent=0, top_k=2, vol_window=4
            )
        )
        sigs = []
        for d in range(18):
            sigs += [(s.symbol.ticker, s.side) for s in h.step_all({A: 30 - d, B: 20, C: 10 + d})]
        return sigs

    assert run() == run()
