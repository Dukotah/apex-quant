"""
scripts/run_once.py
===================
The cron entry point. ONE evaluation cycle, then exit — this is what the GitHub
Actions schedule (or any cron) invokes:

    APEX_MODE=paper APEX_BROKER=alpaca python -m scripts.run_once

A single cycle does exactly this, in order:

  1. Build the wired system from the immutable AppConfig (feed, strategies, risk
     manager, portfolio, execution engine via the factory — the one place the
     paper/live decision is made).
  2. RECONCILE against broker truth. The execution engine reports the broker's
     real positions; the local portfolio is seeded from them so we never trade
     from a stale or imagined state. (The simulator reports nothing, so
     backtest/paper-sim simply start from configured capital.)
  3. FETCH the most recent bar window (enough to warm every indicator), ending at
     the injected clock's "now".
  4. EVALUATE: replay the window so strategies warm up, then act ONLY on signals
     from the latest bar — route each through the RiskManager (the sole producer
     of orders) and submit approved orders to the execution engine.
  5. PERSIST a run record to SQLite (stdlib, in-repo) so successive cron runs
     leave an auditable trail of equity and decisions.
  6. Exit cleanly.

Idempotency: orders carry a stable id and the Alpaca engine refuses to double-
submit, so a re-fired cron run is safe. Determinism: every "now" comes from the
injected clock, never datetime.now() in logic.

Testability (Golden Rule 12): every external dependency — the feed, the execution
engine, the clock, even the state path — is injectable, so the entire cycle runs
end-to-end offline against the SimulatedExecutionEngine with a fake feed. The bare
``main()`` wiring that reads real env/keys is the only part that needs live infra.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from apex.core.clock import Clock, RealClock
from apex.core.config import AppConfig, Broker, ExecutionMode
from apex.core.events import FillEvent, MarketEvent, OrderEvent
from apex.core.models import OrderSide, Symbol
from apex.data.alpaca_feed import AlpacaDataFeed
from apex.data.base_feed import BaseDataFeed
from apex.execution.base_execution import BaseExecutionEngine
from apex.execution.factory import make_execution_engine
from apex.ops.alerts import NtfyNotifier, decide_alerts, send_alerts, should_heartbeat
from apex.risk.capital_allocation import CapitalAllocator
from apex.risk.portfolio import Portfolio
from apex.risk.risk_manager import RiskConfig, RiskManager
from apex.strategy.base_strategy import BaseStrategy, StrategyContext
from apex.validation.drift_monitor import DriftMonitor

logger = logging.getLogger("apex.run_once")

DEFAULT_STATE_PATH = Path("state/apex_state.db")


# --------------------------------------------------------------------- report


@dataclass
class RunReport:
    """The observable outcome of one evaluation cycle."""

    timestamp: datetime
    mode: str
    equity: float
    num_positions: int
    signals_evaluated: int = 0
    orders_submitted: int = 0
    fills: List[FillEvent] = field(default_factory=list)
    halted: bool = False
    reconciled: bool = False
    quarantined: bool = False
    killed: bool = False
    reconcile_discrepancy: bool = False
    drift_summary: str = ""

    def summary(self) -> str:
        base = (
            f"run_once [{self.mode}] @ {self.timestamp:%Y-%m-%d %H:%M}Z: "
            f"equity {self.equity:,.2f}, {self.num_positions} positions, "
            f"{self.signals_evaluated} signals -> {self.orders_submitted} orders, "
            f"{len(self.fills)} fills{', HALTED' if self.halted else ''}"
            f"{', QUARANTINED' if self.quarantined else ''}"
            f"{', RECONCILE-DISCREPANCY' if self.reconcile_discrepancy else ''}"
            f"{', KILLED' if self.killed else ''}"
        )
        return f"{base}\n  {self.drift_summary}" if self.drift_summary else base


# ------------------------------------------------------------------ state store


class StateStore:
    """
    Tiny SQLite-backed audit trail for cron runs (stdlib only — zero setup).

    Records one row per cycle: when it ran, the mode, equity, position count,
    orders/fills, and a JSON snapshot of positions. The schema is created on
    first use; ``save_run`` is the only writer.
    """

    def __init__(self, path: str | Path = DEFAULT_STATE_PATH) -> None:
        self.path = Path(path)
        if str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                ts            TEXT NOT NULL,
                mode          TEXT NOT NULL,
                equity        REAL NOT NULL,
                num_positions INTEGER NOT NULL,
                orders        INTEGER NOT NULL,
                fills         INTEGER NOT NULL,
                halted        INTEGER NOT NULL,
                positions     TEXT NOT NULL,
                PRIMARY KEY (ts, mode)
            )
            """
        )
        # Single-row meta table that tracks the calendar date of the most
        # recently sent alert. Used by the heartbeat logic so silence on a
        # new day means the cron is down, not just quiet.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_meta (
                id            INTEGER PRIMARY KEY CHECK (id = 1),
                last_alert_ts TEXT NOT NULL
            )
            """
        )
        # NOW-5: per-calendar-day opening equity, the clean daily-loss baseline. Keyed
        # by (ISO date, mode) so a mid-day cron re-fire cannot reset the baseline to an
        # already-down intraday value: it is written exactly ONCE per day (first cycle
        # observed) and read thereafter.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_open (
                day    TEXT NOT NULL,
                mode   TEXT NOT NULL,
                equity REAL NOT NULL,
                PRIMARY KEY (day, mode)
            )
            """
        )
        self._conn.commit()

    def save_run(self, report: RunReport, positions: Dict[str, dict]) -> None:
        """Persist one run. Idempotent on (ts, mode): a re-fire overwrites, never dupes."""
        self._conn.execute(
            "INSERT OR REPLACE INTO runs "
            "(ts, mode, equity, num_positions, orders, fills, halted, positions) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report.timestamp.isoformat(),
                report.mode,
                float(report.equity),
                report.num_positions,
                report.orders_submitted,
                len(report.fills),
                1 if report.halted else 0,
                json.dumps(positions, default=str),
            ),
        )
        self._conn.commit()

    def day_start_equity(self, day: str, mode: str, observed_equity: float) -> float:
        """
        Return TODAY's opening-equity baseline for the daily-loss circuit breaker.

        First call for a given (calendar day, mode): records `observed_equity` as the
        day's open and returns it. Every later call that same day: ignores the (possibly
        already-down) `observed_equity` and returns the stored morning value. This makes
        a mid-day cron re-fire after a loss reuse the SAME baseline, never a down one.

        `day` is the ISO date string from the injected clock (callers pass
        ``clock.now().date().isoformat()``) — never the wall clock.
        """
        # Write-only-if-absent: the first writer of the day wins the baseline.
        self._conn.execute(
            "INSERT OR IGNORE INTO daily_open (day, mode, equity) VALUES (?, ?, ?)",
            (day, mode, float(observed_equity)),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT equity FROM daily_open WHERE day = ? AND mode = ?", (day, mode)
        ).fetchone()
        return float(row[0])

    def last_run(self) -> Optional[sqlite3.Row]:
        self._conn.row_factory = sqlite3.Row
        cur = self._conn.execute("SELECT * FROM runs ORDER BY ts DESC LIMIT 1")
        return cur.fetchone()

    def run_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    def recent_equities(self, mode: str, limit: int = 45) -> List[float]:
        """Equity history for `mode`, oldest->newest — feeds the drift monitor."""
        cur = self._conn.execute(
            "SELECT equity FROM runs WHERE mode = ? ORDER BY ts DESC LIMIT ?", (mode, limit)
        )
        return [float(r[0]) for r in reversed(cur.fetchall())]

    def last_alert_date(self) -> date | None:
        """Return the calendar date of the most recently sent alert, or None if never."""
        row = self._conn.execute("SELECT last_alert_ts FROM alert_meta WHERE id = 1").fetchone()
        if row is None:
            return None
        return date.fromisoformat(row[0])

    def record_alert_date(self, d: date) -> None:
        """Persist *d* as the last alert date (upsert — only one row ever exists)."""
        self._conn.execute(
            "INSERT INTO alert_meta (id, last_alert_ts) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_alert_ts = excluded.last_alert_ts",
            (d.isoformat(),),
        )
        self._conn.commit()

    def history(self, mode: str) -> List[sqlite3.Row]:
        """All runs for `mode`, oldest->newest (ts, equity, orders, fills, halted) — for reporting."""
        self._conn.row_factory = sqlite3.Row
        cur = self._conn.execute("SELECT * FROM runs WHERE mode = ? ORDER BY ts ASC", (mode,))
        return cur.fetchall()

    def close(self) -> None:
        self._conn.close()


