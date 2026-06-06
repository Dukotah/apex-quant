"""
Tests for apex.strategy.library.dual_momentum.

Validates the GEM (Global Equities Momentum) logic end-to-end:
  - No signals during warmup
  - Correct asset selection (SPY vs intl vs bonds)
  - SELL + BUY emitted on rotation; nothing emitted when holding unchanged
  - No signals mid-month (only at first bar of a new month)
  - Stop-loss attached to every BUY
  - Determinism: same inputs → same outputs
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import List

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.library.dual_momentum import DualMomentumStrategy

# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------
SPY = Symbol("SPY", AssetClass.ETF)
EFA = Symbol("EFA", AssetClass.ETF)
AGG = Symbol("AGG", AssetClass.ETF)

SYMBOLS = [SPY, EFA, AGG]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bar(symbol: Symbol, dt: datetime, price: float) -> Bar:
    p = Decimal(str(price))
    return Bar(
        symbol=symbol,
        timestamp=dt,
        open=p,
        high=p,
        low=p,
        close=p,
        volume=Decimal("1000000"),
        timeframe="1Day",
    )


def _default_strat(lookback_window: int = 252) -> DualMomentumStrategy:
    return DualMomentumStrategy(
        strategy_id="gem_test",
        symbols=SYMBOLS,
        equity_ticker="SPY",
        intl_ticker="EFA",
        bond_ticker="AGG",
        lookback_window=lookback_window,
    )


def _build_daily_dates(start: datetime, n_days: int) -> List[datetime]:
    """Return n_days consecutive calendar days starting from `start`."""
    return [start + timedelta(days=i) for i in range(n_days)]


def _feed_three_streams(
    strat: DualMomentumStrategy,
    dates: List[datetime],
    spy_prices: List[float],
    efa_prices: List[float],
    agg_prices: List[float],
) -> List:
    """
    Feed bars from all three symbols in chronological order (interleaved by date)
    and return every signal emitted.
    """
    assert len(dates) == len(spy_prices) == len(efa_prices) == len(agg_prices)
    signals = []
    for dt, sp, ef, ag in zip(dates, spy_prices, efa_prices, agg_prices):
        for bar in (
            _make_bar(SPY, dt, sp),
            _make_bar(EFA, dt, ef),
            _make_bar(AGG, dt, ag),
        ):
            signals.extend(strat.on_bar(bar))
    return signals


# ---------------------------------------------------------------------------
# Price generators
# ---------------------------------------------------------------------------


def _trending_prices(start: float, daily_return: float, n: int) -> List[float]:
    """Compound daily_return for n days starting at `start`."""
    prices = [start]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1.0 + daily_return))
    return prices


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_missing_equity_ticker_raises(self):
        with pytest.raises(ValueError, match="equity"):
            DualMomentumStrategy(
                strategy_id="x",
                symbols=[EFA, AGG],
                equity_ticker="SPY",
                intl_ticker="EFA",
                bond_ticker="AGG",
            )

    def test_missing_intl_ticker_raises(self):
        with pytest.raises(ValueError, match="international"):
            DualMomentumStrategy(
                strategy_id="x",
                symbols=[SPY, AGG],
                equity_ticker="SPY",
                intl_ticker="EFA",
                bond_ticker="AGG",
            )

    def test_missing_bond_ticker_raises(self):
        with pytest.raises(ValueError, match="bond"):
            DualMomentumStrategy(
                strategy_id="x",
                symbols=[SPY, EFA],
                equity_ticker="SPY",
                intl_ticker="EFA",
                bond_ticker="AGG",
            )

    def test_valid_construction_succeeds(self):
        strat = _default_strat()
        assert strat.strategy_id == "gem_test"
        assert strat.equity_ticker == "SPY"


class TestWarmup:
    """No signals until lookback_window+1 bars for both equity+intl exist."""

    def test_no_signals_during_warmup(self):
        # Use a small lookback to keep the test fast; warmup still requires N+1 bars.
        strat = _default_strat(lookback_window=20)
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        # Feed only 15 days — well short of 21 required.
        dates = _build_daily_dates(start, 15)
        spy = _trending_prices(400.0, 0.001, 15)
        efa = _trending_prices(50.0, 0.001, 15)
        agg = _trending_prices(100.0, 0.0001, 15)
        signals = _feed_three_streams(strat, dates, spy, efa, agg)
        assert signals == []

    def test_no_signals_before_lookback_plus_one(self):
        strat = _default_strat(lookback_window=20)
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        # Feed exactly lookback_window bars (20) — not yet warm (need 21).
        dates = _build_daily_dates(start, 20)
        spy = _trending_prices(400.0, 0.001, 20)
        efa = _trending_prices(50.0, 0.001, 20)
        agg = _trending_prices(100.0, 0.0001, 20)
        signals = _feed_three_streams(strat, dates, spy, efa, agg)
        assert signals == []


class TestMonthBoundary:
    """Signals only at the first bar of a new month after warmup."""

    def test_signals_only_at_month_boundary(self):
        """
        Feed 2+ months of data; all signals should fall on the first day of a
        calendar month.
        """
        strat = _default_strat(lookback_window=20)
        # Start 2024-01-02, feed ~70 calendar days (spans >2 months).
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        n = 70
        dates = _build_daily_dates(start, n)
        spy = _trending_prices(400.0, 0.003, n)  # strongly up
        efa = _trending_prices(50.0, 0.001, n)  # moderately up
        agg = _trending_prices(100.0, 0.0001, n)

        signals = _feed_three_streams(strat, dates, spy, efa, agg)
        # Every signal must have a timestamp that is the first of its month
        # relative to the first bar of that month we actually fed.
        # More precisely: no signal date can share a (year, month) with any
        # EARLIER bar of the same month in our date list.
        month_first_seen: dict = {}
        for dt in dates:
            key = (dt.year, dt.month)
            if key not in month_first_seen:
                month_first_seen[key] = dt

        for sig in signals:
            ts = sig.timestamp
            assert ts is not None
            key = (ts.year, ts.month)
            assert ts == month_first_seen[key], (
                f"Signal at {ts} is not the first bar of its month "
                f"(expected {month_first_seen[key]})"
            )

    def test_no_mid_month_signals(self):
        """After a rebalance on day 1 of a month, days 2-N emit nothing."""
        strat = _default_strat(lookback_window=20)
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        n = 40
        dates = _build_daily_dates(start, n)
        spy = _trending_prices(400.0, 0.003, n)
        efa = _trending_prices(50.0, 0.001, n)
        agg = _trending_prices(100.0, 0.0001, n)

        # Track which days produce signals
        sig_dates = []
        for dt, sp, ef, ag in zip(dates, spy, efa, agg):
            for bar in (_make_bar(SPY, dt, sp), _make_bar(EFA, dt, ef), _make_bar(AGG, dt, ag)):
                evts = strat.on_bar(bar)
                if evts:
                    sig_dates.append(dt)

        # For any two signals in the same calendar month, they must share the
        # same date (both on day 1 of that month). No mid-month signals.
        seen_months: dict = {}
        for dt in sig_dates:
            key = (dt.year, dt.month)
            if key not in seen_months:
                seen_months[key] = dt
            else:
                assert dt == seen_months[key], (
                    f"Mid-month signal detected on {dt} (first was {seen_months[key]})"
                )


class TestGEMLogic:
    """Core selection rules."""

    def _run_scenario(
        self,
        lookback_window: int,
        n_warmup: int,
        n_scenario: int,
        spy_warmup_ret: float,
        efa_warmup_ret: float,
        spy_scenario_ret: float,
        efa_scenario_ret: float,
        agg_daily_ret: float = 0.0001,
    ):
        """
        Build a scenario:
          - First n_warmup days: SPY/EFA trending at given daily returns (for warmup).
          - Next n_scenario days: new trend rates (this is where we look for signals).
        Returns (strat, all_signals, scenario_signals).
        """
        strat = DualMomentumStrategy(
            strategy_id="gem",
            symbols=SYMBOLS,
            equity_ticker="SPY",
            intl_ticker="EFA",
            bond_ticker="AGG",
            lookback_window=lookback_window,
        )
        # Warmup phase — prices trending steadily
        w_spy = _trending_prices(400.0, spy_warmup_ret, n_warmup)
        w_efa = _trending_prices(50.0, efa_warmup_ret, n_warmup)
        w_agg = _trending_prices(100.0, agg_daily_ret, n_warmup)

        start = datetime(2023, 1, 2, tzinfo=timezone.utc)
        warmup_dates = _build_daily_dates(start, n_warmup)

        # Scenario phase — prices at new rates
        s_start = start + timedelta(days=n_warmup)
        # Ensure scenario starts on the first day of a new month by adjusting.
        # We simply feed scenario from wherever the warmup ended.
        s_spy = _trending_prices(w_spy[-1], spy_scenario_ret, n_scenario)
        s_efa = _trending_prices(w_efa[-1], efa_scenario_ret, n_scenario)
        s_agg = _trending_prices(w_agg[-1], agg_daily_ret, n_scenario)
        scenario_dates = _build_daily_dates(s_start, n_scenario)

        all_dates = warmup_dates + scenario_dates
        all_spy = w_spy + s_spy
        all_efa = w_efa + s_efa
        all_agg = w_agg + s_agg

        all_signals = _feed_three_streams(strat, all_dates, all_spy, all_efa, all_agg)
        # Signals that fell in the scenario window
        scenario_signals = [
            s for s in all_signals if s.timestamp is not None and s.timestamp >= s_start
        ]
        return strat, all_signals, scenario_signals

    def test_spy_up_higher_than_intl_selects_spy(self):
        """
        SPY strongly up, intl moderately up → absolute_mom > 0 → pick SPY
        (higher relative return).
        """
        strat = _default_strat(lookback_window=20)
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        n = 60
        dates = _build_daily_dates(start, n)
        spy = _trending_prices(400.0, 0.004, n)  # ~11% over 20 days
        efa = _trending_prices(50.0, 0.001, n)  # ~2% over 20 days
        agg = _trending_prices(100.0, 0.0001, n)

        signals = _feed_three_streams(strat, dates, spy, efa, agg)
        buys = [s for s in signals if s.side == OrderSide.BUY]
        assert len(buys) >= 1
        # First buy should be SPY (higher momentum, positive abs_mom)
        first_buy_ticker = buys[0].symbol.ticker
        assert first_buy_ticker == "SPY", f"Expected first BUY to be SPY, got {first_buy_ticker}"

    def test_intl_higher_than_spy_both_positive_selects_intl(self):
        """
        Both SPY and intl positive, intl return higher → pick intl ETF.
        """
        strat = _default_strat(lookback_window=20)
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        n = 60
        dates = _build_daily_dates(start, n)
        # EFA grows faster than SPY; both positive
        spy = _trending_prices(400.0, 0.001, n)  # moderate
        efa = _trending_prices(50.0, 0.005, n)  # stronger
        agg = _trending_prices(100.0, 0.0001, n)

        signals = _feed_three_streams(strat, dates, spy, efa, agg)
        buys = [s for s in signals if s.side == OrderSide.BUY]
        assert len(buys) >= 1
        first_buy_ticker = buys[0].symbol.ticker
        assert first_buy_ticker == "EFA", f"Expected first BUY to be EFA, got {first_buy_ticker}"

    def test_spy_negative_selects_bonds(self):
        """
        SPY 12-month return < 0 → absolute momentum fails → rotate to bonds.
        """
        strat = _default_strat(lookback_window=20)
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        n = 60
        dates = _build_daily_dates(start, n)
        # SPY declining, EFA also declining, AGG flat/up
        spy = _trending_prices(400.0, -0.003, n)
        efa = _trending_prices(50.0, -0.002, n)
        agg = _trending_prices(100.0, 0.0002, n)

        signals = _feed_three_streams(strat, dates, spy, efa, agg)
        buys = [s for s in signals if s.side == OrderSide.BUY]
        assert len(buys) >= 1
        first_buy_ticker = buys[0].symbol.ticker
        assert first_buy_ticker == "AGG", (
            f"Expected first BUY to be AGG (bonds), got {first_buy_ticker}"
        )

    def test_rotation_from_equity_to_bonds_emits_sell_then_buy(self):
        """
        Phase 1 (warmup): SPY strongly up → strategy holds SPY.
        Phase 2 (scenario): SPY turns negative → strategy should SELL SPY, BUY AGG.
        """
        lookback = 20
        # Warmup: strong SPY uptrend
        # n_warmup must give us at least 2 full months so we get a BUY on SPY first.
        n_warmup = 80  # ~2.5 months of daily bars; warmup completes at bar 21

        # Scenario: SPY crashes, intl flat, bonds up
        n_scenario = 50

        strat, all_signals, scenario_signals = self._run_scenario(
            lookback_window=lookback,
            n_warmup=n_warmup,
            n_scenario=n_scenario,
            spy_warmup_ret=0.004,  # strong up during warmup
            efa_warmup_ret=0.001,
            spy_scenario_ret=-0.005,  # now falling
            efa_scenario_ret=-0.002,
            agg_daily_ret=0.0002,
        )

        # The key assertion is that scenario_signals include SELL SPY + BUY AGG
        # (we eventually held SPY during warmup, then rotated to AGG).
        spy_sells = [
            s for s in scenario_signals if s.side == OrderSide.SELL and s.symbol.ticker == "SPY"
        ]
        agg_buys = [
            s for s in scenario_signals if s.side == OrderSide.BUY and s.symbol.ticker == "AGG"
        ]

        assert len(agg_buys) >= 1, "Expected at least one BUY AGG signal in scenario"
        # If we had a position in SPY before the scenario, a SELL should accompany.
        if strat._current_holding == "AGG" and any(
            s.side == OrderSide.BUY and s.symbol.ticker == "SPY" for s in all_signals
        ):
            assert len(spy_sells) >= 1, "Expected SELL SPY when rotating to AGG"

    def test_sell_precedes_buy_in_rotation(self):
        """
        Whenever the strategy rotates, the SELL comes before the BUY in the
        returned signal list.
        """
        strat = _default_strat(lookback_window=20)
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        n = 80
        dates = _build_daily_dates(start, n)

        # Phase 1 (~40 days): SPY up, EFA up less → hold SPY
        # Phase 2 (~40 days): SPY down → rotate to AGG
        spy_prices = _trending_prices(400.0, 0.004, 40) + _trending_prices(
            400.0 * (1.004**39), -0.004, 40
        )
        efa_prices = _trending_prices(50.0, 0.001, 80)
        agg_prices = _trending_prices(100.0, 0.0001, 80)

        # Collect signals with their order of emission per rebalance.
        all_signals = _feed_three_streams(strat, dates, spy_prices, efa_prices, agg_prices)

        # Group by timestamp: for any given rebalance date, SELL precedes BUY.
        from collections import defaultdict

        by_ts: dict = defaultdict(list)
        for s in all_signals:
            by_ts[s.timestamp].append(s)

        for ts, evts in by_ts.items():
            if len(evts) >= 2:
                sides = [e.side for e in evts]
                sell_idx = sides.index(OrderSide.SELL) if OrderSide.SELL in sides else -1
                buy_idx = sides.index(OrderSide.BUY) if OrderSide.BUY in sides else -1
                if sell_idx >= 0 and buy_idx >= 0:
                    assert sell_idx < buy_idx, f"At {ts}: SELL must come before BUY in signal list"

    def test_no_duplicate_buy_when_holding_unchanged(self):
        """
        If selection does not change month-over-month, no signal is emitted.
        A sustained uptrend should produce exactly ONE buy (the initial entry).
        """
        strat = _default_strat(lookback_window=20)
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        # Feed ~90 days (3 months) of a sustained SPY uptrend.
        n = 90
        dates = _build_daily_dates(start, n)
        spy = _trending_prices(400.0, 0.003, n)
        efa = _trending_prices(50.0, 0.001, n)
        agg = _trending_prices(100.0, 0.0001, n)

        signals = _feed_three_streams(strat, dates, spy, efa, agg)
        buys = [s for s in signals if s.side == OrderSide.BUY]
        # Should buy once (first eligible rebalance); subsequent months → hold.
        assert len(buys) == 1, f"Expected exactly 1 BUY in a sustained uptrend, got {len(buys)}"

    def test_buy_has_stop_loss(self):
        """Every BUY signal must carry a suggested_stop_loss."""
        strat = _default_strat(lookback_window=20)
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        n = 60
        dates = _build_daily_dates(start, n)
        spy = _trending_prices(400.0, 0.003, n)
        efa = _trending_prices(50.0, 0.001, n)
        agg = _trending_prices(100.0, 0.0001, n)

        signals = _feed_three_streams(strat, dates, spy, efa, agg)
        buys = [s for s in signals if s.side == OrderSide.BUY]
        assert len(buys) >= 1
        for buy in buys:
            assert buy.suggested_stop_loss is not None, (
                f"BUY for {buy.symbol.ticker} is missing suggested_stop_loss"
            )
            assert buy.suggested_stop_loss > Decimal("0")
            # Stop must be below entry price (it is protective).
            close = strat._latest_close.get(buy.symbol.ticker)
            if close is not None:
                assert buy.suggested_stop_loss < close, (
                    f"Stop loss {buy.suggested_stop_loss} >= entry {close}"
                )

    def test_stop_loss_respects_pct(self):
        """
        stop_loss == entry_price * (1 - stop_loss_pct).

        Because the BUY signal is emitted at bar N and the test inspects the
        signal after all bars have been fed, we cannot use _latest_close (which
        reflects bar 60, not bar N).  Instead we reconstruct the entry price
        from the stop itself: stop = price * (1 - pct)  =>  price = stop / (1 - pct).
        We verify the ratio is correct rather than recomputing the absolute value.
        """
        stop_pct = Decimal("0.10")
        strat = DualMomentumStrategy(
            strategy_id="gem",
            symbols=SYMBOLS,
            equity_ticker="SPY",
            intl_ticker="EFA",
            bond_ticker="AGG",
            lookback_window=20,
            stop_loss_pct=stop_pct,
        )
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        n = 60
        dates = _build_daily_dates(start, n)
        spy = _trending_prices(400.0, 0.003, n)
        efa = _trending_prices(50.0, 0.001, n)
        agg = _trending_prices(100.0, 0.0001, n)

        # Capture the latest_close at BUY-signal time by processing bar-by-bar.
        entry_prices: dict = {}
        buy_signals = []
        for dt, sp, ef, ag in zip(dates, spy, efa, agg):
            for bar in (
                _make_bar(SPY, dt, sp),
                _make_bar(EFA, dt, ef),
                _make_bar(AGG, dt, ag),
            ):
                evts = strat.on_bar(bar)
                for sig in evts:
                    if sig.side == OrderSide.BUY:
                        # Record the close at the moment of the BUY
                        entry_prices[id(sig)] = strat._latest_close.get(sig.symbol.ticker)
                        buy_signals.append(sig)

        assert len(buy_signals) >= 1
        for buy in buy_signals:
            entry = entry_prices[id(buy)]
            assert entry is not None
            expected_stop = entry * (Decimal("1") - stop_pct)
            assert buy.suggested_stop_loss == expected_stop, (
                f"Stop {buy.suggested_stop_loss} != entry {entry} * (1 - {stop_pct}) "
                f"= {expected_stop}"
            )


class TestSignalProperties:
    """Check signal metadata and strength."""

    def test_buy_signal_strength_is_one(self):
        strat = _default_strat(lookback_window=20)
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        n = 60
        dates = _build_daily_dates(start, n)
        spy = _trending_prices(400.0, 0.003, n)
        efa = _trending_prices(50.0, 0.001, n)
        agg = _trending_prices(100.0, 0.0001, n)

        signals = _feed_three_streams(strat, dates, spy, efa, agg)
        buys = [s for s in signals if s.side == OrderSide.BUY]
        assert len(buys) >= 1
        for buy in buys:
            assert buy.strength == Decimal("1.0")

    def test_sell_signal_strategy_id(self):
        strat = DualMomentumStrategy(
            strategy_id="my_gem",
            symbols=SYMBOLS,
            equity_ticker="SPY",
            intl_ticker="EFA",
            bond_ticker="AGG",
            lookback_window=20,
        )
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        n = 80
        dates = _build_daily_dates(start, n)
        # First equity positive → hold SPY; then equity negative → rotate
        spy = _trending_prices(400.0, 0.004, 40) + _trending_prices(400.0 * (1.004**39), -0.005, 40)
        efa = _trending_prices(50.0, 0.001, 80)
        agg = _trending_prices(100.0, 0.0001, 80)
        signals = _feed_three_streams(strat, dates, spy, efa, agg)
        for sig in signals:
            assert sig.strategy_id == "my_gem"

    def test_buy_has_non_empty_reason(self):
        strat = _default_strat(lookback_window=20)
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        n = 60
        dates = _build_daily_dates(start, n)
        spy = _trending_prices(400.0, 0.003, n)
        efa = _trending_prices(50.0, 0.001, n)
        agg = _trending_prices(100.0, 0.0001, n)
        signals = _feed_three_streams(strat, dates, spy, efa, agg)
        buys = [s for s in signals if s.side == OrderSide.BUY]
        assert len(buys) >= 1
        for buy in buys:
            assert buy.reason, "BUY signal must carry a non-empty reason string"


class TestIgnoredSymbols:
    def test_unknown_symbol_ignored(self):
        strat = _default_strat(lookback_window=20)
        other = Symbol("MSFT", AssetClass.EQUITY)
        t = datetime(2024, 1, 2, tzinfo=timezone.utc)
        bar = _make_bar(other, t, 300.0)
        assert strat.on_bar(bar) == []


class TestDeterminism:
    """Same inputs → same outputs, every time."""

    def test_deterministic(self):
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        n = 90
        dates = _build_daily_dates(start, n)
        spy = _trending_prices(400.0, 0.003, n)
        efa = _trending_prices(50.0, 0.001, n)
        agg = _trending_prices(100.0, 0.0001, n)

        s1 = _default_strat(lookback_window=20)
        s2 = _default_strat(lookback_window=20)

        sig1 = _feed_three_streams(s1, dates, spy, efa, agg)
        sig2 = _feed_three_streams(s2, dates, spy, efa, agg)

        assert [s.side for s in sig1] == [s.side for s in sig2]
        assert [s.symbol.ticker for s in sig1] == [s.symbol.ticker for s in sig2]
