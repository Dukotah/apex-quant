"""
tests/test_run_experiments.py
=============================
Tests for the multi-book experiment harness. Two things matter most:

  1. The default roster actually constructs — every book's factory instantiates a
     real library strategy (this catches a wrong constructor signature immediately).
  2. A book REMEMBERS its positions across cron cycles. A plain simulator forgets;
     the StatefulSimExecutionEngine seeded from persisted state must carry the book's
     holdings from one cycle into the next. This is the whole point of the harness.

Both run fully offline: the roster test only instantiates strategies, and the
persistence test drives run_once with a fake AlpacaDataFeed (the run_once test pattern).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import List

from apex.core.events import SignalEvent
from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.base_strategy import BaseStrategy
from scripts.run_experiments import (
    ExperimentBook,
    _load_seed,
    default_experiments,
    run_experiment_book,
)
from scripts.run_once import RunReport, StateStore

UTC = timezone.utc
SPY = Symbol("SPY", AssetClass.ETF)
T1 = datetime(2024, 6, 10, 20, 0, tzinfo=UTC)
T2 = datetime(2024, 6, 11, 20, 0, tzinfo=UTC)


class _FixedClock:
    def __init__(self, t):
        self._t = t

    def now(self):
        return self._t


def _raw_bar(ts: str, close: float):
    class _B:
        pass

    b = _B()
    b.timestamp = datetime.fromisoformat(ts).replace(tzinfo=UTC)
    b.open = close
    b.high = close + 1
    b.low = close - 1
    b.close = close
    b.volume = 1000
    return b


def _feed(closes):
    """An AlpacaDataFeed wired to a fake fetcher returning `closes` for SPY."""
    from apex.data.alpaca_feed import AlpacaDataFeed

    bars = [_raw_bar(f"2024-06-{d:02d}", c) for d, c in closes]

    def fetcher(tickers, start, end, tf):
        return {"SPY": bars}

    return AlpacaDataFeed([SPY], bar_fetcher=fetcher, sleep=lambda _s: None)


class _AlwaysBuy(BaseStrategy):
    """BUY (with a valid 5%-away stop) on every bar."""

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        return [
            SignalEvent(
                symbol=bar.symbol,
                side=OrderSide.BUY,
                strength=Decimal("1.0"),
                strategy_id=self.strategy_id,
                suggested_stop_loss=bar.close * Decimal("0.95"),
                reason="test",
            )
        ]


class _NoSignal(BaseStrategy):
    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        return []


# --------------------------------------------------------------------- roster


def test_default_roster_constructs_every_book():
    books = default_experiments()
    assert len(books) >= 5
    ids = [b.id for b in books]
    assert len(ids) == len(set(ids)), "book ids must be unique"
    for book in books:
        strategies = book.make_strategies()  # instantiates the REAL library class
        assert strategies, f"{book.id} produced no strategies"
        assert all(isinstance(s, BaseStrategy) for s in strategies)
        assert strategies[0].strategy_id == book.id


# ------------------------------------------------------------------- seed I/O


def test_load_seed_empty_when_no_runs(tmp_path):
    store = StateStore(tmp_path / "b.db")
    try:
        assert _load_seed(store) == {}
    finally:
        store.close()


def test_load_seed_roundtrips_last_positions(tmp_path):
    store = StateStore(tmp_path / "b.db")
    try:
        report = RunReport(timestamp=T1, mode="paper", equity=100.0, num_positions=1)
        positions = {"SPY": {"qty": "3", "avg_entry_price": "100", "current_price": "101"}}
        store.save_run(report, positions)
        assert _load_seed(store) == positions
    finally:
        store.close()


# ------------------------------------------------------- persistence across cycles


def test_book_remembers_position_across_cycles(tmp_path):
    # Cycle 1: a book that buys SPY. The position must persist to its state DB.
    buy_book = ExperimentBook("t1", "Test", lambda: [_AlwaysBuy("t1", [SPY])])
    r1 = run_experiment_book(
        buy_book,
        state_dir=tmp_path,
        feed=_feed([(3, 100), (4, 101), (5, 102)]),
        clock=_FixedClock(T1),
        lookback=10,
    )
    assert r1.orders_submitted == 1
    assert r1.num_positions == 1
    assert (tmp_path / "t1.db").exists()

    # Cycle 2: SAME book id, but a strategy that emits NOTHING. Without stateful
    # seeding the fresh simulator would start flat (0 positions). Because the engine
    # is seeded from the persisted state, the SPY position carries over.
    hold_book = ExperimentBook("t1", "Test", lambda: [_NoSignal("t1", [SPY])])
    r2 = run_experiment_book(
        hold_book,
        state_dir=tmp_path,
        feed=_feed([(6, 103), (7, 104)]),
        clock=_FixedClock(T2),
        lookback=10,
    )
    assert r2.orders_submitted == 0
    assert r2.num_positions == 1, "position was forgotten — stateful seeding failed"