# ------------------------------------------------------------------- the cycle


def run_once(
    config: AppConfig,
    strategies: Sequence[BaseStrategy],
    *,
    clock: Optional[Clock] = None,
    feed: Optional[BaseDataFeed] = None,
    execution_engine: Optional[BaseExecutionEngine] = None,
    portfolio: Optional[Portfolio] = None,
    risk_manager: Optional[RiskManager] = None,
    state_store: Optional[StateStore] = None,
    lookback: int = 252,
) -> RunReport:
    """
    Execute one evaluation cycle and return a RunReport. All collaborators are
    injectable for offline testing; anything not supplied is built from ``config``.
    """
    clock = clock or RealClock()
    portfolio = portfolio or Portfolio(config.initial_capital)
    risk_manager = risk_manager or RiskManager(config.risk)

    fills: List[FillEvent] = []

    def _on_fill(fill: FillEvent) -> None:
        # Every confirmed fill must both book into the portfolio (close the risk
        # loop) and be recorded for the report — same wiring the backtest engine does.
        portfolio.on_fill(fill)
        fills.append(fill)

    execution_engine = execution_engine or make_execution_engine(config, on_fill=_on_fill)
    # Bind regardless of how the engine was constructed (incl. injected ones).
    execution_engine.bind_fill_handler(_on_fill)

    symbols: List[Symbol] = list(_collect_symbols(strategies))
    feed = feed or AlpacaDataFeed(symbols, timeframe="1Day")

    now = clock.now()
    report = RunReport(
        timestamp=now, mode=config.mode.value, equity=float(portfolio.equity), num_positions=0
    )

    execution_engine.connect()
    try:
        # 2. Reconcile broker truth into the local portfolio.
        report.reconciled = _reconcile(execution_engine, portfolio, symbols)

        # 2b. NOW-7: diff broker truth against what we EXPECTED to hold from the last
        # persisted run. A notional mismatch > $1 means an unrecorded fill or a manual /
        # unexpected trade — we may be acting on a wrong picture, so de-risk: block NEW
        # entries this cycle (exits still allowed). Non-fatal; never crashes the cycle.
        report.reconcile_discrepancy = _detect_reconcile_discrepancy(execution_engine, state_store)

        # 3. Fetch the most recent window, warming up indicators.
        feed.connect()
        try:
            _load_window(feed, lookback, now)
            events = list(feed.stream())
        finally:
            feed.disconnect()

        if not events:
            logger.warning("run_once: no bars fetched — nothing to evaluate.")
            report.equity = float(portfolio.equity)
            report.num_positions = len(portfolio.open_positions)
            _persist(state_store, report, portfolio)
            return report

        # 4. Warm strategies over the window; act only on the latest bar's signals.
        latest_signals = _evaluate(events, strategies, portfolio)
        report.signals_evaluated = len(latest_signals)

        # 4-NOW-5: establish TODAY's clean daily-loss baseline. The window replay above
        # has marked the portfolio to today's market, so portfolio.equity is now the
        # current opening equity. Record it once per calendar day (keyed by the injected
        # clock's date) and seed the circuit breaker's day_start_equity from the stored
        # morning value — so a mid-day re-fire after a loss reuses the SAME baseline, not
        # a down intraday value, and never falls back to initial_capital or yesterday.
        _set_daily_baseline(portfolio, state_store, clock, report.mode)

        # 4a. Manual kill switch (APEX_HALT) — emergency human override. Blocks ALL
        # orders this cycle, no exceptions. Highest priority, checked before anything.
        if _kill_switch_active():
            report.killed = True
            if latest_signals:
                logger.critical(
                    "KILL SWITCH (APEX_HALT) active — blocking ALL %d orders.", len(latest_signals)
                )
            latest_signals = []

        # 4b. Drift guard: if the live strategy has decayed below its quarantine
        # floor (rolling Sharpe < 70% of validated), stop opening NEW positions.
        # De-risking exits are always still allowed.
        pre = _drift_monitor(state_store, report.mode)
        if pre is not None and pre.check().is_quarantined:
            report.quarantined = True
            kept = [s for s in latest_signals if _signal_reduces(s, portfolio)]
            if len(kept) != len(latest_signals):
                logger.critical(
                    "QUARANTINED (alpha decay) — blocking %d new entr(ies).",
                    len(latest_signals) - len(kept),
                )
            latest_signals = kept

        # 4c. NOW-7 reconcile-discrepancy guard: broker truth disagreed with our last
        # persisted snapshot. We may be acting on a wrong picture, so block NEW entries
        # this cycle while still permitting de-risking exits (mirrors the quarantine
        # guard). Already alerted in step 2b; here we just gate the entries.
        if report.reconcile_discrepancy:
            kept = [s for s in latest_signals if _signal_reduces(s, portfolio)]
            if len(kept) != len(latest_signals):
                logger.critical(
                    "RECONCILE DISCREPANCY — blocking %d new entr(ies); exits still allowed.",
                    len(latest_signals) - len(kept),
                )
            latest_signals = kept

        _submit_orders(
            latest_signals,
            events,
            risk_manager,
            portfolio,
            execution_engine,
            report,
            allocator=config.allocation,
        )
    finally:
        execution_engine.disconnect()

    report.fills = fills
    report.halted = risk_manager.is_halted
    report.equity = float(portfolio.equity)
    report.num_positions = len(portfolio.open_positions)

    # 5. Record this cycle's equity into the drift monitor for the report + alerting.
    mon = _drift_monitor(state_store, report.mode)
    if mon is not None:
        reading = mon.record_equity(report.equity)
        report.drift_summary = reading.summary()
        report.quarantined = report.quarantined or reading.is_quarantined

    _persist(state_store, report, portfolio)
    logger.info(report.summary())
    _notify_cycle(report, state_store)
    return report


