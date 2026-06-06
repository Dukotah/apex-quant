"""
Tests for apex.strategy.library.value_momentum.

Position-aware like the value / cross-sectional strategies, so driven through a context
that simulates fills. Covers validation, the combined value+momentum ranking, the weight
knob (value_weight 1.0 == pure value, 0.0 == pure momentum), the optional trend filter,
and determinism.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.value_momentum import ValueMomentumStrategy

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
        ValueMomentumStrategy("s", [A], value_period=1)
    with pytest.raises(ValueError):
        ValueMomentumStrategy("s", [A], value_period=10, skip_recent=10)  # skip >= window
    with pytest.raises(ValueError):
        ValueMomentumStrategy("s", [A], skip_recent=-1)
    with pytest.raises(ValueError):
        ValueMomentumStrategy("s", [A], mom_period=1)
    with pytest.raises(ValueError):
        ValueMomentumStrategy("s", [A], top_k=0)
    with pytest.raises(ValueError):
        ValueMomentumStrategy("s", [A], value_weight=Decimal("1.5"))


def test_warmup_no_signals():
    # value_period+1 bars are required before any combined score exists (value is the
    # binding leg — momentum needs far fewer bars).
    h = _Harness(
        ValueMomentumStrategy(
            "s", [A, B, C], value_period=8, skip_recent=2, mom_period=3, top_k=1, vol_window=3
        )
    )
    out = []
    for _ in range(8):  # only 8 bars < value_period+1 = 9
        out += h.step_all({A: 10, B: 10, C: 10})
    assert out == []


def test_pure_value_weight_buys_the_cheap_laggard():
    # value_weight=1.0 collapses to pure value: the worst long-run performer is "cheapest".
    # AAA falls over the window, CCC rises -> AAA is the value pick, never CCC.
    h = _Harness(
        ValueMomentumStrategy(
            "s",
            [A, B, C],
            value_period=8,
            skip_recent=0,
            mom_period=3,
            top_k=1,
            value_weight=Decimal("1.0"),
            vol_window=3,
        )
    )
    buys = []
    for d in range(16):
        for s in h.step_all({A: 30 - d, B: 20, C: 10 + d}):
            if s.side == OrderSide.BUY:
                buys.append(s.symbol.ticker)
    assert "AAA" in buys  # the cheap laggard is bought (value)
    assert "CCC" not in buys  # the expensive long-run winner is never bought


def test_pure_momentum_weight_buys_the_leader():
    # value_weight=0.0 collapses to pure momentum: the strongest recent performer is held.
    # CCC ramps up hardest over the momentum window -> momentum settles holding CCC.
    h = _Harness(
        ValueMomentumStrategy(
            "s",
            [A, B, C],
            value_period=8,
            skip_recent=0,
            mom_period=4,
            top_k=1,
            value_weight=Decimal("0.0"),
            vol_window=3,
        )
    )
    for d in range(16):
        h.step_all({A: 30 - d, B: 20, C: 10 + d})
    assert h.held["CCC"] > 0  # momentum holds the recent leader
    assert h.held.get("AAA", 0) == 0  # not the falling laggard


def test_combined_prefers_cheap_and_trending():
    # Equal weight: a sleeve that is BOTH a long-run laggard (cheap) AND recently turning up
    # (momentum) should beat one that is only one of the two.
    #   AAA: long, deep decline then a sharp recent rally -> cheap on value AND high momentum.
    #   BBB: steady mild uptrend -> mediocre value, decent momentum.
    #   CCC: steady long rise -> expensive on value, decent momentum.
    # AAA wins the combined rank; CCC (expensive) should not be held at top_k=1.
    h = _Harness(
        ValueMomentumStrategy(
            "s", [A, B, C], value_period=10, skip_recent=0, mom_period=3, top_k=1, vol_window=3
        )
    )
    # 11-bar deep fall for AAA, then a 6-bar rally (recent momentum), others steady.
    a_path = [40, 36, 32, 28, 24, 20, 16, 12, 10, 9, 8, 11, 15, 20, 26, 33, 41]
    for d, a in enumerate(a_path):
        h.step_all({A: a, B: 18 + d * 0.2, C: 10 + d})
    assert h.held["AAA"] > 0  # cheap AND trending up = the combined pick
    assert h.held.get("CCC", 0) == 0  # the expensive steady winner is not held


def test_trend_filter_blocks_falling_pick():
    # With the trend filter ON, even a combined-rank leader must be above its SMA; an all-
    # downtrend universe -> never buy (no catching falling knives).
    h = _Harness(
        ValueMomentumStrategy(
            "s",
            [A, B, C],
            value_period=8,
            skip_recent=0,
            mom_period=3,
            top_k=2,
            use_trend_filter=True,
            trend_period=5,
            vol_window=3,
        )
    )
    out = []
    for d in range(18):
        out += h.step_all({A: 40 - d, B: 40 - d * 1.3, C: 40 - d * 1.6})
    assert all(s.side != OrderSide.BUY for s in out)


def test_deterministic():
    def run():
        h = _Harness(
            ValueMomentumStrategy(
                "s", [A, B, C], value_period=8, skip_recent=0, mom_period=3, top_k=2, vol_window=4
            )
        )
        sigs = []
        for d in range(18):
            sigs += [(s.symbol.ticker, s.side) for s in h.step_all({A: 30 - d, B: 20, C: 10 + d})]
        return sigs

    assert run() == run()
