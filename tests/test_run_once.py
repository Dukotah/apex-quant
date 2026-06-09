"""
Tests for scripts.run_once — the cron evaluation cycle.

Every collaborator is injected, so the whole cycle runs offline against the
SimulatedExecutionEngine + an AlpacaDataFeed driven by a fake fetcher. Covers:
end-to-end submit + persist, the empty-window path, idempotent state on a re-fire,
broker reconciliation seeding the portfolio, and halt suppressing orders.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import List

from apex.core.clock import Clock
from apex.core.config import AppConfig, Broker, ExecutionMode
from apex.core.events import SignalEvent
from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.execution.simulated import SimulatedExecutionEngine
from apex.risk.risk_manager import RiskConfig, RiskManager
from apex.strategy.base_strategy import BaseStrategy
from scripts.run_once import (
    PRODUCTION_RISK,
    RunReport,
    StateStore,
    _detect_reconcile_discrepancy,
    _drift_monitor,
    _heartbeat,
    _is_continuous_step,
    _notify,
    _notify_cycle,
    _weekday_steps,
    run_once,
)

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


class NoSignal(BaseStrategy):
    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        return []


def _config():
    return AppConfig(
        mode=ExecutionMode.PAPER, broker=Broker.SIMULATED, initial_capital=Decimal("100000")
    )


# ----------------------------------------------------------------- end to end


def test_end_to_end_submits_order_and_persists(tmp_path):
    store = StateStore(tmp_path / "s.db")
    engine = SimulatedExecutionEngine()
    report = run_once(
        _config(),
        [AlwaysBuy("buy", [SPY])],
        clock=FixedClock(NOW),
        feed=_feed([(1, 100), (2, 101), (3, 102)]),
        execution_engine=engine,
        state_store=store,
    )
    assert isinstance(report, RunReport)
    assert report.signals_evaluated == 1  # only the latest bar's signal acted on
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
        _config(),
        [AlwaysBuy("buy", [SPY])],
        clock=FixedClock(NOW),
        feed=_feed([]),  # fetcher returns no bars
        execution_engine=SimulatedExecutionEngine(),
        state_store=store,
    )
    assert report.orders_submitted == 0
    assert report.signals_evaluated == 0
    assert store.run_count() == 1  # the (empty) run is still recorded


def test_state_idempotent_on_same_timestamp(tmp_path):
    store = StateStore(tmp_path / "s.db")
    for _ in range(2):  # same clock → same (ts, mode) key
        run_once(
            _config(),
            [AlwaysBuy("buy", [SPY])],
            clock=FixedClock(NOW),
            feed=_feed([(1, 100), (2, 101)]),
            execution_engine=SimulatedExecutionEngine(),
            state_store=store,
        )
    assert store.run_count() == 1  # INSERT OR REPLACE — no duplicate row


# --------------------------------------------------------------- reconciliation


def test_reconcile_seeds_portfolio_from_broker():
    class ReconcilingEngine(SimulatedExecutionEngine):
        def reconcile_positions(self):
            return {
                "SPY": {
                    "qty": Decimal("10"),
                    "avg_entry_price": Decimal("90"),
                    "current_price": Decimal("100"),
                }
            }

    report = run_once(
        _config(),
        [NoSignal("noop", [SPY])],  # no new signals: isolate reconciliation
        clock=FixedClock(NOW),
        feed=_feed([(1, 100), (2, 101)]),
        execution_engine=ReconcilingEngine(),
    )
    assert report.reconciled is True
    assert report.num_positions == 1  # broker's SPY position is now tracked
    assert report.orders_submitted == 0


# ----------------------------------------------------------------------- halt


def test_halt_suppresses_orders():
    rm = RiskManager(RiskConfig())
    rm._halted = True  # simulate a prior drawdown breach
    report = run_once(
        _config(),
        [AlwaysBuy("buy", [SPY])],
        clock=FixedClock(NOW),
        feed=_feed([(1, 100), (2, 101)]),
        execution_engine=SimulatedExecutionEngine(),
        risk_manager=rm,
    )
    assert report.halted is True
    assert report.orders_submitted == 0  # halt blocks all new orders
    assert report.signals_evaluated == 1  # signal was seen, just not acted on


# ----------------------------------------------------------------- state store


def test_state_store_creates_schema_and_persists(tmp_path):
    store = StateStore(tmp_path / "nested" / "dir" / "s.db")  # parent dirs created
    report = RunReport(
        timestamp=NOW, mode="paper", equity=100000.0, num_positions=2, orders_submitted=1
    )
    store.save_run(report, {"SPY": {"qty": "10"}})
    assert store.run_count() == 1
    assert store.last_run()["num_positions"] == 2


# ------------------------------------------------------------- drift monitoring


def _weekdays_from(base, n):
    """The n-th trading-day timestamp from base (skips weekends) — matches how the
    weekday cron actually spaces its run rows, so drift gap-detection sees a
    continuous series."""
    d = base
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def _seed_equities(store, equities):
    base = datetime(2024, 1, 1, tzinfo=UTC)  # a Monday
    for i, eq in enumerate(equities):
        store.save_run(
            RunReport(
                timestamp=_weekdays_from(base, i),
                mode="paper",
                equity=float(eq),
                num_positions=0,
            ),
            {},
        )


def test_recent_equities_oldest_to_newest(tmp_path):
    store = StateStore(tmp_path / "s.db")
    _seed_equities(store, [100.0, 101.0, 102.0])
    assert store.recent_equities("paper") == [100.0, 101.0, 102.0]


def _bleeding_curve(n=35):
    """A noisy DECLINING equity path → strongly negative rolling Sharpe."""
    v, out = 100000.0, []
    for i in range(n):
        v *= 1 + (-0.02 if i % 2 == 0 else 0.01)  # net -0.5%/2d, real variance
        out.append(v)
    return out


def test_drift_quarantine_blocks_new_entries(tmp_path):
    store = StateStore(tmp_path / "s.db")
    _seed_equities(store, _bleeding_curve())  # decayed history → below floor
    report = run_once(
        _config(),
        [AlwaysBuy("buy", [SPY])],
        clock=FixedClock(NOW),
        feed=_feed([(1, 100), (2, 101), (3, 102)]),
        execution_engine=SimulatedExecutionEngine(),
        state_store=store,
    )
    assert report.quarantined is True
    assert report.orders_submitted == 0  # new entries blocked on decay


def test_drift_monitor_warming_up_does_not_block(tmp_path):
    store = StateStore(tmp_path / "s.db")
    _seed_equities(store, [100000.0, 100100.0, 100200.0])  # too little data to judge
    mon = _drift_monitor(store, "paper")
    assert mon is not None and not mon.check().is_quarantined


# ------------------------------------------------- NEXT-6 drift gap-detection


def test_weekday_steps_counts_trading_days():
    d = date(2024, 1, 1)  # Monday
    assert _weekday_steps(d, date(2024, 1, 2)) == 1  # Mon→Tue
    assert _weekday_steps(date(2024, 1, 5), date(2024, 1, 8)) == 1  # Fri→Mon (weekend)
    assert _weekday_steps(d, date(2024, 1, 3)) == 2  # Mon→Wed (Tue skipped)
    assert _weekday_steps(d, d) == 0  # same day (mid-day re-fire)
    assert _weekday_steps(date(2024, 1, 6), date(2024, 1, 7)) == 0  # Sat→Sun, no weekday


def test_is_continuous_step():
    assert _is_continuous_step(date(2024, 1, 5), date(2024, 1, 8)) is True  # Fri→Mon
    assert _is_continuous_step(date(2024, 1, 1), date(2024, 1, 4)) is False  # 3 weekdays


def test_recent_equity_points_carries_timestamps(tmp_path):
    store = StateStore(tmp_path / "s.db")
    _seed_equities(store, [100.0, 101.0, 102.0])
    pts = store.recent_equity_points("paper")
    assert [eq for _, eq in pts] == [100.0, 101.0, 102.0]  # oldest→newest
    assert all(isinstance(ts, str) for ts, _ in pts)
    assert pts[0][0] < pts[1][0] < pts[2][0]  # ascending ISO timestamps


def test_drift_skips_gap_return_no_false_quarantine(tmp_path):
    """A single huge move ACROSS a missed-cycle gap must not count as one daily
    return. With the gap honored, the conflated outlier is skipped, so the monitor
    stays WARMING_UP / non-quarantined rather than tripping on a phantom crash."""
    store = StateStore(tmp_path / "s.db")
    base = datetime(2024, 1, 1, tzinfo=UTC)  # Monday
    # Two flat points one trading day apart, then a -40% drop after a 5-weekday gap.
    store.save_run(RunReport(timestamp=base, mode="paper", equity=100000.0, num_positions=0), {})
    store.save_run(
        RunReport(
            timestamp=_weekdays_from(base, 1), mode="paper", equity=100000.0, num_positions=0
        ),
        {},
    )
    store.save_run(
        RunReport(
            timestamp=_weekdays_from(base, 6),  # 5 weekdays skipped → discontinuous
            mode="paper",
            equity=60000.0,  # would be a catastrophic single-day return if counted
            num_positions=0,
        ),
        {},
    )
    mon = _drift_monitor(store, "paper")
    assert mon is not None
    # The -40% gap return was skipped; only the flat continuous step was booked, so
    # the monitor has too little continuous data to judge and does not quarantine.
    reading = mon.check()
    assert reading.observations == 1  # just the one continuous (flat) return
    assert not reading.is_quarantined


def test_notify_is_silent_without_topic(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    _notify("t", "m")  # must not raise
    _notify_cycle(
        RunReport(timestamp=NOW, mode="paper", equity=1.0, num_positions=0, orders_submitted=1)
    )  # must not raise


# ------------------------------------------------------------- kill switch


def test_kill_switch_blocks_all_orders(monkeypatch, tmp_path):
    monkeypatch.setenv("APEX_HALT", "1")  # manual emergency stop
    report = run_once(
        _config(),
        [AlwaysBuy("buy", [SPY])],
        clock=FixedClock(NOW),
        feed=_feed([(1, 100), (2, 101), (3, 102)]),
        execution_engine=SimulatedExecutionEngine(),
        state_store=StateStore(tmp_path / "s.db"),
    )
    assert report.killed is True
    assert report.orders_submitted == 0  # ALL orders blocked
    assert report.signals_evaluated == 1  # signal was still seen


def test_kill_switch_off_trades_normally(monkeypatch, tmp_path):
    monkeypatch.delenv("APEX_HALT", raising=False)
    report = run_once(
        _config(),
        [AlwaysBuy("buy", [SPY])],
        clock=FixedClock(NOW),
        feed=_feed([(1, 100), (2, 101), (3, 102)]),
        execution_engine=SimulatedExecutionEngine(),
        state_store=StateStore(tmp_path / "s.db"),
    )
    assert report.killed is False
    assert report.orders_submitted == 1  # control: trades when off


def test_kill_switch_various_truthy_values(monkeypatch):
    from scripts.run_once import _kill_switch_active

    for v in ("1", "true", "YES", "on", "True"):
        monkeypatch.setenv("APEX_HALT", v)
        assert _kill_switch_active() is True
    for v in ("0", "false", "", "no"):
        monkeypatch.setenv("APEX_HALT", v)
        assert _kill_switch_active() is False


# --------------------------------------------------------- alert meta / heartbeat


class FakeNotifier:
    """Captures calls to send() without touching the network."""

    def __init__(self) -> None:
        self.sent: List[tuple] = []  # [(title, message, priority), ...]

    def send(self, title: str, message: str, priority: str) -> None:
        self.sent.append((title, message, priority))


def test_state_store_alert_date_round_trip(tmp_path):
    """last_alert_date / record_alert_date persist and retrieve correctly."""
    store = StateStore(tmp_path / "s.db")
    assert store.last_alert_date() is None  # fresh DB → None

    d = date(2024, 6, 3)
    store.record_alert_date(d)
    assert store.last_alert_date() == d

    # Overwrite with a later date — still exactly one row.
    d2 = date(2024, 6, 4)
    store.record_alert_date(d2)
    assert store.last_alert_date() == d2


def test_notify_cycle_actionable_event_sends_alert(tmp_path, monkeypatch):
    """An orders_submitted > 0 cycle sends exactly one 'traded' alert."""
    store = StateStore(tmp_path / "s.db")
    fake = FakeNotifier()
    monkeypatch.setattr("scripts.run_once.NtfyNotifier", lambda: fake)

    report = RunReport(
        timestamp=NOW,
        mode="paper",
        equity=100_000.0,
        num_positions=0,
        orders_submitted=1,
    )
    _notify_cycle(report, store)

    assert len(fake.sent) == 1
    title, _msg, priority = fake.sent[0]
    assert "traded" in title.lower()
    assert priority == "default"
    # Alert date should be recorded.
    assert store.last_alert_date() == NOW.date()


def test_notify_cycle_quiet_new_day_sends_heartbeat(tmp_path, monkeypatch):
    """A quiet cycle on a brand-new calendar day sends exactly one heartbeat."""
    store = StateStore(tmp_path / "s.db")
    fake = FakeNotifier()
    monkeypatch.setattr("scripts.run_once.NtfyNotifier", lambda: fake)

    # No prior alert → is_new_day = True for any date.
    report = RunReport(
        timestamp=NOW,
        mode="paper",
        equity=100_000.0,
        num_positions=0,
        orders_submitted=0,
    )
    _notify_cycle(report, store)

    assert len(fake.sent) == 1
    title, _msg, priority = fake.sent[0]
    assert "heartbeat" in title.lower()
    assert priority == "min"
    assert store.last_alert_date() == NOW.date()


def test_notify_cycle_quiet_same_day_sends_nothing(tmp_path, monkeypatch):
    """A quiet cycle on the SAME day as the last alert sends nothing."""
    store = StateStore(tmp_path / "s.db")
    # Pre-record today's date as the last alert date.
    store.record_alert_date(NOW.date())

    fake = FakeNotifier()
    monkeypatch.setattr("scripts.run_once.NtfyNotifier", lambda: fake)

    report = RunReport(
        timestamp=NOW,
        mode="paper",
        equity=100_000.0,
        num_positions=0,
        orders_submitted=0,
    )
    _notify_cycle(report, store)

    assert fake.sent == []  # silence — already heartbeated today


def test_notify_cycle_store_none_no_heartbeat_tracking(monkeypatch):
    """When store is None, _notify_cycle runs without heartbeat tracking and doesn't raise."""
    fake = FakeNotifier()
    monkeypatch.setattr("scripts.run_once.NtfyNotifier", lambda: fake)

    report = RunReport(
        timestamp=NOW,
        mode="paper",
        equity=100_000.0,
        num_positions=0,
        orders_submitted=0,
    )
    # store=None → last_alert_date is None → is_new_day=True → heartbeat fires
    _notify_cycle(report, store=None)
    # No crash, heartbeat is sent (no store to check/record date).
    assert len(fake.sent) == 1
    assert "heartbeat" in fake.sent[0][0].lower()