# ----------------------------------------------------------------- internals


def _collect_symbols(strategies: Sequence[BaseStrategy]) -> List[Symbol]:
    """Union of every strategy's symbols, de-duplicated by ticker, stable order."""
    seen: Dict[str, Symbol] = {}
    for strat in strategies:
        for sym in strat.symbols:
            seen.setdefault(sym.ticker, sym)
    return list(seen.values())


def _reconcile(
    engine: BaseExecutionEngine, portfolio: Portfolio, symbols: Sequence[Symbol]
) -> bool:
    """
    Seed the portfolio from the broker's real positions. Returns True if any
    position was reconciled. The simulator returns {} → nothing to do.

    Seeding uses only the portfolio's public fill API: a synthetic fill at the
    broker's average entry price establishes each holding without inventing P&L.
    """
    truth = engine.reconcile_positions()
    if not truth:
        return False

    by_ticker = {s.ticker: s for s in symbols}
    for ticker, pos in truth.items():
        symbol = by_ticker.get(ticker)
        if symbol is None:
            logger.warning("Broker holds %s but no strategy trades it — leaving as-is.", ticker)
            continue
        qty = Decimal(str(pos["qty"]))
        avg = Decimal(str(pos["avg_entry_price"]))
        if qty == 0:
            continue
        # Long → BUY |qty|; short → SELL |qty|. Fill at avg entry: equity is
        # unchanged at seed time, then marked to market when the window replays.
        side = OrderSide.BUY if qty > 0 else OrderSide.SELL
        portfolio.on_fill(
            FillEvent(
                symbol=symbol,
                side=side,
                quantity=abs(qty),
                fill_price=avg,
                commission=Decimal("0"),
                slippage=Decimal("0"),
                order_id="reconcile",
                broker_order_id="reconcile",
                timestamp=None,
                is_paper=engine.is_paper,
            )
        )
    logger.info("Reconciled %d broker position(s) into the portfolio.", len(truth))
    return True


