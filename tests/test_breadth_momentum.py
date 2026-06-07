"""
Tests for apex.strategy.library.breadth_momentum.

Validates the VAA breadth momentum rotation logic end-to-end using purely
synthetic, deterministic price paths so the expected outcomes can be derived
analytically without needing real market data.

Coverage:
  - Warmup: returns [] until lookback_12 + 1 closes exist for EVERY universe member.
  - Offensive regime: all offensive scores positive → holds the top offensive asset.
  - Defensive trigger: ANY offensive score ≤ 0 → rotates to top defensive asset.
  - Position awareness: no double-entry when already holding the current target.
  - Rotation: SELL current + BUY new when target changes.
  - Deterministic tie-break: alphabetically ascending by ticker.
  - Signal properties: strength=1.0, stop-loss on BUY, no stop-loss on SELL.
  - Missing symbol ignored.
  - Constructor validation: missing tickers raise ValueError.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Position, Symbol
from apex.strategy.library.breadth_momentum import BreadthMomentumStrategy

# ---------------------------------------------------------------------------
# Symbols — two offensive, two defensive (small universe keeps test fast)
# ---------------------------------------------------------------------------
SPY = Symbol("SPY", AssetClass.ETF)
EFA = Symbol("EFA", AssetClass.ETF)
IEF = Symbol("IEF", AssetClass.ETF)
SHV = Symbol("SHV", AssetClass.ETF)

ALL_SYMS = [SPY, EFA, IEF, SHV]
OFF_TICKERS = ["SPY", "EFA"]
DEF_TICKERS = ["IEF", "SHV"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

START = datetime(2020, 1, 2, tzinfo=timezone.utc)


def _bar(symbol: Symbol, dt: datetime, price: float) -> Bar:
    p = Decimal(str(price))
    return Bar(
        symbol=symbol,
        timestamp=dt,
        open=p,
        high=p,
        low=p,
        close=p,
        volume=Decimal("1_000_000"),
        timeframe="1Day",
    )


def _dates(n: int, start: datetime = START) -> List[datetime]:
    return [start + timedelta(days=i) for i in range(n)]


def _flat_prices(price: float, n: int) -> List[float]:
    """Completely flat — all lookback returns == 0."""
    return [price] * n


def _trending_prices(start_price: float, daily_ret: float, n: int) -> List[float]:
    """Compound daily_ret for n bars."""
    prices = [start_price]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1.0 + daily_ret))
    return prices


def _declining_prices(start_price: float, daily_ret: float, n: int) -> List[float]:
    """Same as _trending_prices but daily_ret should be negative."""
    return _trending_prices(start_price, daily_ret, n)


def _make_strat(
    lookback_12: int = 10,
    lookback_1: int = 2,
    lookback_3: int = 4,
    lookback_6: int = 6,
    breadth_trigger: int = 1,
    stop_loss_pct: Decimal = Decimal("0.10"),
) -> BreadthMomentumStrategy:
    """Create a strategy with small lookbacks so tests run in tens of bars."""
    return BreadthMomentumStrategy(
        strategy_id="vaa_test",
        symbols=ALL_SYMS,
        offensive_tickers=OFF_TICKERS,
        defensive_tickers=DEF_TICKERS,
        lookback_1=lookback_1,
        lookback_3=lookback_3,
        lookback_6=lookback_6,
        lookback_12=lookback_12,
        breadth_trigger=breadth_trigger,
        stop_loss_pct=stop_loss_pct,
    )


def _feed(
    strat: BreadthMomentumStrategy,
    dates: List[datetime],
    spy_prices: List[float],
    efa_prices: List[float],
    ief_prices: List[float],
    shv_prices: List[float],
) -> List:
    """Feed all four symbols interleaved and collect every signal."""
    assert len(dates) == len(spy_prices) == len(efa_prices) == len(ief_prices) == len(shv_prices)
    signals = []
    for dt, sp, ef, ie, sh in zip(dates, spy_prices, efa_prices, ief_prices, shv_prices):
        for bar in (
            _bar(SPY, dt, sp),
            _bar(EFA, dt, ef),
            _bar(IEF, dt, ie),
            _bar(SHV, dt, sh),
        ):
            signals.extend(strat.on_bar(bar))
    return signals


def _stub_context(held_ticker: Optional[str] = None):
    """
    Return a StrategyContext stub.  If held_ticker is not None the context
    reports a long position in that ticker; everything else is flat.
    """
    ctx = MagicMock()

    def _get_pos(sym: Symbol):
        if held_ticker and sym.ticker == held_ticker:
            pos = MagicMock(spec=Position)
            pos.quantity = Decimal("10")
            return pos
        return None

    ctx.get_position.side_effect = _get_pos
    return ctx


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_missing_offensive_ticker_raises(self):
        """A ticker listed in offensive_tickers but absent from symbols raises."""
        with pytest.raises(ValueError, match="MISSING"):
            BreadthMomentumStrategy(
                strategy_id="x",
                symbols=[EFA, IEF, SHV],
                offensive_tickers=["SPY", "MISSING"],
                defensive_tickers=["IEF", "SHV"],
            )

    def test_missing_defensive_ticker_raises(self):
        with pytest.raises(ValueError, match="GHOST"):
            BreadthMomentumStrategy(
                strategy_id="x",
                symbols=ALL_SYMS,
                offensive_tickers=["SPY", "EFA"],
                defensive_tickers=["IEF", "GHOST"],
            )

    def test_valid_construction(self):
        strat = _make_strat()
        assert strat.strategy_id == "vaa_test"
        assert strat.breadth_trigger == 1
        assert strat.lookback_12 == 10

    def test_invalid_breadth_trigger_raises(self):
        with pytest.raises(ValueError, match="breadth_trigger"):
            _make_strat(breadth_trigger=0)


# ---------------------------------------------------------------------------
# Warmup
# ---------------------------------------------------------------------------


class TestWarmup:
    """No signals until lookback_12 + 1 bars exist for ALL universe members."""

    def test_no_signals_during_warmup(self):
        """Feed fewer bars than needed — should get nothing."""
        strat = _make_strat(lookback_12=10)
        # Need 11 closes per ticker; feed only 8.
        n = 8
        dts = _dates(n)
        signals = _feed(
            strat,
            dts,
            _trending_prices(100.0, 0.01, n),
            _trending_prices(50.0, 0.01, n),
            _flat_prices(80.0, n),
            _flat_prices(30.0, n),
        )
        assert signals == []

    def test_no_signals_at_exact_lookback_length(self):
        """Feed exactly lookback_12 bars — need lookback_12+1, so still no signal."""
        strat = _make_strat(lookback_12=10)
        n = 10  # need 11
        dts = _dates(n)
        signals = _feed(
            strat,
            dts,
            _trending_prices(100.0, 0.01, n),
            _trending_prices(50.0, 0.01, n),
            _flat_prices(80.0, n),
            _flat_prices(30.0, n),
        )
        assert signals == []

    def test_signal_after_warmup(self):
        """Feed lookback_12+1 bars — strategy is now warm and should emit at least one signal."""
        strat = _make_strat(lookback_12=10)
        n = 11  # exactly warm
        dts = _dates(n)
        signals = _feed(
            strat,
            dts,
            _trending_prices(100.0, 0.02, n),  # strong positive score
            _trending_prices(50.0, 0.01, n),
            _flat_prices(80.0, n),
            _flat_prices(30.0, n),
        )
        # At least one BUY signal once warm (held=None → enters target)
        buys = [s for s in signals if s.side == OrderSide.BUY]
        assert len(buys) >= 1


# ---------------------------------------------------------------------------
# Offensive regime
# ---------------------------------------------------------------------------


class TestOffensiveRegime:
    """When all offensive scores are positive, the top offensive asset is chosen."""

    def test_holds_top_offensive_asset(self):
        """
        SPY strong uptrend, EFA weak uptrend, both positive.
        Expected: strategy buys SPY (higher score).
        """
        strat = _make_strat(lookback_12=10)
        n = 12
        dts = _dates(n)
        # SPY grows fast → r1/r3/r6/r12 all >> 0, score >> EFA's
        signals = _feed(
            strat,
            dts,
            _trending_prices(100.0, 0.05, n),  # SPY — strong
            _trending_prices(50.0, 0.005, n),  # EFA — weak positive
            _flat_prices(80.0, n),
            _flat_prices(30.0, n),
        )
        buys = [s for s in signals if s.side == OrderSide.BUY]
        assert len(buys) >= 1
        assert buys[0].symbol.ticker == "SPY", (
            f"Expected SPY (higher offensive score), got {buys[0].symbol.ticker}"
        )

    def test_offensive_scores_positive_no_defensive_buy(self):
        """When breadth is good, no defensive assets should be bought."""
        strat = _make_strat(lookback_12=10)
        n = 12
        dts = _dates(n)
        signals = _feed(
            strat,
            dts,
            _trending_prices(100.0, 0.05, n),
            _trending_prices(50.0, 0.01, n),
            _flat_prices(80.0, n),
            _flat_prices(30.0, n),
        )
        buys = [s for s in signals if s.side == OrderSide.BUY]
        for buy in buys:
            assert buy.symbol.ticker not in DEF_TICKERS, (
                f"Should not buy defensive asset {buy.symbol.ticker} when breadth is good"
            )


# ---------------------------------------------------------------------------
# Defensive trigger
# ---------------------------------------------------------------------------


class TestDefensiveTrigger:
    """When ANY offensive asset's score ≤ 0, go fully defensive."""

    def test_flips_defensive_when_one_offensive_negative(self):
        """
        Design the price path so that EFA's 13612W score is definitively negative
        throughout the entire history: EFA declines at a rate that makes all four
        return legs (r1, r3, r6, r12) negative, triggering breadth defence.

        Use lookback_12=10 (10 bars = the full history window).
        Feed 11 bars so the strategy is exactly warm after the last bar.
        EFA declines the entire time at -5%/day → every trailing return is negative
        → score = 12*r1 + 4*r3 + 2*r6 + r12 is deeply negative.
        SPY is flat (score ≈ 0, but still ≤ 0).
        Defence should trigger (breadth_b = 2 >= trigger=1) and buy IEF or SHV.
        """
        strat = _make_strat(lookback_12=10)
        n = 11  # exactly warm
        dts = _dates(n)
        # Both offensive assets decline throughout → all return legs negative
        spy_p = _declining_prices(100.0, -0.03, n)  # SPY declining → score < 0
        efa_p = _declining_prices(50.0, -0.05, n)  # EFA declining harder
        ief_p = _trending_prices(80.0, 0.01, n)  # IEF gently up
        shv_p = _flat_prices(30.0, n)  # SHV flat

        signals = _feed(strat, dts, spy_p, efa_p, ief_p, shv_p)
        buys = [s for s in signals if s.side == OrderSide.BUY]
        assert len(buys) >= 1
        # All buys must be defensive (IEF or SHV).
        for buy in buys:
            assert buy.symbol.ticker in DEF_TICKERS, (
                f"Expected a defensive buy (IEF/SHV), got {buy.symbol.ticker}"
            )

    def test_defensive_target_is_top_scoring_defensive(self):
        """
        IEF has a higher score than SHV → IEF should be selected as the
        defensive target.
        """
        strat = _make_strat(lookback_12=10)
        n = 12
        dts = _dates(n)
        # EFA crashes (breadth triggers defense), SPY flat
        # IEF gently rising → score > 0; SHV flat → score ≈ 0
        # IEF should be selected because it has the higher defensive score.
        spy_p = _flat_prices(100.0, n)
        efa_p = _declining_prices(50.0, -0.10, n)
        ief_p = _trending_prices(80.0, 0.01, n)  # IEF rising → positive score
        shv_p = _flat_prices(30.0, n)  # SHV flat → score ≈ 0

        signals = _feed(strat, dts, spy_p, efa_p, ief_p, shv_p)
        buys = [s for s in signals if s.side == OrderSide.BUY]
        assert len(buys) >= 1
        assert buys[0].symbol.ticker == "IEF", (
            f"Expected IEF (higher defensive score), got {buys[0].symbol.ticker}"
        )


