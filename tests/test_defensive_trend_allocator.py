"""
Tests for apex.strategy.library.defensive_trend_allocator.

Like multi_asset_trend, this strategy is POSITION-AWARE: each bar it targets
"long iff trend AND absolute-momentum agree" and emits the delta against what it
actually holds (read from the broker-reconciled StrategyContext). The harness
drives it through a context and simulates immediate fills, the way the engine /
run_once do.

Covered:
  - constructor validation of every parameter,
  - dual-gate entry: needs BOTH the barbell trend vote AND positive absolute
    momentum (trend alone is not enough),
  - state-based enter-once / exit-on-break / no-pyramiding,
  - cold start into an established uptrend (no fresh cross needed),
  - inverse-vol weighting (calmer sleeve > wilder sleeve, calmest hits 1.0),
  - the capped vol-target overlay only ever scales DOWN (never levers up),
  - the seasonal Halloween tilt de-weights summer entries,
  - determinism.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.defensive_trend_allocator import (
    DefensiveTrendAllocatorStrategy,
)

CALM = Symbol("CALM", AssetClass.ETF)
WILD = Symbol("WILD", AssetClass.ETF)

# Small, fast lookbacks so tests warm up quickly. All canonical defaults are
# exercised separately via the constructor-default test.
FAST_KW = dict(
    trend_lookbacks=[3, 5],
    abs_mom_lookback=3,
    vol_window=3,
)


def _bar(sym: Symbol, t: datetime, price: float) -> Bar:
    p = Decimal(str(price))
    return Bar(symbol=sym, timestamp=t, open=p, high=p, low=p, close=p, volume=Decimal("1000"))


class _Harness:
    """Binds a context, refreshes holdings before each bar, applies emitted signals
    as immediate fills (mirrors the engine's fill-before-dispatch ordering)."""

    def __init__(self, strat: DefensiveTrendAllocatorStrategy, start: datetime | None = None):
        self.strat = strat
        self.ctx = StrategyContext()
        strat.bind_context(self.ctx)
        self.held: dict[str, Decimal] = {}
        self.t = start or datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.i = 0

    def _refresh(self):
        self.ctx.sync_state(
            positions={k: SimpleNamespace(quantity=q) for k, q in self.held.items() if q > 0}
        )

    def step(self, sym: Symbol, price: float):
        self._refresh()
        sigs = self.strat.on_bar(_bar(sym, self.t + timedelta(days=self.i), price))
        self.i += 1
        for s in sigs:
            self.held[sym.ticker] = Decimal("1") if s.side == OrderSide.BUY else Decimal("0")
        return sigs

    def feed(self, sym: Symbol, prices: list[float]):
        out = []
        for p in prices:
            out.extend(self.step(sym, p))
        return out


# ----------------------------------------------------------- validation


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(trend_lookbacks=[1]),  # lookback < 2
        dict(trend_lookbacks=[]),  # empty
        dict(trend_threshold=0.0),  # out of (0, 1]
        dict(trend_threshold=1.5),
        dict(abs_mom_lookback=1),
        dict(vol_window=1),
        dict(target_vol=0.0),
        dict(summer_weight=Decimal("0")),  # out of (0, 1]
        dict(summer_weight=Decimal("1.5")),
        dict(min_strength=Decimal("1.5")),
        dict(vol_method="bogus"),
        dict(ewma_lambda=1.5),
    ],
)
def test_constructor_validation(kwargs):
    with pytest.raises(ValueError):
        DefensiveTrendAllocatorStrategy("s", [CALM], **kwargs)


def test_defaults_are_canonical():
    s = DefensiveTrendAllocatorStrategy("s", [CALM])
    assert s.trend_lookbacks == [50, 200]
    assert s.abs_mom_lookback == 252
    assert s.vol_window == 63
    assert s.target_vol == 0.15
    assert s.summer_weight == Decimal("0.5")
    assert s.vol_method == "ewma"


# ----------------------------------------------------------- behavior


def test_no_signal_during_warmup():
    h = _Harness(DefensiveTrendAllocatorStrategy("s", [CALM], **FAST_KW))
    assert h.feed(CALM, [10, 11, 12]) == []


