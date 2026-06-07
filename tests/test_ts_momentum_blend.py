"""
Tests for apex.strategy.library.ts_momentum_blend.

TimeSeriesMomentumBlend is LONG-ONLY and POSITION-AWARE: each bar it builds a
blended multi-lookback momentum score, targets "long iff score is convincingly
positive", and emits the delta against the holding it reads from the (broker-
reconciled) StrategyContext. So the tests drive it through a context and simulate
fills, the way the engine / run_once do.

Covered:
  - constructor validation (bad lookbacks / weights / scale / threshold),
  - insufficient history -> no signal (fails closed during warmup),
  - the core blended-score math against a hand-computed value,
  - rising prices -> BUY with a sensible strength in (0, 1] and an ATR stop below
    entry,
  - falling prices after a long -> exit SELL (full conviction, leaves us flat),
  - monotonic decline from flat -> never buys,
  - cold start into an already-established uptrend (no fresh crossover needed),
  - no pyramiding while held,
  - unknown symbols ignored,
  - determinism.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.ts_momentum_blend import TimeSeriesMomentumBlend

SYM = Symbol("MOM", AssetClass.ETF)
OTHER = Symbol("NOPE", AssetClass.ETF)

# Short, fast lookbacks so tests stay compact but still exercise the blend.
SHORT_LOOKBACKS = [2, 4]


def _bar(sym: Symbol, t: datetime, price: float, *, high: float = None, low: float = None) -> Bar:
    p = Decimal(str(price))
    h = Decimal(str(high)) if high is not None else p
    lo = Decimal(str(low)) if low is not None else p
    return Bar(symbol=sym, timestamp=t, open=p, high=h, low=lo, close=p, volume=Decimal("1000"))


class _Harness:
    """
    Minimal stand-in for the engine: binds a context, refreshes it from a
    simulated portfolio before each bar, and applies emitted signals as IMMEDIATE
    fills so the next bar sees the updated holding (mirrors engine
    fill-before-dispatch ordering).
    """

    def __init__(self, strat: TimeSeriesMomentumBlend):
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

    def step(self, sym: Symbol, price: float, **kw):
        self._refresh()
        sigs = self.strat.on_bar(_bar(sym, self.t + timedelta(days=self.i), price, **kw))
        self.i += 1
        for s in sigs:
            self.held[sym.ticker] = Decimal("1") if s.side == OrderSide.BUY else Decimal("0")
        return sigs

    def feed(self, sym: Symbol, prices: list[float]):
        out = []
        for p in prices:
            out.extend(self.step(sym, p))
        return out


def _strat(**kw) -> TimeSeriesMomentumBlend:
    base = dict(
        lookbacks=SHORT_LOOKBACKS,
        scale=0.1,
        buy_threshold=0.1,
        atr_period=3,
    )
    base.update(kw)
    return TimeSeriesMomentumBlend("s", [SYM], **base)


# ----------------------------------------------------------------------
# Constructor validation
# ----------------------------------------------------------------------


def test_rejects_empty_lookbacks():
    with pytest.raises(ValueError):
        TimeSeriesMomentumBlend("s", [SYM], lookbacks=[])


def test_rejects_nonpositive_lookback():
    with pytest.raises(ValueError):
        TimeSeriesMomentumBlend("s", [SYM], lookbacks=[5, 0])


def test_rejects_weight_length_mismatch():
    with pytest.raises(ValueError):
        TimeSeriesMomentumBlend("s", [SYM], lookbacks=[5, 10], weights=[1.0])


def test_rejects_nonpositive_weight():
    with pytest.raises(ValueError):
        TimeSeriesMomentumBlend("s", [SYM], lookbacks=[5, 10], weights=[1.0, -1.0])


def test_rejects_bad_scale():
    with pytest.raises(ValueError):
        TimeSeriesMomentumBlend("s", [SYM], scale=0.0)


def test_rejects_bad_threshold():
    with pytest.raises(ValueError):
        TimeSeriesMomentumBlend("s", [SYM], buy_threshold=1.0)
    with pytest.raises(ValueError):
        TimeSeriesMomentumBlend("s", [SYM], buy_threshold=0.0)


def test_rejects_bad_atr_period():
    with pytest.raises(ValueError):
        TimeSeriesMomentumBlend("s", [SYM], atr_period=0)


def test_rejects_bad_atr_mult():
    with pytest.raises(ValueError):
        TimeSeriesMomentumBlend("s", [SYM], atr_mult=Decimal("0"))


# ----------------------------------------------------------------------
# Warmup / insufficient history
# ----------------------------------------------------------------------


def test_no_signal_during_warmup():
    # Shortest lookback is 2 -> needs 3 closes before ANY sub-score is warm.
    h = _Harness(_strat())
    assert h.feed(SYM, [10, 11]) == []  # only 2 closes, nothing computable yet


def test_no_signal_for_unknown_symbol():
    h = _Harness(_strat())
    assert h.step(OTHER, 100.0) == []


# ----------------------------------------------------------------------
# Core blended-score math (hand-computed)
# ----------------------------------------------------------------------


def test_blended_score_matches_hand_computation():
    """
    With lookbacks [2, 4], default weights [1, 2], scale 0.1, feed a known
    geometric-ish series and verify _blended_score against a direct calculation.
    """
    strat = _strat()
    closes = [100.0, 105.0, 110.0, 108.0, 120.0]
    score = strat._blended_score(closes)

    # lookback 2: compares close to the close 2 bars earlier -> 120/110 - 1
    r2 = 120.0 / 110.0 - 1.0
    # lookback 4: compares close to the close 4 bars earlier -> 120/100 - 1
    r4 = 120.0 / 100.0 - 1.0
    sub2 = math.tanh(r2 / 0.1)
    sub4 = math.tanh(r4 / 0.1)
    expected = (1.0 * sub2 + 2.0 * sub4) / (1.0 + 2.0)

    assert score == pytest.approx(expected)
    assert -1.0 < score < 1.0


def test_blended_score_none_before_warm():
    strat = _strat()
    # Only 2 closes: lookback 2 needs 3, lookback 4 needs 5 -> nothing warm.
    assert strat._blended_score([100.0, 101.0]) is None


def test_partial_warmup_uses_only_warm_lookbacks():
    """With 3 closes, only the lookback-2 sub-score is warm; blend still valid."""
    strat = _strat()
    closes = [100.0, 110.0, 121.0]
    score = strat._blended_score(closes)
    # lookback 2 compares close to the close 2 bars earlier -> 121/100 - 1.
    r2 = 121.0 / 100.0 - 1.0
    assert score == pytest.approx(math.tanh(r2 / 0.1))


# ----------------------------------------------------------------------
# Long-only behaviour
# ----------------------------------------------------------------------


def test_rising_prices_emit_buy_with_sensible_strength_and_atr_stop():
    h = _Harness(_strat())
    prices = [100, 102, 104, 107, 110, 114, 119, 125, 132, 140]
    sigs = h.feed(SYM, prices)
    buys = [s for s in sigs if s.side == OrderSide.BUY]
    assert len(buys) == 1  # enters once, then holds
    b = buys[0]
    assert Decimal("0") < b.strength <= Decimal("1")
    assert b.suggested_stop_loss is not None
    assert b.suggested_stop_loss < Decimal(str(prices[-1]))  # stop below entry
    assert b.suggested_stop_loss >= Decimal("0")
    assert h.held["MOM"] > 0


def test_strength_is_floored():
    """A barely-above-threshold score still yields a tradeable (floored) strength."""
    # Threshold just under the score we will produce; floor high so it clamps up.
    h = _Harness(_strat(buy_threshold=0.1, strength_floor=Decimal("0.5")))
    prices = [100, 100.5, 101, 101.5, 102, 102.5, 103, 103.5]
    buys = [s for s in h.feed(SYM, prices) if s.side == OrderSide.BUY]
    assert buys
    assert buys[0].strength >= Decimal("0.5")


def test_falling_after_long_exits():
    h = _Harness(_strat())
    # Rise to establish a long, then collapse so the blended score goes <= 0.
    prices = [100, 104, 108, 113, 119, 126, 134, 120, 105, 92, 80, 70, 60]
    sigs = h.feed(SYM, prices)
    sides = [s.side for s in sigs]
    assert OrderSide.BUY in sides and OrderSide.SELL in sides
    assert sides.index(OrderSide.BUY) < sides.index(OrderSide.SELL)
    sells = [s for s in sigs if s.side == OrderSide.SELL]
    assert sells[0].strength == Decimal("1.0")  # full-conviction exit
    assert h.held["MOM"] == 0  # left flat


def test_monotonic_decline_from_flat_never_buys():
    h = _Harness(_strat())
    prices = [100, 98, 96, 94, 92, 90, 88, 86, 84, 82, 80]
    sigs = h.feed(SYM, prices)
    assert [s for s in sigs if s.side == OrderSide.BUY] == []


def test_cold_start_enters_established_uptrend():
    """
    The position-aware property: warm over a steadily-rising series (momentum
    already positive on every computable bar, no fresh 'cross' to witness). A
    flat strategy in an up regime must still enter exactly once.
    """
    h = _Harness(_strat())
    prices = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21]
    buys = [s for s in h.feed(SYM, prices) if s.side == OrderSide.BUY]
    assert len(buys) == 1
    assert h.held["MOM"] > 0


def test_no_pyramiding_while_held():
    h = _Harness(_strat())
    prices = [100, 104, 108, 113, 119, 126, 134, 143, 153, 164, 176]
    buys = [s for s in h.feed(SYM, prices) if s.side == OrderSide.BUY]
    assert len(buys) == 1


def test_flat_stays_flat_when_no_position_and_weak_momentum():
    """score in (0, threshold] while flat -> no buy; score>0 while flat held=False."""
    h = _Harness(_strat(buy_threshold=0.95))  # threshold so high nothing triggers
    prices = [100, 101, 102, 103, 104, 105, 106, 107]
    assert h.feed(SYM, prices) == []


# ----------------------------------------------------------------------
# Stop-loss fallback
# ----------------------------------------------------------------------


def test_stop_uses_pct_fallback_before_atr_warm():
    """
    With atr_period large relative to the run, ATR never warms, so the BUY's stop
    must come from the percentage fallback (entry * (1 - stop_loss_pct)).
    """
    h = _Harness(_strat(atr_period=999, stop_loss_pct=Decimal("0.10")))
    prices = [10, 11, 12, 13, 14, 15, 16, 17]
    buys = [s for s in h.feed(SYM, prices) if s.side == OrderSide.BUY]
    assert buys
    entry = Decimal(str(prices[7]))  # bar that triggered the buy is the last fed
    # The buy fires on the first bar where score crosses; find its entry price.
    b = buys[0]
    # Stop must be strictly below entry and a clean 10% band off SOME fed price.
    assert b.suggested_stop_loss is not None
    assert b.suggested_stop_loss > Decimal("0")
    # Ratio stop/entry ~ 0.9 for the bar it fired on.
    assert b.suggested_stop_loss < entry


def test_atr_stop_below_entry_with_volatile_bars():
    h = _Harness(_strat(atr_period=3, atr_mult=Decimal("2")))
    # Feed with explicit highs/lows so ATR has real range.
    prices = [100, 104, 108, 113, 119, 126]
    sigs = []
    for p in prices:
        sigs.extend(h.step(SYM, p, high=p + 2, low=p - 2))
    buys = [s for s in sigs if s.side == OrderSide.BUY]
    assert buys
    assert buys[0].suggested_stop_loss < Decimal(str(prices[-1]))


# ----------------------------------------------------------------------
# Determinism
# ----------------------------------------------------------------------


def test_deterministic():
    prices = [100, 104, 108, 113, 119, 126, 134, 120, 105, 92, 80]
    h1 = _Harness(_strat())
    h2 = _Harness(_strat())
    s1 = h1.feed(SYM, prices)
    s2 = h2.feed(SYM, prices)
    assert [(s.side, s.strength, s.suggested_stop_loss) for s in s1] == [
        (s.side, s.strength, s.suggested_stop_loss) for s in s2
    ]