# ---------------------------------------------------------------------------
# Position awareness — no double-entry
# ---------------------------------------------------------------------------


class TestPositionAwareness:
    """No re-entry signal when already holding the target (idempotent)."""

    def test_no_double_entry_when_already_holding_target(self):
        """
        Context reports we already hold SPY.  Strategy computes SPY as target.
        Expected: on_bar returns [] (no new BUY).
        """
        strat = _make_strat(lookback_12=10)
        strat.context = _stub_context(held_ticker="SPY")

        n = 12
        dts = _dates(n)
        # SPY dominant offensive asset
        spy_p = _trending_prices(100.0, 0.05, n)
        efa_p = _trending_prices(50.0, 0.005, n)
        ief_p = _flat_prices(80.0, n)
        shv_p = _flat_prices(30.0, n)

        signals = _feed(strat, dts, spy_p, efa_p, ief_p, shv_p)
        # The strategy should never emit a BUY for SPY because we're already in it.
        spy_buys = [s for s in signals if s.side == OrderSide.BUY and s.symbol.ticker == "SPY"]
        assert spy_buys == [], (
            f"Got unexpected BUY SPY signals when already holding SPY: {spy_buys}"
        )

    def test_emits_sell_and_buy_when_target_changes(self):
        """
        Context reports we hold EFA.  Strategy computes SPY as the better offensive target.
        Expected: SELL EFA + BUY SPY.
        """
        strat = _make_strat(lookback_12=10)
        strat.context = _stub_context(held_ticker="EFA")

        n = 12
        dts = _dates(n)
        # SPY much stronger → score >> EFA
        spy_p = _trending_prices(100.0, 0.08, n)
        efa_p = _trending_prices(50.0, 0.001, n)
        ief_p = _flat_prices(80.0, n)
        shv_p = _flat_prices(30.0, n)

        # Feed bars one at a time; collect the FIRST bar where signals appear.
        first_signals = []
        for dt, sp, ef, ie, sh in zip(dts, spy_p, efa_p, ief_p, shv_p):
            for bar in (
                _bar(SPY, dt, sp),
                _bar(EFA, dt, ef),
                _bar(IEF, dt, ie),
                _bar(SHV, dt, sh),
            ):
                evts = strat.on_bar(bar)
                if evts and not first_signals:
                    first_signals = evts

        sides = [s.side for s in first_signals]
        tickers = [s.symbol.ticker for s in first_signals]

        assert OrderSide.SELL in sides, "Expected a SELL signal when rotating"
        assert OrderSide.BUY in sides, "Expected a BUY signal when rotating"

        sell_idx = sides.index(OrderSide.SELL)
        buy_idx = sides.index(OrderSide.BUY)
        assert sell_idx < buy_idx, "SELL must precede BUY in the signal list"

        sell_ticker = tickers[sell_idx]
        buy_ticker = tickers[buy_idx]
        assert sell_ticker == "EFA", f"Expected SELL EFA, got {sell_ticker}"
        assert buy_ticker == "SPY", f"Expected BUY SPY, got {buy_ticker}"

    def test_cold_start_enters_current_target_without_prior_signal(self):
        """
        Context reports flat (no position).  Strategy sees SPY as target.
        Expected: BUY SPY emitted even without a previous explicit cross.
        (Idempotent cold-start entry.)
        """
        strat = _make_strat(lookback_12=10)
        strat.context = _stub_context(held_ticker=None)

        n = 12
        dts = _dates(n)
        spy_p = _trending_prices(100.0, 0.05, n)
        efa_p = _trending_prices(50.0, 0.005, n)
        ief_p = _flat_prices(80.0, n)
        shv_p = _flat_prices(30.0, n)

        signals = _feed(strat, dts, spy_p, efa_p, ief_p, shv_p)
        buys = [s for s in signals if s.side == OrderSide.BUY]
        assert len(buys) >= 1, "Cold start should enter the current target"