def _load_window(feed: BaseDataFeed, lookback: int, now: datetime) -> None:
    """
    Fetch the recent window. AlpacaDataFeed exposes get_latest_bars; other feeds
    (an injected stub, or HistoricalDataFeed) are assumed already loaded.
    """
    getter = getattr(feed, "get_latest_bars", None)
    if callable(getter):
        getter(lookback=lookback, end=now)


def _evaluate(
    events: List[MarketEvent], strategies: Sequence[BaseStrategy], portfolio: Portfolio
) -> List:
    """
    Replay the window so strategies build their indicator state and the portfolio
    marks to market, then return only the signals emitted on the LATEST bar(s).
    Signals from earlier (warmup) bars are historical and must not be acted on.
    """
    bars = [e.bar for e in events if e.bar is not None]
    if not bars:
        return []
    # Each symbol's OWN most recent bar drives its is_latest flag. A single global max
    # would silently drop ALL signals (exits included) for any symbol whose last bar
    # predates another's — e.g. a commodity ETF trading on a day equities are closed.
    # Events are sorted (timestamp, ticker), so the last write per ticker is its max.
    latest_per_sym: dict = {}
    for b in bars:
        latest_per_sym[b.symbol.ticker] = b.timestamp

    context = StrategyContext()
    quarantined: set[str] = set()
    for strat in strategies:
        strat.bind_context(context)
        try:
            strat.on_start()
        except Exception as exc:  # noqa: BLE001
            logger.error("Strategy %s on_start raised; quarantining: %s", strat.strategy_id, exc)
            quarantined.add(strat.strategy_id)

    latest_signals: List = []
    for event in events:
        bar = event.bar
        if bar is None:
            continue
        portfolio.on_market(event)
        # Show strategies their ACTUAL (broker-reconciled) holdings. No fills happen
        # during this replay, so positions stay = reconciled truth — which is exactly
        # what a position-aware strategy needs to act correctly on the latest bar
        # (e.g. enter an established trend on a cold start, not wait for a new cross).
        context.sync_state(positions=dict(portfolio.open_positions), equity=portfolio.equity)
        is_latest = bar.timestamp == latest_per_sym.get(bar.symbol.ticker)
        for strat in strategies:
            if strat.strategy_id in quarantined:
                continue
            try:
                signals = strat.handle_market_event(event)
            except Exception as exc:  # noqa: BLE001 — quarantine, don't crash the cron
                logger.error("Strategy %s raised; quarantining: %s", strat.strategy_id, exc)
                quarantined.add(strat.strategy_id)
                continue
            if is_latest:
                latest_signals.extend(signals)
    return latest_signals


