"""
Tests for scripts.survivorship_stress.inject_delistings — the pure, deterministic
injection that the F1.1 stress sweep is built on. (The sweep itself runs real backtests
and is exercised by hand, not in CI.)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from apex.core.events import MarketEvent
from apex.core.models import AssetClass, Bar, Symbol
from scripts.survivorship_stress import inject_delistings


def _events(tickers, n=40, step_days=30):
    base = datetime(2010, 1, 1, tzinfo=timezone.utc)
    evs = []
    for i in range(n):
        ts = base + timedelta(days=i * step_days)
        for t in tickers:
            cls = AssetClass.ETF if t == "SPY" else AssetClass.EQUITY
            p = Decimal("100")
            evs.append(
                MarketEvent(
                    bar=Bar(
                        symbol=Symbol(t, cls),
                        timestamp=ts,
                        open=p,
                        high=p,
                        low=p,
                        close=p,
                        volume=Decimal("1000"),
                    )
                )
            )
    evs.sort(key=lambda e: (e.bar.timestamp, e.bar.symbol.ticker))
    return evs


def test_hazard_zero_is_identity():
    evs = _events(["AAA", "BBB", "SPY"])
    out, delisted = inject_delistings(evs, hazard_annual=0.0, severity=0.8, seed=1)
    assert delisted == {}
    assert len(out) == len(evs)


def test_deterministic_given_seed():
    evs = _events(["AAA", "BBB", "CCC", "SPY"])
    a_events, a_dl = inject_delistings(evs, 0.5, 0.8, seed=7)
    b_events, b_dl = inject_delistings(evs, 0.5, 0.8, seed=7)
    assert a_dl == b_dl
    assert len(a_events) == len(b_events)


def test_benchmark_protected_and_crash_applied():
    evs = _events(["AAA", "BBB", "CCC", "DDD", "EEE", "SPY"], n=40)
    out, delisted = inject_delistings(evs, hazard_annual=0.6, severity=0.8, seed=0)
    assert "SPY" not in delisted  # the benchmark is never delisted
    assert len(delisted) >= 1  # at this hazard some names should delist
    assert len(out) < len(evs)  # delisted names lose their tail bars

    by_ticker: dict = {}
    for ev in out:
        by_ticker.setdefault(ev.bar.symbol.ticker, []).append(ev.bar)
    for ticker in delisted:
        bars = by_ticker[ticker]
        assert len(bars) < 40  # truncated
        assert bars[-1].close < Decimal("100") * Decimal("0.5")  # final bar crashed ~80%
    # SPY keeps its full, unmodified series.
    assert len(by_ticker["SPY"]) == 40
