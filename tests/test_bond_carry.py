"""
Tests for apex.strategy.library.bond_carry.

BondCarryStrategy is position-aware: it compares desired regime to actual holdings
(via StrategyContext) and emits delta signals.  Tests drive it through a lightweight
harness that simulates broker reconciliation — the same pattern used by
test_multi_asset_trend and test_cross_asset_value.

Covered:
  1. Constructor validation (bad inputs raise ValueError).
  2. Warmup: returns [] until BOTH yield tickers have been seen.
  3. Positive slope → enters the duration ETF.
  4. Inversion → exits duration and enters the defensive ETF.
  5. No double-entry when already holding the correct sleeve (position-aware).
  6. Never emits a SignalEvent for a yield ticker (^TNX / ^IRX).
  7. Hysteresis buffer: a slope inside (0, buffer] does NOT flip to risk-on from
     risk-off (prevents whipsaw on tiny positive slopes).
  8. SELL is full-conviction (strength 1.0).
  9. Entry carry a suggested_stop_loss, exit does not require one.
 10. Unknown symbol (not in universe) returns [].
 11. Determinism: identical inputs produce identical outputs.
 12. Cold-start correctness: if we are already in the correct sleeve when the
     strategy starts, it does not re-enter (idempotent).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.bond_carry import BondCarryStrategy

# ---------------------------------------------------------------------------
# Fixtures — symbols
# ---------------------------------------------------------------------------

LONG_ETF = Symbol("IEF", AssetClass.ETF)  # duration sleeve (tradeable)
SHORT_ETF = Symbol("SHV", AssetClass.ETF)  # defensive sleeve (tradeable)
TNX = Symbol("^TNX", AssetClass.ETF)  # 10Y yield, non-tradeable ride-along
IRX = Symbol("^IRX", AssetClass.ETF)  # 3M yield, non-tradeable ride-along
OTHER = Symbol("SPY", AssetClass.ETF)  # not in universe at all

ALL_SYMS = [LONG_ETF, SHORT_ETF, TNX, IRX]

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar(sym: Symbol, price: float, t: datetime | None = None) -> Bar:
    """Build a minimal Bar with open==high==low==close==price."""
    p = Decimal(str(price))
    return Bar(
        symbol=sym,
        timestamp=t or T0,
        open=p,
        high=p,
        low=p,
        close=p,
        volume=Decimal("100000"),
    )


def _make_strat(
    buffer: Decimal = Decimal("0"),
    stop_pct: Decimal = Decimal("0.05"),
) -> BondCarryStrategy:
    return BondCarryStrategy(
        "carry_test",
        ALL_SYMS,
        long_etf=LONG_ETF,
        short_etf=SHORT_ETF,
        inversion_buffer=buffer,
        stop_loss_pct=stop_pct,
    )


class _Harness:
    """
    Minimal engine stand-in.  Binds a StrategyContext, keeps a simulated portfolio
    (held: dict[ticker, qty]), and applies emitted signals as immediate fills so the
    next on_bar() call sees the updated positions.
    """

    def __init__(self, strat: BondCarryStrategy) -> None:
        self.strat = strat
        self.ctx = StrategyContext()
        strat.bind_context(self.ctx)
        self.held: dict[str, Decimal] = {}
        self._day = 0

    def _ts(self) -> datetime:
        return T0 + timedelta(days=self._day)

    def _refresh(self) -> None:
        self.ctx.sync_state(
            positions={k: SimpleNamespace(quantity=q) for k, q in self.held.items() if q > 0}
        )

    def push(self, sym: Symbol, price: float) -> list:
        """Push one bar; refresh context; apply fills; return signals."""
        self._refresh()
        sigs = self.strat.on_bar(_bar(sym, price, self._ts()))
        self._day += 1
        for s in sigs:
            self.held[s.symbol.ticker] = Decimal("1") if s.side == OrderSide.BUY else Decimal("0")
        return sigs

    def set_yields(self, tnx: float, irx: float) -> None:
        """Inject both yield bars (returns discarded — they should be [])."""
        self.push(TNX, tnx)
        self.push(IRX, irx)


# ---------------------------------------------------------------------------
# 1. Constructor validation
# ---------------------------------------------------------------------------


def test_long_etf_not_in_symbols_raises():
    extra = Symbol("TLT", AssetClass.ETF)
    with pytest.raises(ValueError, match="long_etf"):
        BondCarryStrategy("s", ALL_SYMS, long_etf=extra, short_etf=SHORT_ETF)


def test_short_etf_not_in_symbols_raises():
    extra = Symbol("BIL", AssetClass.ETF)
    with pytest.raises(ValueError, match="short_etf"):
        BondCarryStrategy("s", ALL_SYMS, long_etf=LONG_ETF, short_etf=extra)


def test_same_etf_raises():
    with pytest.raises(ValueError, match="different"):
        BondCarryStrategy("s", ALL_SYMS, long_etf=LONG_ETF, short_etf=LONG_ETF)


def test_negative_buffer_raises():
    with pytest.raises(ValueError, match="inversion_buffer"):
        BondCarryStrategy(
            "s",
            ALL_SYMS,
            long_etf=LONG_ETF,
            short_etf=SHORT_ETF,
            inversion_buffer=Decimal("-0.01"),
        )


def test_bad_stop_pct_raises():
    with pytest.raises(ValueError, match="stop_loss_pct"):
        BondCarryStrategy(
            "s",
            ALL_SYMS,
            long_etf=LONG_ETF,
            short_etf=SHORT_ETF,
            stop_loss_pct=Decimal("0"),
        )
    with pytest.raises(ValueError, match="stop_loss_pct"):
        BondCarryStrategy(
            "s",
            ALL_SYMS,
            long_etf=LONG_ETF,
            short_etf=SHORT_ETF,
            stop_loss_pct=Decimal("1"),
        )


# ---------------------------------------------------------------------------
# 2. Warmup: no signal until BOTH yields have been seen
# ---------------------------------------------------------------------------


def test_warmup_no_signal_before_tnx():
    h = _Harness(_make_strat())
    # Only IRX delivered yet — should still be in warmup.
    sigs = h.push(IRX, 5.0)
    assert sigs == [], "should return [] before ^TNX seen"
    # ETF bar arrives before ^TNX — still warmup.
    sigs2 = h.push(LONG_ETF, 100.0)
    assert sigs2 == [], "ETF bar before both yields → []"


def test_warmup_no_signal_before_irx():
    h = _Harness(_make_strat())
    sigs = h.push(TNX, 4.0)
    assert sigs == [], "should return [] before ^IRX seen"
    sigs2 = h.push(LONG_ETF, 100.0)
    assert sigs2 == [], "ETF bar before both yields → []"


def test_warmup_clears_after_both_yields():
    h = _Harness(_make_strat())
    h.push(TNX, 4.5)  # 10Y yield 4.5%
    h.push(IRX, 5.2)  # 3M yield 5.2% → inverted at this point
    # Now the ETF bar for SHORT_ETF should produce a BUY (inverted curve)
    sigs = h.push(SHORT_ETF, 100.0)
    assert len(sigs) == 1
    assert sigs[0].side == OrderSide.BUY


# ---------------------------------------------------------------------------
# 3. Positive slope → enters duration ETF
# ---------------------------------------------------------------------------


def test_positive_slope_buys_duration_etf():
    h = _Harness(_make_strat())
    h.push(TNX, 4.5)  # 10Y
    h.push(IRX, 0.5)  # 3M → slope = 4.0%  → risk-on
    sigs = h.push(LONG_ETF, 100.0)
    assert len(sigs) == 1
    buy = sigs[0]
    assert buy.side == OrderSide.BUY
    assert buy.symbol.ticker == "IEF"
    assert buy.strategy_id == "carry_test"


def test_positive_slope_does_not_buy_defensive():
    h = _Harness(_make_strat())
    h.push(TNX, 4.5)
    h.push(IRX, 0.5)  # slope = +4.0%
    sigs = h.push(SHORT_ETF, 50.0)
    assert sigs == [], "risk-on → no buy of defensive ETF"


def test_buy_has_stop_loss():
    h = _Harness(_make_strat(stop_pct=Decimal("0.05")))
    h.push(TNX, 4.5)
    h.push(IRX, 0.5)
    sigs = h.push(LONG_ETF, 100.0)
    assert len(sigs) == 1
    buy = sigs[0]
    assert buy.suggested_stop_loss is not None
    assert buy.suggested_stop_loss == Decimal("95.00")


# ---------------------------------------------------------------------------
# 4. Inversion → exits duration, enters defensive
# ---------------------------------------------------------------------------


def test_inversion_exits_duration_enters_defensive():
    h = _Harness(_make_strat())
    # Step 1: positive slope → enter duration
    h.push(TNX, 4.5)
    h.push(IRX, 0.5)
    h.push(LONG_ETF, 100.0)  # enters IEF
    h.push(SHORT_ETF, 50.0)  # no signal (risk-on)

    # Step 2: yield curve inverts
    h.push(TNX, 4.5)
    h.push(IRX, 5.0)  # slope = -0.5% → risk-off

    sell_sigs = h.push(LONG_ETF, 100.0)
    assert len(sell_sigs) == 1
    assert sell_sigs[0].side == OrderSide.SELL
    assert sell_sigs[0].symbol.ticker == "IEF"

    buy_sigs = h.push(SHORT_ETF, 50.0)
    assert len(buy_sigs) == 1
    assert buy_sigs[0].side == OrderSide.BUY
    assert buy_sigs[0].symbol.ticker == "SHV"


def test_sell_is_full_conviction():
    h = _Harness(_make_strat())
    h.push(TNX, 4.5)
    h.push(IRX, 0.5)
    h.push(LONG_ETF, 100.0)
    # Invert
    h.push(TNX, 4.5)
    h.push(IRX, 5.0)
    sells = h.push(LONG_ETF, 100.0)
    assert sells and sells[0].strength == Decimal("1.0")


# ---------------------------------------------------------------------------
# 5. Position-aware: no double-entry when already holding the right sleeve
# ---------------------------------------------------------------------------


def test_no_double_entry_duration():
    h = _Harness(_make_strat())
    h.push(TNX, 4.5)
    h.push(IRX, 0.5)

    # First bar → BUY
    first = h.push(LONG_ETF, 100.0)
    assert len(first) == 1 and first[0].side == OrderSide.BUY

    # Same yield, same ETF bar again — already held → no signal
    h.push(TNX, 4.5)
    h.push(IRX, 0.5)
    second = h.push(LONG_ETF, 101.0)
    assert second == [], "should not re-buy when already long duration ETF"


def test_no_double_entry_defensive():
    h = _Harness(_make_strat())
    h.push(TNX, 4.5)
    h.push(IRX, 5.0)  # inverted

    first = h.push(SHORT_ETF, 50.0)
    assert len(first) == 1 and first[0].side == OrderSide.BUY

    h.push(TNX, 4.5)
    h.push(IRX, 5.0)
    second = h.push(SHORT_ETF, 50.5)
    assert second == [], "should not re-buy when already long defensive ETF"


# ---------------------------------------------------------------------------
# 6. Never emits a SignalEvent for a yield ticker
# ---------------------------------------------------------------------------


def test_yield_bars_return_empty_list():
    strat = _make_strat()
    tnx_sigs = strat.on_bar(_bar(TNX, 4.5))
    irx_sigs = strat.on_bar(_bar(IRX, 0.5))
    assert tnx_sigs == [], "^TNX bar must never produce signals"
    assert irx_sigs == [], "^IRX bar must never produce signals"


def test_no_signal_for_yield_ticker_ever():
    """Drive the full lifecycle and confirm no signal carries a yield symbol."""
    h = _Harness(_make_strat())
    h.push(TNX, 4.5)
    h.push(IRX, 0.5)
    h.push(LONG_ETF, 100.0)
    h.push(SHORT_ETF, 50.0)
    h.push(TNX, 4.5)
    h.push(IRX, 5.0)  # invert
    h.push(LONG_ETF, 100.0)
    h.push(SHORT_ETF, 50.0)

    # Re-drive with explicit checks
    all_signals = []
    strat = _make_strat()
    ctx = StrategyContext()
    strat.bind_context(ctx)
    for sym, px in [
        (TNX, 4.5),
        (IRX, 0.5),
        (LONG_ETF, 100.0),
        (SHORT_ETF, 50.0),
        (TNX, 4.5),
        (IRX, 5.0),
        (LONG_ETF, 100.0),
        (SHORT_ETF, 50.0),
    ]:
        all_signals.extend(strat.on_bar(_bar(sym, px)))

    yield_tickers = {"^TNX", "^IRX"}
    assert all(s.symbol.ticker not in yield_tickers for s in all_signals), (
        "A signal was emitted for a yield ticker — must never happen"
    )


# ---------------------------------------------------------------------------
# 7. Hysteresis buffer: tiny positive slope does NOT flip to risk-on from risk-off
# ---------------------------------------------------------------------------


def test_buffer_prevents_flip_on_small_positive_slope():
    """
    With buffer=0.25 (25 bps), a slope of +0.10 is inside the dead-band.
    Starting from a risk-off (inverted) regime, the strategy must NOT flip
    to risk-on until slope exceeds 0.25.
    """
    buf = Decimal("0.25")
    h = _Harness(_make_strat(buffer=buf))

    # Start inverted → risk-off
    h.push(TNX, 4.5)
    h.push(IRX, 5.0)  # slope = -0.5 → risk-off
    buy_def = h.push(SHORT_ETF, 50.0)
    assert len(buy_def) == 1 and buy_def[0].side == OrderSide.BUY

    # Curve improves slightly: slope = +0.10 — inside buffer, must stay risk-off
    h.push(TNX, 5.1)
    h.push(IRX, 5.0)  # slope = +0.10 < buffer 0.25 → still risk-off
    no_flip = h.push(LONG_ETF, 100.0)
    assert no_flip == [], "slope inside hysteresis buffer must not trigger duration entry"

    # Defensive ETF bar: should NOT exit (still risk-off)
    still_held = h.push(SHORT_ETF, 50.5)
    assert still_held == [], "defensive ETF should remain held inside buffer zone"


def test_buffer_allows_entry_above_threshold():
    """
    With buffer=0.25, a slope of +0.30 (> buffer) must trigger a risk-on entry.
    """
    buf = Decimal("0.25")
    h = _Harness(_make_strat(buffer=buf))

    # Start inverted
    h.push(TNX, 4.5)
    h.push(IRX, 5.0)
    h.push(SHORT_ETF, 50.0)  # enters defensive

    # Curve steepens above buffer
    h.push(TNX, 5.3)
    h.push(IRX, 5.0)  # slope = +0.30 > buffer 0.25 → risk-on

    # Duration ETF bar must trigger BUY
    buy_dur = h.push(LONG_ETF, 100.0)
    assert len(buy_dur) == 1 and buy_dur[0].side == OrderSide.BUY
    assert buy_dur[0].symbol.ticker == "IEF"


def test_zero_slope_is_risk_off():
    """slope exactly 0 → risk-off (inverted/flat boundary is inclusive)."""
    h = _Harness(_make_strat())
    h.push(TNX, 4.5)
    h.push(IRX, 4.5)  # slope = 0.0 → risk-off
    dur_sigs = h.push(LONG_ETF, 100.0)
    def_sigs = h.push(SHORT_ETF, 50.0)
    assert all(s.side != OrderSide.BUY or s.symbol.ticker == "SHV" for s in def_sigs)
    assert dur_sigs == [], "flat curve (slope=0) should not buy duration"


# ---------------------------------------------------------------------------
# 8-9. Sell conviction + stop-loss carried on buy only
# ---------------------------------------------------------------------------


def test_sell_has_no_required_stop():
    """Exits should not carry a stop; they are full-position unwinds."""
    h = _Harness(_make_strat())
    h.push(TNX, 4.5)
    h.push(IRX, 0.5)
    h.push(LONG_ETF, 100.0)
    h.push(TNX, 4.5)
    h.push(IRX, 5.0)
    sells = h.push(LONG_ETF, 100.0)
    assert sells and sells[0].suggested_stop_loss is None


# ---------------------------------------------------------------------------
# 10. Unknown symbol (not in universe) → []
# ---------------------------------------------------------------------------


def test_unknown_symbol_returns_empty():
    strat = _make_strat()
    sigs = strat.on_bar(_bar(OTHER, 450.0))
    assert sigs == []


# ---------------------------------------------------------------------------
# 11. Determinism
# ---------------------------------------------------------------------------


def test_deterministic():
    def _run():
        h = _Harness(_make_strat())
        out = []
        sequence = [
            (TNX, 4.5),
            (IRX, 0.5),
            (LONG_ETF, 100.0),
            (SHORT_ETF, 50.0),
            (TNX, 4.5),
            (IRX, 5.0),
            (LONG_ETF, 100.0),
            (SHORT_ETF, 50.0),
            (TNX, 5.5),
            (IRX, 0.5),
            (LONG_ETF, 101.0),
            (SHORT_ETF, 50.5),
        ]
        for sym, px in sequence:
            out.extend([(s.symbol.ticker, s.side, s.strength) for s in h.push(sym, px)])
        return out

    assert _run() == _run()


# ---------------------------------------------------------------------------
# 12. Cold-start correctness: idempotent when already in the right sleeve
# ---------------------------------------------------------------------------


def test_cold_start_already_holding_duration_no_reentry():
    """
    Simulate a restart: context says we already hold IEF, curve is still positive.
    The strategy must NOT emit a second BUY.
    """
    strat = _make_strat()
    ctx = StrategyContext()
    strat.bind_context(ctx)

    # Inject a pre-existing IEF position (as if we were holding it before restart).
    ctx.sync_state(positions={"IEF": SimpleNamespace(quantity=Decimal("10"))})

    # Deliver both yields (positive slope) and then the IEF bar.
    strat.on_bar(_bar(TNX, 4.5))
    strat.on_bar(_bar(IRX, 0.5))
    sigs = strat.on_bar(_bar(LONG_ETF, 100.0))
    assert sigs == [], "should not re-enter IEF when already held on cold start"


def test_cold_start_wrong_sleeve_corrects():
    """
    Simulate a restart where we somehow hold the DEFENSIVE sleeve but the curve is
    now positive. The strategy must SELL SHV and BUY IEF to restore the correct state.
    """
    strat = _make_strat()
    ctx = StrategyContext()
    strat.bind_context(ctx)

    # Pre-existing SHV position but curve is now positive.
    ctx.sync_state(positions={"SHV": SimpleNamespace(quantity=Decimal("10"))})

    strat.on_bar(_bar(TNX, 4.5))
    strat.on_bar(_bar(IRX, 0.5))  # slope = +4.0% → risk-on

    # IEF bar: should enter IEF (flat + risk-on)
    ief_sigs = strat.on_bar(_bar(LONG_ETF, 100.0))
    assert len(ief_sigs) == 1 and ief_sigs[0].side == OrderSide.BUY

    # Refresh context to reflect SHV held (the harness did not clear it)
    ctx.sync_state(positions={"SHV": SimpleNamespace(quantity=Decimal("10"))})

    # SHV bar: should SELL SHV (held + risk-on → unwanted)
    shv_sigs = strat.on_bar(_bar(SHORT_ETF, 50.0))
    assert len(shv_sigs) == 1 and shv_sigs[0].side == OrderSide.SELL
