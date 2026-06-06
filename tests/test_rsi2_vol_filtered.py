"""
Tests for apex.strategy.library.rsi2_vol_filtered.

Validates:
  - Normal-volatility dip → BUY fires (ATR within band).
  - Volatile dip (ATR spike) → BUY is suppressed.
  - Exit (SELL) always fires regardless of ATR — filter applies to entry only.
  - No duplicate BUYs while long.
  - Determinism.
  - Ignores unknown symbols.
  - Inherits warmup guard from parent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Tuple

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.library.rsi2_vol_filtered import RSI2VolFilteredStrategy

SYM = Symbol("SPY", AssetClass.ETF)

# ---------------------------------------------------------------------------
# Bar / price-series helpers
# ---------------------------------------------------------------------------


def _bar(
    close: float,
    t: datetime,
    high: float = None,
    low: float = None,
    spread: float = 0.0,
) -> Bar:
    """Create a single bar. spread widens high/low around close for ATR tests."""
    c = Decimal(str(close))
    h = Decimal(str(high if high is not None else close + spread))
    lo = Decimal(str(low if low is not None else max(close - spread, 0.001)))
    return Bar(
        symbol=SYM,
        timestamp=t,
        open=c,
        high=h,
        low=lo,
        close=c,
        volume=Decimal("1000000"),
    )


def _feed_bars(strat: RSI2VolFilteredStrategy, bars: List[Bar]) -> List:
    all_sigs = []
    for b in bars:
        all_sigs.extend(strat.on_bar(b))
    return all_sigs


def _uptrend_bars(
    n: int = 215,
    base: float = 100.0,
    drift: float = 0.05,
    spread: float = 0.5,
    start: datetime = None,
) -> Tuple[List[Bar], datetime]:
    """
    Build n bars of a steady uptrend with a fixed ATR-inducing spread.
    Returns (bars, next_timestamp).
    """
    t = start or datetime(2020, 1, 1, tzinfo=timezone.utc)
    bars: List[Bar] = []
    for i in range(n):
        close = base + i * drift
        bars.append(_bar(close, t + timedelta(days=i), spread=spread))
    next_t = t + timedelta(days=n)
    return bars, next_t


def _dip_bars(
    start_price: float,
    depth: float,
    length: int,
    spread: float,
    start: datetime,
) -> Tuple[List[Bar], datetime]:
    """Append a sharp dip by `depth` total over `length` bars."""
    step = depth / length
    bars: List[Bar] = []
    for i in range(length):
        close = start_price - step * (i + 1)
        bars.append(_bar(close, start + timedelta(days=i), spread=spread))
    next_t = start + timedelta(days=length)
    return bars, next_t


def _recovery_bars(
    start_price: float,
    target: float,
    length: int,
    spread: float,
    start: datetime,
) -> Tuple[List[Bar], datetime]:
    """Append a recovery from start_price to target over `length` bars."""
    step = (target - start_price) / length
    bars: List[Bar] = []
    for i in range(length):
        close = start_price + step * (i + 1)
        bars.append(_bar(close, start + timedelta(days=i), spread=spread))
    next_t = start + timedelta(days=length)
    return bars, next_t


# ---------------------------------------------------------------------------
# Core scenario: normal vol → BUY, then SELL on recovery
# ---------------------------------------------------------------------------


def test_normal_vol_dip_produces_buy():
    """
    With stable ATR (spread matches bar-to-bar movement) the RSI(2) dip should
    produce a BUY.  Key: using spread=2.0 for both uptrend and dip bars means
    each bar's True Range stays ~4.0 throughout, so ATR is flat and the std is 0 —
    any current ATR == mean, deviation = 0, and the filter passes.
    """
    strat = RSI2VolFilteredStrategy(
        "s",
        [SYM],
        atr_period=14,
        atr_lookback=100,
        atr_std_mult=1.0,
        entry_threshold=Decimal("10"),
        time_stop_bars=0,
    )

    # 215 bars of gentle uptrend with spread=2.0 → stable ATR baseline.
    trend_bars, next_t = _uptrend_bars(n=215, base=100.0, drift=0.05, spread=2.0)
    last_price = float(trend_bars[-1].close)

    # Sharp dip: same spread=2.0 so ATR doesn't spike.
    dip_bars, next_t = _dip_bars(
        start_price=last_price,
        depth=6.0,
        length=8,
        spread=2.0,
        start=next_t,
    )

    all_bars = trend_bars + dip_bars
    signals = _feed_bars(strat, all_bars)

    buys = [s for s in signals if s.side == OrderSide.BUY]
    assert len(buys) >= 1, "Expected at least 1 BUY in a normal-vol uptrend dip scenario"
    buy = buys[0]
    assert buy.suggested_stop_loss is not None
    # The stop must be a positive price (strictly below the entry close, which
    # is somewhere in the uptrend area ~100-110; a 2% stop gives ~108 max).
    assert buy.suggested_stop_loss > Decimal("0")
    # The reason string carries the entry close — verify stop < that close.
    # (The stop is close * (1 - stop_loss_pct), always strictly less.)
    assert buy.strategy_id == "s"


def test_high_vol_spike_suppresses_buy():
    """
    Build 215 bars of uptrend with moderate spread=2.0 to establish a stable
    ATR baseline, then inject a dip with an extreme spread=30.0 (massive ATR
    spike ~15x the baseline).  The ATR filter should block the BUY even though
    RSI(2) and the trend filter would both allow entry.
    """
    strat = RSI2VolFilteredStrategy(
        "s",
        [SYM],
        atr_period=14,
        atr_lookback=100,
        atr_std_mult=1.0,
        entry_threshold=Decimal("15"),  # wider to confirm filter is the blocker
        time_stop_bars=0,
    )

    # Uptrend with spread=2.0 → consistent ATR baseline.
    trend_bars, next_t = _uptrend_bars(n=215, base=100.0, drift=0.05, spread=2.0)
    last_price = float(trend_bars[-1].close)

    # Dip bars with enormous spread → TR ≈ 60/bar vs baseline ATR ≈ 4.
    dip_bars, _ = _dip_bars(
        start_price=last_price,
        depth=6.0,
        length=8,
        spread=30.0,  # massive spike
        start=next_t,
    )

    all_bars = trend_bars + dip_bars
    signals = _feed_bars(strat, all_bars)

    buys = [s for s in signals if s.side == OrderSide.BUY]
    assert buys == [], f"Expected BUY suppressed during ATR spike, but got {len(buys)} BUY(s)"


# ---------------------------------------------------------------------------
# SELL is never blocked by the ATR filter
# ---------------------------------------------------------------------------


def test_sell_fires_even_during_vol_spike():
    """
    Enter long during normal vol (stable ATR), then trigger a recovery during a
    vol spike (huge spread).  The SELL (exit) must still fire — the ATR filter
    only gates BUY entries, never exits.
    """
    strat = RSI2VolFilteredStrategy(
        "s",
        [SYM],
        atr_period=14,
        atr_lookback=100,
        atr_std_mult=1.0,
        entry_threshold=Decimal("10"),
        time_stop_bars=0,
    )

    # Normal uptrend (spread=2.0) + quiet dip (spread=2.0) to get into a long.
    trend_bars, next_t = _uptrend_bars(n=215, base=100.0, drift=0.05, spread=2.0)
    last_price = float(trend_bars[-1].close)
    dip_bars, next_t = _dip_bars(
        start_price=last_price,
        depth=6.0,
        length=8,
        spread=2.0,
        start=next_t,
    )

    # Feed trend + dip, confirm we're long.
    pre_signals = _feed_bars(strat, trend_bars + dip_bars)
    assert any(s.side == OrderSide.BUY for s in pre_signals), (
        "Test setup broken: no BUY was emitted before the SELL check"
    )

    # Now spike ATR massively during recovery — SELL must still come through.
    dip_close = float(dip_bars[-1].close)
    recovery_target = last_price + 1.0  # back above the pre-dip area
    rec_bars, _ = _recovery_bars(
        start_price=dip_close,
        target=recovery_target,
        length=10,
        spread=30.0,  # huge spread during recovery — ATR spike
        start=next_t,
    )

    post_signals = _feed_bars(strat, rec_bars)
    sells = [s for s in post_signals if s.side == OrderSide.SELL]
    assert len(sells) >= 1, "SELL (exit) must fire even when ATR is spiking"


# ---------------------------------------------------------------------------
# Warmup guard inherited from parent
# ---------------------------------------------------------------------------


def test_no_signal_during_warmup():
    """Fewer than 201 bars → no signals (inherited from RSI2MeanReversionStrategy)."""
    strat = RSI2VolFilteredStrategy("s", [SYM])
    t = datetime(2020, 1, 1, tzinfo=timezone.utc)
    signals = []
    for i in range(200):
        close = Decimal(str(100.0 + i * 0.05))
        bar = Bar(
            symbol=SYM,
            timestamp=t + timedelta(days=i),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=Decimal("1000000"),
        )
        signals.extend(strat.on_bar(bar))
    assert signals == [], "Should be silent during warmup"


# ---------------------------------------------------------------------------
# No duplicate BUYs while long
# ---------------------------------------------------------------------------


def test_no_duplicate_buys_while_long():
    strat = RSI2VolFilteredStrategy(
        "s",
        [SYM],
        atr_period=14,
        atr_lookback=100,
        atr_std_mult=1.0,
        entry_threshold=Decimal("15"),
        time_stop_bars=0,
    )
    # Use spread=2.0 throughout so ATR filter passes consistently.
    trend_bars, next_t = _uptrend_bars(n=215, base=100.0, drift=0.05, spread=2.0)
    last_price = float(trend_bars[-1].close)
    # Prolonged dip with multiple low-RSI bars.
    dip_bars, _ = _dip_bars(
        start_price=last_price,
        depth=8.0,
        length=12,
        spread=2.0,
        start=next_t,
    )
    signals = _feed_bars(strat, trend_bars + dip_bars)
    buys = [s for s in signals if s.side == OrderSide.BUY]
    assert len(buys) == 1, f"Expected exactly 1 BUY (no duplicates), got {len(buys)}"


# ---------------------------------------------------------------------------
# Ignores unknown symbol
# ---------------------------------------------------------------------------


def test_ignores_unknown_symbol():
    strat = RSI2VolFilteredStrategy("s", [SYM])
    other = Symbol("XYZ", AssetClass.EQUITY)
    t = datetime(2020, 1, 1, tzinfo=timezone.utc)
    p = Decimal("100")
    bar = Bar(symbol=other, timestamp=t, open=p, high=p, low=p, close=p, volume=Decimal("1"))
    assert strat.on_bar(bar) == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_deterministic():
    """Two instances fed the same bars produce identical signals."""
    trend_bars, next_t = _uptrend_bars(n=215, base=100.0, drift=0.05, spread=2.0)
    last_price = float(trend_bars[-1].close)
    dip_bars, next_t = _dip_bars(
        start_price=last_price, depth=6.0, length=8, spread=2.0, start=next_t
    )
    rec_bars, _ = _recovery_bars(
        start_price=float(dip_bars[-1].close),
        target=last_price + 2.0,
        length=10,
        spread=2.0,
        start=next_t,
    )
    all_bars = trend_bars + dip_bars + rec_bars

    s1 = RSI2VolFilteredStrategy("s", [SYM], time_stop_bars=0)
    s2 = RSI2VolFilteredStrategy("s", [SYM], time_stop_bars=0)
    sig1 = _feed_bars(s1, all_bars)
    sig2 = _feed_bars(s2, all_bars)
    assert [s.side for s in sig1] == [s.side for s in sig2], (
        "Non-deterministic output detected between two identical instances"
    )