def test_notify_cycle_fail_open_on_notifier_error(tmp_path, monkeypatch):
    """If the notifier raises, _notify_cycle swallows the exception (fail-open)."""

    class BrokenNotifier:
        def send(self, title: str, message: str, priority: str) -> None:
            raise RuntimeError("network down")

    monkeypatch.setattr("scripts.run_once.NtfyNotifier", lambda: BrokenNotifier())
    store = StateStore(tmp_path / "s.db")

    report = RunReport(
        timestamp=NOW,
        mode="paper",
        equity=100_000.0,
        num_positions=0,
        orders_submitted=1,  # actionable → would normally send
    )
    # Must not raise even though the notifier is broken.
    _notify_cycle(report, store)  # no AssertionError, no exception propagated


# ----------------------------------------------- NEXT-7 external heartbeat wiring


def test_heartbeat_forwards_success_to_pinger(monkeypatch):
    """_heartbeat builds a HealthchecksPinger and forwards the success flag."""

    class FakePinger:
        def __init__(self) -> None:
            self.pings: list[bool] = []

        def ping(self, success: bool = True) -> None:
            self.pings.append(success)

    fake = FakePinger()
    monkeypatch.setattr("scripts.run_once.HealthchecksPinger", lambda: fake)
    _heartbeat(True)
    _heartbeat(False)
    assert fake.pings == [True, False]