def _submit_orders(
    signals: List,
    events: List[MarketEvent],
    risk_manager: RiskManager,
    portfolio: Portfolio,
    engine: BaseExecutionEngine,
    report: RunReport,
    allocator: Optional[CapitalAllocator] = None,
) -> None:
    """
    Risk-evaluate the latest signals (reducing/exit signals first so freed capital
    is available to entries) and submit each approved order to the execution engine.

    When an ``allocator`` is supplied (Phase F3.3 multi-strategy), each ENTRY is sized against
    a capital-scoped view of the portfolio (its sleeve's weight × equity); REDUCES are never
    scoped so an exit can always flatten its full position. With no allocator (the default,
    single-sleeve trend bot) sizing is unchanged.
    """
    if not signals:
        return
    latest_close = {e.bar.symbol.ticker: e.bar.close for e in events if e.bar is not None}

    reduces = [s for s in signals if _signal_reduces(s, portfolio)]
    entries = [s for s in signals if not _signal_reduces(s, portfolio)]
    # (signal, is_entry) — reduces first so freed capital is available to entries.
    ordered = [(s, False) for s in reduces] + [(s, True) for s in entries]
    for signal, is_entry in ordered:
        if risk_manager.is_halted:
            logger.warning("System halted — skipping remaining signals.")
            break
        sizing_portfolio = (
            allocator.scoped(portfolio, signal.strategy_id)
            if (is_entry and allocator is not None)
            else portfolio
        )
        order = risk_manager.evaluate(signal, sizing_portfolio)
        if order is None:
            continue
        _submit(order, latest_close, engine, report)


