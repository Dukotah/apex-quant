"""
Tests for apex.strategy.library.cross_sectional_momentum.

Position-aware like the trend strategy, so driven through a context that simulates
fills. Covers validation, the top-K relative-strength gate, the absolute trend
filter, and the four transitions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.cross_sectional_momentum import CrossSectionalMomentumStrategy

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
        self.ctx.sync_state(positions={k: SimpleNamespace(quantity=q)
                                       for k, q in self.held.items() if q > 0})
        out = []
        for sym, px in prices.items():
            for s in self.strat.on_bar(_bar(sym, self.t + timedelta(days=self.i), px)):
                out.append(s)
                self.held[sym.ticker] = Decimal("1") if s.side == OrderSide.BUY else Decimal("0")
        self.i += 1
        return out


def test_validation():
    with pytest.raises(ValueError):
        CrossSectionalMomentumStrategy("s", [A], mom_period=1)
    with pytest.raises(ValueError):
        CrossSectionalMomentumStrategy("s", [A], top_k=0)


def test_warmup_no_signals():
    h = _Harness(CrossSectionalMomentumStrategy("s", [A, B, C], mom_period=5, top_k=1,
                                                trend_period=5, vol_window=3))
    out = []
    for _ in range(4):
        out += h.step_all({A: 10, B: 10, C: 10})
    assert out == []


def test_holds_top_k_leader_and_exits_laggard():
    # 3 sleeves; top_k=1. AAA ramps hardest -> should be the held leader.
    h = _Harness(CrossSectionalMomentumStrategy("s", [A, B, C], mom_period=6, top_k=1,
                                                trend_period=6, vol_window=4))
    # AAA ramps hardest -> the clear relative-strength leader over the whole path.
    buys = []
    for d in range(16):
        for s in h.step_all({A: 10 + d * 2, B: 10 + d, C: 10 + d * 0.5}):
            if s.side == OrderSide.BUY:
                buys.append(s.symbol.ticker)
    assert "AAA" in buys                      # the strongest leader is bought
    assert h.held["AAA"] > 0
    # Only top_k=1 held: BBB/CCC should never be the held leader.
    assert h.held.get("BBB", 0) == 0 and h.held.get("CCC", 0) == 0


def test_absolute_filter_blocks_falling_leader():
    # All three FALLING: even the "least bad" (relative leader) is below its SMA,
    # so the absolute trend filter must keep us flat.
    h = _Harness(CrossSectionalMomentumStrategy("s", [A, B, C], mom_period=5, top_k=2,
                                                trend_period=5, vol_window=3))
    out = []
    for d in range(14):
        out += h.step_all({A: 30 - d, B: 30 - d * 1.5, C: 30 - d * 2})
    assert all(s.side != OrderSide.BUY for s in out)   # never long a downtrend


def test_deterministic():
    def run():
        h = _Harness(CrossSectionalMomentumStrategy("s", [A, B, C], mom_period=6, top_k=2,
                                                    trend_period=6, vol_window=4))
        sigs = []
        for d in range(16):
            sigs += [(s.symbol.ticker, s.side) for s in
                     h.step_all({A: 10 + d * 2, B: 10 + d, C: 10 + d * 0.5})]
        return sigs
    assert run() == run()