def test_heartbeat_is_noop_without_url(monkeypatch):
    """With no APEX_HEARTBEAT_URL set, _heartbeat is a safe no-op (real pinger)."""
    monkeypatch.delenv("APEX_HEARTBEAT_URL", raising=False)
    _heartbeat(True)  # must not raise


def test_heartbeat_swallows_pinger_errors(monkeypatch):
    """A broken pinger must never break the cron cycle (fail-open)."""

    class BrokenPinger:
        def ping(self, success: bool = True) -> None:
            raise RuntimeError("network down")

    monkeypatch.setattr("scripts.run_once.HealthchecksPinger", lambda: BrokenPinger())
    _heartbeat(True)  # no exception propagated


# ===================================================== NOW-4/5/7 (Session 32)


class AlwaysSell(BaseStrategy):
    """Emits a SELL on every bar (a reduce/exit when a long is held)."""

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        return [
            SignalEvent(
                symbol=bar.symbol,
                side=OrderSide.SELL,
                strength=Decimal("1.0"),
                strategy_id=self.strategy_id,
                reason="test-exit",
            )
        ]


def _holding_engine(qty="10", avg="90", cur="100"):
    """A SimulatedExecutionEngine whose broker truth reports a SPY position."""

    class _Holding(SimulatedExecutionEngine):
        def reconcile_positions(self):
            return {
                "SPY": {
                    "qty": Decimal(qty),
                    "avg_entry_price": Decimal(avg),
                    "current_price": Decimal(cur),
                }
            }

    return _Holding()


