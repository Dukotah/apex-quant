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
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
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
from apex.risk.portfolio import Portfolio
from apex.risk.risk_manager import RiskConfig, RiskManager
from apex.strategy.base_strategy import BaseStrategy, StrategyContext

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

    def summary(self) -> str:
        return (
            f"run_once [{self.mode}] @ {self.timestamp:%Y-%m-%d %H:%M}Z: "
            f"equity {self.equity:,.2f}, {self.num_positions} positions, "
            f"{self.signals_evaluated} signals -> {self.orders_submitted} orders, "
            f"{len(self.fills)} fills{', HALTED' if self.halted else ''}"
        )


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

    def last_run(self) -> Optional[sqlite3.Row]:
        self._conn.row_factory = sqlite3.Row
        cur = self._conn.execute("SELECT * FROM runs ORDER BY ts DESC LIMIT 1")
        return cur.fetchone()

    def run_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

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
    report = RunReport(timestamp=now, mode=config.mode.value,
                       equity=float(portfolio.equity), num_positions=0)

    execution_engine.connect()
    try:
        # 2. Reconcile broker truth into the local portfolio.
        report.reconciled = _reconcile(execution_engine, portfolio, symbols)

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

        _submit_orders(latest_signals, events, risk_manager, portfolio, execution_engine, report)
    finally:
        execution_engine.disconnect()

    report.fills = fills
    report.halted = risk_manager.is_halted
    report.equity = float(portfolio.equity)
    report.num_positions = len(portfolio.open_positions)
    _persist(state_store, report, portfolio)
    logger.info(report.summary())
    return report


# ----------------------------------------------------------------- internals

def _collect_symbols(strategies: Sequence[BaseStrategy]) -> List[Symbol]:
    """Union of every strategy's symbols, de-duplicated by ticker, stable order."""
    seen: Dict[str, Symbol] = {}
    for strat in strategies:
        for sym in strat.symbols:
            seen.setdefault(sym.ticker, sym)
    return list(seen.values())


def _reconcile(engine: BaseExecutionEngine, portfolio: Portfolio,
               symbols: Sequence[Symbol]) -> bool:
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
        portfolio.on_fill(FillEvent(
            symbol=symbol, side=side, quantity=abs(qty), fill_price=avg,
            commission=Decimal("0"), slippage=Decimal("0"),
            order_id="reconcile", broker_order_id="reconcile",
            timestamp=None, is_paper=engine.is_paper,
        ))
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


def _evaluate(events: List[MarketEvent], strategies: Sequence[BaseStrategy],
              portfolio: Portfolio) -> List:
    """
    Replay the window so strategies build their indicator state and the portfolio
    marks to market, then return only the signals emitted on the LATEST bar(s).
    Signals from earlier (warmup) bars are historical and must not be acted on.
    """
    bars = [e.bar for e in events if e.bar is not None]
    if not bars:
        return []
    latest_ts = bars[-1].timestamp

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
        is_latest = bar.timestamp == latest_ts
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


def _submit_orders(signals: List, events: List[MarketEvent], risk_manager: RiskManager,
                   portfolio: Portfolio, engine: BaseExecutionEngine, report: RunReport) -> None:
    """
    Risk-evaluate the latest signals (reducing/exit signals first so freed capital
    is available to entries) and submit each approved order to the execution engine.
    """
    if not signals:
        return
    latest_close = {e.bar.symbol.ticker: e.bar.close for e in events if e.bar is not None}

    reduces = [s for s in signals if _signal_reduces(s, portfolio)]
    entries = [s for s in signals if not _signal_reduces(s, portfolio)]
    for signal in reduces + entries:
        if risk_manager.is_halted:
            logger.warning("System halted — skipping remaining signals.")
            break
        order = risk_manager.evaluate(signal, portfolio)
        if order is None:
            continue
        _submit(order, latest_close, engine, report)


def _submit(order: OrderEvent, latest_close: Dict[str, Decimal],
            engine: BaseExecutionEngine, report: RunReport) -> None:
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


def _persist(store: Optional[StateStore], report: RunReport, portfolio: Portfolio) -> None:
    if store is None:
        return
    positions = {
        t: {"qty": str(p.quantity), "avg_entry_price": str(p.avg_entry_price),
            "current_price": str(p.current_price)}
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
DEPLOYED_UNIVERSE = ("SPY", "EFA", "TLT", "GLD", "DBC")

# Production risk for the multi-asset trend sleeve. Position cap 20% (the calmest
# sleeve's full inverse-vol weight); circuit breakers set ABOVE the strategy's
# normal drawdown range so they act as catastrophe stops, not constant trips (a
# trend strategy's ordinary drawdowns would trip the default 10% breaker every
# cycle).
PRODUCTION_RISK = RiskConfig(
    max_position_size_pct=Decimal("0.20"),
    max_total_exposure_pct=Decimal("1.0"),
    max_leverage=Decimal("1.0"),
    max_drawdown_pct=Decimal("0.40"),      # catastrophe halt, well above normal DD
    max_daily_loss_pct=Decimal("0.10"),
    require_stop_loss=True,
    # Survive the strategy's deep trend drawdowns: once down 12% from peak, size new
    # entries down, reaching 35% size by a 30% drawdown. Protects the equity path so a
    # bad run is survivable well before the 40% catastrophe halt fires.
    drawdown_throttle_start=Decimal("0.12"),
    drawdown_throttle_full=Decimal("0.30"),
    drawdown_throttle_floor=Decimal("0.35"),
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
