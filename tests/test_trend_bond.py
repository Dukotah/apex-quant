"""
Tests for apex.strategy.library.trend_bond.

Verifies the always-invested trend/bond rotation: warmup silence, entry into the
risk asset in an uptrend, rotation to the bond sleeve on a trend break (and back),
no churn while the trend holds, and that exactly one asset is held after warmup.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Tuple

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.library.trend_bond import TrendBondStrategy

SPY = Symbol("SPY", AssetClass.ETF)
AGG = Symbol("AGG", AssetClass.ETF)
UTC = timezone.utc
DAY0 = datetime(2020, 1, 1, tzinfo=UTC)


def _bar(sym: Symbol, close: float, i: int) -> Bar:
    c = Decimal(str(close))
    return Bar(symbol=sym, timestamp=DAY0 + timedelta(days=i),
               open=c, high=c, low=c, close=c, volume=Decimal("1000"))


def _drive(spy_closes: List[float], agg_close: float = 100.0,
           slow: int = 5) -> List[Tuple[str, str]]:
    """Feed AGG then SPY each day; return the (side, ticker) signals emitted."""
    strat = TrendBondStrategy("tb", [SPY, AGG], slow_period=slow)
    out: List[Tuple[str, str]] = []
    for i, spx in enumerate(spy_closes):
        strat.on_bar(_bar(AGG, agg_close, i))            # bond bar first (price known)
        for sig in strat.on_bar(_bar(SPY, spx, i)):      # decision on the risk bar
            out.append((sig.side.value, sig.symbol.ticker))
    return out


def test_requires_two_symbols():
    with pytest.raises(ValueError):
        TrendBondStrategy("tb", [SPY])


def test_silent_during_warmup():
    # Fewer than slow_period risk bars → no decision yet.
    sigs = _drive([10, 10, 10, 10], slow=5)
    assert sigs == []


def test_enters_risk_asset_in_uptrend():
    # A rising warmup means price > SMA at the first decision → buy SPY directly,
    # with nothing to exit yet.
    sigs = _drive([10, 11, 12, 13, 14, 15], slow=5)
    assert ("buy", "SPY") in sigs
    assert not any(side == "sell" for side, _ in sigs)   # nothing to exit yet


def test_rotates_to_bonds_on_trend_break():
    # Up (in SPY), then a sharp drop below the SMA → sell SPY, buy AGG.
    closes = [10, 11, 12, 13, 14, 15, 16, 1]
    sigs = _drive(closes, slow=5)
    # The last rotation pair should be exit SPY then enter AGG.
    assert ("sell", "SPY") in sigs
    assert ("buy", "AGG") in sigs
    assert sigs.index(("sell", "SPY")) < sigs.index(("buy", "AGG"))


def test_rotates_back_to_risk_and_no_churn():
    # up → buy SPY; down → SPY->AGG; back up → AGG->SPY. Stable stretches: no churn.
    closes = [10, 10, 10, 10, 10,
              20, 20, 20,            # solidly in SPY
              1, 1, 1,               # drop → to AGG
              50, 50, 50]            # surge → back to SPY
    sigs = _drive(closes, slow=5)
    # Expect both a buy-AGG (risk-off) and a later buy-SPY (risk-on) round trip.
    assert ("buy", "AGG") in sigs
    assert ("buy", "SPY") in sigs
    # No duplicate consecutive identical signals (no churn): each transition is distinct.
    for a, b in zip(sigs, sigs[1:]):
        assert a != b


def test_always_invested_after_first_entry():
    # After the first entry the strategy always targets exactly one asset; every
    # rotation is a SELL+BUY pair, never a bare SELL into cash.
    closes = [10, 10, 10, 10, 10, 20, 20, 1, 1, 50, 50]
    sigs = _drive(closes, slow=5)
    buys = [t for s, t in sigs if s == "buy"]
    sells = [t for s, t in sigs if s == "sell"]
    # First action is an entry (buy) with no preceding sell; thereafter sells and
    # buys pair up, so buys == sells + 1.
    assert len(buys) == len(sells) + 1
