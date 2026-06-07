"""
tests/test_options_income.py
============================
Offline, deterministic tests for options_income.py strategies.

apex.core.option is NOT yet on disk (sibling agent creates it).  This test file
defines a LOCAL SHIM that exactly matches the interface specified in the task brief.
The shim lives in this file only — production code (options_income.py) imports from
apex.core.option and will raise an ImportError with a clear message until that
module lands.

To run only these tests (the "light verify" step):
    python -m pytest tests/test_options_income.py -q
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

# ---------------------------------------------------------------------------
# LOCAL SHIM — mirrors the interface of apex.core.option exactly.
# Injected into sys.modules BEFORE options_income is imported so that the
# ``from apex.core.option import ...`` in options_income.py resolves to this shim.
# ---------------------------------------------------------------------------


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


class OptionRight(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class OptionContract:
    underlying: object  # Symbol
    expiry: date
    strike: Decimal
    option_type: OptionType

    @property
    def occ_symbol(self) -> str:
        """OCC-style symbol string (simplified for testing)."""
        ticker = str(self.underlying)
        exp = self.expiry.strftime("%y%m%d")
        right = "C" if self.option_type == OptionType.CALL else "P"
        strike_int = int(self.strike * 1000)
        return f"{ticker}{exp}{right}{strike_int:08d}"


@dataclass(frozen=True)
class OptionGreeks:
    delta: float
    gamma: float
    theta: float
    vega: float
    implied_vol: float


@dataclass(frozen=True)
class OptionQuote:
    contract: OptionContract
    bid: Decimal
    ask: Decimal
    last: Decimal
    timestamp: datetime
    greeks: Optional[OptionGreeks] = None


@dataclass(frozen=True)
class OptionLeg:
    contract: OptionContract
    right: OptionRight
    ratio: int = 1


@dataclass(frozen=True)
class OptionOrder:
    legs: tuple[OptionLeg, ...]
    quantity: int
    limit_price: Optional[Decimal] = None


# Inject the shim into sys.modules ONLY for apex.core.option.
# The real apex package is already importable — do NOT replace it.
# We only need to inject the one missing submodule.
_shim_module = types.ModuleType("apex.core.option")
_shim_module.OptionType = OptionType  # type: ignore[attr-defined]
_shim_module.OptionRight = OptionRight  # type: ignore[attr-defined]
_shim_module.OptionContract = OptionContract  # type: ignore[attr-defined]
_shim_module.OptionGreeks = OptionGreeks  # type: ignore[attr-defined]
_shim_module.OptionQuote = OptionQuote  # type: ignore[attr-defined]
_shim_module.OptionLeg = OptionLeg  # type: ignore[attr-defined]
_shim_module.OptionOrder = OptionOrder  # type: ignore[attr-defined]

# Register the shim only for apex.core.option — leave apex and apex.core alone
# so the real package (and its existing submodules) continue to resolve normally.
sys.modules["apex.core.option"] = _shim_module

# ---------------------------------------------------------------------------
# NOW import the module under test (shim is in place).
# ---------------------------------------------------------------------------
from apex.core.models import AssetClass, Symbol  # noqa: E402
from apex.strategy.library.options_income import (  # noqa: E402
    BullPutSpread,
    CashSecuredPut,
    CoveredCall,
    OptionSignal,
)

# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------

_UNDERLYING = Symbol(ticker="SPY", asset_class=AssetClass.ETF)
_SPOT = Decimal("450.00")
_AS_OF = date(2026, 6, 10)
_EXPIRY = date(2026, 7, 18)  # 38 DTE — satisfies default min_dte=21
_TS = datetime(2026, 6, 10, 14, 30, 0, tzinfo=timezone.utc)


def _make_call(strike: Decimal, delta: float, bid: Decimal, ask: Decimal) -> OptionQuote:
    contract = OptionContract(
        underlying=_UNDERLYING,
        expiry=_EXPIRY,
        strike=strike,
        option_type=OptionType.CALL,
    )
    greeks = OptionGreeks(delta=delta, gamma=0.01, theta=-0.05, vega=0.20, implied_vol=0.20)
    return OptionQuote(contract=contract, bid=bid, ask=ask, last=bid, timestamp=_TS, greeks=greeks)


def _make_put(strike: Decimal, delta: float, bid: Decimal, ask: Decimal) -> OptionQuote:
    contract = OptionContract(
        underlying=_UNDERLYING,
        expiry=_EXPIRY,
        strike=strike,
        option_type=OptionType.PUT,
    )
    greeks = OptionGreeks(delta=delta, gamma=0.01, theta=-0.05, vega=0.20, implied_vol=0.20)
    return OptionQuote(contract=contract, bid=bid, ask=ask, last=bid, timestamp=_TS, greeks=greeks)


def _make_synthetic_chain() -> list[OptionQuote]:
    """
    A synthetic but internally consistent option chain around SPY @ 450.

    Calls (OTM: strike > 450):
      460  delta=0.35  bid=3.00  ask=3.20
      465  delta=0.28  bid=2.00  ask=2.20   ← closest to 0.30 target
      470  delta=0.20  bid=1.20  ask=1.40
      480  delta=0.12  bid=0.60  ask=0.80

    Puts (OTM: strike < 450):
      445  delta=-0.28  bid=2.10  ask=2.30   ← closest to -0.30 target
      440  delta=-0.22  bid=1.50  ask=1.70
      435  delta=-0.15  bid=1.00  ask=1.20   ← closest to -0.15 (long leg of spread)
      430  delta=-0.10  bid=0.70  ask=0.90
    """
    return [
        # calls
        _make_call(Decimal("460"), 0.35, Decimal("3.00"), Decimal("3.20")),
        _make_call(Decimal("465"), 0.28, Decimal("2.00"), Decimal("2.20")),
        _make_call(Decimal("470"), 0.20, Decimal("1.20"), Decimal("1.40")),
        _make_call(Decimal("480"), 0.12, Decimal("0.60"), Decimal("0.80")),
        # puts
        _make_put(Decimal("445"), -0.28, Decimal("2.10"), Decimal("2.30")),
        _make_put(Decimal("440"), -0.22, Decimal("1.50"), Decimal("1.70")),
        _make_put(Decimal("435"), -0.15, Decimal("1.00"), Decimal("1.20")),
        _make_put(Decimal("430"), -0.10, Decimal("0.70"), Decimal("0.90")),
    ]


# ---------------------------------------------------------------------------
# OptionSignal shape tests
# ---------------------------------------------------------------------------


class TestOptionSignalShape:
    """Verify that OptionSignal is frozen and carries the required fields."""

    def test_option_signal_is_frozen(self) -> None:
        """OptionSignal must be immutable (frozen dataclass)."""
        leg = OptionLeg(
            contract=OptionContract(_UNDERLYING, _EXPIRY, Decimal("465"), OptionType.CALL),
            right=OptionRight.SELL,
            ratio=1,
        )
        order = OptionOrder(legs=(leg,), quantity=1, limit_price=Decimal("2.10"))
        sig = OptionSignal(
            order=order,
            strategy_id="test",
            reason="unit test",
            max_loss=Decimal("100.00"),
        )
        import pytest

        with pytest.raises((AttributeError, TypeError)):
            sig.max_loss = Decimal("999")  # type: ignore[misc]

    def test_option_signal_fields_present(self) -> None:
        """All four required fields exist and are accessible."""
        leg = OptionLeg(
            contract=OptionContract(_UNDERLYING, _EXPIRY, Decimal("465"), OptionType.CALL),
            right=OptionRight.SELL,
        )
        order = OptionOrder(legs=(leg,), quantity=1)
        sig = OptionSignal(order=order, strategy_id="s1", reason="r", max_loss=Decimal("50"))
        assert sig.order is order
        assert sig.strategy_id == "s1"
        assert sig.reason == "r"
        assert sig.max_loss == Decimal("50")


# ---------------------------------------------------------------------------
# CoveredCall tests
# ---------------------------------------------------------------------------


class TestCoveredCall:
    def _strategy(self, target_delta: float = 0.30, min_dte: int = 21) -> CoveredCall:
        return CoveredCall(
            strategy_id="cc_test",
            underlying=_UNDERLYING,
            target_delta=target_delta,
            min_dte=min_dte,
        )

    def test_returns_option_signal(self) -> None:
        chain = _make_synthetic_chain()
        sig = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        assert isinstance(sig, OptionSignal)

    def test_picks_otm_call(self) -> None:
        """The selected call must be OTM (strike > spot)."""
        chain = _make_synthetic_chain()
        sig = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        leg = sig.order.legs[0]
        assert leg.contract.option_type == OptionType.CALL
        assert leg.contract.strike > _SPOT

    def test_picks_call_near_target_delta(self) -> None:
        """
        The selected call's delta should be the closest to the target delta.
        With target_delta=0.30, the chain has 0.35 (dist=0.05) and 0.28 (dist=0.02).
        So 0.28 (strike 465) should win.
        """
        chain = _make_synthetic_chain()
        sig = self._strategy(target_delta=0.30).evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        leg = sig.order.legs[0]
        assert leg.contract.strike == Decimal("465")

    def test_single_sell_leg(self) -> None:
        """CoveredCall produces a 1-leg order, right=SELL."""
        chain = _make_synthetic_chain()
        sig = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        assert len(sig.order.legs) == 1
        assert sig.order.legs[0].right == OptionRight.SELL

    def test_max_loss_positive_finite(self) -> None:
        """max_loss must be positive and finite."""
        chain = _make_synthetic_chain()
        sig = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        assert sig.max_loss > 0
        assert sig.max_loss < Decimal("Infinity")

    def test_max_loss_formula(self) -> None:
        """max_loss = (spot - premium) * 100.  Premium = mid of 465 call = (2.00+2.20)/2=2.10"""
        chain = _make_synthetic_chain()
        sig = self._strategy(target_delta=0.30).evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        expected_premium = (Decimal("2.00") + Decimal("2.20")) / Decimal("2")
        expected_max_loss = (_SPOT - expected_premium) * Decimal("100")
        assert sig.max_loss == expected_max_loss

    def test_no_signal_when_dte_too_short(self) -> None:
        """If min_dte is set to 100 and chain only has 38 DTE, return None."""
        chain = _make_synthetic_chain()
        sig = self._strategy(min_dte=100).evaluate(_SPOT, chain, _AS_OF)
        assert sig is None

    def test_limit_price_is_mid(self) -> None:
        """limit_price on the order should be the midpoint of the selected quote."""
        chain = _make_synthetic_chain()
        sig = self._strategy(target_delta=0.30).evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        expected_mid = (Decimal("2.00") + Decimal("2.20")) / Decimal("2")
        assert sig.order.limit_price == expected_mid

    def test_deterministic(self) -> None:
        """Same inputs produce identical OptionSignal both times."""
        chain = _make_synthetic_chain()
        sig1 = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        sig2 = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig1 == sig2

    def test_strategy_id_propagated(self) -> None:
        chain = _make_synthetic_chain()
        strat = CoveredCall(strategy_id="my_cc", underlying=_UNDERLYING)
        sig = strat.evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        assert sig.strategy_id == "my_cc"


# ---------------------------------------------------------------------------
# CashSecuredPut tests
# ---------------------------------------------------------------------------


class TestCashSecuredPut:
    def _strategy(self, target_delta: float = 0.30, min_dte: int = 21) -> CashSecuredPut:
        return CashSecuredPut(
            strategy_id="csp_test",
            underlying=_UNDERLYING,
            target_delta=target_delta,
            min_dte=min_dte,
        )

    def test_returns_option_signal(self) -> None:
        chain = _make_synthetic_chain()
        sig = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        assert isinstance(sig, OptionSignal)

    def test_picks_otm_put(self) -> None:
        """The selected put must be OTM (strike < spot)."""
        chain = _make_synthetic_chain()
        sig = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        leg = sig.order.legs[0]
        assert leg.contract.option_type == OptionType.PUT
        assert leg.contract.strike < _SPOT

    def test_picks_put_near_target_delta(self) -> None:
        """
        With target_delta=0.30, compare abs(delta) to 0.30:
          445: delta=-0.28, |dist|=0.02
          440: delta=-0.22, |dist|=0.08
          435: delta=-0.15, |dist|=0.15
          430: delta=-0.10, |dist|=0.20
        The 445 put (delta -0.28, closest to -0.30) should be selected.
        On tie: prefer_higher_strike=True means higher strike wins (more like 445).
        """
        chain = _make_synthetic_chain()
        sig = self._strategy(target_delta=0.30).evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        leg = sig.order.legs[0]
        assert leg.contract.strike == Decimal("445")

    def test_single_sell_leg(self) -> None:
        chain = _make_synthetic_chain()
        sig = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        assert len(sig.order.legs) == 1
        assert sig.order.legs[0].right == OptionRight.SELL

    def test_max_loss_formula(self) -> None:
        """
        max_loss = (strike - premium) * 100.
        445 put: mid = (2.10 + 2.30) / 2 = 2.20
        max_loss = (445 - 2.20) * 100 = 44280.00
        """
        chain = _make_synthetic_chain()
        sig = self._strategy(target_delta=0.30).evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        expected_premium = (Decimal("2.10") + Decimal("2.30")) / Decimal("2")
        expected_max_loss = (Decimal("445") - expected_premium) * Decimal("100")
        assert sig.max_loss == expected_max_loss

    def test_max_loss_positive_finite(self) -> None:
        chain = _make_synthetic_chain()
        sig = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        assert sig.max_loss > 0
        assert sig.max_loss < Decimal("Infinity")

    def test_no_signal_when_dte_too_short(self) -> None:
        chain = _make_synthetic_chain()
        sig = self._strategy(min_dte=100).evaluate(_SPOT, chain, _AS_OF)
        assert sig is None

    def test_deterministic(self) -> None:
        chain = _make_synthetic_chain()
        sig1 = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        sig2 = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig1 == sig2

    def test_strategy_id_propagated(self) -> None:
        chain = _make_synthetic_chain()
        strat = CashSecuredPut(strategy_id="my_csp", underlying=_UNDERLYING)
        sig = strat.evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        assert sig.strategy_id == "my_csp"


# ---------------------------------------------------------------------------
# BullPutSpread tests
# ---------------------------------------------------------------------------


class TestBullPutSpread:
    def _strategy(
        self,
        short_delta: float = 0.30,
        long_delta: float = 0.15,
        min_dte: int = 21,
    ) -> BullPutSpread:
        return BullPutSpread(
            strategy_id="bps_test",
            underlying=_UNDERLYING,
            short_delta=short_delta,
            long_delta=long_delta,
            min_dte=min_dte,
        )

    def test_returns_option_signal(self) -> None:
        chain = _make_synthetic_chain()
        sig = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        assert isinstance(sig, OptionSignal)

    def test_two_leg_order(self) -> None:
        """BullPutSpread must produce exactly 2 legs."""
        chain = _make_synthetic_chain()
        sig = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        assert len(sig.order.legs) == 2

    def test_leg_rights(self) -> None:
        """First leg SELL, second leg BUY."""
        chain = _make_synthetic_chain()
        sig = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        assert sig.order.legs[0].right == OptionRight.SELL
        assert sig.order.legs[1].right == OptionRight.BUY

    def test_both_legs_are_puts(self) -> None:
        chain = _make_synthetic_chain()
        sig = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        for leg in sig.order.legs:
            assert leg.contract.option_type == OptionType.PUT

    def test_short_strike_above_long_strike(self) -> None:
        """Short leg must have the higher strike (closer to ATM)."""
        chain = _make_synthetic_chain()
        sig = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        short_strike = sig.order.legs[0].contract.strike
        long_strike = sig.order.legs[1].contract.strike
        assert short_strike > long_strike

    def test_both_legs_otm(self) -> None:
        """Both put strikes must be below spot."""
        chain = _make_synthetic_chain()
        sig = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        for leg in sig.order.legs:
            assert leg.contract.strike < _SPOT

    def test_short_leg_picks_445(self) -> None:
        """Short delta=0.30 → 445 put (delta -0.28, closest to -0.30)."""
        chain = _make_synthetic_chain()
        sig = self._strategy(short_delta=0.30, long_delta=0.15).evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        assert sig.order.legs[0].contract.strike == Decimal("445")

    def test_long_leg_picks_435(self) -> None:
        """
        Long delta=0.15 → among puts with strike < 445:
          440: |delta|=0.22, dist=0.07
          435: |delta|=0.15, dist=0.00  ← exact match
          430: |delta|=0.10, dist=0.05
        So 435 should be selected.
        """
        chain = _make_synthetic_chain()
        sig = self._strategy(short_delta=0.30, long_delta=0.15).evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        assert sig.order.legs[1].contract.strike == Decimal("435")

    def test_max_loss_formula(self) -> None:
        """
        Short 445 put: mid = (2.10+2.30)/2 = 2.20
        Long  435 put: mid = (1.00+1.20)/2 = 1.10
        net_credit = 2.20 - 1.10 = 1.10
        width = 445 - 435 = 10
        max_loss = (10 - 1.10) * 100 = 890.00
        """
        chain = _make_synthetic_chain()
        sig = self._strategy(short_delta=0.30, long_delta=0.15).evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        short_mid = (Decimal("2.10") + Decimal("2.30")) / Decimal("2")  # 2.20
        long_mid = (Decimal("1.00") + Decimal("1.20")) / Decimal("2")  # 1.10
        net_credit = short_mid - long_mid  # 1.10
        width = Decimal("445") - Decimal("435")  # 10
        expected_max_loss = (width - net_credit) * Decimal("100")  # 890.00
        assert sig.max_loss == expected_max_loss

    def test_max_loss_positive_finite(self) -> None:
        chain = _make_synthetic_chain()
        sig = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        assert sig.max_loss > 0
        assert sig.max_loss < Decimal("Infinity")

    def test_net_credit_is_limit_price(self) -> None:
        """The order limit_price should equal the net credit collected."""
        chain = _make_synthetic_chain()
        sig = self._strategy(short_delta=0.30, long_delta=0.15).evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        short_mid = (Decimal("2.10") + Decimal("2.30")) / Decimal("2")
        long_mid = (Decimal("1.00") + Decimal("1.20")) / Decimal("2")
        expected_credit = short_mid - long_mid
        assert sig.order.limit_price == expected_credit

    def test_no_signal_when_dte_too_short(self) -> None:
        chain = _make_synthetic_chain()
        sig = self._strategy(min_dte=100).evaluate(_SPOT, chain, _AS_OF)
        assert sig is None

    def test_deterministic(self) -> None:
        """Same inputs → same output, every call."""
        chain = _make_synthetic_chain()
        sig1 = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        sig2 = self._strategy().evaluate(_SPOT, chain, _AS_OF)
        assert sig1 == sig2

    def test_constructor_rejects_invalid_deltas(self) -> None:
        """long_delta >= short_delta should raise ValueError at construction."""
        import pytest

        with pytest.raises(ValueError, match="long_delta"):
            BullPutSpread(
                strategy_id="bad",
                underlying=_UNDERLYING,
                short_delta=0.20,
                long_delta=0.25,  # long > short — invalid
            )

    def test_strategy_id_propagated(self) -> None:
        chain = _make_synthetic_chain()
        strat = BullPutSpread(strategy_id="my_bps", underlying=_UNDERLYING)
        sig = strat.evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        assert sig.strategy_id == "my_bps"


# ---------------------------------------------------------------------------
# Strike selection determinism — edge cases
# ---------------------------------------------------------------------------


class TestStrikeSelectionDeterminism:
    """Verify that tie-breaking is deterministic and produces the expected winner."""

    def test_covered_call_tie_break_prefers_lower_strike(self) -> None:
        """
        When two calls are equidistant from target_delta, the more OTM one
        (lower delta, i.e. higher strike) wins for calls (prefer_higher_strike=False
        means lower strike in our tie-break).

        Actually: prefer_higher_strike=False → tie-break key = +strike (ascending),
        so lower strike wins on a tie.
        """
        # Two calls with identical delta distance from 0.30 — one at 462, one at 468.
        ts = datetime(2026, 6, 10, 14, 30, 0, tzinfo=timezone.utc)
        expiry = date(2026, 7, 18)
        underlying = _UNDERLYING

        def _call(strike: Decimal, delta: float) -> OptionQuote:
            contract = OptionContract(underlying, expiry, strike, OptionType.CALL)
            g = OptionGreeks(delta=delta, gamma=0.01, theta=-0.05, vega=0.20, implied_vol=0.20)
            return OptionQuote(
                contract=contract,
                bid=Decimal("2.00"),
                ask=Decimal("2.20"),
                last=Decimal("2.10"),
                timestamp=ts,
                greeks=g,
            )

        chain = [
            _call(Decimal("462"), 0.25),  # dist from 0.30 = 0.05
            _call(Decimal("468"), 0.35),  # dist from 0.30 = 0.05  ← tie
        ]
        strat = CoveredCall(
            strategy_id="tie_cc", underlying=underlying, target_delta=0.30, min_dte=21
        )
        sig = strat.evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        # prefer_higher_strike=False → tie-break key is +strike ascending → lower strike (462) wins
        assert sig.order.legs[0].contract.strike == Decimal("462")

    def test_csp_tie_break_prefers_higher_strike(self) -> None:
        """
        For CSP: prefer_higher_strike=True → on a tie, higher strike (less OTM) wins.
        """
        ts = datetime(2026, 6, 10, 14, 30, 0, tzinfo=timezone.utc)
        expiry = date(2026, 7, 18)
        underlying = _UNDERLYING

        def _put(strike: Decimal, delta: float) -> OptionQuote:
            contract = OptionContract(underlying, expiry, strike, OptionType.PUT)
            g = OptionGreeks(delta=delta, gamma=0.01, theta=-0.05, vega=0.20, implied_vol=0.20)
            return OptionQuote(
                contract=contract,
                bid=Decimal("2.00"),
                ask=Decimal("2.20"),
                last=Decimal("2.10"),
                timestamp=ts,
                greeks=g,
            )

        chain = [
            _put(Decimal("442"), -0.25),  # |dist from -0.30| = 0.05
            _put(Decimal("448"), -0.35),  # |dist from -0.30| = 0.05  ← tie
        ]
        strat = CashSecuredPut(
            strategy_id="tie_csp", underlying=underlying, target_delta=0.30, min_dte=21
        )
        sig = strat.evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        # prefer_higher_strike=True → tie-break key = -strike descending → higher strike (448) wins
        assert sig.order.legs[0].contract.strike == Decimal("448")

    def test_no_greeks_fallback_call(self) -> None:
        """When greeks are None, CoveredCall falls back and still returns a valid signal."""
        ts = datetime(2026, 6, 10, 14, 30, 0, tzinfo=timezone.utc)
        expiry = date(2026, 7, 18)
        underlying = _UNDERLYING

        chain = [
            OptionQuote(
                contract=OptionContract(underlying, expiry, Decimal("460"), OptionType.CALL),
                bid=Decimal("3.00"),
                ask=Decimal("3.20"),
                last=Decimal("3.10"),
                timestamp=ts,
                greeks=None,
            ),
            OptionQuote(
                contract=OptionContract(underlying, expiry, Decimal("465"), OptionType.CALL),
                bid=Decimal("2.00"),
                ask=Decimal("2.20"),
                last=Decimal("2.10"),
                timestamp=ts,
                greeks=None,
            ),
        ]
        strat = CoveredCall(strategy_id="no_greeks_cc", underlying=underlying, min_dte=21)
        sig = strat.evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        # No greeks → fallback selects by prefer_higher_strike=False → max(quotes, key=lambda: strike)
        # Wait: _pick_by_delta with no greeks returns max when prefer_higher_strike=True,
        # min when prefer_higher_strike=False.  For calls, prefer_higher_strike=False → min strike.
        assert sig.order.legs[0].contract.strike == Decimal("460")

    def test_no_greeks_fallback_put(self) -> None:
        """When greeks are None, CashSecuredPut still returns a valid signal."""
        ts = datetime(2026, 6, 10, 14, 30, 0, tzinfo=timezone.utc)
        expiry = date(2026, 7, 18)
        underlying = _UNDERLYING

        chain = [
            OptionQuote(
                contract=OptionContract(underlying, expiry, Decimal("445"), OptionType.PUT),
                bid=Decimal("2.10"),
                ask=Decimal("2.30"),
                last=Decimal("2.20"),
                timestamp=ts,
                greeks=None,
            ),
            OptionQuote(
                contract=OptionContract(underlying, expiry, Decimal("440"), OptionType.PUT),
                bid=Decimal("1.50"),
                ask=Decimal("1.70"),
                last=Decimal("1.60"),
                timestamp=ts,
                greeks=None,
            ),
        ]
        strat = CashSecuredPut(strategy_id="no_greeks_csp", underlying=underlying, min_dte=21)
        sig = strat.evaluate(_SPOT, chain, _AS_OF)
        assert sig is not None
        # No greeks → prefer_higher_strike=True → max(quotes, key=lambda: strike) → 445
        assert sig.order.legs[0].contract.strike == Decimal("445")
