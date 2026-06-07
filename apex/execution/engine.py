"""
apex.execution.engine
=====================
TradingEngine: the orchestration loop that wires the whole system together.

Data flow it drives (the canonical Apex loop):

    MarketEvent ─▶ Portfolio.on_market (mark to market)
               └─▶ Strategy.on_bar ─▶ SignalEvent
                                   └─▶ RiskManager.evaluate ─▶ OrderEvent
                                                            └─▶ (queued)
    next bar for that symbol ─▶ ExecutionEngine.submit_order ─▶ FillEvent
                                                             └─▶ Portfolio.on_fill

Two correctness properties this loop guarantees:

  1. NO LOOK-AHEAD. A signal computed on a bar's close is filled at the NEXT
     bar's OPEN for that symbol — never at the close it was computed from. This
     is the single most important anti-overfitting property of a backtester;
     filling at the deciding bar's price silently inflates every result.

  2. HALT IS ABSOLUTE. Once the RiskManager halts (drawdown/daily-loss breach),
     no new orders are queued. Marking-to-market and equity recording continue
     so the halted equity curve is still observable.

The same engine runs a backtest (feed = HistoricalDataFeed.stream() or an
in-memory list) and a live/paper session (feed = a live feed's stream()).
Only the data source and the execution engine differ — selected by config.

Determinism: given the same events and components, the engine reaches the same
state every run. It uses no wall-clock time and no randomness of its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Set

from apex.core.events import FillEvent, MarketEvent
from apex.core.models import OrderSide
from apex.execution.base_execution import BaseExecutionEngine
from apex.risk.portfolio import Portfolio
from apex.risk.risk_manager import RiskManager
from apex.strategy.base_strategy import BaseStrategy, StrategyContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Snapshot:
    """
    A read-only portfolio view passed to the RiskManager when sizing entries
    against a portfolio projected free of pending exits. Exposes exactly the six
    attributes the RiskManager reads.
    """

    equity: Decimal
    peak_equity: Decimal
    day_start_equity: Decimal
    open_positions: Dict[str, object]
    exposure: Decimal
    last_price: Dict[str, Decimal]
    realized_volatility: Optional[float] = None  # so vol-targeting works on rotation bars


@dataclass
class BacktestResult:
    """The observable outcome of an engine run."""

    equity_curve: List[float] = field(default_factory=list)
    equity_timestamps: List[datetime] = field(default_factory=list)
    trade_returns: List[float] = field(default_factory=list)
    trade_timestamps: List[datetime] = field(default_factory=list)
    fills: List[FillEvent] = field(default_factory=list)
    initial_capital: float = 0.0
    final_equity: float = 0.0
    num_trades: int = 0
    halted: bool = False

    def summary(self) -> str:
        ret = (self.final_equity / self.initial_capital - 1.0) if self.initial_capital else 0.0
        return (
            f"Backtest: {len(self.equity_curve)} bars, {self.num_trades} trades, "
            f"final equity {self.final_equity:,.0f} ({ret:+.1%}), "
            f"{'HALTED' if self.halted else 'completed'}"
        )


class TradingEngine:
    """
    Wires feed → strategies → risk → execution → portfolio and runs the loop.

    Args:
        market_events: an iterable of MarketEvents in chronological order
                       (e.g. HistoricalDataFeed.stream(), or an in-memory list).
        strategies:    the strategies to run (each gets a read-only context).
        risk_manager:  the single gatekeeper; only producer of OrderEvents.
        portfolio:     position/cash/equity tracker (closes the risk loop).
        execution_engine: turns approved orders into fills.
        fill_timing:   "next_open" (default, no look-ahead) or "close"
                       (fill at the deciding bar's close — optimistic, for
                       quick checks only).
    """

    def __init__(
        self,
        market_events: Iterable[MarketEvent],
        strategies: List[BaseStrategy],
        risk_manager: RiskManager,
        portfolio: Portfolio,
        execution_engine: BaseExecutionEngine,
        fill_timing: str = "next_open",
    ) -> None:
        if fill_timing not in ("next_open", "close"):
            raise ValueError("fill_timing must be 'next_open' or 'close'")
        self.market_events = market_events
        self.strategies = strategies
        self.risk_manager = risk_manager
        self.portfolio = portfolio
        self.execution_engine = execution_engine
        self.fill_timing = fill_timing

        # Approved orders awaiting their fill bar (next_open mode).
        self._pending: List = []
        # Bar timestamp currently being processed (for dating recorded trades).
        self._current_ts: Optional[datetime] = None
        # Strategies that raised — quarantined so one bad strategy can't crash the run.
        self._quarantined: Set[str] = set()
        # The shared read-only context bound to every strategy (set in run()).
        self._context: Optional[StrategyContext] = None

        # Result accumulators.
        self.result = BacktestResult(
            initial_capital=float(self.portfolio.equity),
        )

        # Route fills from the execution engine back into our handler.
        self.execution_engine.bind_fill_handler(self._on_fill)

    # ------------------------------------------------------------------ run

    def run(self) -> BacktestResult:
        """Execute the full loop over all market events and return the result."""
        self.execution_engine.connect()

        # Give every strategy a read-only context and the startup hook.
        context = StrategyContext()
        self._context = context
        for strat in self.strategies:
            strat.bind_context(context)
            try:
                strat.on_start()
            except Exception as exc:  # noqa: BLE001 — never let setup crash the run
                logger.error("Strategy %s on_start raised: %s", strat.strategy_id, exc)
                self._quarantined.add(strat.strategy_id)

        prev_ts: Optional[datetime] = None
        _was_halted: bool = False  # tracks the transition so cancel fires exactly once
        try:
            for event in self.market_events:
                bar = event.bar
                if bar is None:
                    continue  # ticks not handled by this bar-driven loop

                ts = bar.timestamp
                self._current_ts = ts
                # A new timestamp means the previous day is fully processed.
                if prev_ts is not None and ts != prev_ts:
                    self._record_equity(prev_ts)
                    self.portfolio.start_new_day()
                    # Clear any daily-loss halt for the new day (drawdown halts,
                    # being more severe, are sticky and NOT cleared here).
                    self.risk_manager.reset_daily()
                    # A cleared daily halt resumes trading — reset the sentinel.
                    if _was_halted and not self.risk_manager.is_halted:
                        _was_halted = False
                prev_ts = ts

                # 1. Fill orders queued on a prior bar, at THIS bar's open (no look-ahead).
                if self.fill_timing == "next_open":
                    self._fill_pending(bar.symbol.ticker, bar.open)

                # 2. Mark the position to this bar's close.
                self.execution_engine.update_price(bar.symbol.ticker, bar.close)
                self.portfolio.on_market(event)

                # 3. Strategies react to the bar → signals → risk → queued orders.
                #    Refresh the read-only context FIRST: prior-bar orders have just
                #    filled (step 1), so position-aware strategies see their true,
                #    current holdings and won't re-enter what they already hold.
                self._sync_context()
                self._dispatch(event)

                # NOW-6: On the first bar where the RiskManager transitions into a
                # halted state, cancel all working broker orders immediately so the
                # system carries NO live exposure from resting orders after a halt.
                # Called exactly once per halt event (the sentinel prevents re-firing
                # on every subsequent bar while still halted).
                if self.risk_manager.is_halted and not _was_halted:
                    _was_halted = True
                    try:
                        self.execution_engine.cancel_open_orders()
                        logger.critical("HALT detected — cancelled all open broker orders.")
                    except Exception as exc:  # noqa: BLE001 — never let cancel crash the run
                        logger.error("cancel_open_orders() raised during halt handling: %s", exc)

                # 4. "close" timing fills immediately at this same close (optimistic).
                if self.fill_timing == "close":
                    self._fill_pending(bar.symbol.ticker, bar.close)
        finally:
            self.execution_engine.disconnect()

        # Record the final day and assemble the result.
        if prev_ts is not None:
            self._record_equity(prev_ts)
        # Any order still queued at end-of-stream never got a bar to fill against (a symbol
        # gap, a data error, or a signal on the very last bar). Surface it loudly rather
        # than silently abandoning capital the risk snapshot already allocated.
        for order in self._pending:
            logger.warning(
                "OrderEvent %s for %s never filled (no subsequent bar) — dropped.",
                order.event_id,
                order.symbol.ticker,
            )
        self.result.final_equity = float(self.portfolio.equity)
        self.result.num_trades = len(self.result.trade_returns)
        self.result.halted = self.risk_manager.is_halted
        if self._quarantined:
            logger.warning("Quarantined strategies (raised during run): %s", self._quarantined)
        return self.result

    # ------------------------------------------------------------- internals

    def _sync_context(self) -> None:
        """Refresh the strategies' read-only view with the portfolio's live state."""
        if self._context is not None:
            self._context.sync_state(
                positions=dict(self.portfolio.open_positions),
                equity=self.portfolio.equity,
            )

    def _dispatch(self, event: MarketEvent) -> None:
        """
        Collect this bar's signals, then queue approved orders.

        Exits (reducing signals) are evaluated first against the live portfolio.
        Entries are then evaluated against a portfolio PROJECTED free of those
        pending exits — so a rotation's BUY is sized against the capital its SELL
        is about to free, even though both fills happen next bar. Without this,
        the BUY would be sized against the still-fully-invested portfolio and
        rejected for "zero quantity (exposure cap)".
        """
        all_signals = []
        for strat in self.strategies:
            if strat.strategy_id in self._quarantined:
                continue
            try:
                all_signals.extend(strat.handle_market_event(event))
            except Exception as exc:  # noqa: BLE001 — quarantine, don't crash
                logger.error(
                    "Strategy %s raised on bar; quarantining: %s",
                    strat.strategy_id,
                    exc,
                    exc_info=True,
                )
                self._quarantined.add(strat.strategy_id)
        if not all_signals:
            return

        reduces = [s for s in all_signals if self._signal_reduces(s)]
        entries = [s for s in all_signals if not self._signal_reduces(s)]

        exiting: Set[str] = set()
        for signal in reduces:
            if self.risk_manager.is_halted:
                return
            order = self.risk_manager.evaluate(signal, self.portfolio)
            if order is not None:
                self._pending.append(order)
                exiting.add(signal.symbol.ticker)

        snapshot = self._project_after_exits(exiting) if exiting else self.portfolio
        for signal in entries:
            if self.risk_manager.is_halted:
                return
            order = self.risk_manager.evaluate(signal, snapshot)
            if order is not None:
                self._pending.append(order)

    def _signal_reduces(self, signal) -> bool:
        """Mirror of the RiskManager's reduce test: SELL while long / BUY while short."""
        held = self.portfolio.open_positions.get(signal.symbol.ticker)
        if held is None:
            return False
        q = held.quantity
        return (q > 0 and signal.side == OrderSide.SELL) or (q < 0 and signal.side == OrderSide.BUY)

    def _project_after_exits(self, exiting: Set[str]) -> "_Snapshot":
        """A read-only portfolio snapshot with the exiting positions removed."""
        positions = {t: p for t, p in self.portfolio.open_positions.items() if t not in exiting}
        exposure = sum((abs(p.market_value) for p in positions.values()), Decimal("0"))
        return _Snapshot(
            equity=self.portfolio.equity,
            peak_equity=self.portfolio.peak_equity,
            day_start_equity=self.portfolio.day_start_equity,
            open_positions=positions,
            exposure=exposure,
            last_price=self.portfolio.last_price,
            realized_volatility=getattr(self.portfolio, "realized_volatility", None),
        )

    def _fill_pending(self, ticker: str, price: Decimal) -> None:
        """Submit every queued order for `ticker`, filling at `price`."""
        if not self._pending:
            return
        self.execution_engine.update_price(ticker, price)
        still_pending: List = []
        for order in self._pending:
            if order.symbol.ticker == ticker:
                try:
                    self.execution_engine.submit_order(order)
                except Exception as exc:  # noqa: BLE001 — a bad fill drops the order, run continues
                    logger.error("submit_order failed for %s, dropping: %s", ticker, exc)
            else:
                still_pending.append(order)
        self._pending = still_pending

    def _on_fill(self, fill: FillEvent) -> None:
        """Execution engine callback: book the fill and record any closed trade."""
        ticker = fill.symbol.ticker
        pos_before = self.portfolio.open_positions.get(ticker)
        self.portfolio.on_fill(fill)

        # A SELL that reduces an existing long realizes a round-trip return.
        if fill.side == OrderSide.SELL and pos_before is not None and pos_before.quantity > 0:
            entry = pos_before.avg_entry_price
            if entry > 0:
                self.result.trade_returns.append(float((fill.fill_price - entry) / entry))
                self.result.trade_timestamps.append(self._current_ts)
        # A BUY that covers an existing short realizes a round-trip return too — without
        # this, Monte Carlo (Gate 4) would see an incomplete trade set for short strategies.
        elif fill.side == OrderSide.BUY and pos_before is not None and pos_before.quantity < 0:
            entry = pos_before.avg_entry_price
            if entry > 0:
                self.result.trade_returns.append(float((entry - fill.fill_price) / entry))
                self.result.trade_timestamps.append(self._current_ts)

        self.result.fills.append(fill)

    def _record_equity(self, ts: datetime) -> None:
        """Append one equity point for a completed timestamp (one point per day)."""
        self.result.equity_curve.append(float(self.portfolio.equity))
        self.result.equity_timestamps.append(ts)