def test_uptrend_emits_buy_with_stop():
    h = _Harness(DefensiveTrendAllocatorStrategy("s", [CALM], **FAST_KW))
    prices = [20, 19, 18, 17, 16, 15, 16, 18, 21, 25, 30]
    buys = [s for s in h.feed(CALM, prices) if s.side == OrderSide.BUY]
    assert len(buys) == 1  # enters once, no pyramiding
    assert buys[0].suggested_stop_loss is not None
    assert buys[0].suggested_stop_loss < Decimal("30")
    assert Decimal("0") < buys[0].strength <= Decimal("1")


def test_trend_without_momentum_does_not_enter():
    """
    THE DUAL-GATE PROPERTY. A series that is in a short-term uptrend (price above
    both SMAs) but whose absolute momentum over abs_mom_lookback is NEGATIVE
    (it ends below where it started `abs_mom_lookback` bars ago) must NOT enter —
    the momentum gate vetoes the trend vote.
    """
    h = _Harness(
        DefensiveTrendAllocatorStrategy(
            "s", [CALM], trend_lookbacks=[2, 3], abs_mom_lookback=8, vol_window=3
        )
    )
    # Big drop, then a small recovery bounce. At the last bar price > SMA(2),SMA(3)
    # (recent uptick) but is still far below the price 8 bars ago → abs-mom < 0.
    prices = [100, 98, 70, 55, 45, 40, 38, 39, 41, 43]
    buys = [s for s in h.feed(CALM, prices) if s.side == OrderSide.BUY]
    assert buys == [], "negative absolute momentum must veto the trend vote"


def test_cold_start_enters_established_uptrend():
    h = _Harness(DefensiveTrendAllocatorStrategy("s", [CALM], **FAST_KW))
    prices = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
    buys = [s for s in h.feed(CALM, prices) if s.side == OrderSide.BUY]
    assert len(buys) == 1
    assert h.held["CALM"] > 0


def test_exit_on_trend_break():
    h = _Harness(DefensiveTrendAllocatorStrategy("s", [CALM], **FAST_KW))
    prices = [16, 15, 14, 13, 12, 13, 15, 18, 22, 26, 30, 28, 24, 20, 16, 12, 8]
    sides = [s.side for s in h.feed(CALM, prices)]
    assert OrderSide.BUY in sides and OrderSide.SELL in sides
    assert sides.index(OrderSide.BUY) < sides.index(OrderSide.SELL)
    assert h.held["CALM"] == 0


def test_sell_is_full_conviction():
    h = _Harness(DefensiveTrendAllocatorStrategy("s", [CALM], **FAST_KW))
    prices = [16, 15, 14, 13, 12, 13, 15, 18, 22, 26, 30, 28, 24, 20, 16, 12, 8]
    sells = [s for s in h.feed(CALM, prices) if s.side == OrderSide.SELL]
    assert sells and sells[0].strength == Decimal("1.0")


def test_no_duplicate_buys_while_held():
    h = _Harness(DefensiveTrendAllocatorStrategy("s", [CALM], **FAST_KW))
    prices = [16, 15, 14, 13, 12, 13, 15, 18, 22, 26, 30, 34, 38, 42, 46]
    buys = [s for s in h.feed(CALM, prices) if s.side == OrderSide.BUY]
    assert len(buys) == 1


def test_ignores_unknown_symbol():
    h = _Harness(DefensiveTrendAllocatorStrategy("s", [CALM], **FAST_KW))
    assert h.step(Symbol("NOPE", AssetClass.ETF), 1.0) == []


