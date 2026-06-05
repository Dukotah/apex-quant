"""
Tests for scripts.run_once — the cron evaluation cycle.

Every collaborator is injected, so the whole cycle runs offline against the
SimulatedExecutionEngine + an AlpacaDataFeed driven by a fake fetcher. Covers:
end-to-end submit + persist, the empty-window path, idempotent state on a re-fire,
broker reconciliation seeding the portfolio, and halt suppressing orders.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import List

from apex.core.clock import Clock
from apex.core.config import AppConfig, Broker, ExecutionMode
from apex.core.events import SignalEvent
from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.execution.simulated import SimulatedExecutionEngine
from apex.risk.risk_manager import RiskConfig, RiskManager
from apex.strategy.base_strategy import BaseStrategy
from scripts.run_once import RunReport, StateStore, run_once

SPY = Symbol("SPY", AssetClass.ETF)
UTC = timezone.utc
NOW = datetime(2024, 6, 3, 20, 0, tzinfo=UTC)


class FixedClock(Clock):
    def __init__(self, t):
        self._t = t

    def now(self):
        return self._t


def _raw_bar(ts: str, close: float):
    class _B:
        pass
    b = _B()
    b.timestamp = datetime.fromisoformat(ts).replace(tzinfo=UTC)
    b.open = b.high = b.low = b.close = close
    b.high = close + 1
    b.low = close - 1
    b.volume = 1000
    return b


def _feed(closes):
    """An AlpacaDataFeed wired to a fake fetcher returning `closes` for SPY."""
    from apex.data.alpaca_feed import AlpacaDataFeed
    bars = [_raw_bar(f"2024-06-{d:02d}", c) for d, c in closes]

    def fetcher(tickers, start, end, tf):
        return {"SPY": bars}

    return AlpacaDataFeed([SPY], bar_fetcher=fetcher, sleep=lambda _s: None)


class AlwaysBuy(BaseStrategy):
    """Emits a BUY (with a valid 5%-away stop) on every bar."""
    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        return [SignalEvent(
            symbol=bar.symbol, side=OrderSide.BUY, strength=Decimal("1.0"),
            strategy_id=self.strategy_id,
            suggested_stop_loss=bar.close * Decimal("0.95"), reason="test",
        )]


class NoSignal(BaseStrategy):
    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        return []


def _config():
    return AppConfig(mode=ExecutionMode.PAPER, broker=Broker.SIMULATED,
                     initial_capital=Decimal("100000"))


# ----------------------------------------------------------------- end to end

def test_end_to_end_submits_order_and_persists(tmp_path):
    store = StateStore(tmp_path / "s.db")
    engine = SimulatedExecutionEngine()
    report = run_once(
        _config(), [AlwaysBuy("buy", [SPY])],
        clock=FixedClock(NOW),
        feed=_feed([(1, 100), (2, 101), (3, 102)]),
        execution_engine=engine,
        state_store=store,
    )
    assert isinstance(report, RunReport)
    assert report.signals_evaluated == 1        # only the latest bar's signal acted on
    assert report.orders_submitted == 1
    assert len(report.fills) == 1
    assert report.num_positions == 1
    assert store.run_count() == 1
    row = store.last_run()
    assert row["mode"] == "paper"
    assert row["orders"] == 1


def test_empty_window_is_safe(tmp_path):
    store = StateStore(tmp_path / "s.db")
    report = run_once(
        _config(), [AlwaysBuy("buy", [SPY])],
        clock=FixedClock(NOW),
        feed=_feed([]),                          # fetcher returns no bars
        execution_engine=SimulatedExecutionEngine(),
        state_store=store,
    )
    assert report.orders_submitted == 0
    assert report.signals_evaluated == 0
    assert store.run_count() == 1                # the (empty) run is still recorded


def test_state_idempotent_on_same_timestamp(tmp_path):
    store = StateStore(tmp_path / "s.db")
    for _ in range(2):                           # same clock → same (ts, mode) key
        run_once(
            _config(), [AlwaysBuy("buy", [SPY])],
            clock=FixedClock(NOW),
            feed=_feed([(1, 100), (2, 101)]),
            execution_engine=SimulatedExecutionEngine(),
            state_store=store,
        )
    assert store.run_count() == 1                # INSERT OR REPLACE — no duplicate row


# --------------------------------------------------------------- reconciliation

def test_reconcile_seeds_portfolio_from_broker():
    class ReconcilingEngine(SimulatedExecutionEngine):
        def reconcile_positions(self):
            return {"SPY": {"qty": Decimal("10"), "avg_entry_price": Decimal("90"),
                            "current_price": Decimal("100")}}

    report = run_once(
        _config(), [NoSignal("noop", [SPY])],    # no new signals: isolate reconciliation
        clock=FixedClock(NOW),
        feed=_feed([(1, 100), (2, 101)]),
        execution_engine=ReconcilingEngine(),
    )
    assert report.reconciled is True
    assert report.num_positions == 1             # broker's SPY position is now tracked
    assert report.orders_submitted == 0


# ----------------------------------------------------------------------- halt

def test_halt_suppresses_orders():
    rm = RiskManager(RiskConfig())
    rm._halted = True                            # simulate a prior drawdown breach
    report = run_once(
        _config(), [AlwaysBuy("buy", [SPY])],
        clock=FixedClock(NOW),
        feed=_feed([(1, 100), (2, 101)]),
        execution_engine=SimulatedExecutionEngine(),
        risk_manager=rm,
    )
    assert report.halted is True
    assert report.orders_submitted == 0          # halt blocks all new orders
    assert report.signals_evaluated == 1         # signal was seen, just not acted on


# ----------------------------------------------------------------- state store

def test_state_store_creates_schema_and_persists(tmp_path):
    store = StateStore(tmp_path / "nested" / "dir" / "s.db")   # parent dirs created
    report = RunReport(timestamp=NOW, mode="paper", equity=100000.0, num_positions=2,
                       orders_submitted=1)
    store.save_run(report, {"SPY": {"qty": "10"}})
    assert store.run_count() == 1
    assert store.last_run()["num_positions"] == 2
