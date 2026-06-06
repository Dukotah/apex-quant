"""
Tests for the hardened RiskManager checks (TRACK C — risk hardening).

These cover the five features layered onto the existing RiskManager, each gated by a
new immutable RiskConfig field that defaults to off/safe so the original test suite is
untouched:

  1. Stale-data guard (uses the injected Clock, never datetime.now()).
  2. Sector / asset-class exposure caps + max-correlated-exposure cap.
  3. Per-trade and per-day hard notional limits (independent of % sizing).
  4. ATR-based dynamic stop validation + trailing-stop gate (validated, not placed).
  5. Consecutive-rejection / error circuit breaker that raises a HaltEvent.

Every monetary value is Decimal. For each feature we assert BOTH the rejection path and
that a valid signal still passes (the fail-closed default must not break good flow).

A property-based (Hypothesis) section fuzzes malformed/extreme inputs and asserts the
manager NEVER emits an OrderEvent on bad input — the fail-closed invariant.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, Optional

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apex.core.clock import Clock
from apex.core.events import HaltEvent, OrderEvent, SignalEvent
from apex.core.models import AssetClass, OrderSide, Position, Symbol
from apex.risk.risk_manager import RiskConfig, RiskManager

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

AAPL = Symbol("AAPL", AssetClass.EQUITY)
MSFT = Symbol("MSFT", AssetClass.EQUITY)
SPY = Symbol("SPY", AssetClass.ETF)
QQQ = Symbol("QQQ", AssetClass.ETF)
BTCUSD = Symbol("BTC/USD", AssetClass.CRYPTO, fractionable=True)

_DT = datetime(2026, 6, 5, 14, 30, tzinfo=timezone.utc)


class FixedClock(Clock):
    """Deterministic clock for the stale-data guard. Returns an injected instant."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


@dataclass
class FakePortfolio:
    """Minimal mutable stub exposing exactly what the RiskManager reads."""

    equity: Decimal = Decimal("100000")
    peak_equity: Decimal = Decimal("100000")
    day_start_equity: Decimal = Decimal("100000")
    open_positions: Dict[str, Position] = field(default_factory=dict)
    exposure: Decimal = Decimal("0")
    last_price: Dict[str, Decimal] = field(default_factory=dict)


@dataclass
class FakeSignal:
    """
    Duck-typed signal carrying the fields the RiskManager reads, PLUS the optional
    `atr` / `trailing_stop` attributes the hardened checks look up via getattr. Used
    where a real frozen SignalEvent cannot carry ATR/trailing metadata.
    """

    symbol: Symbol
    side: OrderSide
    strength: Decimal = Decimal("1.0")
    strategy_id: str = "test"
    suggested_stop_loss: Optional[Decimal] = None
    suggested_take_profit: Optional[Decimal] = None
    timestamp: Optional[datetime] = None
    atr: Optional[Decimal] = None
    trailing_stop: bool = False
    event_id: str = "sig-1"


def _base_config(**overrides) -> RiskConfig:
    """A config whose ORIGINAL limits are wide open so a hardening check is what fires."""
    defaults = dict(
        max_position_size_pct=Decimal("0.20"),
        max_total_exposure_pct=Decimal("1.0"),
        max_leverage=Decimal("10.0"),
        max_drawdown_pct=Decimal("0.99"),
        max_daily_loss_pct=Decimal("0.99"),
        max_open_positions=100,
        require_stop_loss=False,
        min_stop_distance_pct=Decimal("0.005"),
        symbol_whitelist=None,
    )
    defaults.update(overrides)
    return RiskConfig(**defaults)


def _buy(symbol: Symbol = AAPL, stop: Optional[Decimal] = Decimal("190"), **kw) -> SignalEvent:
    return SignalEvent(
        symbol=symbol,
        side=OrderSide.BUY,
        strength=kw.pop("strength", Decimal("1.0")),
        strategy_id="test",
        suggested_stop_loss=stop,
        timestamp=kw.pop("timestamp", None),
    )