def test_inverse_vol_calm_outweighs_wild():
    """Risk-parity property: the calmer sleeve earns more conviction; the calmest
    hits the full cap. Seasonal/vol-target factors are shared across both sleeves
    entering on the same bars, so the RELATIVE ordering isolates inverse-vol."""
    # target_vol huge → the vol-target overlay never bites (factor 1.0), so
    # strength is driven purely by inverse-vol; otherwise both sleeves clamp to
    # the floor on these deliberately-volatile synthetic paths.
    strat = DefensiveTrendAllocatorStrategy(
        "s",
        [CALM, WILD],
        trend_lookbacks=[3, 5],
        abs_mom_lookback=3,
        vol_window=5,
        target_vol=100.0,
    )
    ctx = StrategyContext()
    strat.bind_context(ctx)
    held: dict[str, Decimal] = {}
    # Winter start date so the seasonal factor is 1.0 (isolates inverse-vol/vol-target).
    base = [20, 19, 18, 17, 16, 15, 16, 18, 21, 25, 30]
    calm_path = base
    wild_path = [20 + (p - 20) * 3 for p in base]  # same trend, 3x the wiggle
    buys: dict[str, Decimal] = {}
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i in range(len(base)):
        for sym, path in ((CALM, calm_path), (WILD, wild_path)):
            ctx.sync_state(
                positions={k: SimpleNamespace(quantity=q) for k, q in held.items() if q > 0}
            )
            for sig in strat.on_bar(_bar(sym, t + timedelta(days=i), path[i])):
                if sig.side == OrderSide.BUY:
                    buys[sym.ticker] = sig.strength
                    held[sym.ticker] = Decimal("1")
    assert "CALM" in buys and "WILD" in buys
    assert buys["CALM"] > buys["WILD"], "calmer sleeve must earn more conviction"
    assert buys["WILD"] >= strat.min_strength


def test_vol_target_overlay_only_scales_down():
    """The capped overlay must never exceed 1.0 (no leverage), and must cut
    conviction when annualized portfolio vol exceeds the target."""
    s = DefensiveTrendAllocatorStrategy("s", [CALM], target_vol=0.15, **FAST_KW)
    # Calm regime: tiny daily vol → factor capped at 1.0.
    s._vol["CALM"] = 0.0001
    assert s._vol_target_factor() == Decimal("1")
    # Turbulent regime: 5%/day ≈ 79% annualized >> 15% target → factor < 1.
    s._vol["CALM"] = 0.05
    factor = s._vol_target_factor()
    assert Decimal("0") < factor < Decimal("1")


def test_seasonal_summer_deweights_entry():
    """Same entry, two seasons: a summer (June) entry must carry LESS conviction
    than an identical winter (January) entry, by the summer_weight factor."""
    prices = [20, 19, 18, 17, 16, 15, 16, 18, 21, 25, 30]

    # target_vol huge → isolate the seasonal factor (no overlay clamping to the floor).
    winter = _Harness(
        DefensiveTrendAllocatorStrategy(
            "s", [CALM], summer_weight=Decimal("0.5"), target_vol=100.0, **FAST_KW
        ),
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),  # entry lands in winter
    )
    summer = _Harness(
        DefensiveTrendAllocatorStrategy(
            "s", [CALM], summer_weight=Decimal("0.5"), target_vol=100.0, **FAST_KW
        ),
        start=datetime(2024, 6, 1, tzinfo=timezone.utc),  # entry lands in summer
    )
    w_buy = [s for s in winter.feed(CALM, prices) if s.side == OrderSide.BUY][0]
    s_buy = [s for s in summer.feed(CALM, prices) if s.side == OrderSide.BUY][0]
    assert s_buy.strength < w_buy.strength
    assert s_buy.strength == w_buy.strength * Decimal("0.5")


def test_seasonal_disabled_when_summer_weight_one():
    prices = [20, 19, 18, 17, 16, 15, 16, 18, 21, 25, 30]
    winter = _Harness(
        DefensiveTrendAllocatorStrategy("s", [CALM], summer_weight=Decimal("1"), **FAST_KW),
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    summer = _Harness(
        DefensiveTrendAllocatorStrategy("s", [CALM], summer_weight=Decimal("1"), **FAST_KW),
        start=datetime(2024, 6, 1, tzinfo=timezone.utc),
    )
    w_buy = [s for s in winter.feed(CALM, prices) if s.side == OrderSide.BUY][0]
    s_buy = [s for s in summer.feed(CALM, prices) if s.side == OrderSide.BUY][0]
    assert s_buy.strength == w_buy.strength


def test_deterministic():
    prices = [16, 15, 14, 13, 12, 13, 15, 18, 22, 26, 30, 28, 24, 20]
    h1 = _Harness(DefensiveTrendAllocatorStrategy("s", [CALM], **FAST_KW))
    h2 = _Harness(DefensiveTrendAllocatorStrategy("s", [CALM], **FAST_KW))
    sig1 = h1.feed(CALM, prices)
    sig2 = h2.feed(CALM, prices)
    assert [(s.side, s.strength) for s in sig1] == [(s.side, s.strength) for s in sig2]
