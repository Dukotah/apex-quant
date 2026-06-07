"""
Tests for apex.execution.engine.TradingEngine and apex.execution.factory.

Validates the orchestration loop: next-bar-open fills (no look-ahead), per-day
equity recording, trade-return capture, halt enforcement, strategy quarantine,
and the mode→engine factory. Also probes the RiskManager exit-sizing behavior
so the integration assumptions are explicit.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List

from apex.core.config import AppConfig, Broker, ExecutionMode
from apex.core.events import MarketEvent, SignalEvent
from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.execution.engine import TradingEngine
from apex.execution.factory import make_execution_engine
from apex.execution.simulated import SimulatedExecutionEngine
from apex.risk.portfolio import Portfolio
from apex.risk.risk_manager import RiskConfig, RiskManager
from apex.strategy.base_strategy import BaseStrategy

SYM = Symbol("TEST", AssetClass.EQUITY)


class ScriptedStrategy(BaseStrategy):
    """Emits a BUY on bar index buy_at and a SELL on bar index sell_at."""

    def __init__(self, sid, symbols, buy_at=None, sell_at=None):
        super().__init__(sid, symbols)
        self.buy_at = buy_at
        self.sell_at = sell_at
        self._i = -1

    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        self._i += 1
        buy_bars = self.buy_at if isinstance(self.buy_at, (set, list, tuple)) else {self.buy_at}
        if self._i in buy_bars:
            return [
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.BUY,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    suggested_stop_loss=bar.close * Decimal("0.9"),
                    reason="scripted buy",
                )
            ]
        if self._i == self.sell_at:
            return [
                SignalEvent(
                    symbol=bar.symbol,
                    side=OrderSide.SELL,
                    strength=Decimal("1.0"),
                    strategy_id=self.strategy_id,
                    suggested_stop_loss=bar.close * Decimal("1.1"),
                    reason="scripted sell",
                )
            ]
        return []


class RaisingStrategy(BaseStrategy):
    def on_bar(self, bar: Bar) -> List[SignalEvent]:
        raise RuntimeError("boom")


def _bars(prices, sym=SYM, start=None):
    start = start or datetime(2024, 1, 1, tzinfo=timezone.utc)
    events = []
    for i, p in enumerate(prices):
        price = Decimal(str(p))
        bar = Bar(
            symbol=sym,
            timestamp=start + timedelta(days=i),
            open=price,
            high=price * Decimal("1.01"),
            low=price * Decimal("0.99"),
            close=price,
            volume=Decimal("1000"),
        )
        events.append(MarketEvent(bar=bar))
    return events


def _full_risk(**over):
    """RiskConfig allowing full deployment (single-strategy backtest)."""
    base = dict(
        max_position_size_pct=Decimal("1.0"),
        max_total_exposure_pct=Decimal("1.0"),
        max_leverage=Decimal("1.0"),
    )
    base.update(over)
    return RiskConfig(**base)


def _engine(events, strategies, risk_config=None, capital="100000", slippage="0.001"):
    portfolio = Portfolio(Decimal(capital))
    risk = RiskManager(risk_config or _full_risk())
    execu = SimulatedExecutionEngine(slippage_pct=Decimal(slippage))
    return TradingEngine(events, strategies, risk, portfolio, execu), portfolio, risk


def test_buy_fills_at_next_bar_open_with_slippage():
    events = _bars([100, 110, 120, 130])
    strat = ScriptedStrategy("s", [SYM], buy_at=0)  # buy on bar 0 (close=100)
    engine, portfolio, _ = _engine(events, [strat])
    result = engine.run()

    # The order is queued on bar 0 and filled at bar 1's OPEN (110), + slippage.
    assert len(result.fills) == 1
    fill = result.fills[0]
    assert fill.side == OrderSide.BUY
    assert fill.fill_price == Decimal("110") * Decimal("1.001")
    # Position exists and was sized from bar-0 close (100): ~100000/100 = 1000 shares.
    assert "TEST" in portfolio.open_positions
    assert portfolio.open_positions["TEST"].quantity == Decimal("1000")


def test_no_lookahead_signal_bar_is_not_the_fill_bar():
    events = _bars([100, 200])
    strat = ScriptedStrategy("s", [SYM], buy_at=0)
    engine, _, _ = _engine(events, [strat])
    result = engine.run()
    # If it filled at the deciding bar (100) the price would be 100*1.001.
    # Correct (next-open) behavior fills at bar 1 open = 200*1.001.
    assert result.fills[0].fill_price == Decimal("200") * Decimal("1.001")


def test_equity_recorded_once_per_day():
    events = _bars([100, 101, 102, 103, 104])
    strat = ScriptedStrategy("s", [SYM])  # no trades
    engine, _, _ = _engine(events, [strat])
    result = engine.run()
    assert len(result.equity_curve) == 5
    assert len(result.equity_timestamps) == 5
    # Flat (no positions) → equity stays at initial capital every day.
    assert all(abs(e - 100000.0) < 1e-6 for e in result.equity_curve)


def test_halt_blocks_new_orders():
    # Drawdown limit 1%; we force equity below peak by holding a long into a crash.
    events = _bars([100, 100, 50, 50, 50])
    # Buy on bar 0, then attempt another buy on bar 3 (post-crash) so the
    # RiskManager re-evaluates and detects the drawdown breach at step 1.
    strat = ScriptedStrategy("s", [SYM], buy_at={0, 3})
    rc = _full_risk(max_drawdown_pct=Decimal("0.05"))
    engine, portfolio, risk = _engine(events, [strat], risk_config=rc)
    result = engine.run()
    # Bought ~1000 shares at ~100; price crashes to 50 → ~50% drawdown → halt.
    assert risk.is_halted
    assert result.halted


def test_quarantine_isolates_raising_strategy():
    events = _bars([100, 101, 102])
    good = ScriptedStrategy("good", [SYM], buy_at=0)
    bad = RaisingStrategy("bad", [SYM])
    engine, portfolio, _ = _engine(events, [good, bad])
    result = engine.run()
    # The raising strategy is quarantined; the good one still trades.
    assert "bad" in engine._quarantined
    assert len(result.fills) >= 1


def test_factory_backtest_returns_simulated():
    cfg = AppConfig(mode=ExecutionMode.BACKTEST, broker=Broker.SIMULATED)
    engine = make_execution_engine(cfg)
    assert isinstance(engine, SimulatedExecutionEngine)
    assert engine.is_paper


def test_factory_live_alpaca_builds_live_engine():
    # Live Alpaca execution is now built — the factory returns the live (non-paper)
    # engine. (Constructing it does not touch the SDK; connect() would.)
    from apex.execution.alpaca import AlpacaExecutionEngine

    cfg = AppConfig(
        mode=ExecutionMode.LIVE, broker=Broker.ALPACA, alpaca_key="k", alpaca_secret="s"
    )
    engine = make_execution_engine(cfg)
    assert isinstance(engine, AlpacaExecutionEngine)
    assert engine.is_paper is False


def test_factory_live_unbuilt_broker_raises():
    cfg = AppConfig(mode=ExecutionMode.LIVE, broker=Broker.IBKR)
    try:
        make_execution_engine(cfg)
        assert False, "expected NotImplementedError"
    except NotImplementedError:
        pass


def test_exit_signal_closes_position_via_reduce_path():
    """A SELL while long flattens the position (reduce-aware sizing), even at
    full exposure where the old exposure-room sizing would have rejected it."""
    events = _bars([100, 101, 102, 103, 104])
    strat = ScriptedStrategy("s", [SYM], buy_at=0, sell_at=2)
    engine, portfolio, _ = _engine(events, [strat])  # full exposure config
    result = engine.run()
    # Bought ~1000 shares on bar 0→1; sold them on bar 2→3.
    sides = [f.side for f in result.fills]
    assert OrderSide.BUY in sides and OrderSide.SELL in sides
    assert "TEST" not in portfolio.open_positions  # fully flat after the exit
    assert len(result.trade_returns) == 1  # one completed round trip


def test_daily_loss_halt_clears_next_day():
    """A daily-loss breach halts trading for that day, but the engine's per-day
    reset_daily() clears it next day — it must NOT halt the system permanently."""
    # Hold a long through a one-day -10% drop (breaches the 2% daily-loss cap),
    # then recover. Strategy signals every bar so the breach is actually evaluated.
    events = _bars([100, 100, 100, 90, 100, 100])
    strat = ScriptedStrategy("s", [SYM], buy_at={0, 1, 2, 3, 4, 5})
    rc = _full_risk(max_daily_loss_pct=Decimal("0.02"), max_drawdown_pct=Decimal("0.99"))
    engine, portfolio, risk = _engine(events, [strat], risk_config=rc)
    engine.run()
    # Without the per-day reset this stays True forever after the -10% day.
    assert risk.is_halted is False


def test_round_trip_sell_behavior_is_observable():
    """
    Probe: with partial exposure there is room for a SELL to be sized, so a
    long can be (approximately) closed and a trade return recorded. This
    documents the current RiskManager exit-sizing behavior explicitly.
    """
    events = _bars([100, 105, 110, 115, 120])
    strat = ScriptedStrategy("s", [SYM], buy_at=0, sell_at=2)
    rc = _full_risk(max_position_size_pct=Decimal("0.4"))
    engine, portfolio, _ = _engine(events, [strat], risk_config=rc)
    result = engine.run()
    # At least the BUY fills; whether the SELL sizes depends on remaining room.
    sides = [f.side for f in result.fills]
    assert OrderSide.BUY in sides


# ------------------------------------------------------------------ NOW-6: halt cancels orders


class _TrackingExecutionEngine(SimulatedExecutionEngine):
    """
    Wraps SimulatedExecutionEngine and records every cancel_open_orders() call
    so integration tests can assert the engine was instructed to cancel on halt.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancel_open_calls: int = 0
        # Track any open order ids registered externally (simulates resting orders).
        self._open_orders: list = []

    def register_open_order(self, order_id: str) -> None:
        """Test helper: register a fake resting order id."""
        self._open_orders.append(order_id)

    def cancel_open_orders(self) -> None:
        self.cancel_open_calls += 1
        self._open_orders.clear()  # cancel clears them all
        import logging

        logging.getLogger(__name__).debug("_TrackingExecutionEngine: cancel_open_orders() called.")

    @property
    def open_orders(self) -> list:
        return list(self._open_orders)


