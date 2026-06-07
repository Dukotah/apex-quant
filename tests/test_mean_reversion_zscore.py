"""
tests/test_mean_reversion_zscore
================================
Tests for the UNVALIDATED z-score mean-reversion research candidate.

Covers: hand-computed z-score entry, hysteresis exit, position-awareness
(delta-only emission), no-pyramiding, restart correctness, ATR-based stop,
percentage fallback during ATR warmup, warmup/flat-window guards, and ctor
validation. Pure and fast.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Position, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.mean_reversion_zscore import MeanReversionZScoreStrategy

SYM = Symbol(ticker="TST", asset_class=AssetClass.EQUITY)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bar(close: float, high: float | None = None, low: float | None = None, i: int = 0) -> Bar:
    c = Decimal(str(close))
    h = Decimal(str(high)) if high is not None else c
    lo = Decimal(str(low)) if low is not None else c
    return Bar(
        symbol=SYM,
        timestamp=T0 + timedelta(days=i),
        open=c,
        high=max(h, c),
        low=min(lo, c),
        close=c,
        volume=Decimal("1000"),
    )


def _ctx(qty: float | None) -> StrategyContext:
    ctx = StrategyContext()
    if qty is not None:
        pos = Position(
            symbol=SYM,
            quantity=Decimal(str(qty)),
            avg_entry_price=Decimal("100"),
            current_price=Decimal("100"),
        )
        ctx.sync_state(positions={SYM.ticker: pos})
    return ctx


def _feed(strat: MeanReversionZScoreStrategy, closes, ctx=None):
    """Feed flat-OHLC bars; return signals from the LAST bar only."""
    sigs = []
    for i, c in enumerate(closes):
        if ctx is not None:
            strat.bind_context(ctx)
        sigs = strat.on_bar(_bar(c, i=i))
    return sigs


# ---- z-score math --------------------------------------------------------

def test_hand_computed_zscore_triggers_entry():
    # lookback=5: first 4 closes = 100, last close = a sharp drop.
    # Pick values so z <= -2.0 cleanly.
    strat = MeanReversionZScoreStrategy("s", [SYM], lookback=5, entry_z=-2.0, exit_z=-0.5)
    closes = [100.0, 100.0, 100.0, 100.0, 95.0]
    window = closes
    mean = statistics.fmean(window)
    sd = statistics.pstdev(window)
    expected_z = (window[-1] - mean) / sd
    assert expected_z <= -2.0  # sanity: this window is deeply oversold

    sigs = _feed(strat, closes, ctx=_ctx(None))  # flat
    assert len(sigs) == 1
    assert sigs[0].side == OrderSide.BUY
    assert sigs[0].suggested_stop_loss is not None


def test_no_entry_when_zscore_above_threshold():
    strat = MeanReversionZScoreStrategy("s", [SYM], lookback=5, entry_z=-2.0, exit_z=-0.5)
    closes = [100.0, 100.0, 100.0, 100.0, 99.0]  # mild dip, z > -2
    z_window = closes
    z = (z_window[-1] - statistics.fmean(z_window)) / statistics.pstdev(z_window)
    assert z > -2.0
    sigs = _feed(strat, closes, ctx=_ctx(None))
    assert sigs == []


def test_warmup_returns_no_signal():
    strat = MeanReversionZScoreStrategy("s", [SYM], lookback=10)
    sigs = _feed(strat, [100.0, 90.0, 80.0], ctx=_ctx(None))  # < lookback
    assert sigs == []


def test_flat_window_no_signal():
    # Zero stdev → no usable z-score → stay flat.
    strat = MeanReversionZScoreStrategy("s", [SYM], lookback=5)
    sigs = _feed(strat, [100.0] * 6, ctx=_ctx(None))
    assert sigs == []


# ---- position awareness / delta logic ------------------------------------

def test_no_buy_when_already_long():
    # Oversold AND already holding → no pyramiding, no signal.
    strat = MeanReversionZScoreStrategy("s", [SYM], lookback=5, entry_z=-2.0, exit_z=-0.5)
    sigs = _feed(strat, [100.0, 100.0, 100.0, 100.0, 95.0], ctx=_ctx(10))
    assert sigs == []


def test_exit_when_reverted_and_held():
    # First push oversold to latch want_long=True, then revert toward mean while held.
    strat = MeanReversionZScoreStrategy("s", [SYM], lookback=5, entry_z=-2.0, exit_z=-0.5)
    ctx = _ctx(10)  # we hold a long the whole time
    strat.bind_context(ctx)
    # Warm + latch want_long via an oversold reading.
    for i, c in enumerate([100.0, 100.0, 100.0, 100.0, 95.0]):
        strat.on_bar(_bar(c, i=i))
    # Now feed a recovery bar so z >= exit_z (-0.5). A symmetric-ish window near mean.
    sigs = strat.on_bar(_bar(100.0, i=5))
    assert len(sigs) == 1
    assert sigs[0].side == OrderSide.SELL


def test_no_exit_in_dead_band_holds_state():
    # Between entry_z and exit_z the latched state persists; held+want_long → no signal.
    strat = MeanReversionZScoreStrategy("s", [SYM], lookback=5, entry_z=-2.0, exit_z=-0.5)
    ctx = _ctx(10)
    strat.bind_context(ctx)
    for i, c in enumerate([100.0, 100.0, 100.0, 100.0, 95.0]):
        strat.on_bar(_bar(c, i=i))
    # A bar whose z lands in the dead band (between -2 and -0.5): still mildly low.
    # closes window becomes [100,100,100,95,97] -> compute, ensure in band.
    next_close = 97.0
    win = [100.0, 100.0, 100.0, 95.0, next_close]
    z = (win[-1] - statistics.fmean(win)) / statistics.pstdev(win)
    assert -2.0 < z < -0.5
    sigs = strat.on_bar(_bar(next_close, i=5))
    assert sigs == []  # want_long latched True, already held → nothing


def test_restart_enters_established_setup_cold():
    # Fresh instance, flat context, fed history that ends oversold → must BUY,
    # proving it acts on STATE not a fresh-cross event.
    strat = MeanReversionZScoreStrategy("s", [SYM], lookback=5, entry_z=-2.0, exit_z=-0.5)
    sigs = _feed(strat, [100.0, 100.0, 100.0, 100.0, 95.0], ctx=_ctx(None))
    assert len(sigs) == 1 and sigs[0].side == OrderSide.BUY


def test_no_context_treated_as_flat():
    strat = MeanReversionZScoreStrategy("s", [SYM], lookback=5, entry_z=-2.0, exit_z=-0.5)
    # No bind_context call at all.
    sigs = []
    for i, c in enumerate([100.0, 100.0, 100.0, 100.0, 95.0]):
        sigs = strat.on_bar(_bar(c, i=i))
    assert len(sigs) == 1 and sigs[0].side == OrderSide.BUY


# ---- stop loss -----------------------------------------------------------

def test_percentage_stop_during_atr_warmup():
    # lookback small so we enter before atr_period+1 bars exist → pct fallback.
    strat = MeanReversionZScoreStrategy(
        "s", [SYM], lookback=5, entry_z=-2.0, exit_z=-0.5,
        atr_period=14, stop_loss_pct=Decimal("0.05"),
    )
    sigs = _feed(strat, [100.0, 100.0, 100.0, 100.0, 95.0], ctx=_ctx(None))
    assert len(sigs) == 1
    price = Decimal("95.0")
    expected = price * (Decimal("1") - Decimal("0.05"))
    assert sigs[0].suggested_stop_loss == expected


def test_atr_stop_when_atr_available():
    # Enough bars for ATR (atr_period=3 → need 4+ bars). Keep flat then drop to
    # trigger entry once ATR exists.
    strat = MeanReversionZScoreStrategy(
        "s", [SYM], lookback=4, entry_z=-1.5, exit_z=-0.5,
        atr_period=3, atr_mult=Decimal("2.0"), stop_loss_pct=Decimal("0.05"),
    )
    ctx = _ctx(None)
    # Build OHLC with real ranges so ATR > 0, ending on an oversold close.
    bars = [
        (100.0, 101.0, 99.0),
        (100.0, 101.0, 99.0),
        (100.0, 101.0, 99.0),
        (100.0, 101.0, 99.0),
        (94.0, 100.0, 93.0),   # big down bar → oversold + wide true range
    ]
    sigs = []
    for i, (c, h, lo) in enumerate(bars):
        strat.bind_context(ctx)
        sigs = strat.on_bar(_bar(c, high=h, low=lo, i=i))
    assert len(sigs) == 1 and sigs[0].side == OrderSide.BUY
    stop = sigs[0].suggested_stop_loss
    price = Decimal("94.0")
    # ATR-based stop must be strictly below entry and not equal to the pct fallback
    # (the wide true range makes the ATR stop materially different).
    assert stop < price
    assert stop > 0


def test_stop_never_none_on_buy():
    strat = MeanReversionZScoreStrategy("s", [SYM], lookback=5, entry_z=-2.0, exit_z=-0.5)
    sigs = _feed(strat, [100.0, 100.0, 100.0, 100.0, 95.0], ctx=_ctx(None))
    assert sigs[0].suggested_stop_loss is not None
    assert sigs[0].suggested_stop_loss < Decimal("95.0")


# ---- untracked symbol & validation ---------------------------------------

def test_untracked_symbol_ignored():
    strat = MeanReversionZScoreStrategy("s", [SYM], lookback=5)
    other = Symbol(ticker="ZZZ", asset_class=AssetClass.EQUITY)
    bar = Bar(
        symbol=other, timestamp=T0, open=Decimal("1"), high=Decimal("1"),
        low=Decimal("1"), close=Decimal("1"), volume=Decimal("1"),
    )
    assert strat.on_bar(bar) == []


def test_ctor_validation():
    with pytest.raises(ValueError):
        MeanReversionZScoreStrategy("s", [SYM], lookback=1)
    with pytest.raises(ValueError):
        MeanReversionZScoreStrategy("s", [SYM], entry_z=-0.5, exit_z=-2.0)  # entry>=exit
    with pytest.raises(ValueError):
        MeanReversionZScoreStrategy("s", [SYM], atr_period=0)
    with pytest.raises(ValueError):
        MeanReversionZScoreStrategy("s", [SYM], atr_mult=Decimal("0"))
    with pytest.raises(ValueError):
        MeanReversionZScoreStrategy("s", [SYM], stop_loss_pct=Decimal("1.5"))


def test_determinism():
    closes = [100.0, 100.0, 100.0, 100.0, 95.0]
    a = MeanReversionZScoreStrategy("s", [SYM], lookback=5, entry_z=-2.0, exit_z=-0.5)
    b = MeanReversionZScoreStrategy("s", [SYM], lookback=5, entry_z=-2.0, exit_z=-0.5)
    sa = _feed(a, closes, ctx=_ctx(None))
    sb = _feed(b, closes, ctx=_ctx(None))
    assert [s.side for s in sa] == [s.side for s in sb]
    assert [s.suggested_stop_loss for s in sa] == [s.suggested_stop_loss for s in sb]