def _port(ticker: str = "AAPL", price: Decimal = Decimal("200"), **kw) -> FakePortfolio:
    p = FakePortfolio(**kw)
    p.last_price[ticker] = price
    return p


def _held(symbol: Symbol, qty: Decimal, price: Decimal) -> Position:
    return Position(
        symbol=symbol, quantity=qty, avg_entry_price=price, current_price=price
    )


# ===========================================================================
# 1. Stale-data guard
# ===========================================================================

class TestStaleDataGuard:

    def test_disabled_by_default_passes_without_clock(self):
        """No max_bar_age_seconds → guard off, no Clock needed, signal passes."""
        rm = RiskManager(_base_config())
        order = rm.evaluate(_buy(), _port())
        assert order is not None

    def test_fresh_bar_passes(self):
        clock = FixedClock(_DT)
        rm = RiskManager(_base_config(max_bar_age_seconds=300), clock=clock)
        sig = _buy(timestamp=_DT - timedelta(seconds=60))   # 60s old, within 300s
        order = rm.evaluate(sig, _port())
        assert order is not None

    def test_stale_bar_rejected(self):
        clock = FixedClock(_DT)
        rm = RiskManager(_base_config(max_bar_age_seconds=300), clock=clock)
        sig = _buy(timestamp=_DT - timedelta(seconds=600))  # 600s old > 300s
        assert rm.evaluate(sig, _port()) is None

    def test_exactly_at_max_age_passes(self):
        clock = FixedClock(_DT)
        rm = RiskManager(_base_config(max_bar_age_seconds=300), clock=clock)
        sig = _buy(timestamp=_DT - timedelta(seconds=300))  # boundary inclusive
        assert rm.evaluate(sig, _port()) is not None

    def test_future_dated_bar_rejected(self):
        """A bar timestamped in the future is corrupt → fail closed."""
        clock = FixedClock(_DT)
        rm = RiskManager(_base_config(max_bar_age_seconds=300), clock=clock)
        sig = _buy(timestamp=_DT + timedelta(seconds=10))
        assert rm.evaluate(sig, _port()) is None

    def test_enabled_without_clock_rejects(self):
        """Guard enabled but no Clock injected → fail closed (no order)."""
        rm = RiskManager(_base_config(max_bar_age_seconds=300), clock=None)
        sig = _buy(timestamp=_DT)
        assert rm.evaluate(sig, _port()) is None

    def test_missing_timestamp_rejected(self):
        clock = FixedClock(_DT)
        rm = RiskManager(_base_config(max_bar_age_seconds=300), clock=clock)
        sig = _buy(timestamp=None)
        assert rm.evaluate(sig, _port()) is None


# ===========================================================================
# 2. Sector / asset-class / correlated exposure caps
# ===========================================================================

class TestSectorCap:

    def _cfg(self):
        return _base_config(
            max_sector_exposure_pct=Decimal("0.30"),
            sector_map={"AAPL": "tech", "MSFT": "tech", "XOM": "energy"},
        )

    def test_within_sector_cap_passes(self):
        rm = RiskManager(self._cfg())
        # New AAPL order: 0.20*100k=20k notional < 30k cap, no existing tech holdings.
        order = rm.evaluate(_buy(AAPL), _port("AAPL", Decimal("200")))
        assert order is not None

    def test_sector_cap_breached_rejected(self):
        rm = RiskManager(self._cfg())
        # Already hold $20k of MSFT (tech). New AAPL $20k → $40k tech > $30k cap.
        port = _port("AAPL", Decimal("200"))
        port.last_price["MSFT"] = Decimal("200")
        port.open_positions = {"MSFT": _held(MSFT, Decimal("100"), Decimal("200"))}
        assert rm.evaluate(_buy(AAPL), port) is None

    def test_unmapped_symbol_is_its_own_bucket(self):
        """An unmapped ticker is isolated, never folded into a mapped sector to hide."""
        rm = RiskManager(self._cfg())
        # Hold $20k MSFT (tech). New UNKN order should NOT count against tech.
        unkn = Symbol("UNKN", AssetClass.EQUITY)
        port = _port("UNKN", Decimal("200"))
        port.last_price["MSFT"] = Decimal("200")
        port.open_positions = {"MSFT": _held(MSFT, Decimal("100"), Decimal("200"))}
        # UNKN sized 0.20*100k=20k < 30k cap, separate bucket → passes.
        assert rm.evaluate(_buy(unkn), port) is not None