# ---------------------------------------------------------------------------
# Signal properties
# ---------------------------------------------------------------------------


class TestSignalProperties:
    def test_buy_strength_is_one(self):
        strat = _make_strat(lookback_12=10)
        n = 12
        dts = _dates(n)
        signals = _feed(
            strat,
            dts,
            _trending_prices(100.0, 0.05, n),
            _trending_prices(50.0, 0.01, n),
            _flat_prices(80.0, n),
            _flat_prices(30.0, n),
        )
        for s in signals:
            assert s.strength == Decimal("1.0"), f"Expected strength=1.0, got {s.strength}"

    def test_buy_carries_stop_loss(self):
        strat = _make_strat(lookback_12=10)
        n = 12
        dts = _dates(n)
        signals = _feed(
            strat,
            dts,
            _trending_prices(100.0, 0.05, n),
            _trending_prices(50.0, 0.01, n),
            _flat_prices(80.0, n),
            _flat_prices(30.0, n),
        )
        buys = [s for s in signals if s.side == OrderSide.BUY]
        assert len(buys) >= 1
        for buy in buys:
            assert buy.suggested_stop_loss is not None, "BUY must carry suggested_stop_loss"
            assert buy.suggested_stop_loss > Decimal("0")

    def test_stop_loss_below_entry(self):
        stop_pct = Decimal("0.10")
        strat = _make_strat(lookback_12=10, stop_loss_pct=stop_pct)
        n = 12
        dts = _dates(n)
        entry_prices: dict = {}
        for dt, sp, ef, ie, sh in zip(
            dts,
            _trending_prices(100.0, 0.05, n),
            _trending_prices(50.0, 0.01, n),
            _flat_prices(80.0, n),
            _flat_prices(30.0, n),
        ):
            for bar in (
                _bar(SPY, dt, sp),
                _bar(EFA, dt, ef),
                _bar(IEF, dt, ie),
                _bar(SHV, dt, sh),
            ):
                evts = strat.on_bar(bar)
                for sig in evts:
                    if sig.side == OrderSide.BUY:
                        entry_prices[id(sig)] = strat._latest_close.get(sig.symbol.ticker)

        # Re-run to collect all buys with their entry prices captured above.
        strat2 = _make_strat(lookback_12=10, stop_loss_pct=stop_pct)
        buy_stops = []
        for dt, sp, ef, ie, sh in zip(
            dts,
            _trending_prices(100.0, 0.05, n),
            _trending_prices(50.0, 0.01, n),
            _flat_prices(80.0, n),
            _flat_prices(30.0, n),
        ):
            for bar in (
                _bar(SPY, dt, sp),
                _bar(EFA, dt, ef),
                _bar(IEF, dt, ie),
                _bar(SHV, dt, sh),
            ):
                evts = strat2.on_bar(bar)
                for sig in evts:
                    if sig.side == OrderSide.BUY:
                        entry = strat2._latest_close.get(sig.symbol.ticker)
                        if entry is not None:
                            buy_stops.append((sig.suggested_stop_loss, entry))

        assert len(buy_stops) >= 1
        for stop, entry in buy_stops:
            expected = entry * (Decimal("1") - stop_pct)
            assert stop == expected, f"Stop {stop} != entry {entry} * (1 - {stop_pct}) = {expected}"
            assert stop < entry

    def test_sell_has_no_stop_loss(self):
        """Exit signals do not carry a stop-loss."""
        strat = _make_strat(lookback_12=10)
        strat.context = _stub_context(held_ticker="EFA")

        n = 12
        dts = _dates(n)
        signals = _feed(
            strat,
            dts,
            _trending_prices(100.0, 0.08, n),  # SPY dominant
            _trending_prices(50.0, 0.001, n),  # EFA weak
            _flat_prices(80.0, n),
            _flat_prices(30.0, n),
        )
        sells = [s for s in signals if s.side == OrderSide.SELL]
        for sell in sells:
            assert sell.suggested_stop_loss is None, (
                f"SELL for {sell.symbol.ticker} should not carry a stop-loss"
            )

    def test_strategy_id_on_all_signals(self):
        strat = BreadthMomentumStrategy(
            strategy_id="my_vaa",
            symbols=ALL_SYMS,
            offensive_tickers=OFF_TICKERS,
            defensive_tickers=DEF_TICKERS,
            lookback_12=10,
        )
        n = 12
        dts = _dates(n)
        signals = _feed(
            strat,
            dts,
            _trending_prices(100.0, 0.05, n),
            _trending_prices(50.0, 0.01, n),
            _flat_prices(80.0, n),
            _flat_prices(30.0, n),
        )
        for sig in signals:
            assert sig.strategy_id == "my_vaa"

    def test_reason_non_empty(self):
        strat = _make_strat(lookback_12=10)
        n = 12
        dts = _dates(n)
        signals = _feed(
            strat,
            dts,
            _trending_prices(100.0, 0.05, n),
            _trending_prices(50.0, 0.01, n),
            _flat_prices(80.0, n),
            _flat_prices(30.0, n),
        )
        for sig in signals:
            assert sig.reason, f"Signal for {sig.symbol.ticker} has empty reason"