def _engine_tracking(events, strategies, risk_config=None, capital="100000"):
    """Like _engine() but uses _TrackingExecutionEngine instead of plain Simulated."""
    portfolio = Portfolio(Decimal(capital))
    risk = RiskManager(risk_config or _full_risk())
    execu = _TrackingExecutionEngine(slippage_pct=Decimal("0.001"))
    engine = TradingEngine(events, strategies, risk, portfolio, execu)
    return engine, portfolio, risk, execu


def test_halt_triggers_cancel_open_orders_exactly_once():
    """
    Integration test (NOW-6): when a drawdown breach halts the system, the
    TradingEngine must call execution_engine.cancel_open_orders() so that no
    resting broker orders remain open after halt.

    Scenario:
      - 5% drawdown limit; strategy buys on bar 0 at price 100.
      - Price crashes to 50 on bar 2 → drawdown ~50% >> 5% → halt fires.
      - Engine must call cancel_open_orders() exactly once on the halt bar.
      - After the run, zero open orders remain (the engine cleared them).
    """
    events = _bars([100, 100, 50, 50, 50])
    strat = ScriptedStrategy("s", [SYM], buy_at={0, 3})
    rc = _full_risk(max_drawdown_pct=Decimal("0.05"))
    engine, portfolio, risk, tracking_execu = _engine_tracking(events, [strat], risk_config=rc)

    # Pre-register a fake resting order to simulate broker exposure.
    tracking_execu.register_open_order("FAKE-RESTING-ORDER-1")
    assert len(tracking_execu.open_orders) == 1

    result = engine.run()

    # 1. The system halted.
    assert risk.is_halted
    assert result.halted

    # 2. cancel_open_orders() was called exactly once (not on every subsequent bar).
    assert tracking_execu.cancel_open_calls == 1, (
        f"Expected exactly 1 cancel_open_orders() call on halt, "
        f"got {tracking_execu.cancel_open_calls}"
    )

    # 3. Zero open orders remain — the system carries no live broker exposure.
    assert tracking_execu.open_orders == [], (
        "Open orders must be empty after halt (system must carry zero resting exposure)"
    )