def _submit(
    order: OrderEvent,
    latest_close: Dict[str, Decimal],
    engine: BaseExecutionEngine,
    report: RunReport,
) -> None:
    """Submit one approved order, priming the simulator's price if it needs one."""
    update_price = getattr(engine, "update_price", None)
    if callable(update_price) and order.symbol.ticker in latest_close:
        update_price(order.symbol.ticker, latest_close[order.symbol.ticker])
    try:
        engine.submit_order(order)
        report.orders_submitted += 1
    except Exception as exc:  # noqa: BLE001 — a failed submit must not abort the cycle
        logger.error("submit_order failed for %s: %s", order.symbol.ticker, exc)


def _signal_reduces(signal, portfolio: Portfolio) -> bool:
    """SELL while long / BUY while short — an exit that frees capital."""
    held = portfolio.open_positions.get(signal.symbol.ticker)
    if held is None:
        return False
    q = held.quantity
    return (q > 0 and signal.side == OrderSide.SELL) or (q < 0 and signal.side == OrderSide.BUY)


# Notional tolerance for the reconciliation diff: a per-symbol |qty delta| x price
# discrepancy above this (in account currency) is treated as a real mismatch — an
# unrecorded fill or a manual/unexpected trade. Sub-dollar drift (rounding, fractional
# dust) is ignored to avoid false alarms.
_RECONCILE_NOTIONAL_TOLERANCE = Decimal("1")


def _set_daily_baseline(
    portfolio: Portfolio,
    store: Optional[StateStore],
    clock: Clock,
    mode: str,
) -> None:
    """
    NOW-5: seed the daily-loss circuit breaker's baseline with TODAY's opening equity,
    recorded ONCE per calendar day (keyed by the injected clock's date), so a mid-day
    re-fire after a loss reuses the morning value instead of an already-down one.

    Without a store there is nothing to persist across re-fires, so we leave the
    portfolio's own day_start_equity (set at construction) untouched. The portfolio
    exposes no public setter for an arbitrary baseline (start_new_day always snaps to
    *current* equity, which is exactly the down-value bug we are avoiding), so we set the
    backing field directly — the only safe way to inject the stored morning baseline.
    """
    if store is None:
        return
    day = clock.now().date().isoformat()
    baseline = store.day_start_equity(day, mode, float(portfolio.equity))
    portfolio._day_start_equity = Decimal(str(baseline))


def _detect_reconcile_discrepancy(engine: BaseExecutionEngine, store: Optional[StateStore]) -> bool:
    """
    NOW-7: compare the broker's CURRENT positions against what we EXPECTED to hold from
    the last persisted run. Returns True if any symbol's quantity differs by a notional
    value (|qty delta| x price) above the tolerance — an unrecorded fill or a manual /
    unexpected trade. Logs loudly and fires a non-fatal alert on any discrepancy.

    Fully defensive: any error (no store, malformed snapshot, engine quirk) returns False
    so the diff can never crash the cycle. The entry-blocking de-risk is applied upstream.
    """
    if store is None:
        return False
    try:
        truth = engine.reconcile_positions() or {}
    except Exception as exc:  # noqa: BLE001 — diff must never crash the cycle
        logger.warning("reconcile diff: broker truth unavailable: %s", exc)
        return False

    last = store.last_run()
    expected: Dict[str, dict] = {}
    if last is not None:
        try:
            expected = json.loads(last["positions"]) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("reconcile diff: could not parse last snapshot: %s", exc)
            expected = {}

    mismatches: List[str] = []
    for ticker in set(truth) | set(expected):
        broker = truth.get(ticker, {})
        ours = expected.get(ticker, {})
        try:
            broker_qty = Decimal(str(broker.get("qty", "0")))
            our_qty = Decimal(str(ours.get("qty", "0")))
            # Prefer the broker's current price; fall back to its avg entry or our last.
            price_src = (
                broker.get("current_price")
                or broker.get("avg_entry_price")
                or ours.get("current_price")
                or ours.get("avg_entry_price")
                or "0"
            )
            price = Decimal(str(price_src))
        except Exception as exc:  # noqa: BLE001 — bad row → flag conservatively, fail closed
            logger.warning("reconcile diff: unparseable position for %s: %s", ticker, exc)
            mismatches.append(ticker)
            continue
        notional = abs(broker_qty - our_qty) * abs(price)
        if notional > _RECONCILE_NOTIONAL_TOLERANCE:
            mismatches.append(
                f"{ticker}: broker={broker_qty} expected={our_qty} (~${notional:.2f})"
            )

    if not mismatches:
        return False

    detail = "; ".join(str(m) for m in mismatches)
    logger.critical(
        "RECONCILE DISCREPANCY — broker truth disagrees with last persisted run: %s", detail
    )
    _notify(
        "Apex Quant - RECONCILE DISCREPANCY",
        f"Broker positions diverge from last run; blocking new entries this cycle. {detail}",
        priority="urgent",
    )
    return True