# ------------------------------------------------------- NOW-4: live risk config


def test_production_risk_enables_vol_target_and_throttle():
    # Vol-target overlay must be ENABLED (non-None) at the roadmap ~0.12 target.
    assert PRODUCTION_RISK.target_volatility is not None
    assert PRODUCTION_RISK.target_volatility == Decimal("0.12")
    # Drawdown throttle deliberately populated at its trend-aware values (start=0.12,
    # NOT the roadmap's 0.05 — see code comment).
    assert PRODUCTION_RISK.drawdown_throttle_start == Decimal("0.12")
    assert PRODUCTION_RISK.drawdown_throttle_full == Decimal("0.30")
    assert PRODUCTION_RISK.drawdown_throttle_floor == Decimal("0.35")


# ------------------------------------------- NOW-5: clean daily-loss baseline


def test_daily_baseline_records_and_uses_todays_opening_equity(tmp_path):
    from apex.risk.portfolio import Portfolio

    store = StateStore(tmp_path / "s.db")
    port = Portfolio(Decimal("100000"))
    run_once(
        _config(),
        [NoSignal("noop", [SPY])],
        clock=FixedClock(NOW),
        feed=_feed([(1, 100), (2, 101), (3, 102)]),
        execution_engine=SimulatedExecutionEngine(),
        state_store=store,
        portfolio=port,
    )
    # Opening equity for today was recorded in the daily_open table AND seeded as the
    # portfolio's day-start baseline (== today's opening equity, here 100000).
    day = NOW.date().isoformat()
    assert store.day_start_equity(day, "paper", 999.0) == 100000.0  # already stored → ignores arg
    assert port.day_start_equity == Decimal("100000")


