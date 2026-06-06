"""
Regression tests for the correctness/safety/determinism bugs fixed after the
multi-agent audit (DECISIONS.md Session 25). Each test pins behaviour that was
previously wrong so it cannot silently regress.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict

from apex.core.events import FillEvent, SignalEvent
from apex.core.models import AssetClass, OrderSide, Position, Symbol
from apex.risk.portfolio import Portfolio
from apex.risk.risk_manager import RiskConfig, RiskManager
from apex.strategy.library.cross_asset_value import CrossAssetValueStrategy
from apex.strategy.library.value_momentum import ValueMomentumStrategy

A = Symbol("AAA", AssetClass.ETF)
B = Symbol("BBB", AssetClass.ETF)
C = Symbol("CCC", AssetClass.ETF)
SYM = Symbol("AAPL", AssetClass.EQUITY)
_DT = datetime(2024, 6, 1, 14, 30, tzinfo=timezone.utc)


# --- Portfolio: BUY covering a short must pop the position and book PnL ---------

def test_buy_covering_short_pops_position_and_books_pnl():
    p = Portfolio(Decimal("10000"))
    # Open a short from flat: SELL 10 @ 100.
    p.on_fill(FillEvent(symbol=SYM, side=OrderSide.SELL, quantity=Decimal("10"),
                        fill_price=Decimal("100"), commission=Decimal("0"), timestamp=_DT))
    assert p.open_positions["AAPL"].quantity == Decimal("-10")
    # Cover fully: BUY 10 @ 90 → short profit (100-90)*10 = 100.
    p.on_fill(FillEvent(symbol=SYM, side=OrderSide.BUY, quantity=Decimal("10"),
                        fill_price=Decimal("90"), commission=Decimal("0"), timestamp=_DT))
    assert "AAPL" not in p.open_positions          # no zero-qty zombie left behind
    assert p.realized_pnl == Decimal("100")        # short PnL is now booked


# --- Strategy ranking: ties must break on ticker, not construction order --------

def test_cross_asset_value_tie_break_is_deterministic():
    s1 = CrossAssetValueStrategy("s", [A, B, C], value_period=4, skip_recent=0,
                                 top_k=1, vol_window=2)
    s2 = CrossAssetValueStrategy("s", [C, B, A], value_period=4, skip_recent=0,
                                 top_k=1, vol_window=2)
    for s in (s1, s2):
        s._value = {"AAA": 0.5, "BBB": 0.5, "CCC": 0.5}   # all tied
    # Tie broken by ticker ascending → "AAA" regardless of symbol order.
    assert s1._in_top_k("AAA") and not s1._in_top_k("BBB")
    assert s2._in_top_k("AAA") and not s2._in_top_k("BBB")


def test_value_momentum_tie_break_is_deterministic():
    vm1 = ValueMomentumStrategy("s", [A, B, C], value_period=4, skip_recent=0,
                                mom_period=2, top_k=1, vol_window=2)
    vm2 = ValueMomentumStrategy("s", [C, B, A], value_period=4, skip_recent=0,
                                mom_period=2, top_k=1, vol_window=2)
    for vm in (vm1, vm2):
        vm._value = {"AAA": 0.1, "BBB": 0.1, "CCC": 0.1}
        vm._mom = {"AAA": 0.2, "BBB": 0.2, "CCC": 0.2}
    assert vm1._wanted_set() == {"AAA"}
    assert vm2._wanted_set() == {"AAA"}


# --- RiskManager: events inherit bar-time; halt reset keys off structured cause --

@dataclass
class _FakePortfolio:
    equity: Decimal = Decimal("100000")
    peak_equity: Decimal = Decimal("100000")
    day_start_equity: Decimal = Decimal("100000")
    open_positions: Dict[str, Position] = field(default_factory=dict)
    exposure: Decimal = Decimal("0")
    last_price: Dict[str, Decimal] = field(default_factory=dict)


def _config(**ov) -> RiskConfig:
    base = dict(
        max_position_size_pct=Decimal("0.05"), max_total_exposure_pct=Decimal("0.5"),
        max_leverage=Decimal("1.0"), max_drawdown_pct=Decimal("0.10"),
        max_daily_loss_pct=Decimal("0.02"), require_stop_loss=True,
        min_stop_distance_pct=Decimal("0.005"), symbol_whitelist=None,
    )
    base.update(ov)
    return RiskConfig(**base)


def test_order_inherits_signal_timestamp():
    rm = RiskManager(_config())
    port = _FakePortfolio()
    port.last_price["AAA"] = Decimal("100")
    ts = datetime(2021, 7, 4, tzinfo=timezone.utc)
    sig = SignalEvent(symbol=A, side=OrderSide.BUY, strength=Decimal("1.0"),
                      strategy_id="t", suggested_stop_loss=Decimal("95"), timestamp=ts)
    order = rm.evaluate(sig, port)
    assert order is not None
    assert order.timestamp == ts        # bar-time, not utc_now()


def test_drawdown_halt_is_sticky_across_daily_reset():
    rm = RiskManager(_config())
    # 15% drawdown (also a daily loss) — drawdown is checked first and is the stored cause.
    port = _FakePortfolio(equity=Decimal("85000"))
    port.last_price["AAA"] = Decimal("100")
    sig = SignalEvent(symbol=A, side=OrderSide.BUY, strength=Decimal("1.0"),
                      strategy_id="t", suggested_stop_loss=Decimal("95"), timestamp=_DT)
    assert rm.evaluate(sig, port) is None
    assert rm.is_halted
    rm.reset_daily()
    assert rm.is_halted                 # a drawdown halt must NOT clear on a daily reset


def test_daily_loss_halt_clears_on_daily_reset():
    rm = RiskManager(_config())
    # 2.5% daily loss but only 2.5% drawdown (< 10%) → pure daily-loss halt.
    port = _FakePortfolio(equity=Decimal("97500"))
    port.last_price["AAA"] = Decimal("100")
    sig = SignalEvent(symbol=A, side=OrderSide.BUY, strength=Decimal("1.0"),
                      strategy_id="t", suggested_stop_loss=Decimal("95"), timestamp=_DT)
    assert rm.evaluate(sig, port) is None
    assert rm.is_halted
    rm.reset_daily()
    assert not rm.is_halted             # daily-loss halt clears for the new day