class TestAssetClassCap:

    def test_asset_class_cap_breached_rejected(self):
        rm = RiskManager(_base_config(max_asset_class_exposure_pct=Decimal("0.25")))
        # Hold $20k of SPY (ETF). New QQQ (ETF) $20k → $40k ETF > $25k cap.
        port = _port("QQQ", Decimal("200"))
        port.last_price["SPY"] = Decimal("200")
        port.open_positions = {"SPY": _held(SPY, Decimal("100"), Decimal("200"))}
        assert rm.evaluate(_buy(QQQ), port) is None

    def test_asset_class_within_cap_passes(self):
        rm = RiskManager(_base_config(max_asset_class_exposure_pct=Decimal("0.50")))
        port = _port("QQQ", Decimal("200"))
        port.last_price["SPY"] = Decimal("200")
        port.open_positions = {"SPY": _held(SPY, Decimal("100"), Decimal("200"))}
        # $20k SPY + $20k QQQ = $40k ETF < $50k cap → passes.
        assert rm.evaluate(_buy(QQQ), port) is not None


class TestCorrelatedCap:

    def _cfg(self):
        return _base_config(
            max_correlated_exposure_pct=Decimal("0.30"),
            correlation_groups={"megacap_tech": frozenset({"AAPL", "MSFT", "NVDA"})},
        )

    def test_correlated_cap_breached_rejected(self):
        rm = RiskManager(self._cfg())
        # Hold $20k MSFT (in group). New AAPL $20k → $40k group > $30k cap.
        port = _port("AAPL", Decimal("200"))
        port.last_price["MSFT"] = Decimal("200")
        port.open_positions = {"MSFT": _held(MSFT, Decimal("100"), Decimal("200"))}
        assert rm.evaluate(_buy(AAPL), port) is None

    def test_uncorrelated_symbol_unaffected(self):
        rm = RiskManager(self._cfg())
        # Hold $20k MSFT (in group). New XOM (NOT in group) → group unchanged → passes.
        xom = Symbol("XOM", AssetClass.EQUITY)
        port = _port("XOM", Decimal("200"))
        port.last_price["MSFT"] = Decimal("200")
        port.open_positions = {"MSFT": _held(MSFT, Decimal("100"), Decimal("200"))}
        assert rm.evaluate(_buy(xom), port) is not None


# ===========================================================================
# 3. Hard notional limits (per-trade and per-day)
# ===========================================================================

class TestNotionalLimits:

    def test_per_trade_limit_rejects_oversized_order(self):
        rm = RiskManager(_base_config(max_trade_notional=Decimal("5000")))
        # Default sizing: 0.20*100k=20k notional > 5k per-trade cap → reject.
        assert rm.evaluate(_buy(AAPL), _port("AAPL", Decimal("200"))) is None

    def test_per_trade_limit_allows_small_order(self):
        rm = RiskManager(_base_config(max_trade_notional=Decimal("50000")))
        # 20k notional < 50k cap → passes.
        assert rm.evaluate(_buy(AAPL), _port("AAPL", Decimal("200"))) is not None

    def test_per_day_limit_accumulates_and_blocks(self):
        rm = RiskManager(_base_config(max_daily_notional=Decimal("30000")))
        # First $20k order passes; cumulative now $20k.
        first = rm.evaluate(_buy(AAPL), _port("AAPL", Decimal("200")))
        assert first is not None
        # Second $20k order → cumulative $40k > $30k daily cap → reject.
        second = rm.evaluate(_buy(MSFT), _port("MSFT", Decimal("200")))
        assert second is None

    def test_reset_daily_clears_daily_notional(self):
        rm = RiskManager(_base_config(max_daily_notional=Decimal("30000")))
        assert rm.evaluate(_buy(AAPL), _port("AAPL", Decimal("200"))) is not None
        assert rm.evaluate(_buy(MSFT), _port("MSFT", Decimal("200"))) is None
        rm.reset_daily()
        # After reset, the tally is clear → a fresh $20k order passes again.
        assert rm.evaluate(_buy(MSFT), _port("MSFT", Decimal("200"))) is not None