# The deployed strategy's Gauntlet-validated full Sharpe (smart-7 trend, Session 15/16).
# The drift monitor quarantines if the live 30-day rolling Sharpe falls below 70% of it.
DEPLOYED_VALIDATED_SHARPE = 0.85


def _notify(title: str, message: str, priority: str = "default") -> None:
    """Free push via ntfy.sh if NTFY_TOPIC is set. Never raises — observability only."""
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": "robot"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:  # noqa: BLE001 — a notify failure must never break the cron
        logger.warning("ntfy notify failed: %s", exc)


def _notify_cycle(report: RunReport, store: Optional[StateStore] = None) -> None:
    """Send actionable alerts and a once-daily heartbeat via the alerts policy module.

    Uses ``report.timestamp.date()`` (the injected clock's date) rather than
    ``date.today()`` so the function stays deterministic. Wrapped in a top-level
    try/except so a notify or DB failure can never break the cron cycle.
    """
    try:
        notifier = NtfyNotifier()
        today = report.timestamp.date()
        last = store.last_alert_date() if store is not None else None
        is_new = should_heartbeat(last, today)
        alerts = decide_alerts(
            killed=report.killed,
            quarantined=report.quarantined,
            halted=report.halted,
            orders_submitted=report.orders_submitted,
            summary=report.summary(),
            is_new_day=is_new,
        )
        send_alerts(notifier, alerts)
        if alerts and store is not None:
            store.record_alert_date(today)
    except Exception as exc:  # noqa: BLE001 — notify/db failure must never break the cron
        logger.warning("_notify_cycle failed (non-fatal): %s", exc)


def _kill_switch_active() -> bool:
    """
    Manual emergency stop. Set the APEX_HALT env var (1/true/yes/on) to block ALL new
    orders on the next cycle — the going-live kill switch. Independent of the automatic
    drawdown/daily circuit breakers; this is the human override.
    """
    return os.getenv("APEX_HALT", "").strip().lower() in ("1", "true", "yes", "on")


def _drift_monitor(store: Optional[StateStore], mode: str) -> Optional[DriftMonitor]:
    """Rebuild the drift monitor from persisted equity history (one point per cycle)."""
    if store is None:
        return None
    try:
        mon = DriftMonitor(
            "multi_asset_trend", validated_sharpe=DEPLOYED_VALIDATED_SHARPE, window=30
        )
        for eq in store.recent_equities(mode):
            mon.record_equity(eq)
        return mon
    except Exception as exc:  # noqa: BLE001 — drift is protective, never fatal
        logger.warning("drift monitor unavailable: %s", exc)
        return None


def _persist(store: Optional[StateStore], report: RunReport, portfolio: Portfolio) -> None:
    if store is None:
        return
    positions = {
        t: {
            "qty": str(p.quantity),
            "avg_entry_price": str(p.avg_entry_price),
            "current_price": str(p.current_price),
        }
        for t, p in portfolio.open_positions.items()
    }
    store.save_run(report, positions)


# ------------------------------------------------------------------- main wiring