def test_daily_baseline_survives_midsession_refire_after_loss(tmp_path):
    """A second cycle the SAME day, after a loss, must keep the MORNING baseline."""
    from apex.risk.portfolio import Portfolio

    store = StateStore(tmp_path / "s.db")
    # Morning cycle: equity = 100000 → baseline recorded.
    port_am = Portfolio(Decimal("100000"))
    run_once(
        _config(),
        [NoSignal("noop", [SPY])],
        clock=FixedClock(NOW),
        feed=_feed([(1, 100), (2, 101), (3, 102)]),
        execution_engine=SimulatedExecutionEngine(),
        state_store=store,
        portfolio=port_am,
    )
    assert port_am.day_start_equity == Decimal("100000")

    # Mid-session re-fire, SAME calendar day, but the account is now DOWN to 95000.
    port_pm = Portfolio(Decimal("95000"))  # already-down intraday equity
    run_once(
        _config(),
        [NoSignal("noop", [SPY])],
        clock=FixedClock(NOW),  # same day
        feed=_feed([(1, 100), (2, 101), (3, 102)]),
        execution_engine=SimulatedExecutionEngine(),
        state_store=store,
        portfolio=port_pm,
    )
    # Baseline must remain the MORNING value, NOT the down 95000.
    assert port_pm.day_start_equity == Decimal("100000")