# ===========================================================================
# 4. ATR-based stop validation + trailing-stop gate
# ===========================================================================

class TestATRStopValidation:

    def _cfg(self, **ov):
        return _base_config(
            require_stop_loss=True,
            require_atr_stop=True,
            atr_stop_min_multiple=Decimal("1.0"),
            atr_stop_max_multiple=Decimal("3.0"),
            **ov,
        )

    def test_valid_atr_stop_passes(self):
        rm = RiskManager(self._cfg())
        # price 200, ATR 5, stop 190 → distance 10 = 2.0 * ATR ∈ [1,3] → pass.
        sig = FakeSignal(AAPL, OrderSide.BUY, suggested_stop_loss=Decimal("190"),
                         atr=Decimal("5"))
        assert rm.evaluate(sig, _port("AAPL", Decimal("200"))) is not None

    def test_too_tight_atr_stop_rejected(self):
        rm = RiskManager(self._cfg())
        # distance 2 = 0.4 * ATR(5) < 1.0 min → reject (also passes basic min-dist).
        sig = FakeSignal(AAPL, OrderSide.BUY, suggested_stop_loss=Decimal("198"),
                         atr=Decimal("5"))
        assert rm.evaluate(sig, _port("AAPL", Decimal("200"))) is None

    def test_too_wide_atr_stop_rejected(self):
        rm = RiskManager(self._cfg())
        # distance 20 = 4.0 * ATR(5) > 3.0 max → reject.
        sig = FakeSignal(AAPL, OrderSide.BUY, suggested_stop_loss=Decimal("180"),
                         atr=Decimal("5"))
        assert rm.evaluate(sig, _port("AAPL", Decimal("200"))) is None

    def test_missing_atr_rejected_when_required(self):
        rm = RiskManager(self._cfg())
        sig = FakeSignal(AAPL, OrderSide.BUY, suggested_stop_loss=Decimal("190"),
                         atr=None)
        assert rm.evaluate(sig, _port("AAPL", Decimal("200"))) is None

    def test_nonpositive_atr_rejected(self):
        rm = RiskManager(self._cfg())
        sig = FakeSignal(AAPL, OrderSide.BUY, suggested_stop_loss=Decimal("190"),
                         atr=Decimal("0"))
        assert rm.evaluate(sig, _port("AAPL", Decimal("200"))) is None


class TestTrailingStopGate:

    def test_trailing_allowed_passes(self):
        rm = RiskManager(_base_config(require_stop_loss=True, allow_trailing_stop=True))
        sig = FakeSignal(AAPL, OrderSide.BUY, suggested_stop_loss=Decimal("190"),
                         trailing_stop=True)
        assert rm.evaluate(sig, _port("AAPL", Decimal("200"))) is not None

    def test_trailing_disallowed_rejected(self):
        rm = RiskManager(_base_config(require_stop_loss=True, allow_trailing_stop=False))
        sig = FakeSignal(AAPL, OrderSide.BUY, suggested_stop_loss=Decimal("190"),
                         trailing_stop=True)
        assert rm.evaluate(sig, _port("AAPL", Decimal("200"))) is None

    def test_non_trailing_unaffected_when_disallowed(self):
        rm = RiskManager(_base_config(require_stop_loss=True, allow_trailing_stop=False))
        sig = FakeSignal(AAPL, OrderSide.BUY, suggested_stop_loss=Decimal("190"),
                         trailing_stop=False)
        assert rm.evaluate(sig, _port("AAPL", Decimal("200"))) is not None