def test_daily_loss_halt_triggers_cancel_then_clears_next_day():
    """
    A daily-loss breach also fires cancel_open_orders() when it halts. After
    reset_daily() clears the halt, a subsequent halt on the next day fires
    cancel_open_orders() again (sentinel resets with the daily state).

    This test uses a single daily-loss breach: cancel fires on the breach bar,
    and the run completes normally.
    """
    events = _bars([100, 100, 100, 90, 100, 100])
    strat = ScriptedStrategy("s", [SYM], buy_at={0, 1, 2, 3, 4, 5})
    rc = _full_risk(max_daily_loss_pct=Decimal("0.02"), max_drawdown_pct=Decimal("0.99"))
    engine, portfolio, risk, tracking_execu = _engine_tracking(events, [strat], risk_config=rc)

    engine.run()

    # The daily-loss halt was cleared by next-day reset, so not halted at end.
    assert risk.is_halted is False
    # cancel_open_orders() was called at least once (when daily loss breach fired).
    assert tracking_execu.cancel_open_calls >= 1


def test_no_cancel_when_no_halt():
    """
    Sanity: if no halt ever occurs, cancel_open_orders() is never called during
    the run (it may be called once by disconnect(), but that's separate — the
    tracking engine overrides cancel_open_orders() so this test is clean).
    """
    events = _bars([100, 101, 102, 103, 104])
    strat = ScriptedStrategy("s", [SYM])  # no trades → no drawdown
    engine, portfolio, risk, tracking_execu = _engine_tracking(events, [strat])

    engine.run()

    assert not risk.is_halted
    # No halt → cancel_open_orders() never called during the run.
    assert tracking_execu.cancel_open_calls == 0