def test_daily_baseline_is_not_initial_capital_when_account_grew(tmp_path):
    """When equity has grown above initial_capital, the baseline tracks the grown open."""
    from apex.risk.portfolio import Portfolio

    store = StateStore(tmp_path / "s.db")
    # Seed growth by reconciling a winning SPY position so equity > capital.
    port = Portfolio(Decimal("100000"))
    run_once(
        _config(),
        [NoSignal("noop", [SPY])],
        clock=FixedClock(NOW),
        feed=_feed([(1, 100), (2, 101), (3, 102)]),
        execution_engine=_holding_engine(qty="100", avg="90", cur="100"),
        state_store=store,
        portfolio=port,
    )
    # Reconciled 100 sh @90 entry, marked to 102 close → unrealized gain over capital.
    assert port.day_start_equity > Decimal("100000")
    assert port.day_start_equity == port.equity  # baseline == today's grown open


# ------------------------------------------ NOW-7: reconciliation diff + alert


def test_reconcile_clean_match_no_discrepancy(tmp_path):
    store = StateStore(tmp_path / "s.db")
    # Persist a snapshot matching what the broker will report (SPY 10 @100).
    store.save_run(
        RunReport(
            timestamp=NOW - timedelta(days=1), mode="paper", equity=100000.0, num_positions=1
        ),
        {"SPY": {"qty": "10", "avg_entry_price": "90", "current_price": "100"}},
    )
    discrepancy = _detect_reconcile_discrepancy(_holding_engine(qty="10", cur="100"), store)
    assert discrepancy is False


def test_reconcile_clean_match_allows_entries(tmp_path):
    store = StateStore(tmp_path / "s.db")
    report = run_once(
        _config(),
        [AlwaysBuy("buy", [SPY])],
        clock=FixedClock(NOW),
        feed=_feed([(1, 100), (2, 101), (3, 102)]),
        execution_engine=SimulatedExecutionEngine(),  # broker flat, last run flat → match
        state_store=store,
    )
    assert report.reconcile_discrepancy is False
    assert report.orders_submitted == 1  # entries allowed on a clean match


def test_reconcile_mismatch_sets_flag_and_blocks_entries(tmp_path):
    store = StateStore(tmp_path / "s.db")
    # Broker shows a SPY position we never persisted (last run is empty) → mismatch.
    report = run_once(
        _config(),
        [AlwaysBuy("buy", [SPY])],
        clock=FixedClock(NOW),
        feed=_feed([(1, 100), (2, 101), (3, 102)]),
        execution_engine=_holding_engine(qty="10", cur="100"),
        state_store=store,
    )
    assert report.reconcile_discrepancy is True
    assert report.orders_submitted == 0  # new entries blocked on discrepancy


def test_reconcile_mismatch_still_allows_exits(tmp_path):
    store = StateStore(tmp_path / "s.db")
    # Broker holds SPY (unpersisted → discrepancy); a SELL is a reduce/exit → allowed.
    report = run_once(
        _config(),
        [AlwaysSell("sell", [SPY])],
        clock=FixedClock(NOW),
        feed=_feed([(1, 100), (2, 101), (3, 102)]),
        execution_engine=_holding_engine(qty="10", avg="90", cur="100"),
        state_store=store,
    )
    assert report.reconcile_discrepancy is True
    assert report.orders_submitted == 1  # de-risking exit still goes through


def test_reconcile_subdollar_drift_is_not_a_discrepancy(tmp_path):
    store = StateStore(tmp_path / "s.db")
    # Persist 10.000 sh; broker reports 10.001 sh @ ~$100 → ~$0.10 notional < $1 tol.
    store.save_run(
        RunReport(
            timestamp=NOW - timedelta(days=1), mode="paper", equity=100000.0, num_positions=1
        ),
        {"SPY": {"qty": "10.000", "avg_entry_price": "90", "current_price": "100"}},
    )
    discrepancy = _detect_reconcile_discrepancy(_holding_engine(qty="10.001", cur="100"), store)
    assert discrepancy is False  # sub-dollar dust ignored
