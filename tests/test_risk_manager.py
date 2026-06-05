"""
Tests for apex.risk.risk_manager.RiskManager.

All money values use Decimal. The portfolio snapshot is a plain dataclass stub
that exposes exactly the attributes RiskManager reads:
  .equity, .peak_equity, .day_start_equity, .open_positions, .exposure,
  .last_price.

Test coverage:
  1.  Compliant BUY → correct sizing (quantity, side, stop_loss).
  2.  Missing stop-loss (require_stop_loss=True) → rejected (None).
  3.  Stop too close (< min_stop_distance_pct) → rejected.
  4.  Stop on the wrong side (stop >= price for BUY) → rejected.
  5.  Oversized: exposure already at cap → sizing yields 0 → rejected.
  6.  Drawdown breach → rejected + is_halted=True; subsequent valid signal
      also rejected (sticky halt).
  7.  Daily-loss breach → rejected/halted; reset_daily() clears it.
  8.  Whitelist enforcement: symbol not in whitelist → rejected.
  9.  Leverage cap respected.
  10. Fail-closed: malformed/None field raises internally → evaluate returns None.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Optional

import pytest

from apex.core.events import SignalEvent
from apex.core.models import AssetClass, OrderSide, Position, Symbol
from apex.risk.risk_manager import RiskConfig, RiskManager


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

AAPL = Symbol("AAPL", AssetClass.EQUITY)
TSLA = Symbol("TSLA", AssetClass.EQUITY)
BTCUSD = Symbol("BTC/USD", AssetClass.CRYPTO, fractionable=True)


@dataclass
class FakePortfolio:
    """
    A minimal, mutable stub that RiskManager can read.
    RiskManager accesses: .equity, .peak_equity, .day_start_equity,
    .open_positions, .exposure, .last_price.
    """
    equity: Decimal = Decimal("100000")
    peak_equity: Decimal = Decimal("100000")
    day_start_equity: Decimal = Decimal("100000")
    open_positions: Dict[str, Position] = field(default_factory=dict)
    exposure: Decimal = Decimal("0")
    last_price: Dict[str, Decimal] = field(default_factory=dict)


def _default_config(**overrides) -> RiskConfig:
    """Build a RiskConfig with sane defaults that can be overridden."""
    defaults = dict(
        max_position_size_pct=Decimal("0.05"),   # 5 %
        max_total_exposure_pct=Decimal("0.50"),  # 50 %
        max_leverage=Decimal("1.0"),
        max_drawdown_pct=Decimal("0.10"),        # 10 %
        max_daily_loss_pct=Decimal("0.02"),      # 2 %
        max_open_positions=10,
        require_stop_loss=True,
        min_stop_distance_pct=Decimal("0.005"),  # 0.5 %
        symbol_whitelist=None,
    )
    defaults.update(overrides)
    return RiskConfig(**defaults)


def _buy_signal(
    symbol: Symbol = AAPL,
    strength: Decimal = Decimal("1.0"),
    stop: Optional[Decimal] = None,
) -> SignalEvent:
    """Helper: build a BUY SignalEvent with sensible defaults."""
    return SignalEvent(
        symbol=symbol,
        side=OrderSide.BUY,
        strength=strength,
        strategy_id="test_strategy",
        suggested_stop_loss=stop,
    )


def _sell_signal(
    symbol: Symbol = AAPL,
    strength: Decimal = Decimal("1.0"),
    stop: Optional[Decimal] = None,
) -> SignalEvent:
    """Helper: build a SELL SignalEvent."""
    return SignalEvent(
        symbol=symbol,
        side=OrderSide.SELL,
        strength=strength,
        strategy_id="test_strategy",
        suggested_stop_loss=stop,
    )


def _portfolio_with_price(
    ticker: str,
    price: Decimal,
    equity: Decimal = Decimal("100000"),
    **kwargs,
) -> FakePortfolio:
    """Portfolio stub pre-loaded with a last_price entry."""
    port = FakePortfolio(equity=equity, peak_equity=equity, day_start_equity=equity)
    port.last_price[ticker] = price
    for k, v in kwargs.items():
        setattr(port, k, v)
    return port


# ---------------------------------------------------------------------------
# 1. Compliant BUY → correct sizing
# ---------------------------------------------------------------------------

class TestCompliantBuy:
    """
    Scenario:
      equity = $100 000, price = $200, strength = 1.0, max_position_size = 5 %
      → target_dollars = 100 000 * 0.05 * 1.0 = $5 000
      → quantity = 5000 / 200 = 25 shares (whole units)
    """

    def test_returns_order_event_not_none(self):
        rm = RiskManager(_default_config())
        port = _portfolio_with_price("AAPL", Decimal("200"))
        sig = _buy_signal(stop=Decimal("190"))   # 5 % below → valid
        order = rm.evaluate(sig, port)
        assert order is not None

    def test_correct_quantity(self):
        rm = RiskManager(_default_config())
        port = _portfolio_with_price("AAPL", Decimal("200"))
        sig = _buy_signal(stop=Decimal("190"))
        order = rm.evaluate(sig, port)
        assert order.quantity == Decimal("25")

    def test_correct_side(self):
        rm = RiskManager(_default_config())
        port = _portfolio_with_price("AAPL", Decimal("200"))
        sig = _buy_signal(stop=Decimal("190"))
        order = rm.evaluate(sig, port)
        assert order.side == OrderSide.BUY

    def test_stop_loss_attached(self):
        rm = RiskManager(_default_config())
        port = _portfolio_with_price("AAPL", Decimal("200"))
        stop_price = Decimal("190")
        sig = _buy_signal(stop=stop_price)
        order = rm.evaluate(sig, port)
        assert order.stop_loss == stop_price

    def test_signal_links_back_via_signal_id(self):
        rm = RiskManager(_default_config())
        port = _portfolio_with_price("AAPL", Decimal("200"))
        sig = _buy_signal(stop=Decimal("190"))
        order = rm.evaluate(sig, port)
        assert order.signal_id == sig.event_id

    def test_strength_scales_quantity(self):
        """Half conviction (strength=0.5) should halve the position."""
        rm = RiskManager(_default_config())
        port = _portfolio_with_price("AAPL", Decimal("200"))
        sig = _buy_signal(stop=Decimal("190"), strength=Decimal("0.5"))
        order = rm.evaluate(sig, port)
        # 100_000 * 0.05 * 0.5 / 200 = 12 (floor)
        assert order.quantity == Decimal("12")


# ---------------------------------------------------------------------------
# 2. Missing stop-loss when require_stop_loss=True
# ---------------------------------------------------------------------------

class TestMissingStopLoss:

    def test_no_stop_is_rejected(self):
        rm = RiskManager(_default_config(require_stop_loss=True))
        port = _portfolio_with_price("AAPL", Decimal("200"))
        sig = _buy_signal(stop=None)
        assert rm.evaluate(sig, port) is None

    def test_stop_loss_not_required_passes(self):
        """When require_stop_loss=False the signal should pass even without a stop."""
        rm = RiskManager(_default_config(require_stop_loss=False))
        port = _portfolio_with_price("AAPL", Decimal("200"))
        sig = _buy_signal(stop=None)
        order = rm.evaluate(sig, port)
        assert order is not None
        assert order.stop_loss is None


# ---------------------------------------------------------------------------
# 3. Stop too close (< min_stop_distance_pct)
# ---------------------------------------------------------------------------

class TestStopTooClose:

    def test_stop_within_min_distance_rejected(self):
        """
        Price = $200, min_stop_distance = 0.5 %.
        Stop at $199.50 is exactly 0.25 % away — too close.
        """
        rm = RiskManager(_default_config(min_stop_distance_pct=Decimal("0.005")))
        port = _portfolio_with_price("AAPL", Decimal("200"))
        close_stop = Decimal("199.50")  # 0.25 % below price
        sig = _buy_signal(stop=close_stop)
        assert rm.evaluate(sig, port) is None

    def test_stop_at_exactly_min_distance_accepted(self):
        """
        Distance must be >= min_stop_distance_pct, so an equal stop passes
        (the check is strict <, so exactly at the boundary should pass).
        """
        rm = RiskManager(_default_config(min_stop_distance_pct=Decimal("0.005")))
        port = _portfolio_with_price("AAPL", Decimal("200"))
        # 0.5 % below $200 = $199.00
        stop_at_min = Decimal("199.00")
        sig = _buy_signal(stop=stop_at_min)
        order = rm.evaluate(sig, port)
        assert order is not None


# ---------------------------------------------------------------------------
# 4. Stop on the wrong side (stop >= price for a BUY)
# ---------------------------------------------------------------------------

class TestStopWrongSide:

    def test_buy_stop_above_price_rejected(self):
        """For a BUY, a stop at or above entry price is nonsensical → reject."""
        rm = RiskManager(_default_config())
        port = _portfolio_with_price("AAPL", Decimal("200"))
        sig = _buy_signal(stop=Decimal("210"))   # stop ABOVE price
        assert rm.evaluate(sig, port) is None

    def test_buy_stop_equal_to_price_rejected(self):
        rm = RiskManager(_default_config())
        port = _portfolio_with_price("AAPL", Decimal("200"))
        sig = _buy_signal(stop=Decimal("200"))   # stop AT price
        assert rm.evaluate(sig, port) is None

    def test_sell_stop_below_price_rejected(self):
        """For a SELL, a stop at or below entry price is wrong → reject."""
        rm = RiskManager(_default_config())
        port = _portfolio_with_price("AAPL", Decimal("200"))
        # For a short, stop must be ABOVE entry; here stop is 190 < 200 → wrong side.
        sig = _sell_signal(stop=Decimal("190"))
        assert rm.evaluate(sig, port) is None

    def test_sell_stop_above_price_accepted(self):
        """Correct short stop: above entry."""
        rm = RiskManager(_default_config())
        port = _portfolio_with_price("AAPL", Decimal("200"))
        # 5 % above $200 = $210
        sig = _sell_signal(stop=Decimal("210"))
        order = rm.evaluate(sig, port)
        assert order is not None


# ---------------------------------------------------------------------------
# 5. Exposure already at cap → sizing yields 0 → rejected
# ---------------------------------------------------------------------------

class TestExposureCap:

    def test_fully_exposed_rejects_new_signal(self):
        """
        exposure == max_total_exposure_pct * equity → remaining = 0 → qty = 0.
        max_total_exposure = 50 %, equity = $100 000 → cap = $50 000.
        Set current exposure to $50 000.
        """
        rm = RiskManager(_default_config())
        port = _portfolio_with_price("AAPL", Decimal("200"))
        port.exposure = Decimal("50000")   # fully at the 50 % cap
        sig = _buy_signal(stop=Decimal("190"))
        assert rm.evaluate(sig, port) is None

    def test_partial_exposure_reduces_quantity(self):
        """
        Remaining room is smaller than the per-position allocation → smaller qty.
        equity = $100 000, cap = $50 000, current exposure = $49 000 → remaining $1 000.
        At $200/share → 5 shares, but available room = $1 000 / $200 = 5 shares.
        Per-position target = $5 000 → capped at $1 000 → 5 shares.
        """
        rm = RiskManager(_default_config())
        port = _portfolio_with_price("AAPL", Decimal("200"))
        port.exposure = Decimal("49000")
        sig = _buy_signal(stop=Decimal("190"))
        order = rm.evaluate(sig, port)
        assert order is not None
        assert order.quantity == Decimal("5")


# ---------------------------------------------------------------------------
# 6. Drawdown breach → halted; subsequent signal also rejected (sticky)
# ---------------------------------------------------------------------------

class TestDrawdownHalt:

    def _drawdown_portfolio(self, equity: Decimal, peak: Decimal) -> FakePortfolio:
        port = FakePortfolio(
            equity=equity,
            peak_equity=peak,
            day_start_equity=equity,
        )
        port.last_price["AAPL"] = Decimal("200")
        return port

    def test_drawdown_breach_rejects_signal(self):
        """
        peak = $100 000, equity = $89 000 → drawdown = 11 % > 10 % cap → halt.
        """
        rm = RiskManager(_default_config(max_drawdown_pct=Decimal("0.10")))
        port = self._drawdown_portfolio(Decimal("89000"), Decimal("100000"))
        sig = _buy_signal(stop=Decimal("190"))
        result = rm.evaluate(sig, port)
        assert result is None

    def test_drawdown_breach_sets_is_halted(self):
        rm = RiskManager(_default_config(max_drawdown_pct=Decimal("0.10")))
        port = self._drawdown_portfolio(Decimal("89000"), Decimal("100000"))
        sig = _buy_signal(stop=Decimal("190"))
        rm.evaluate(sig, port)
        assert rm.is_halted is True

    def test_halt_is_sticky(self):
        """Once halted, a subsequent fully valid signal must also be rejected."""
        rm = RiskManager(_default_config(max_drawdown_pct=Decimal("0.10")))
        # First call: breach → halt
        breached_port = self._drawdown_portfolio(Decimal("89000"), Decimal("100000"))
        rm.evaluate(_buy_signal(stop=Decimal("190")), breached_port)
        assert rm.is_halted

        # Second call: healthy portfolio, but system is still halted.
        healthy_port = _portfolio_with_price("AAPL", Decimal("200"))
        second_signal = _buy_signal(stop=Decimal("190"))
        result = rm.evaluate(second_signal, healthy_port)
        assert result is None

    def test_no_halt_when_drawdown_within_limit(self):
        """5 % drawdown with 10 % limit → should pass."""
        rm = RiskManager(_default_config(max_drawdown_pct=Decimal("0.10")))
        port = self._drawdown_portfolio(Decimal("95000"), Decimal("100000"))
        sig = _buy_signal(stop=Decimal("190"))
        order = rm.evaluate(sig, port)
        assert order is not None
        assert rm.is_halted is False


# ---------------------------------------------------------------------------
# 7. Daily-loss breach → halted; reset_daily() clears it
# ---------------------------------------------------------------------------

class TestDailyLossHalt:

    def _daily_loss_portfolio(
        self, equity: Decimal, day_start: Decimal
    ) -> FakePortfolio:
        port = FakePortfolio(
            equity=equity,
            peak_equity=day_start,
            day_start_equity=day_start,
        )
        port.last_price["AAPL"] = Decimal("200")
        return port

    def test_daily_loss_breach_rejects_and_halts(self):
        """
        day_start = $100 000, equity = $97 500 → daily loss = 2.5 % > 2 % cap.
        """
        rm = RiskManager(_default_config(max_daily_loss_pct=Decimal("0.02")))
        port = self._daily_loss_portfolio(Decimal("97500"), Decimal("100000"))
        sig = _buy_signal(stop=Decimal("190"))
        result = rm.evaluate(sig, port)
        assert result is None
        assert rm.is_halted is True

    def test_reset_daily_clears_daily_loss_halt(self):
        """After reset_daily(), the same valid signal should be accepted."""
        rm = RiskManager(_default_config(max_daily_loss_pct=Decimal("0.02")))
        # Trigger the daily-loss halt.
        port_breached = self._daily_loss_portfolio(Decimal("97500"), Decimal("100000"))
        rm.evaluate(_buy_signal(stop=Decimal("190")), port_breached)
        assert rm.is_halted

        # Reset the daily counter.
        rm.reset_daily()
        assert rm.is_halted is False

        # Now a valid signal on a healthy portfolio should be accepted.
        healthy_port = _portfolio_with_price("AAPL", Decimal("200"))
        order = rm.evaluate(_buy_signal(stop=Decimal("190")), healthy_port)
        assert order is not None

    def test_reset_daily_does_not_clear_drawdown_halt(self):
        """
        reset_daily() only clears daily-loss halts. A max-drawdown halt must
        persist across resets — it is a different, more severe condition.
        """
        rm = RiskManager(
            _default_config(max_drawdown_pct=Decimal("0.10"), max_daily_loss_pct=Decimal("0.02"))
        )
        # Trigger the drawdown halt (not a daily-loss halt).
        port = FakePortfolio(
            equity=Decimal("89000"),
            peak_equity=Decimal("100000"),
            day_start_equity=Decimal("97000"),   # daily loss < 2 % alone
        )
        port.last_price["AAPL"] = Decimal("200")
        rm.evaluate(_buy_signal(stop=Decimal("190")), port)
        assert rm.is_halted

        rm.reset_daily()
        # Drawdown halt is NOT cleared by reset_daily().
        assert rm.is_halted is True


# ---------------------------------------------------------------------------
# 8. Whitelist enforcement
# ---------------------------------------------------------------------------

class TestWhitelist:

    def test_symbol_not_in_whitelist_rejected(self):
        rm = RiskManager(_default_config(symbol_whitelist=frozenset({"SPY", "QQQ"})))
        port = _portfolio_with_price("AAPL", Decimal("200"))
        sig = _buy_signal(stop=Decimal("190"))
        assert rm.evaluate(sig, port) is None

    def test_symbol_in_whitelist_accepted(self):
        rm = RiskManager(_default_config(symbol_whitelist=frozenset({"AAPL", "SPY"})))
        port = _portfolio_with_price("AAPL", Decimal("200"))
        sig = _buy_signal(stop=Decimal("190"))
        order = rm.evaluate(sig, port)
        assert order is not None

    def test_none_whitelist_allows_any_symbol(self):
        """symbol_whitelist=None means all symbols are permitted."""
        rm = RiskManager(_default_config(symbol_whitelist=None))
        port = _portfolio_with_price("AAPL", Decimal("200"))
        sig = _buy_signal(stop=Decimal("190"))
        order = rm.evaluate(sig, port)
        assert order is not None


# ---------------------------------------------------------------------------
# 9. Leverage cap respected
# ---------------------------------------------------------------------------

class TestLeverageCap:

    def test_leverage_exceeded_rejected(self):
        """
        max_leverage = 1.0.
        equity = $100 000, current exposure = $90 000 (90 %).
        New order: 25 shares @ $200 = $5 000 → total notional = $95 000.
        leverage = 95 000 / 100 000 = 0.95 — but if we lower equity drastically
        so that even a single position exceeds the cap...

        Easier path: set equity very low so that sizing * price / equity > 1.0.
        equity = $1 000, price = $200, max_position = 5 % → target = $50 → 0 shares
        before leverage even fires. Instead, use a high-leverage scenario directly:
        max_leverage = 0.01 (1 % of equity in notional only).
        equity = $100 000, price = $200 → 25 shares → notional $5 000 → leverage = 5 % > 1 %.
        """
        rm = RiskManager(_default_config(max_leverage=Decimal("0.01")))
        port = _portfolio_with_price("AAPL", Decimal("200"))
        sig = _buy_signal(stop=Decimal("190"))
        assert rm.evaluate(sig, port) is None

    def test_leverage_within_cap_accepted(self):
        """
        max_leverage = 1.0.
        equity = $100 000, price = $200, quantity = 25 → notional = $5 000.
        leverage = 5 000 / 100 000 = 0.05 ≤ 1.0 → accepted.
        """
        rm = RiskManager(_default_config(max_leverage=Decimal("1.0")))
        port = _portfolio_with_price("AAPL", Decimal("200"))
        sig = _buy_signal(stop=Decimal("190"))
        order = rm.evaluate(sig, port)
        assert order is not None


# ---------------------------------------------------------------------------
# 10. Fail-closed: malformed/None fields that cause an internal exception
# ---------------------------------------------------------------------------

class TestFailClosed:

    def test_none_last_price_dict_returns_none(self):
        """
        If portfolio.last_price is None (malformed), the internal dict.get() call
        will raise AttributeError. RiskManager must catch it and return None.
        """
        rm = RiskManager(_default_config())
        port = FakePortfolio()
        port.last_price = None   # type: ignore[assignment]  — intentionally bad
        sig = _buy_signal(stop=Decimal("190"))
        result = rm.evaluate(sig, port)
        assert result is None

    def test_none_equity_returns_none(self):
        """
        If portfolio.equity is None the Decimal conversion will blow up.
        Should be caught and return None.
        """
        rm = RiskManager(_default_config())
        port = FakePortfolio()
        port.equity = None   # type: ignore[assignment]
        sig = _buy_signal(stop=Decimal("190"))
        result = rm.evaluate(sig, port)
        assert result is None

    def test_none_open_positions_returns_none(self):
        """
        If portfolio.open_positions is None the dict.get() call raises.
        Must fail closed.
        """
        rm = RiskManager(_default_config())
        port = FakePortfolio()
        port.open_positions = None   # type: ignore[assignment]
        port.last_price["AAPL"] = Decimal("200")
        sig = _buy_signal(stop=Decimal("190"))
        result = rm.evaluate(sig, port)
        assert result is None

    def test_string_exposure_that_cannot_convert_returns_none(self):
        """
        exposure = 'bad_value' → Decimal('bad_value') raises InvalidOperation.
        """
        rm = RiskManager(_default_config())
        port = FakePortfolio()
        port.last_price["AAPL"] = Decimal("200")
        port.exposure = "bad_value"   # type: ignore[assignment]
        sig = _buy_signal(stop=Decimal("190"))
        result = rm.evaluate(sig, port)
        assert result is None

    def test_is_not_halted_after_fail_closed_rejection(self):
        """
        A fail-closed rejection is NOT a drawdown/daily-loss breach. The risk
        manager must NOT set is_halted on a generic exception path.
        """
        rm = RiskManager(_default_config())
        port = FakePortfolio()
        port.last_price = None   # type: ignore[assignment]
        sig = _buy_signal(stop=Decimal("190"))
        rm.evaluate(sig, port)
        assert rm.is_halted is False


# ---------------------------------------------------------------------------
# Additional edge-case / integration tests
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_fractionable_symbol_returns_decimal_quantity(self):
        """Crypto symbols are fractionable; quantity should be a Decimal, not int."""
        rm = RiskManager(_default_config(require_stop_loss=False))
        port = FakePortfolio()
        port.last_price["BTC/USD"] = Decimal("60000")
        sig = SignalEvent(
            symbol=BTCUSD,
            side=OrderSide.BUY,
            strength=Decimal("1.0"),
            strategy_id="crypto_test",
            suggested_stop_loss=None,
        )
        order = rm.evaluate(sig, port)
        assert order is not None
        # 100 000 * 0.05 / 60 000 ≈ 0.0833 BTC (quantized to 4 dp)
        assert order.quantity > Decimal("0")
        # Verify it's fractional (not a whole integer multiple)
        assert order.quantity == order.quantity.quantize(Decimal("0.0001"))

    def test_max_open_positions_blocks_new_symbol(self):
        """When max_open_positions is reached, a new ticker must be rejected."""
        rm = RiskManager(_default_config(max_open_positions=2))
        # Build a portfolio already holding 2 positions.
        pos_a = Position(
            symbol=Symbol("SPY", AssetClass.ETF),
            quantity=Decimal("10"),
            avg_entry_price=Decimal("400"),
            current_price=Decimal("400"),
        )
        pos_b = Position(
            symbol=Symbol("QQQ", AssetClass.ETF),
            quantity=Decimal("5"),
            avg_entry_price=Decimal("300"),
            current_price=Decimal("300"),
        )
        port = _portfolio_with_price("AAPL", Decimal("200"))
        port.open_positions = {"SPY": pos_a, "QQQ": pos_b}
        sig = _buy_signal(stop=Decimal("190"))
        assert rm.evaluate(sig, port) is None

    def test_adding_to_existing_position_ignores_position_count_limit(self):
        """
        Sizing up an existing holding should NOT be blocked by max_open_positions.
        The existing position in AAPL means it's not a new position.
        """
        rm = RiskManager(_default_config(max_open_positions=1))
        aapl_pos = Position(
            symbol=AAPL,
            quantity=Decimal("5"),
            avg_entry_price=Decimal("198"),
            current_price=Decimal("200"),
        )
        port = _portfolio_with_price("AAPL", Decimal("200"))
        port.open_positions = {"AAPL": aapl_pos}
        sig = _buy_signal(stop=Decimal("190"))
        order = rm.evaluate(sig, port)
        assert order is not None

    def test_zero_equity_returns_none(self):
        """Sizing with zero equity must gracefully yield 0 → rejection, not crash."""
        rm = RiskManager(_default_config())
        port = FakePortfolio(equity=Decimal("0"), peak_equity=Decimal("0"), day_start_equity=Decimal("0"))
        port.last_price["AAPL"] = Decimal("200")
        sig = _buy_signal(stop=Decimal("190"))
        assert rm.evaluate(sig, port) is None

    def test_is_halted_initially_false(self):
        rm = RiskManager(_default_config())
        assert rm.is_halted is False


# ---------------------------------------------------------------------------
# Drawdown sizing throttle
# ---------------------------------------------------------------------------

class TestDrawdownThrottle:
    """De-risk new entries as the account draws down from its peak."""

    def _throttle_cfg(self, **ov) -> RiskConfig:
        return _default_config(
            max_position_size_pct=Decimal("0.20"),
            max_total_exposure_pct=Decimal("1.0"),
            max_drawdown_pct=Decimal("0.99"),         # don't let the halt mask the throttle
            drawdown_throttle_start=Decimal("0.10"),
            drawdown_throttle_full=Decimal("0.30"),
            drawdown_throttle_floor=Decimal("0.40"),
            **ov,
        )

    def _port(self, peak: str, equity: str) -> FakePortfolio:
        p = FakePortfolio(equity=Decimal(equity), peak_equity=Decimal(peak),
                          day_start_equity=Decimal(equity))
        p.last_price["AAPL"] = Decimal("200")
        return p

    def test_disabled_by_default(self):
        # Default config has throttle_start=None → always full size.
        rm = RiskManager(_default_config())
        assert rm._drawdown_throttle(self._port("100000", "70000")) == Decimal("1")

    def test_full_size_above_start(self):
        rm = RiskManager(self._throttle_cfg())
        # Only 5% down (< 10% start) → no throttling.
        assert rm._drawdown_throttle(self._port("100000", "95000")) == Decimal("1")

    def test_floor_at_and_beyond_full(self):
        rm = RiskManager(self._throttle_cfg())
        # 30% down = `full` → floor; 50% down → still floor.
        assert rm._drawdown_throttle(self._port("100000", "70000")) == Decimal("0.40")
        assert rm._drawdown_throttle(self._port("100000", "50000")) == Decimal("0.40")

    def test_linear_ramp_midpoint(self):
        rm = RiskManager(self._throttle_cfg())
        # 20% down is the midpoint of [10%, 30%] → halfway between 1 and 0.40 = 0.70.
        assert rm._drawdown_throttle(self._port("100000", "80000")) == Decimal("0.70")

    def test_no_peak_is_full_size(self):
        rm = RiskManager(self._throttle_cfg())
        p = FakePortfolio(equity=Decimal("0"), peak_equity=Decimal("0"),
                          day_start_equity=Decimal("0"))
        assert rm._drawdown_throttle(p) == Decimal("1")

    def test_throttle_shrinks_actual_order(self):
        """End-to-end: the same signal sizes smaller in a drawdown than at the peak."""
        rm = RiskManager(self._throttle_cfg())
        sig = _buy_signal(stop=Decimal("190"))
        at_peak = rm.evaluate(sig, self._port("100000", "100000"))
        in_dd = rm.evaluate(_buy_signal(stop=Decimal("190")), self._port("100000", "80000"))
        assert at_peak is not None and in_dd is not None
        # 20% drawdown → 0.70x sizing → strictly fewer shares.
        assert in_dd.quantity < at_peak.quantity
