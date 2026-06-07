"""
Tests for apex.strategy.library.turn_of_month.

The TOM strategy is POSITION-AWARE: it reads its actual holding from a
StrategyContext each bar and emits only the delta needed to reach the target
sleeve. Tests drive it through a lightweight harness that mirrors the engine's
context-refresh-before-dispatch pattern, exactly like test_multi_asset_trend.

Covered:
  - In-window (risk ETF long) vs off-window (defensive or cash) logic.
  - Transition at month boundary: risk SELL + defensive BUY when window closes,
    and the reverse when the window opens.
  - No double-entry while already holding the correct sleeve (idempotent).
  - Cold start mid-window: enters the risk sleeve immediately with no prior bars.
  - Cold start off-window with defensive symbol: enters defensive immediately.
  - Cash mode (no defensive): no BUY emitted off-window, SELL still emitted.
  - stop_loss is attached on risk BUY; absent on defensive BUY and all SELLs.
  - strength=1.0 on all signals (binary calendar).
  - Threshold params honoured: custom month_end_day / month_start_days.
  - Constructor validation: bad month_end_day, bad month_start_days,
    bad stop_loss_pct.
  - Determinism: identical inputs produce identical outputs.
  - Bars for symbols not in the strategy universe are silently ignored.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.turn_of_month import TurnOfMonthStrategy

# ---------------------------------------------------------------------------
# Shared symbols
# ---------------------------------------------------------------------------

SPY = Symbol("SPY", AssetClass.ETF)
IEF = Symbol("IEF", AssetClass.ETF)
OTHER = Symbol("XYZ", AssetClass.ETF)


# ---------------------------------------------------------------------------
# Helper: build a Bar at a specific calendar date
# ---------------------------------------------------------------------------


def _bar(sym: Symbol, year: int, month: int, day: int, price: float = 100.0) -> Bar:
    """Create a 1-Day bar for `sym` at the given calendar date (UTC)."""
    ts = datetime(year, month, day, 0, 0, 0, tzinfo=timezone.utc)
    p = Decimal(str(price))
    return Bar(
        symbol=sym,
        timestamp=ts,
        open=p,
        high=p,
        low=p,
        close=p,
        volume=Decimal("1_000_000"),
        timeframe="1Day",
    )


# ---------------------------------------------------------------------------
# Harness: mirrors engine's context-refresh-before-dispatch ordering
# ---------------------------------------------------------------------------


class _Harness:
    """
    Minimal engine stand-in. Binds a context to the strategy, refreshes it from
    a simulated portfolio before each bar, and immediately applies emitted
    signals so the next bar sees the correct holding.
    """

    def __init__(self, strat: TurnOfMonthStrategy) -> None:
        self.strat = strat
        self.ctx = StrategyContext()
        strat.bind_context(self.ctx)
        self.held: dict[str, Decimal] = {}  # ticker -> qty

    def _refresh(self) -> None:
        self.ctx.sync_state(
            positions={k: SimpleNamespace(quantity=q) for k, q in self.held.items() if q > 0}
        )

    def step(self, bar: Bar) -> list:
        """Feed one bar, return the list of SignalEvents it produced."""
        self._refresh()
        sigs = self.strat.on_bar(bar)
        for s in sigs:
            if s.side == OrderSide.BUY:
                self.held[s.symbol.ticker] = Decimal("1")
            else:
                self.held[s.symbol.ticker] = Decimal("0")
        return sigs

    def feed(self, bars: list[Bar]) -> list:
        out = []
        for b in bars:
            out.extend(self.step(b))
        return out


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_invalid_month_end_day_zero():
    with pytest.raises(ValueError, match="month_end_day"):
        TurnOfMonthStrategy("s", SPY, month_end_day=0)


def test_invalid_month_end_day_too_large():
    with pytest.raises(ValueError, match="month_end_day"):
        TurnOfMonthStrategy("s", SPY, month_end_day=32)


def test_invalid_month_start_days_negative():
    with pytest.raises(ValueError, match="month_start_days"):
        TurnOfMonthStrategy("s", SPY, month_start_days=-1)


def test_invalid_month_start_days_too_large():
    with pytest.raises(ValueError, match="month_start_days"):
        TurnOfMonthStrategy("s", SPY, month_start_days=11)


def test_invalid_stop_loss_zero():
    with pytest.raises(ValueError, match="stop_loss_pct"):
        TurnOfMonthStrategy("s", SPY, stop_loss_pct=Decimal("0"))


def test_invalid_stop_loss_one():
    with pytest.raises(ValueError, match="stop_loss_pct"):
        TurnOfMonthStrategy("s", SPY, stop_loss_pct=Decimal("1"))


# ---------------------------------------------------------------------------
# Calendar window logic: _in_tom_window
# ---------------------------------------------------------------------------


def test_in_window_at_month_end_day():
    """Day == month_end_day is inside the window."""
    strat = TurnOfMonthStrategy("s", SPY, month_end_day=24, month_start_days=3)
    assert strat._in_tom_window(_bar(SPY, 2024, 1, 24))


def test_in_window_after_month_end_day():
    """Day > month_end_day is inside the window."""
    strat = TurnOfMonthStrategy("s", SPY, month_end_day=24, month_start_days=3)
    assert strat._in_tom_window(_bar(SPY, 2024, 1, 30))


def test_in_window_at_month_start_days():
    """Day == month_start_days is inside the window."""
    strat = TurnOfMonthStrategy("s", SPY, month_end_day=24, month_start_days=3)
    assert strat._in_tom_window(_bar(SPY, 2024, 2, 3))


def test_in_window_below_month_start_days():
    """Day < month_start_days is inside the window."""
    strat = TurnOfMonthStrategy("s", SPY, month_end_day=24, month_start_days=3)
    assert strat._in_tom_window(_bar(SPY, 2024, 2, 1))


def test_off_window_mid_month():
    """A mid-month day is outside the window."""
    strat = TurnOfMonthStrategy("s", SPY, month_end_day=24, month_start_days=3)
    assert not strat._in_tom_window(_bar(SPY, 2024, 1, 10))


def test_custom_thresholds():
    """Custom thresholds are honoured."""
    strat = TurnOfMonthStrategy("s", SPY, month_end_day=28, month_start_days=1)
    assert not strat._in_tom_window(_bar(SPY, 2024, 1, 27))  # just outside
    assert strat._in_tom_window(_bar(SPY, 2024, 1, 28))  # at threshold
    assert not strat._in_tom_window(_bar(SPY, 2024, 2, 2))  # just outside
    assert strat._in_tom_window(_bar(SPY, 2024, 2, 1))  # at threshold


# ---------------------------------------------------------------------------
# Core position-aware logic: long/flat with defensive sleeve
# ---------------------------------------------------------------------------


def test_enters_risk_in_window():
    """During TOM window, emits BUY for the risk ETF when flat."""
    h = _Harness(TurnOfMonthStrategy("s", SPY, IEF))
    sigs = h.step(_bar(SPY, 2024, 1, 26))  # day 26 >= 24 → in window
    buys = [s for s in sigs if s.side == OrderSide.BUY]
    assert len(buys) == 1
    assert buys[0].symbol == SPY


def test_no_signal_when_already_long_risk_in_window():
    """No double-entry when already holding the risk ETF during the window."""
    h = _Harness(TurnOfMonthStrategy("s", SPY, IEF))
    # Pre-load position
    h.held["SPY"] = Decimal("1")
    sigs = h.step(_bar(SPY, 2024, 1, 26))
    assert sigs == []


def test_exits_risk_and_enters_defensive_off_window():
    """
    When the window closes and the risk ETF is held, strategy emits SELL on
    risk + BUY on defensive in the same bar.
    """
    h = _Harness(TurnOfMonthStrategy("s", SPY, IEF))
    # Simulate holding SPY
    h.held["SPY"] = Decimal("1")
    # Day 10 is off-window
    sigs = h.step(_bar(SPY, 2024, 1, 10))
    sides_by_sym = {s.symbol.ticker: s.side for s in sigs}
    assert sides_by_sym.get("SPY") == OrderSide.SELL
    assert sides_by_sym.get("IEF") == OrderSide.BUY


def test_exits_defensive_and_enters_risk_when_window_opens():
    """
    When the window opens and the defensive ETF is held, strategy emits SELL
    on defensive + BUY on risk.
    """
    h = _Harness(TurnOfMonthStrategy("s", SPY, IEF))
    # Simulate holding IEF off-window
    h.held["IEF"] = Decimal("1")
    # Day 25 is in-window
    sigs = h.step(_bar(SPY, 2024, 1, 25))
    sides_by_sym = {s.symbol.ticker: s.side for s in sigs}
    assert sides_by_sym.get("IEF") == OrderSide.SELL
    assert sides_by_sym.get("SPY") == OrderSide.BUY


def test_no_signal_when_already_long_defensive_off_window():
    """No redundant signal when already holding the defensive ETF off-window."""
    h = _Harness(TurnOfMonthStrategy("s", SPY, IEF))
    h.held["IEF"] = Decimal("1")
    sigs = h.step(_bar(IEF, 2024, 1, 10))  # off-window, IEF bar
    assert sigs == []


# ---------------------------------------------------------------------------
# Month-boundary sequence
# ---------------------------------------------------------------------------


def test_full_month_boundary_sequence():
    """
    Feed a sequence spanning a month boundary and assert:
      - risk held at end of January (day >= 24)
      - risk still held in early February (day <= 3)
      - defensive entered mid-February (day 10)
      - only one BUY and one SELL for each sleeve at the correct transition
    """
    h = _Harness(TurnOfMonthStrategy("s", SPY, IEF))

    # January 26 — in-window → buy SPY
    bars = [
        _bar(SPY, 2024, 1, 26),  # in-window: buy SPY
        _bar(SPY, 2024, 2, 1),  # still in-window (day 1 <= 3): no change
        _bar(SPY, 2024, 2, 3),  # still in-window (day 3 <= 3): no change
        _bar(SPY, 2024, 2, 5),  # off-window: sell SPY, buy IEF
        _bar(SPY, 2024, 2, 10),  # off-window: no change (IEF held via IEF bar below)
    ]
    sigs = h.feed(bars)

    spy_buys = [s for s in sigs if s.symbol == SPY and s.side == OrderSide.BUY]
    spy_sells = [s for s in sigs if s.symbol == SPY and s.side == OrderSide.SELL]
    ief_buys = [s for s in sigs if s.symbol == IEF and s.side == OrderSide.BUY]

    assert len(spy_buys) == 1, "SPY entered exactly once"
    assert len(spy_sells) == 1, "SPY exited exactly once"
    assert len(ief_buys) == 1, "IEF entered exactly once"
    # Verify ordering: SPY entry precedes SPY exit
    buy_idx = sigs.index(spy_buys[0])
    sell_idx = sigs.index(spy_sells[0])
    assert buy_idx < sell_idx


# ---------------------------------------------------------------------------
# Cold-start tests
# ---------------------------------------------------------------------------


def test_cold_start_mid_window_enters_risk():
    """
    Cold start (no prior bars, context flat) mid-window → immediately BUY SPY.
    This is the analogue of the multi_asset_trend "established trend" fix.
    """
    h = _Harness(TurnOfMonthStrategy("s", SPY, IEF))
    sigs = h.step(_bar(SPY, 2024, 1, 28))  # in-window
    buys = [s for s in sigs if s.side == OrderSide.BUY and s.symbol == SPY]
    assert len(buys) == 1, "must enter risk on cold start mid-window"


def test_cold_start_off_window_enters_defensive():
    """Cold start off-window with a defensive symbol → immediately BUY IEF."""
    h = _Harness(TurnOfMonthStrategy("s", SPY, IEF))
    sigs = h.step(_bar(SPY, 2024, 1, 15))  # off-window
    buys = [s for s in sigs if s.side == OrderSide.BUY and s.symbol == IEF]
    assert len(buys) == 1, "must enter defensive on cold start off-window"


def test_cold_start_off_window_no_defensive_emits_nothing():
    """Cold start off-window with no defensive symbol → no BUY (stay cash)."""
    h = _Harness(TurnOfMonthStrategy("s", SPY))  # no defensive
    sigs = h.step(_bar(SPY, 2024, 1, 15))  # off-window
    buys = [s for s in sigs if s.side == OrderSide.BUY]
    assert buys == [], "no defensive, should stay cash"


# ---------------------------------------------------------------------------
# Stop-loss and strength
# ---------------------------------------------------------------------------


def test_risk_buy_has_stop_loss():
    """Risk-sleeve BUY carries a suggested_stop_loss below entry price."""
    h = _Harness(TurnOfMonthStrategy("s", SPY, IEF, stop_loss_pct=Decimal("0.08")))
    sigs = h.step(_bar(SPY, 2024, 1, 26, price=100.0))
    buy = next(s for s in sigs if s.symbol == SPY and s.side == OrderSide.BUY)
    assert buy.suggested_stop_loss is not None
    assert buy.suggested_stop_loss < Decimal("100")
    # 8% below 100 = 92
    assert buy.suggested_stop_loss == Decimal("92.00")


def test_defensive_buy_has_no_stop_loss():
    """Defensive-sleeve BUY does NOT carry a stop (low-vol holding)."""
    h = _Harness(TurnOfMonthStrategy("s", SPY, IEF))
    sigs = h.step(_bar(IEF, 2024, 1, 15))  # off-window → buy IEF
    buy = next(s for s in sigs if s.symbol == IEF and s.side == OrderSide.BUY)
    assert buy.suggested_stop_loss is None


def test_sell_has_no_stop_loss():
    """SELL signals do not carry a stop (we're exiting, not entering)."""
    h = _Harness(TurnOfMonthStrategy("s", SPY, IEF))
    h.held["SPY"] = Decimal("1")
    sigs = h.step(_bar(SPY, 2024, 1, 10))  # off-window → sell SPY
    sell = next(s for s in sigs if s.symbol == SPY and s.side == OrderSide.SELL)
    assert sell.suggested_stop_loss is None


def test_all_signals_strength_one():
    """All emitted signals carry strength=1.0 (binary calendar signal)."""
    h = _Harness(TurnOfMonthStrategy("s", SPY, IEF))
    # In-window buy
    sigs = h.step(_bar(SPY, 2024, 1, 26))
    h.held["SPY"] = Decimal("1")
    # Off-window transition
    sigs += h.step(_bar(SPY, 2024, 1, 10))
    for s in sigs:
        assert s.strength == Decimal("1.0"), f"Expected strength 1.0, got {s.strength}"


# ---------------------------------------------------------------------------
# Cash mode (no defensive symbol)
# ---------------------------------------------------------------------------


def test_cash_mode_exits_risk_off_window():
    """With no defensive symbol, exits risk off-window and emits only a SELL."""
    h = _Harness(TurnOfMonthStrategy("s", SPY))  # no IEF
    h.held["SPY"] = Decimal("1")
    sigs = h.step(_bar(SPY, 2024, 1, 10))  # off-window
    assert any(s.side == OrderSide.SELL and s.symbol == SPY for s in sigs)
    assert not any(s.side == OrderSide.BUY for s in sigs), "no BUY in cash mode off-window"


def test_cash_mode_enters_risk_in_window():
    """With no defensive symbol, enters risk ETF during the window."""
    h = _Harness(TurnOfMonthStrategy("s", SPY))  # no IEF
    sigs = h.step(_bar(SPY, 2024, 1, 26))  # in-window
    assert any(s.side == OrderSide.BUY and s.symbol == SPY for s in sigs)


# ---------------------------------------------------------------------------
# Unknown symbol is ignored
# ---------------------------------------------------------------------------


def test_ignores_unknown_symbol():
    """Bars for symbols not in the strategy universe return no signals."""
    h = _Harness(TurnOfMonthStrategy("s", SPY, IEF))
    sigs = h.step(_bar(OTHER, 2024, 1, 26))
    assert sigs == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_deterministic():
    """Same sequence of bars produces identical signals on two independent runs."""
    bars = [
        _bar(SPY, 2024, 1, 10),
        _bar(SPY, 2024, 1, 26),
        _bar(SPY, 2024, 2, 1),
        _bar(SPY, 2024, 2, 10),
    ]
    h1 = _Harness(TurnOfMonthStrategy("det", SPY, IEF))
    h2 = _Harness(TurnOfMonthStrategy("det", SPY, IEF))
    s1 = h1.feed(bars)
    s2 = h2.feed(bars)
    assert [(s.symbol, s.side, s.strength) for s in s1] == [
        (s.symbol, s.side, s.strength) for s in s2
    ]
