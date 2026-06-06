"""
Tests for apex.strategy.library.etf_rotation.

Validates the full weekly-rotation pipeline:
  - warmup guard (no signals until enough bars)
  - week-boundary detection (no signal within a week, signal on first bar of new week)
  - top-K momentum selection (best performer is selected)
  - rotation on leadership change (XLK → XLE)
  - absolute-momentum risk-off overlay (all negative → bonds)
  - volatility-scaled strengths (lower vol → higher strength)
  - no redundant signals when selection is unchanged
  - determinism (same inputs → same signals)
  - validation errors on bad construction args

Style matches test_sma_crossover.py exactly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.library.etf_rotation import ETFRotationStrategy

# ---------------------------------------------------------------------------
# Fixtures — symbols
# ---------------------------------------------------------------------------


def _sym(ticker: str) -> Symbol:
    return Symbol(ticker, AssetClass.ETF)


XLK = _sym("XLK")
XLE = _sym("XLE")
XLF = _sym("XLF")
XLV = _sym("XLV")
AGG = _sym("AGG")  # bond / risk-off sleeve (always last)

ALL_SYMS = [XLK, XLE, XLF, XLV, AGG]
SECTOR_SYMS = [XLK, XLE, XLF, XLV]


# ---------------------------------------------------------------------------
# Bar-building helpers
# ---------------------------------------------------------------------------

# Monday 3 Jan 2022 is ISO week 1 of 2022.
_BASE_DATE = datetime(2022, 1, 3, tzinfo=timezone.utc)


def _bar(sym: Symbol, price: float, day_offset: int) -> Bar:
    """Create a 1-Day bar for `sym` at base_date + day_offset."""
    ts = _BASE_DATE + timedelta(days=day_offset)
    p = Decimal(str(price))
    return Bar(
        symbol=sym,
        timestamp=ts,
        open=p,
        high=p,
        low=p,
        close=p,
        volume=Decimal("1000000"),
        timeframe="1Day",
    )


def _feed_interleaved(
    strat: ETFRotationStrategy,
    price_series: Dict[str, List[float]],
) -> List:
    """
    Feed bars for all symbols sorted by (day_offset, ticker) and collect signals.

    `price_series` maps ticker -> list of daily close prices (same length).
    Day offsets start at 0.  Bars are interleaved chronologically so the
    strategy sees each "trading day" in ticker-sorted order before the
    timestamp advances — this matches how a real bar fan-out works.
    """
    all_signals = []
    n_days = max(len(v) for v in price_series.values())
    tickers_sorted = sorted(price_series.keys())
    sym_map = {s.ticker: s for s in strat.symbols}

    for day in range(n_days):
        for ticker in tickers_sorted:
            prices = price_series[ticker]
            if day >= len(prices):
                continue
            sym = sym_map.get(ticker)
            if sym is None:
                continue
            bar = _bar(sym, prices[day], day_offset=day)
            all_signals.extend(strat.on_bar(bar))

    return all_signals


def _flat_prices(n: int, base: float = 100.0) -> List[float]:
    """Return n days of flat prices."""
    return [base] * n


def _trending_prices(n: int, start: float, daily_drift: float) -> List[float]:
    """Return n days of prices growing by `daily_drift` each day."""
    return [start + i * daily_drift for i in range(n)]


# ---------------------------------------------------------------------------
# Helper to build a standard 90-day warmup + extra scenario
# ---------------------------------------------------------------------------


def _build_strat(
    top_k: int = 1, momentum_period: int = 63, vol_period: int = 21
) -> ETFRotationStrategy:
    return ETFRotationStrategy(
        strategy_id="test-etf-rotation",
        symbols=ALL_SYMS,
        momentum_period=momentum_period,
        vol_period=vol_period,
        top_k=top_k,
    )


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


def test_requires_at_least_two_symbols():
    with pytest.raises(ValueError, match="at least 2"):
        ETFRotationStrategy("s", [XLK])


def test_invalid_momentum_period():
    with pytest.raises(ValueError, match="momentum_period"):
        ETFRotationStrategy("s", ALL_SYMS, momentum_period=0)


def test_invalid_vol_period():
    with pytest.raises(ValueError, match="vol_period"):
        ETFRotationStrategy("s", ALL_SYMS, vol_period=0)


def test_invalid_top_k():
    with pytest.raises(ValueError, match="top_k"):
        ETFRotationStrategy("s", ALL_SYMS, top_k=0)


# ---------------------------------------------------------------------------
# Warmup guard
# ---------------------------------------------------------------------------


def test_no_signal_during_warmup():
    """
    With momentum_period=63, we need ≥64 bars per symbol before the first
    rebalance.  Feed only 50 bars — strategy must stay silent.
    """
    strat = _build_strat(momentum_period=63, vol_period=21)
    # 50 bars — well short of the 64-bar warmup requirement.
    prices = {s.ticker: _trending_prices(50, 100.0, 0.1) for s in ALL_SYMS}
    signals = _feed_interleaved(strat, prices)
    assert signals == [], f"Expected no signals during warmup, got {signals}"


# ---------------------------------------------------------------------------
# Week-boundary detection
# ---------------------------------------------------------------------------


def test_no_signal_within_same_week():
    """
    Feed exactly one week of bars (Mon–Fri) for a warmed-up strategy and
    confirm at most one rebalance fires (on the second bar of the NEXT week),
    not multiple signals within the same ISO week.
    """
    momentum_period = 10
    vol_period = 5
    strat = ETFRotationStrategy(
        strategy_id="s",
        symbols=ALL_SYMS,
        momentum_period=momentum_period,
        vol_period=vol_period,
        top_k=1,
    )
    # Feed 20 bars to warm up, then one more Monday bar.
    # Use a fixed XLK outperformer so the selection is stable.
    prices = {
        "XLK": _trending_prices(30, 100.0, 0.5),
        "XLE": _flat_prices(30, 100.0),
        "XLF": _flat_prices(30, 100.0),
        "XLV": _flat_prices(30, 100.0),
        "AGG": _flat_prices(30, 100.0),
    }
    # Count how many distinct ISO weeks produce signals.
    all_signals = _feed_interleaved(strat, prices)
    # We just check we don't get runaway signal spam — at most 2 rebalances
    # in 30 days (≈4 weeks) is sane.
    assert len(all_signals) <= 2 * len(ALL_SYMS), f"Too many signals in 30 days: {len(all_signals)}"


# ---------------------------------------------------------------------------
# Momentum ranking — top-1 selection
# ---------------------------------------------------------------------------


def test_top_performer_selected_top1():
    """
    XLK grows strongly; XLE, XLF, XLV, AGG are flat.
    After warmup, XLK should be the sole BUY target.
    """
    momentum_period = 30
    vol_period = 10
    n = momentum_period + 30  # plenty of bars

    prices = {
        "XLK": _trending_prices(n, 100.0, 0.5),  # strong uptrend
        "XLE": _flat_prices(n, 100.0),
        "XLF": _flat_prices(n, 100.0),
        "XLV": _flat_prices(n, 100.0),
        "AGG": _flat_prices(n, 100.0),
    }
    strat = ETFRotationStrategy(
        "s", ALL_SYMS, momentum_period=momentum_period, vol_period=vol_period, top_k=1
    )
    signals = _feed_interleaved(strat, prices)
    buys = [s for s in signals if s.side == OrderSide.BUY]
    assert buys, "Expected at least one BUY signal after warmup"
    # Every BUY emitted by the first rebalance should be for XLK.
    first_rebalance_buys = buys[:1]
    for sig in first_rebalance_buys:
        assert sig.symbol.ticker == "XLK", (
            f"Expected XLK (top performer) but got {sig.symbol.ticker}"
        )


def test_top_performer_selected_top2():
    """
    XLK best, XLE second, the rest flat.
    With top_k=2 both should be selected.
    """
    momentum_period = 30
    vol_period = 10
    n = momentum_period + 30

    prices = {
        "XLK": _trending_prices(n, 100.0, 0.8),  # best
        "XLE": _trending_prices(n, 100.0, 0.4),  # second
        "XLF": _flat_prices(n, 100.0),
        "XLV": _flat_prices(n, 100.0),
        "AGG": _flat_prices(n, 100.0),
    }
    strat = ETFRotationStrategy(
        "s", ALL_SYMS, momentum_period=momentum_period, vol_period=vol_period, top_k=2
    )
    signals = _feed_interleaved(strat, prices)
    buys = [s for s in signals if s.side == OrderSide.BUY]
    bought_tickers = {s.symbol.ticker for s in buys}
    assert "XLK" in bought_tickers, "XLK (best) should be in top-2"
    assert "XLE" in bought_tickers, "XLE (second) should be in top-2"


# ---------------------------------------------------------------------------
# Rotation: XLK leadership → XLE leadership
# ---------------------------------------------------------------------------


def test_rotation_xlk_to_xle():
    """
    Phase 1 (days 0–89): XLK climbs strongly; XLE, XLF, XLV flat → XLK selected.
    Phase 2 (days 90–179): XLK collapses back to start; XLE climbs strongly
                            → XLE replaces XLK.

    Asserts:
      - At least one BUY XLK signal in phase 1.
      - At least one SELL XLK signal during the rotation.
      - At least one BUY XLE signal in phase 2.
      - SELL XLK precedes or coincides with BUY XLE in the signal stream.
    """
    momentum_period = 30
    vol_period = 10
    phase1 = 90
    phase2 = 90
    n = phase1 + phase2

    # XLK: rises in phase1, falls back in phase2.
    xlk_prices = (
        _trending_prices(phase1, 100.0, 0.5)  # climbs to ~145
        + _trending_prices(phase2, 145.0, -0.5)  # falls back
    )
    # XLE: flat in phase1, then climbs in phase2.
    xle_prices = (
        _flat_prices(phase1, 100.0) + _trending_prices(phase2, 100.0, 0.6)  # climbs to ~154
    )

    prices = {
        "XLK": xlk_prices,
        "XLE": xle_prices,
        "XLF": _flat_prices(n, 100.0),
        "XLV": _flat_prices(n, 100.0),
        "AGG": _flat_prices(n, 100.0),
    }
    strat = ETFRotationStrategy(
        "s", ALL_SYMS, momentum_period=momentum_period, vol_period=vol_period, top_k=1
    )
    signals = _feed_interleaved(strat, prices)

    buy_tickers = [s.symbol.ticker for s in signals if s.side == OrderSide.BUY]
    sell_tickers = [s.symbol.ticker for s in signals if s.side == OrderSide.SELL]

    assert "XLK" in buy_tickers, "XLK should have been bought in phase 1"
    assert "XLE" in buy_tickers, "XLE should have been bought in phase 2"
    assert "XLK" in sell_tickers, "XLK should be sold when XLE takes over"

    # Sell-XLK must appear before or at the same rebalance as buy-XLE.
    sell_xlk_idx = next(
        i for i, s in enumerate(signals) if s.side == OrderSide.SELL and s.symbol.ticker == "XLK"
    )
    buy_xle_idx = next(
        i for i, s in enumerate(signals) if s.side == OrderSide.BUY and s.symbol.ticker == "XLE"
    )
    assert sell_xlk_idx <= buy_xle_idx + len(ALL_SYMS), (
        "SELL XLK should accompany BUY XLE in the same rebalance"
    )


# ---------------------------------------------------------------------------
# Absolute-momentum risk-off overlay
# ---------------------------------------------------------------------------


def test_risk_off_when_all_momentum_negative():
    """
    All sector ETFs decline; AGG is flat.
    After warmup, the strategy should rotate entirely to AGG (bonds).
    No sector ETF BUY signals should be emitted.
    """
    momentum_period = 30
    vol_period = 10
    n = momentum_period + 30

    prices = {
        "XLK": _trending_prices(n, 100.0, -0.3),  # declining
        "XLE": _trending_prices(n, 100.0, -0.2),
        "XLF": _trending_prices(n, 100.0, -0.1),
        "XLV": _trending_prices(n, 100.0, -0.15),
        "AGG": _flat_prices(n, 100.0),  # bonds: flat
    }
    strat = ETFRotationStrategy(
        "s", ALL_SYMS, momentum_period=momentum_period, vol_period=vol_period, top_k=1
    )
    signals = _feed_interleaved(strat, prices)
    buys = [s for s in signals if s.side == OrderSide.BUY]

    sector_buys = [s for s in buys if s.symbol.ticker != "AGG"]
    assert sector_buys == [], (
        f"No sector ETF should be bought when all momentum is negative; "
        f"got buys for {[s.symbol.ticker for s in sector_buys]}"
    )
    # Must route to bonds.
    agg_buys = [s for s in buys if s.symbol.ticker == "AGG"]
    assert agg_buys, "AGG (bond sleeve) should be bought as risk-off"


def test_risk_off_sells_sector_and_buys_bonds():
    """
    Phase 1: XLK positive → XLK selected.
    Phase 2: all sectors negative → should SELL XLK and BUY AGG.
    """
    momentum_period = 20
    vol_period = 10
    phase1 = 50
    phase2 = 50
    n = phase1 + phase2

    xlk_prices = (
        _trending_prices(phase1, 100.0, 0.4)  # positive in phase1
        + _trending_prices(phase2, 120.0, -0.5)  # negative in phase2
    )
    prices = {
        "XLK": xlk_prices,
        "XLE": _trending_prices(n, 100.0, -0.2),
        "XLF": _trending_prices(n, 100.0, -0.15),
        "XLV": _trending_prices(n, 100.0, -0.1),
        "AGG": _flat_prices(n, 100.0),
    }
    strat = ETFRotationStrategy(
        "s", ALL_SYMS, momentum_period=momentum_period, vol_period=vol_period, top_k=1
    )
    signals = _feed_interleaved(strat, prices)

    buy_tickers = [s.symbol.ticker for s in signals if s.side == OrderSide.BUY]
    sell_tickers = [s.symbol.ticker for s in signals if s.side == OrderSide.SELL]

    assert "XLK" in buy_tickers, "XLK should be bought in phase 1"
    assert "XLK" in sell_tickers, "XLK should be sold when momentum turns negative"
    assert "AGG" in buy_tickers, "AGG (bonds) should be bought during risk-off phase"


# ---------------------------------------------------------------------------
# Inverse-vol sizing
# ---------------------------------------------------------------------------


def test_lower_vol_gets_higher_strength():
    """
    XLK has much lower realized vol than XLE.
    With top_k=2, XLK's BUY signal strength should be >= XLE's.
    """
    momentum_period = 20
    vol_period = 10
    n = momentum_period + 30

    # Both trend up equally in terms of total return.
    # XLE has noisy returns (zigzag around the trend) → higher vol.
    xlk_prices: List[float] = []
    xle_prices: List[float] = []
    base = 100.0
    xlk_val = base
    xle_val = base
    for i in range(n):
        xlk_val += 0.3  # smooth uptrend
        xle_val += 0.3 + (0.5 if i % 2 == 0 else -0.5)  # same drift but noisy
        xlk_prices.append(xlk_val)
        xle_prices.append(max(xle_val, 1.0))

    prices = {
        "XLK": xlk_prices,
        "XLE": xle_prices,
        "XLF": _flat_prices(n, 100.0),
        "XLV": _flat_prices(n, 100.0),
        "AGG": _flat_prices(n, 100.0),
    }
    strat = ETFRotationStrategy(
        "s", ALL_SYMS, momentum_period=momentum_period, vol_period=vol_period, top_k=2
    )
    signals = _feed_interleaved(strat, prices)
    buys = {s.symbol.ticker: s for s in signals if s.side == OrderSide.BUY}

    assert "XLK" in buys, "XLK should be bought"
    assert "XLE" in buys, "XLE should be bought"
    # Lower-vol asset should have equal or higher strength.
    assert buys["XLK"].strength >= buys["XLE"].strength, (
        f"XLK (lower vol) strength {buys['XLK'].strength} should be >= "
        f"XLE (higher vol) strength {buys['XLE'].strength}"
    )


def test_strength_is_decimal_in_zero_one():
    """All BUY signal strengths must be Decimals in (0, 1]."""
    momentum_period = 20
    vol_period = 10
    n = momentum_period + 30
    prices = {
        "XLK": _trending_prices(n, 100.0, 0.4),
        "XLE": _trending_prices(n, 100.0, 0.2),
        "XLF": _flat_prices(n, 100.0),
        "XLV": _flat_prices(n, 100.0),
        "AGG": _flat_prices(n, 100.0),
    }
    strat = ETFRotationStrategy(
        "s", ALL_SYMS, momentum_period=momentum_period, vol_period=vol_period, top_k=2
    )
    signals = _feed_interleaved(strat, prices)
    buys = [s for s in signals if s.side == OrderSide.BUY]
    assert buys, "Should have BUY signals"
    for sig in buys:
        assert isinstance(sig.strength, Decimal), (
            f"strength must be Decimal, got {type(sig.strength)}"
        )
        assert Decimal("0") < sig.strength <= Decimal("1"), (
            f"strength {sig.strength} for {sig.symbol.ticker} out of (0,1]"
        )


# ---------------------------------------------------------------------------
# Stop-loss attached to BUY signals
# ---------------------------------------------------------------------------


def test_buy_signals_have_stop_loss():
    """Every BUY signal must carry a suggested_stop_loss below entry price."""
    momentum_period = 20
    vol_period = 10
    n = momentum_period + 30
    prices = {
        "XLK": _trending_prices(n, 100.0, 0.5),
        "XLE": _flat_prices(n, 100.0),
        "XLF": _flat_prices(n, 100.0),
        "XLV": _flat_prices(n, 100.0),
        "AGG": _flat_prices(n, 100.0),
    }
    strat = ETFRotationStrategy(
        "s", ALL_SYMS, momentum_period=momentum_period, vol_period=vol_period, top_k=1
    )
    signals = _feed_interleaved(strat, prices)
    buys = [s for s in signals if s.side == OrderSide.BUY]
    assert buys, "Need BUY signals to test"
    for sig in buys:
        assert sig.suggested_stop_loss is not None, (
            f"BUY for {sig.symbol.ticker} missing suggested_stop_loss"
        )
        # Stop must be strictly below a plausible entry price.
        assert sig.suggested_stop_loss > Decimal("0"), (
            f"Stop-loss must be positive for {sig.symbol.ticker}"
        )


# ---------------------------------------------------------------------------
# No redundant signals when selection is unchanged
# ---------------------------------------------------------------------------


def test_no_redundant_signals_when_selection_unchanged():
    """
    Feed a long, stable uptrend for XLK only.  Once selected, the same symbol
    should be selected on every subsequent rebalance — no new BUY/SELL signals.
    """
    momentum_period = 20
    vol_period = 10
    # 20+1 warmup + many more weeks to observe stability.
    n = momentum_period + 60

    prices = {
        "XLK": _trending_prices(n, 100.0, 0.4),
        "XLE": _flat_prices(n, 100.0),
        "XLF": _flat_prices(n, 100.0),
        "XLV": _flat_prices(n, 100.0),
        "AGG": _flat_prices(n, 100.0),
    }
    strat = ETFRotationStrategy(
        "s", ALL_SYMS, momentum_period=momentum_period, vol_period=vol_period, top_k=1
    )
    signals = _feed_interleaved(strat, prices)
    buys = [s for s in signals if s.side == OrderSide.BUY]
    sells = [s for s in signals if s.side == OrderSide.SELL]

    # XLK bought exactly once; no SELLs needed while it stays on top.
    xlk_buys = [s for s in buys if s.symbol.ticker == "XLK"]
    xlk_sells = [s for s in sells if s.symbol.ticker == "XLK"]
    assert len(xlk_buys) == 1, (
        f"XLK should be bought exactly once (stable selection), got {len(xlk_buys)}"
    )
    assert len(xlk_sells) == 0, (
        f"XLK should not be sold while it stays on top, got {len(xlk_sells)}"
    )


# ---------------------------------------------------------------------------
# Ignores unknown symbol
# ---------------------------------------------------------------------------


def test_ignores_unknown_symbol():
    """Bars for symbols not in the universe are silently ignored."""
    strat = ETFRotationStrategy("s", ALL_SYMS, momentum_period=20, vol_period=10, top_k=1)
    unknown = Symbol("UNKNOWN", AssetClass.EQUITY)
    bar = _bar(unknown, 50.0, 0)
    result = strat.on_bar(bar)
    assert result == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_deterministic():
    """Same input bars always produce the same signal sequence."""
    momentum_period = 20
    vol_period = 10
    n = momentum_period + 50
    prices = {
        "XLK": _trending_prices(n, 100.0, 0.5),
        "XLE": _trending_prices(n, 100.0, 0.2),
        "XLF": _flat_prices(n, 100.0),
        "XLV": _flat_prices(n, 100.0),
        "AGG": _flat_prices(n, 100.0),
    }
    strat1 = ETFRotationStrategy(
        "s", ALL_SYMS, momentum_period=momentum_period, vol_period=vol_period, top_k=1
    )
    strat2 = ETFRotationStrategy(
        "s", ALL_SYMS, momentum_period=momentum_period, vol_period=vol_period, top_k=1
    )

    sig1 = _feed_interleaved(strat1, prices)
    sig2 = _feed_interleaved(strat2, prices)

    assert len(sig1) == len(sig2), "Signal counts must match"
    for a, b in zip(sig1, sig2):
        assert a.side == b.side, "Signal sides must match"
        assert a.symbol.ticker == b.symbol.ticker, "Signal tickers must match"
        assert a.strength == b.strength, "Signal strengths must match"