# The deployed strategy: MULTI-ASSET TREND FOLLOWING with INVERSE-VOL sizing.
# The 200-day trend filter applied across five uncorrelated asset classes (US
# equities, intl equities, long Treasuries, gold, broad commodities), sized by
# inverse volatility (risk-parity) rather than flat 20% — the calmest sleeve gets
# the full cap, wilder sleeves scale down. Validated through the real-data Gauntlet
# (Session 11): grade A 7/7, full Sharpe 0.81, OOS 1.10, MC p=0.002, survives 2x
# costs, beats SPY at lower correlation (0.25). Inverse-vol cut the realized
# backtest drawdown from 15% to 8% vs. the equal-weight baseline. All five are
# liquid Alpaca ETFs.
DEPLOYED_UNIVERSE = ("SPY", "EFA", "TLT", "GLD", "DBC", "UUP", "DBA")

# Production risk for the multi-asset trend sleeve. Position cap 20% (the calmest
# sleeve's full inverse-vol weight); circuit breakers set ABOVE the strategy's
# normal drawdown range so they act as catastrophe stops, not constant trips (a
# trend strategy's ordinary drawdowns would trip the default 10% breaker every
# cycle).
PRODUCTION_RISK = RiskConfig(
    max_position_size_pct=Decimal("0.16"),
    max_total_exposure_pct=Decimal("1.0"),
    max_leverage=Decimal("1.0"),
    max_drawdown_pct=Decimal("0.40"),  # catastrophe halt, well above normal DD
    max_daily_loss_pct=Decimal("0.10"),
    require_stop_loss=True,
    # Survive the strategy's deep trend drawdowns: once down 12% from peak, size new
    # entries down, reaching 35% size by a 30% drawdown. Protects the equity path so a
    # bad run is survivable well before the 40% catastrophe halt fires.
    #
    # NOTE (NOW-4): drawdown_throttle_start is DELIBERATELY 0.12, not the roadmap's
    # suggested 0.05. A 200-day trend strategy's ordinary drawdowns routinely run past
    # 5%, so a 0.05 throttle would down-size nearly every cycle and bleed the edge. The
    # 0.12 start sits just above the strategy's normal DD band so the throttle acts as a
    # slump-survival overlay, not a constant brake. Left unchanged on purpose.
    drawdown_throttle_start=Decimal("0.12"),
    drawdown_throttle_full=Decimal("0.30"),
    drawdown_throttle_floor=Decimal("0.35"),
    # NOW-4: ENABLE the volatility-target overlay (built but previously OFF). New entries
    # are scaled by target_volatility / portfolio_realized_vol, clamped to
    # [vol_scale_min, vol_scale_max]. With vol_scale_max=1.0 and no leverage it acts as a
    # pure turbulence de-risker: the book shrinks when realized vol runs hot above the
    # ~12% annualized target and returns to full size when it cools. 0.12 matches the
    # managed-futures roadmap target for this sleeve.
    target_volatility=Decimal("0.12"),
)


def _build_strategies(config: AppConfig) -> List[BaseStrategy]:  # pragma: no cover - live wiring
    """The deployed roster: the inverse-vol multi-asset trend strategy."""
    from apex.core.models import AssetClass
    from apex.strategy.library.multi_asset_trend import MultiAssetTrendStrategy

    syms = [Symbol(t, AssetClass.ETF) for t in DEPLOYED_UNIVERSE]
    return [MultiAssetTrendStrategy("multi_asset_trend", syms, fast_period=20, slow_period=200)]


def main() -> int:  # pragma: no cover - reads real env/keys + network
    import dataclasses

    logging.basicConfig(level=logging.INFO)
    config = AppConfig.from_env()
    if config.mode == ExecutionMode.BACKTEST:
        print("run_once is for paper/live cron cycles. Use run_backtest for backtests.")
        return 2
    if config.broker != Broker.ALPACA:
        print(f"run_once currently supports the Alpaca broker only, got {config.broker.value}.")
        return 2

    # Apply the deployed strategy's production risk config.
    config = dataclasses.replace(config, risk=PRODUCTION_RISK)
    report = run_once(config, _build_strategies(config), state_store=StateStore())
    print(report.summary())
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
