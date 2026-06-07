"""
Tests for apex.strategy.library.atr_channel_breakout.

Pure, fast, deterministic. Feeds hand-constructed bars and asserts the breakout
entry, the cross-back exit, position-awareness (no pyramiding, correct on a
"restart" with an existing holding), and that every BUY carries a stop — both
ATR-based and via the percentage warmup fallback.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from apex.core.models import AssetClass, Bar, OrderSide, Position, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.atr_channel_breakout import ATRChannelBreakoutStrategy

SYM = Symbol(ticker="TEST", asset_class=AssetClass.ETF)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bar(i: int, *, o: float, h: float, lo: float, c: float, v: float = 1000.0) -> Bar:
    return Bar(
        symbol=SYM,
        timestamp=T0 + timedelta(days=i),
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(lo)),
        close=Decimal(str(c)),
        volume=Decimal(str(v)),
    )


def _flat_bar(i: int, price: float) -> Bar:
    """A calm bar with a tiny range, all OHLC ~ price."""
    return _bar(i, o=price, h=price + 0.1, lo=price - 0.1, c=price)


def _strategy(**kw) -> ATRChannelBreakoutStrategy:
    return ATRChannelBreakoutStrategy("atrcb-test", [SYM], **kw)


# ---- validation ----------------------------------------------------------


def test_invalid_params_raise():
    for kw in (
        {"sma_period": 0},
        {"atr_period": 0},
        {"channel_mult": 0.0},
        {"stop_atr_mult": -1.0},
        {"stop_loss_pct": Decimal("0")},
    ):
        try:
            _strategy(**kw)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {kw}")


def test_unknown_symbol_is_ignored():
    strat = _strategy(sma_period=3, atr_period=2)
    other = Symbol(ticker="OTHER", asset_class=AssetClass.ETF)
    bar = Bar(
        symbol=other,
        timestamp=T0,
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10"),
        volume=Decimal("1"),
    )
    assert strat.on_bar(bar) == []


# ---- warmup --------------------------------------------------------------


def test_no_signal_during_sma_warmup():
    strat = _strategy(sma_period=5, atr_period=3)
    sigs = []
    for i in range(4):  # fewer than sma_period bars
        sigs += strat.on_bar(_flat_bar(i, 100.0))
    assert sigs == []


# ---- entry ---------------------------------------------------------------


def test_breakout_above_upper_band_emits_buy_with_atr_stop():
    # Calm baseline so SMA and ATR are well-defined and small, then a big spike
    # that pushes the close clearly above mid + channel_mult*ATR.
    strat = _strategy(sma_period=5, atr_period=3, channel_mult=2.0, stop_atr_mult=2.0)
    sigs = []
    for i in range(8):
        sigs += strat.on_bar(_flat_bar(i, 100.0))
    assert sigs == []  # flat baseline: close == mid, never above upper

    spike = _bar(8, o=100.0, h=130.0, lo=100.0, c=130.0)
    out = strat.on_bar(spike)
    assert len(out) == 1
    sig = out[0]
    assert sig.side is OrderSide.BUY
    assert sig.symbol is SYM
    assert sig.strategy_id == "atrcb-test"
    assert sig.timestamp == spike.timestamp
    # ATR-based stop: strictly below the entry price and positive.
    assert sig.suggested_stop_loss is not None
    assert Decimal("0") < sig.suggested_stop_loss < spike.close


def test_no_buy_when_close_inside_channel():
    strat = _strategy(sma_period=5, atr_period=3, channel_mult=2.0)
    sigs = []
    for i in range(8):
        sigs += strat.on_bar(_flat_bar(i, 100.0))
    # Mild move that stays under the upper band (only ~+1, ATR band is wider).
    mild = _bar(8, o=100.0, h=101.0, lo=100.0, c=101.0)
    assert strat.on_bar(mild) == []


# ---- no pyramiding / position awareness ----------------------------------


def _ctx_long() -> StrategyContext:
    ctx = StrategyContext()
    pos = Position(
        symbol=SYM,
        quantity=Decimal("10"),
        avg_entry_price=Decimal("120"),
        current_price=Decimal("130"),
    )
    ctx.sync_state(positions={SYM.ticker: pos})
    return ctx


def test_no_pyramiding_when_already_long():
    strat = _strategy(sma_period=5, atr_period=3, channel_mult=2.0)
    strat.bind_context(_ctx_long())
    for i in range(8):
        strat.on_bar(_flat_bar(i, 100.0))
    # Breakout bar, but we already hold a long → no second BUY.
    out = strat.on_bar(_bar(8, o=100.0, h=130.0, lo=100.0, c=130.0))
    assert out == []


def test_enters_existing_breakout_on_cold_start_when_flat():
    # No context (flat). Warm up calm, then a breakout bar → a single BUY,
    # proving entry is state-based (no prior crossing bar required).
    strat = _strategy(sma_period=5, atr_period=3, channel_mult=2.0)
    for i in range(8):
        strat.on_bar(_flat_bar(i, 100.0))
    out = strat.on_bar(_bar(8, o=100.0, h=130.0, lo=100.0, c=130.0))
    assert len(out) == 1 and out[0].side is OrderSide.BUY


# ---- exit ----------------------------------------------------------------


def test_exit_when_close_crosses_back_inside_channel():
    strat = _strategy(sma_period=5, atr_period=3, channel_mult=2.0)
    strat.bind_context(_ctx_long())  # we currently hold a long
    for i in range(8):
        strat.on_bar(_flat_bar(i, 100.0))
    # Close drops to/under the midline (~100) → SELL to flat.
    out = strat.on_bar(_bar(8, o=100.0, h=100.0, lo=90.0, c=95.0))
    assert len(out) == 1
    sig = out[0]
    assert sig.side is OrderSide.SELL
    assert sig.strength == Decimal("1.0")


def test_no_exit_while_inside_band_above_midline():
    strat = _strategy(sma_period=5, atr_period=3, channel_mult=2.0)
    strat.bind_context(_ctx_long())
    for i in range(8):
        strat.on_bar(_flat_bar(i, 100.0))
    # Close stays above the midline but inside the band → HOLD (no signal).
    out = strat.on_bar(_bar(8, o=100.0, h=102.0, lo=100.0, c=101.0))
    assert out == []


# ---- stop-loss warmup fallback -------------------------------------------


def test_percentage_stop_fallback_during_atr_warmup():
    # SMA ready quickly, ATR still warming: sma_period < atr_period+1 so there is
    # a window where mid exists but ATR is None. A breakout there must still carry
    # the percentage fallback stop.
    strat = _strategy(
        sma_period=2, atr_period=10, channel_mult=2.0, stop_loss_pct=Decimal("0.05")
    )
    # bar 0,1 establish SMA(2); ATR(10) needs 11 bars, so still None here.
    strat.on_bar(_flat_bar(0, 100.0))
    out = strat.on_bar(_bar(1, o=100.0, h=200.0, lo=100.0, c=200.0))
    # With ATR None, the channel upper == mid, and close(200) > mid → BUY.
    assert len(out) == 1
    sig = out[0]
    assert sig.side is OrderSide.BUY
    # Fallback stop = price * (1 - 0.05).
    expected = Decimal("200") * (Decimal("1") - Decimal("0.05"))
    assert sig.suggested_stop_loss == expected


def test_every_buy_has_a_stop():
    strat = _strategy(sma_period=5, atr_period=3, channel_mult=2.0)
    for i in range(8):
        strat.on_bar(_flat_bar(i, 100.0))
    out = strat.on_bar(_bar(8, o=100.0, h=140.0, lo=100.0, c=140.0))
    assert len(out) == 1
    assert out[0].suggested_stop_loss is not None


# ---- determinism ---------------------------------------------------------


def test_deterministic_same_inputs_same_outputs():
    def run() -> list[tuple]:
        strat = _strategy(sma_period=5, atr_period=3, channel_mult=2.0)
        seq = []
        prices = [100, 100, 100, 100, 100, 100, 100, 100, 135, 134, 90]
        for i, p in enumerate(prices):
            for s in strat.on_bar(_bar(i, o=p, h=p + 5, lo=p - 5, c=p)):
                seq.append((s.side, str(s.suggested_stop_loss)))
        return seq

    assert run() == run()
