"""
Tests for apex.strategy.library.short_term_reversal.

Position-aware, driven through a context that simulates fills. Covers validation, the
oversold-dip-in-an-uptrend gate, the falling-knife block, and determinism.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.short_term_reversal import ShortTermReversalStrategy

A = Symbol("AAA", AssetClass.ETF)
B = Symbol("BBB", AssetClass.ETF)


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

    def step(self, prices: dict):
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
        ShortTermReversalStrategy("s", [A], reversal_period=0)
    with pytest.raises(ValueError):
        ShortTermReversalStrategy("s", [A], bottom_k=0)


def test_buys_oversold_dip_in_uptrend():
    # AAA: strong long uptrend (SMA lags far below) then a MILD dip that stays above the
    # trend filter -> the oversold leader -> bought and held. BBB keeps rising.
    h = _Harness(
        ShortTermReversalStrategy(
            "s", [A, B], reversal_period=3, bottom_k=1, trend_period=10, vol_window=4
        )
    )
    up = list(range(10, 85, 5))  # steep uptrend: 10,15,...,80
    path_a = up + [78, 76, 74]  # mild pullback, still well above SMA10
    path_b = up + [82, 84, 86]  # keeps rising (never oversold)
    for i in range(len(path_a)):
        h.step({A: path_a[i], B: path_b[i]})
    assert h.held["AAA"] > 0  # oversold dip in an uptrend is held
    assert h.held.get("BBB", 0) == 0  # the steady riser is not the laggard


def test_blocks_falling_knife():
    # A collapses below its SMA: oversold but NOT in an uptrend -> never bought.
    h = _Harness(
        ShortTermReversalStrategy(
            "s", [A, B], reversal_period=3, bottom_k=2, trend_period=8, vol_window=4
        )
    )
    out = []
    path = list(range(40, 8, -2))  # monotonic collapse
    for i in range(len(path)):
        out += h.step({A: path[i], B: path[i] + 1})
    assert all(s.side != OrderSide.BUY for s in out)


def test_deterministic():
    def run():
        h = _Harness(
            ShortTermReversalStrategy(
                "s", [A, B], reversal_period=3, bottom_k=1, trend_period=10, vol_window=4
            )
        )
        path_a = list(range(10, 40, 2)) + [38, 34, 31]
        path_b = list(range(10, 40, 2)) + [40, 41, 42]
        sigs = []
        for i in range(len(path_a)):
            sigs += [(s.symbol.ticker, s.side) for s in h.step({A: path_a[i], B: path_b[i]})]
        return sigs

    assert run() == run()
