"""
Tests for apex.strategy.library.value_momentum.

Position-aware like the trend / cross-sectional / cross-asset-value strategies, so
driven through a context that simulates fills. Covers validation, each leg's metric
against hand-computed values, the COMBINED composite ranking (the whole point — an
asset that is both cheap AND trending beats one that is only cheap or only trending),
the optional trend-trap filter, graceful degradation, and determinism.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.value_momentum import (
    DEFAULT_MOM_LOOKBACKS,
    DEFAULT_VALMOM_UNIVERSE,
    ValueMomentumStrategy,
    default_universe,
)

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


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def test_validation():
    with pytest.raises(ValueError):
        ValueMomentumStrategy("s", [A], value_period=1)
    with pytest.raises(ValueError):
        ValueMomentumStrategy("s", [A], value_period=10, skip_recent=10)   # skip >= window
    with pytest.raises(ValueError):
        ValueMomentumStrategy("s", [A], skip_recent=-1)
    with pytest.raises(ValueError):
        ValueMomentumStrategy("s", [A], top_k=0)
    with pytest.raises(ValueError):
        ValueMomentumStrategy("s", [A], mom_lookbacks=[])
    with pytest.raises(ValueError):
        ValueMomentumStrategy("s", [A], mom_lookbacks=[0])
    with pytest.raises(ValueError):
        ValueMomentumStrategy("s", [A], mom_lookbacks=[4], mom_weights=[1.0, 2.0])  # mismatch
    with pytest.raises(ValueError):
        ValueMomentumStrategy("s", [A], value_weight=0, momentum_weight=0)
    with pytest.raises(ValueError):
        ValueMomentumStrategy("s", [A], vol_window=1)


# --------------------------------------------------------------------------
# raw leg metrics — hand-computed
# --------------------------------------------------------------------------

def test_value_raw_is_negative_long_horizon_return():
    # value_period=4, skip_recent=0: value = -(close[-1]/close[-5] - 1).
    s = ValueMomentumStrategy("s", [A], value_period=4, skip_recent=0,
                              mom_lookbacks=[2], vol_window=2)
    closes = [100.0, 110.0, 120.0, 130.0, 80.0]   # last vs 5-ago (100): 80/100-1 = -0.20
    assert s._value_raw(closes) == pytest.approx(0.20)   # -(-0.20) = +0.20 (a cheap laggard)
    # one bar short of value_period+1 -> None (warmup)
    assert s._value_raw(closes[:4]) is None


def test_value_raw_respects_skip_recent():
    # value_period=4, skip_recent=1: measure from close[-5] to close[-2] (skip the last bar).
    s = ValueMomentumStrategy("s", [A], value_period=4, skip_recent=1,
                              mom_lookbacks=[2], vol_window=2)
    closes = [100.0, 0.0, 0.0, 0.0, 90.0, 999.0]   # old=100 (index -5), recent=90 (index -2)
    assert s._value_raw(closes) == pytest.approx(0.10)   # -(90/100 - 1) = +0.10


def test_momentum_raw_blends_multiple_lookbacks():
    # Two lookbacks, equal weight. close[-1]=120.
    #   lb=2: 120/close[-3]=120/100 - 1 = 0.20
    #   lb=4: 120/close[-5]=120/80  - 1 = 0.50
    # blend = (0.20 + 0.50)/2 = 0.35
    s = ValueMomentumStrategy("s", [A], value_period=6, skip_recent=0,
                              mom_lookbacks=[2, 4], vol_window=2)
    closes = [80.0, 90.0, 100.0, 110.0, 115.0, 120.0]
    assert s._momentum_raw(closes) == pytest.approx(0.35)
    # warmup: needs longest lookback + 1 = 5 points; 4 -> None
    assert s._momentum_raw(closes[:4]) is None


def test_momentum_raw_weighted():
    # Weighted blend: lb=2 weight 3, lb=4 weight 1.
    #   lb=2 ret = 0.20, lb=4 ret = 0.50  ->  (3*0.20 + 1*0.50)/4 = 1.10/4 = 0.275
    s = ValueMomentumStrategy("s", [A], value_period=6, skip_recent=0,
                              mom_lookbacks=[2, 4], mom_weights=[3.0, 1.0], vol_window=2)
    closes = [80.0, 90.0, 100.0, 110.0, 115.0, 120.0]
    assert s._momentum_raw(closes) == pytest.approx(0.275)


# --------------------------------------------------------------------------
# composite scoring — the combined VALUE + MOMENTUM rank (the thesis)
# --------------------------------------------------------------------------

def test_composite_blends_zscored_legs():
    # Hand-check the composite math directly. Three assets with known raw legs.
    s = ValueMomentumStrategy("s", [A, B, C], value_period=4, skip_recent=0,
                              mom_lookbacks=[2], value_weight=0.5, momentum_weight=0.5,
                              vol_window=2)
    s._value = {"AAA": 0.30, "BBB": 0.00, "CCC": -0.30}   # AAA cheapest
    s._mom = {"AAA": -0.10, "BBB": 0.00, "CCC": 0.10}     # CCC strongest momentum
    comp = s._composites()

    # z-scores: symmetric raw sets {+x,0,-x} -> z = {+1.2247.., 0, -1.2247..} (pstdev).
    zmag = (0.30 - 0.0) / math.sqrt((0.30**2 + 0.0 + 0.30**2) / 3)
    assert comp["AAA"] == pytest.approx(0.5 * zmag + 0.5 * (-zmag))   # = 0 (cheap but weak)
    assert comp["BBB"] == pytest.approx(0.0)
    assert comp["CCC"] == pytest.approx(0.5 * (-zmag) + 0.5 * zmag)   # = 0 (dear but strong)
    # Pure-value AAA and pure-momentum CCC cancel to the SAME composite as flat BBB.


def test_composite_prefers_both_cheap_and_trending():
    # The whole point: an asset that is BOTH cheap AND trending must outrank one that is
    # only cheap or only strong. Make AAA top on both legs.
    s = ValueMomentumStrategy("s", [A, B, C], value_period=4, skip_recent=0,
                              mom_lookbacks=[2], vol_window=2)
    s._value = {"AAA": 0.30, "BBB": 0.00, "CCC": -0.30}   # AAA cheapest
    s._mom = {"AAA": 0.30, "BBB": 0.00, "CCC": -0.30}     # AAA strongest too
    comp = s._composites()
    assert comp["AAA"] == max(comp.values())              # both legs agree -> clear winner
    assert s._in_top_k("AAA", comp)
    assert not s._in_top_k("CCC", comp)


def test_composite_skips_assets_missing_a_leg():
    # An asset with only one leg scored must NOT enter the ranking.
    s = ValueMomentumStrategy("s", [A, B, C], value_period=4, skip_recent=0,
                              mom_lookbacks=[2], vol_window=2)
    s._value = {"AAA": 0.30, "BBB": 0.00, "CCC": None}
    s._mom = {"AAA": 0.10, "BBB": None, "CCC": 0.50}
    comp = s._composites()
    assert set(comp) == {"AAA"}        # only AAA has BOTH legs


# --------------------------------------------------------------------------
# end-to-end on synthetic bars
# --------------------------------------------------------------------------

def test_warmup_no_signals():
    # Needs value_period+1 AND longest-momentum-lookback+1 bars before any score.
    h = _Harness(ValueMomentumStrategy("s", [A, B, C], value_period=8, skip_recent=0,
                                       mom_lookbacks=[4], top_k=1, vol_window=3))
    out = []
    for _ in range(8):                          # 8 bars < value_period+1 = 9
        out += h.step_all({A: 10, B: 10, C: 10})
    assert out == []


# Synthetic paths reused by the end-to-end tests. AAA is a deep long-run LAGGARD that
# is RECENTLY recovering (the composite's ideal: cheap AND trending up). BBB rises
# steadily (mid value, steady momentum). CCC is the long-run WINNER that keeps climbing
# (expensive = negative value, positive momentum) — it should be vetoed by the value leg.
# All paths are strictly positive so every Bar is valid.
_AAA = [100, 90, 80, 70, 60, 50, 40, 30, 36, 44, 54, 66, 80, 96, 114, 134]   # crash + recover
_BBB = [50, 52, 54, 56, 58, 60, 62, 64, 66, 68, 70, 72, 74, 76, 78, 80]       # steady up
_CCC = [40, 48, 56, 64, 72, 80, 88, 96, 104, 112, 120, 128, 136, 144, 152, 160]  # long-run winner


def test_buys_the_cheap_recovering_asset():
    # The composite's ideal asset — cheap (deep long-run loss) AND recovering (positive
    # recent momentum) — must be bought at some point during its recovery leg.
    h = _Harness(ValueMomentumStrategy("s", [A, B, C], value_period=8, skip_recent=0,
                                       mom_lookbacks=[3], top_k=1, vol_window=3))
    buys: list[str] = []
    for d in range(len(_AAA)):
        for sig in h.step_all({A: _AAA[d], B: _BBB[d], C: _CCC[d]}):
            if sig.side == OrderSide.BUY:
                buys.append(sig.symbol.ticker)
    assert "AAA" in buys                        # the cheap-AND-recovering asset is bought
    # CCC is the long-run WINNER (expensive) the whole way: the value leg vetoes it at
    # top_k=1 every bar it competes, so it is never the sole composite leader.
    assert "CCC" not in buys


def test_signal_carries_stop_and_reason():
    h = _Harness(ValueMomentumStrategy("s", [A, B, C], value_period=8, skip_recent=0,
                                       mom_lookbacks=[3], top_k=2, vol_window=3,
                                       stop_loss_pct=Decimal("0.05")))
    captured = []
    for d in range(len(_AAA)):
        for sig in h.step_all({A: _AAA[d], B: _BBB[d], C: _CCC[d]}):
            if sig.side == OrderSide.BUY:
                captured.append(sig)
    assert captured, "expected at least one BUY signal"
    sig = captured[0]
    assert sig.suggested_stop_loss is not None
    assert isinstance(sig.suggested_stop_loss, Decimal)
    assert sig.suggested_stop_loss > Decimal("0")        # a protective stop below entry
    assert Decimal("0") < sig.strength <= Decimal("1")
    assert "value+momentum composite" in sig.reason


def test_trend_filter_blocks_falling_composite_leader():
    # With the trend filter ON, every sleeve is in a sustained downtrend (below its SMA),
    # so even the top composite must stay flat — never catch the falling knife.
    h = _Harness(ValueMomentumStrategy("s", [A, B, C], value_period=8, skip_recent=0,
                                       mom_lookbacks=[3], top_k=2, use_trend_filter=True,
                                       trend_period=5, vol_window=3))
    out = []
    for d in range(20):
        out += h.step_all({A: 40 - d, B: 40 - d * 1.3, C: 40 - d * 1.6})
    assert all(s.side != OrderSide.BUY for s in out)   # never buy into a downtrend


def test_single_live_asset_degrades_gracefully():
    # With only one asset that has both legs, both Z-legs are 0 (no dispersion); the
    # composite is 0 and the lone asset is trivially top-K -> it should still be buyable,
    # not crash on a divide-by-zero.
    s = ValueMomentumStrategy("s", [A], value_period=4, skip_recent=0,
                              mom_lookbacks=[2], top_k=1, vol_window=2)
    s._value = {"AAA": 0.30}
    s._mom = {"AAA": 0.10}
    comp = s._composites()
    assert comp == {"AAA": pytest.approx(0.0)}
    assert s._in_top_k("AAA", comp)


def test_deterministic():
    def run():
        h = _Harness(ValueMomentumStrategy("s", [A, B, C], value_period=8, skip_recent=0,
                                           mom_lookbacks=[3, 6], top_k=2, vol_window=4))
        sigs = []
        aaa = [100, 90, 80, 70, 60, 50, 40, 30, 36, 44, 54, 66, 80, 96, 114, 134, 150, 168]
        bbb = [50, 52, 54, 56, 58, 60, 62, 64, 66, 68, 70, 72, 74, 76, 78, 80, 82, 84]
        ccc = [120, 116, 112, 108, 104, 100, 96, 92, 88, 84, 80, 76, 72, 68, 64, 60, 56, 52]
        for d in range(len(aaa)):
            sigs += [(s.symbol.ticker, s.side) for s in
                     h.step_all({A: aaa[d], B: bbb[d], C: ccc[d]})]
        return sigs
    assert run() == run()


def test_default_universe_is_the_broad_expanded_pool():
    syms = default_universe()
    assert [s.ticker for s in syms] == list(DEFAULT_VALMOM_UNIVERSE)
    assert all(s.asset_class == AssetClass.ETF for s in syms)
    assert len(DEFAULT_VALMOM_UNIVERSE) == 10            # the richer 10-ETF pool
    assert DEFAULT_MOM_LOOKBACKS == (21, 63, 126, 252)   # ~1/3/6/12 months