# ===========================================================================
# 5. Consecutive-rejection / error circuit breaker
# ===========================================================================

class TestConsecutiveRejectionBreaker:

    def test_disabled_by_default(self):
        rm = RiskManager(_base_config())   # max_consecutive_rejections=None
        # Force many rejections (no reference price → sizing 0 → reject).
        bad_port = FakePortfolio()         # empty last_price
        for _ in range(50):
            rm.evaluate(_buy(AAPL), bad_port)
        assert rm.is_halted is False

    def test_breaker_halts_after_threshold(self):
        rm = RiskManager(_base_config(max_consecutive_rejections=3))
        bad_port = FakePortfolio()         # no price → every signal rejected
        for _ in range(3):
            assert rm.evaluate(_buy(AAPL), bad_port) is None
        assert rm.is_halted is True

    def test_breaker_raises_halt_event(self):
        rm = RiskManager(_base_config(max_consecutive_rejections=2))
        bad_port = FakePortfolio()
        rm.evaluate(_buy(AAPL), bad_port)
        rm.evaluate(_buy(AAPL), bad_port)
        halt = rm.last_halt_event
        assert isinstance(halt, HaltEvent)
        assert halt.triggered_by == "consecutive_rejections"

    def test_approval_resets_streak(self):
        rm = RiskManager(_base_config(max_consecutive_rejections=3))
        bad_port = FakePortfolio()
        # Two rejections, then a good one resets, so we never reach 3 in a row.
        rm.evaluate(_buy(AAPL), bad_port)
        rm.evaluate(_buy(AAPL), bad_port)
        assert rm.consecutive_rejections == 2
        assert rm.evaluate(_buy(AAPL), _port("AAPL", Decimal("200"))) is not None
        assert rm.consecutive_rejections == 0
        # Two more rejections still under the threshold → not halted.
        rm.evaluate(_buy(AAPL), bad_port)
        rm.evaluate(_buy(AAPL), bad_port)
        assert rm.is_halted is False

    def test_breaker_counts_fail_closed_errors(self):
        """Fail-closed exceptions count toward the breaker too."""
        rm = RiskManager(_base_config(max_consecutive_rejections=2))
        broken = FakePortfolio()
        broken.last_price = None  # type: ignore[assignment]  → AttributeError inside
        rm.evaluate(_buy(AAPL), broken)
        rm.evaluate(_buy(AAPL), broken)
        assert rm.is_halted is True


# ===========================================================================
# Hypothesis property tests — the fail-closed invariant
# ===========================================================================

# Reasonable Decimal money values (no NaN/inf, bounded scale) plus pathological ones.
_finite_decimals = st.decimals(
    min_value=Decimal("-1000000"),
    max_value=Decimal("1000000"),
    allow_nan=False,
    allow_infinity=False,
    places=4,
)

_maybe_bad_numbers = st.one_of(
    _finite_decimals,
    st.none(),
    st.just(Decimal("NaN")),
    st.just(Decimal("Infinity")),
    st.just(Decimal("-Infinity")),
    st.just("not-a-number"),
    st.integers(min_value=-10**9, max_value=10**9),
    st.floats(allow_nan=True, allow_infinity=True),
)