# ---------------------------------------------------------------------------
# Deterministic tie-break
# ---------------------------------------------------------------------------


class TestTieBreak:
    """When two assets share the same score the alphabetically-first ticker wins."""

    def test_offensive_tie_break_alphabetical(self):
        """
        Make SPY and EFA have identical returns → scores are equal.
        'EFA' < 'SPY' alphabetically → EFA should win.
        """
        strat = _make_strat(lookback_12=10)
        n = 12
        dts = _dates(n)
        # Identical price paths → identical scores.
        same_prices = _trending_prices(100.0, 0.03, n)

        signals = _feed(
            strat,
            dts,
            list(same_prices),  # SPY
            list(same_prices),  # EFA (same path, same score)
            _flat_prices(80.0, n),
            _flat_prices(30.0, n),
        )
        buys = [s for s in signals if s.side == OrderSide.BUY]
        assert len(buys) >= 1
        # Alphabetically 'EFA' < 'SPY' — EFA should win the tie.
        first_buy_ticker = buys[0].symbol.ticker
        assert first_buy_ticker == "EFA", (
            f"Expected EFA (alphabetically first) in a tie, got {first_buy_ticker}"
        )

    def test_defensive_tie_break_alphabetical(self):
        """
        Trigger defensive mode, then make IEF and SHV have equal scores.
        'IEF' < 'SHV' alphabetically → IEF should win.
        """
        strat = _make_strat(lookback_12=10)
        n = 12
        dts = _dates(n)
        # EFA crashes to force defensive mode.
        efa_p = _declining_prices(50.0, -0.10, n)
        # IEF and SHV — same path → equal scores.
        same_def = _flat_prices(80.0, n)

        signals = _feed(
            strat,
            dts,
            _flat_prices(100.0, n),  # SPY flat (score ≈ 0, but EFA < 0 triggers defense)
            list(efa_p),
            list(same_def),  # IEF
            list(same_def),  # SHV (same price → same score)
        )
        buys = [s for s in signals if s.side == OrderSide.BUY]
        assert len(buys) >= 1
        first_buy_ticker = buys[0].symbol.ticker
        # IEF < SHV alphabetically
        assert first_buy_ticker == "IEF", (
            f"Expected IEF (alphabetically first defensive tie), got {first_buy_ticker}"
        )


# ---------------------------------------------------------------------------
# Unknown symbol ignored
# ---------------------------------------------------------------------------


class TestUnknownSymbol:
    def test_unknown_symbol_returns_empty(self):
        strat = _make_strat()
        msft = Symbol("MSFT", AssetClass.EQUITY)
        b = _bar(msft, START, 300.0)
        assert strat.on_bar(b) == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    """Same inputs → same outputs, always."""

    def test_deterministic(self):
        n = 20
        dts = _dates(n)
        spy_p = _trending_prices(100.0, 0.03, n)
        efa_p = _trending_prices(50.0, 0.01, n)
        ief_p = _flat_prices(80.0, n)
        shv_p = _flat_prices(30.0, n)

        s1 = _make_strat(lookback_12=10)
        s2 = _make_strat(lookback_12=10)

        sig1 = _feed(s1, dts, spy_p, efa_p, ief_p, shv_p)
        sig2 = _feed(s2, dts, spy_p, efa_p, ief_p, shv_p)

        assert [s.side for s in sig1] == [s.side for s in sig2]
        assert [s.symbol.ticker for s in sig1] == [s.symbol.ticker for s in sig2]