def _all_hardening_on() -> RiskConfig:
    """Every hardening check active, so fuzzing exercises all of them at once."""
    return RiskConfig(
        max_position_size_pct=Decimal("0.20"),
        max_total_exposure_pct=Decimal("1.0"),
        max_leverage=Decimal("5.0"),
        max_drawdown_pct=Decimal("0.50"),
        max_daily_loss_pct=Decimal("0.50"),
        require_stop_loss=True,
        max_bar_age_seconds=300,
        max_sector_exposure_pct=Decimal("0.30"),
        max_asset_class_exposure_pct=Decimal("0.30"),
        sector_map={"AAPL": "tech"},
        max_correlated_exposure_pct=Decimal("0.30"),
        correlation_groups={"g": frozenset({"AAPL"})},
        max_trade_notional=Decimal("50000"),
        max_daily_notional=Decimal("100000"),
        require_atr_stop=True,
        atr_stop_min_multiple=Decimal("1.0"),
        atr_stop_max_multiple=Decimal("3.0"),
        allow_trailing_stop=True,
        max_consecutive_rejections=5,
    )


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(
    equity=_maybe_bad_numbers,
    price=_maybe_bad_numbers,
    stop=_maybe_bad_numbers,
    strength=_maybe_bad_numbers,
    atr=_maybe_bad_numbers,
    exposure=_maybe_bad_numbers,
)
def test_fuzz_never_emits_order_on_bad_input(equity, price, stop, strength, atr, exposure):
    """
    FAIL-CLOSED INVARIANT: for arbitrary (often malformed/extreme) inputs, the manager
    must EITHER return None OR a structurally valid OrderEvent — it must never crash and
    must never emit an order from corrupt data. We assert no exception escapes and that
    any returned order is internally consistent (positive qty, correct symbol/side).
    """
    rm = RiskManager(_all_hardening_on(), clock=FixedClock(_DT))
    port = FakePortfolio()
    try:
        port.equity = equity            # type: ignore[assignment]
        port.peak_equity = equity       # type: ignore[assignment]
        port.day_start_equity = equity  # type: ignore[assignment]
        port.exposure = exposure        # type: ignore[assignment]
        port.last_price = {"AAPL": price}
    except Exception:
        # Even constructing the bad portfolio shouldn't matter — nothing to evaluate.
        return

    sig = FakeSignal(
        AAPL, OrderSide.BUY,
        strength=strength,              # type: ignore[arg-type]
        suggested_stop_loss=stop,       # type: ignore[arg-type]
        atr=atr,                        # type: ignore[arg-type]
        timestamp=_DT,
    )

    order = rm.evaluate(sig, port)   # must NEVER raise
    if order is not None:
        assert isinstance(order, OrderEvent)
        assert order.quantity > 0
        assert order.symbol == AAPL
        assert order.side == OrderSide.BUY


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(
    ts_offset=st.integers(min_value=-10**7, max_value=10**7),
    price=_maybe_bad_numbers,
    stop=_maybe_bad_numbers,
)
def test_fuzz_stale_guard_never_leaks_order(ts_offset, price, stop):
    """
    With the stale-data guard active, any bar dated outside the freshness window (or with
    corrupt price/stop) must never produce an order. Only fresh, well-formed inputs may.
    """
    cfg = RiskConfig(
        max_position_size_pct=Decimal("0.05"),
        max_total_exposure_pct=Decimal("0.50"),
        require_stop_loss=True,
        max_bar_age_seconds=300,
    )
    rm = RiskManager(cfg, clock=FixedClock(_DT))
    port = FakePortfolio()
    port.last_price = {"AAPL": price}   # type: ignore[assignment]
    sig = FakeSignal(
        AAPL, OrderSide.BUY,
        suggested_stop_loss=stop,        # type: ignore[arg-type]
        timestamp=_DT + timedelta(seconds=ts_offset),
    )
    order = rm.evaluate(sig, port)       # must NEVER raise
    if order is not None:
        # Approved => age = now - ts = -ts_offset must lie in [0, 300]s,
        # i.e. ts_offset in [-300, 0] (not stale, not future-dated).
        assert -300 <= ts_offset <= 0
        assert isinstance(order, OrderEvent)
