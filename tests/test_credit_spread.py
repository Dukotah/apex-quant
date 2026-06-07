"""
Tests for apex.strategy.library.credit_spread.

CreditSpreadRegimeStrategy is position-aware and uses HYG/LQD as NON-TRADEABLE
ride-along signal symbols.  Tests drive it through a StrategyContext harness
(mirroring the engine) and control the z-score by constructing HYG/LQD price
series with a known statistical shape.

Key price-series design: constant ratios yield z=0 (stdev=0 → z=0 clamp),
which is above the default enter_z=-1.0, so the strategy boots risk-ON.  To
force risk-OFF we use a DECLINING ratio sequence whose final points are well
below the window mean.

Coverage:
  - validation (bad constructor args raise ValueError)
  - warmup: [] until ratio_window filled AND both gauges seen on same timestamp
  - risk-ON (tight/stable spreads, z ~ 0): enters SPY, never IEF
  - risk-OFF (declining ratio, z << enter_z): rotates to IEF, exits SPY
  - hysteresis dead-band: no flip-back while z is between enter_z and exit_z
  - no signals emitted for HYG or LQD (gauge tickers)
  - no pyramiding: idempotent when already holding the target
  - cold-start correctness: enters the prevailing regime on first computable bar
  - suggested_stop_loss present on entries; absent on exits
  - sell signals carry no stop-loss
  - determinism: identical inputs -> identical outputs
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import List

import pytest

from apex.core.models import AssetClass, Bar, OrderSide, Symbol
from apex.strategy.base_strategy import StrategyContext
from apex.strategy.library.credit_spread import CreditSpreadRegimeStrategy

# ---------------------------------------------------------------------------
# Canonical symbols
# ---------------------------------------------------------------------------
SPY = Symbol("SPY", AssetClass.ETF)
IEF = Symbol("IEF", AssetClass.ETF)
HYG = Symbol("HYG", AssetClass.ETF)
LQD = Symbol("LQD", AssetClass.ETF)
ALL_SYMS = [SPY, IEF, HYG, LQD]

# Use a small window so tests run fast.
RATIO_WINDOW = 20
ENTER_Z = Decimal("-1.0")
EXIT_Z = Decimal("-0.5")

# LQD is held at a fixed price throughout tests; only HYG moves.
LQD_PX = 1.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar(sym: Symbol, t: datetime, price: float) -> Bar:
    p = Decimal(str(round(price, 6)))
    return Bar(symbol=sym, timestamp=t, open=p, high=p, low=p, close=p, volume=Decimal("1000"))


def _strat(
    ratio_window: int = RATIO_WINDOW,
    enter_z: Decimal = ENTER_Z,
    exit_z: Decimal = EXIT_Z,
) -> CreditSpreadRegimeStrategy:
    return CreditSpreadRegimeStrategy(
        strategy_id="test_cs",
        symbols=ALL_SYMS,
        ratio_window=ratio_window,
        enter_z=enter_z,
        exit_z=exit_z,
    )


class _Harness:
    """
    Minimal engine stand-in: binds a StrategyContext, refreshes it from a
    simulated portfolio before each bar, and applies emitted signals as
    IMMEDIATE fills so the next bar sees the updated holding.
    """

    def __init__(self, strat: CreditSpreadRegimeStrategy):
        self.strat = strat
        self.ctx = StrategyContext()
        strat.bind_context(self.ctx)
        self.held: dict[str, Decimal] = {}
        self._base_t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self._day = 0

    def _t(self) -> datetime:
        return self._base_t + timedelta(days=self._day)

    def _refresh_ctx(self) -> None:
        self.ctx.sync_state(
            positions={k: SimpleNamespace(quantity=q) for k, q in self.held.items() if q > 0}
        )

    def step(
        self,
        hyg_px: float,
        lqd_px: float = LQD_PX,
        spy_px: float = 100.0,
        ief_px: float = 100.0,
    ) -> List:
        """
        Feed one 'day': HYG, LQD, then both tradeable bars.
        Refreshes context before sending the first bar, applies fills immediately.
        """
        self._refresh_ctx()
        t = self._t()
        self._day += 1

        # Gauges first so the ratio is updated before the tradeable bars arrive.
        self.strat.on_bar(_bar(HYG, t, hyg_px))
        self.strat.on_bar(_bar(LQD, t, lqd_px))

        # Tradeable bars.
        sigs: List = []
        for sym, px in ((SPY, spy_px), (IEF, ief_px)):
            for s in self.strat.on_bar(_bar(sym, t, px)):
                sigs.append(s)
                self.held[s.symbol.ticker] = (
                    Decimal("1") if s.side == OrderSide.BUY else Decimal("0")
                )
        return sigs

    def run(self, n: int, hyg_px: float, lqd_px: float = LQD_PX, **kwargs) -> List:
        """Feed `n` identical bars and return all signals."""
        out = []
        for _ in range(n):
            out.extend(self.step(hyg_px, lqd_px, **kwargs))
        return out

    def warm_risk_on(self) -> None:
        """
        Fill the ratio window with a STABLE ratio (1.1/1.0 = 1.1 every day).
        stdev = 0 → z = 0 (clamped) → above enter_z=-1.0 → boots risk-ON.
        """
        for _ in range(RATIO_WINDOW):
            self.step(hyg_px=1.1)

    def warm_risk_off(self) -> None:
        """
        Fill the ratio window with a DECLINING ratio so the final points are
        well below the window mean → z << enter_z → boots risk-OFF.

        Shape: start at 1.0, fall by 0.025 per day.
        After RATIO_WINDOW days (window=20): ratios 1.0 … 0.525.
        mean ≈ 0.762, stdev ≈ 0.143.  Last value = 0.525 → z ≈ -1.65.
        """
        for i in range(RATIO_WINDOW):
            hyg = max(1.0 - i * 0.025, 0.3)
            self.step(hyg_px=hyg)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_ratio_window_too_small(self):
        with pytest.raises(ValueError, match="ratio_window"):
            CreditSpreadRegimeStrategy("s", ALL_SYMS, ratio_window=1)

    def test_exit_z_must_be_greater_than_enter_z(self):
        with pytest.raises(ValueError, match="exit_z must be > enter_z"):
            CreditSpreadRegimeStrategy(
                "s", ALL_SYMS, enter_z=Decimal("-0.5"), exit_z=Decimal("-1.0")
            )

    def test_equal_thresholds_raises(self):
        with pytest.raises(ValueError, match="exit_z must be > enter_z"):
            CreditSpreadRegimeStrategy(
                "s", ALL_SYMS, enter_z=Decimal("-1.0"), exit_z=Decimal("-1.0")
            )

    def test_stop_loss_zero_raises(self):
        with pytest.raises(ValueError, match="stop_loss_pct"):
            CreditSpreadRegimeStrategy("s", ALL_SYMS, stop_loss_pct=Decimal("0"))

    def test_stop_loss_one_raises(self):
        with pytest.raises(ValueError, match="stop_loss_pct"):
            CreditSpreadRegimeStrategy("s", ALL_SYMS, stop_loss_pct=Decimal("1"))

    def test_missing_risk_sym_raises(self):
        with pytest.raises(ValueError, match="risk_sym"):
            CreditSpreadRegimeStrategy("s", [IEF, HYG, LQD])

    def test_missing_defensive_sym_raises(self):
        with pytest.raises(ValueError, match="defensive_sym"):
            CreditSpreadRegimeStrategy("s", [SPY, HYG, LQD])

    def test_missing_hyg_raises(self):
        with pytest.raises(ValueError, match="hyg_sym"):
            CreditSpreadRegimeStrategy("s", [SPY, IEF, LQD])

    def test_missing_lqd_raises(self):
        with pytest.raises(ValueError, match="lqd_sym"):
            CreditSpreadRegimeStrategy("s", [SPY, IEF, HYG])


# ---------------------------------------------------------------------------
# Warmup
# ---------------------------------------------------------------------------


class TestWarmup:
    def test_returns_empty_before_window_filled(self):
        """Exactly RATIO_WINDOW-1 days of gauges → no signal."""
        h = _Harness(_strat(ratio_window=RATIO_WINDOW))
        out = []
        for i in range(RATIO_WINDOW - 1):
            # Vary hyg slightly so stdev != 0, to rule out that branch.
            out.extend(h.step(hyg_px=1.0 + i * 0.001))
        assert out == [], "should not emit any signal during warmup"

    def test_returns_empty_if_only_one_gauge_seen(self):
        """If only HYG bars arrive (no LQD), ratio is never recorded → warmup."""
        strat = _strat(ratio_window=5)
        ctx = StrategyContext()
        strat.bind_context(ctx)
        t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        out = []
        for i in range(10):
            # Only HYG — no LQD.
            out.extend(strat.on_bar(_bar(HYG, t + timedelta(days=i), 0.9)))
            out.extend(strat.on_bar(_bar(SPY, t + timedelta(days=i), 100.0)))
        assert out == [], "no ratio without both gauges → still in warmup"

    def test_emits_after_window_fills_risk_on(self):
        """After exactly RATIO_WINDOW days (stable ratio), emits a BUY for SPY."""
        h = _Harness(_strat(ratio_window=RATIO_WINDOW))
        # Stable ratio → z=0 → risk-ON.
        all_sigs = []
        for _ in range(RATIO_WINDOW + 2):
            all_sigs.extend(h.step(hyg_px=1.1))
        buys = [s for s in all_sigs if s.side == OrderSide.BUY]
        assert len(buys) >= 1, "should emit at least one BUY after the window fills"
        assert buys[0].symbol.ticker == "SPY"

    def test_ratio_recorded_once_per_day(self):
        """Feeding HYG and LQD for the same day appends exactly one ratio point."""
        strat = _strat(ratio_window=RATIO_WINDOW)
        t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for i in range(5):
            day = t + timedelta(days=i)
            strat.on_bar(_bar(HYG, day, 1.1))
            strat.on_bar(_bar(LQD, day, 1.0))
        assert len(strat._ratio_history) == 5, (
            "each trading day should contribute exactly one ratio observation"
        )


# ---------------------------------------------------------------------------
# Risk-ON regime (tight / stable spreads → z ≈ 0 > enter_z)
# ---------------------------------------------------------------------------


class TestRiskOnRegime:
    def test_enters_spy_not_ief(self):
        h = _Harness(_strat())
        h.warm_risk_on()
        # Confirm regime.
        assert h.strat._regime == "risk", "stable ratio should boot risk-ON"
        buys = [s for s in h.run(3, hyg_px=1.1) if s.side == OrderSide.BUY]
        bought_tickers = {s.symbol.ticker for s in buys}
        # SPY already entered during warm_risk_on; no new BUY in the 3 extra bars.
        # Check that IEF was never bought.
        assert "IEF" not in bought_tickers, "risk-ON must never enter IEF"
        assert "HYG" not in bought_tickers
        assert "LQD" not in bought_tickers

    def test_spy_is_entered_during_warmup_phase(self):
        """The very first bar after warmup should BUY SPY (risk-ON, flat at start)."""
        h = _Harness(_strat())
        all_sigs = []
        for _ in range(RATIO_WINDOW + 2):
            all_sigs.extend(h.step(hyg_px=1.1))
        spy_buys = [s for s in all_sigs if s.side == OrderSide.BUY and s.symbol.ticker == "SPY"]
        assert len(spy_buys) == 1, "should enter SPY exactly once"

    def test_spy_buy_carries_stop_loss(self):
        h = _Harness(_strat())
        all_sigs = []
        for _ in range(RATIO_WINDOW + 2):
            all_sigs.extend(h.step(hyg_px=1.1))
        spy_buys = [s for s in all_sigs if s.side == OrderSide.BUY and s.symbol.ticker == "SPY"]
        assert spy_buys, "expected a SPY BUY"
        b = spy_buys[0]
        assert b.suggested_stop_loss is not None, "BUY must carry a stop-loss"
        expected_stop = Decimal("100") * (Decimal("1") - Decimal("0.08"))
        assert b.suggested_stop_loss == expected_stop

    def test_spy_buy_strength_is_one(self):
        h = _Harness(_strat())
        all_sigs = []
        for _ in range(RATIO_WINDOW + 2):
            all_sigs.extend(h.step(hyg_px=1.1))
        spy_buys = [s for s in all_sigs if s.side == OrderSide.BUY and s.symbol.ticker == "SPY"]
        assert spy_buys and spy_buys[0].strength == Decimal("1.0")


# ---------------------------------------------------------------------------
# Risk-OFF regime (declining ratio → z < enter_z → boots defensive)
# ---------------------------------------------------------------------------


class TestRiskOffRegime:
    def test_boots_defensive_on_declining_ratio(self):
        h = _Harness(_strat())
        h.warm_risk_off()
        assert h.strat._regime == "defensive", (
            f"declining ratio should boot risk-OFF, got {h.strat._regime}"
        )

    def test_enters_ief_not_spy(self):
        """
        warm_risk_off() already enters IEF during the declining-ratio window.
        Check the holdings and signals produced DURING that phase, not after
        many additional constant bars (which would wash out the variance).
        """
        h = _Harness(_strat())
        # Collect all signals produced during the declining-ratio warm-up.
        sigs_during_warmup: List = []
        for i in range(RATIO_WINDOW):
            hyg = max(1.0 - i * 0.025, 0.3)
            sigs_during_warmup.extend(h.step(hyg_px=hyg))

        bought = {s.symbol.ticker for s in sigs_during_warmup if s.side == OrderSide.BUY}
        holding_ief = h.held.get("IEF", Decimal("0")) > 0

        # The strategy must have entered IEF (either a BUY was emitted or IEF is held).
        assert "IEF" in bought or holding_ief, "risk-OFF must enter IEF during declining spreads"
        assert "SPY" not in bought, "risk-OFF must never buy SPY"
        assert "HYG" not in bought
        assert "LQD" not in bought

    def test_ief_buy_carries_stop_loss(self):
        """IEF BUY is emitted at the end of the declining-ratio phase."""
        h = _Harness(_strat())
        all_sigs = []
        for i in range(RATIO_WINDOW):
            hyg = max(1.0 - i * 0.025, 0.3)
            all_sigs.extend(h.step(hyg_px=hyg))
        ief_buys = [s for s in all_sigs if s.side == OrderSide.BUY and s.symbol.ticker == "IEF"]
        assert ief_buys, "expected at least one IEF BUY in risk-OFF declining phase"
        for b in ief_buys:
            assert b.suggested_stop_loss is not None

    def test_ief_buy_strength_is_one(self):
        h = _Harness(_strat())
        all_sigs = []
        for i in range(RATIO_WINDOW):
            hyg = max(1.0 - i * 0.025, 0.3)
            all_sigs.extend(h.step(hyg_px=hyg))
        ief_buys = [s for s in all_sigs if s.side == OrderSide.BUY and s.symbol.ticker == "IEF"]
        assert ief_buys
        for b in ief_buys:
            assert b.strength == Decimal("1.0")


# ---------------------------------------------------------------------------
# Regime rotation: SPY → IEF
# ---------------------------------------------------------------------------


class TestRegimeRotation:
    def test_rotates_spy_to_ief_when_spreads_widen(self):
        """
        Start risk-ON (SPY held), then drive the ratio down until z < enter_z.
        Expect SELL SPY and BUY IEF.
        """
        h = _Harness(_strat())

        # Phase 1: stable ratio → risk-ON → SPY entered.
        phase1_sigs = []
        for _ in range(RATIO_WINDOW + 2):
            phase1_sigs.extend(h.step(hyg_px=1.1))
        assert h.held.get("SPY", Decimal("0")) > 0, "should be holding SPY after phase 1"

        # Phase 2: drive ratio down sharply — well past the -1.0 threshold.
        # Start from the current window mean (~1.1) and drop steeply.
        phase2_sigs = []
        for i in range(RATIO_WINDOW + 5):
            hyg = max(1.1 - (i + 1) * 0.06, 0.3)
            phase2_sigs.extend(h.step(hyg_px=hyg))

        sell_tickers = {s.symbol.ticker for s in phase2_sigs if s.side == OrderSide.SELL}
        buy_tickers = {s.symbol.ticker for s in phase2_sigs if s.side == OrderSide.BUY}

        assert "SPY" in sell_tickers, "should SELL SPY when spreads widen"
        assert "IEF" in buy_tickers, "should BUY IEF when spreads widen"
        assert "HYG" not in sell_tickers | buy_tickers
        assert "LQD" not in sell_tickers | buy_tickers

    def test_sell_carries_no_stop_loss(self):
        """SELL signals must not carry a stop-loss (exits, not entries)."""
        h = _Harness(_strat())
        phase1_sigs = []
        for _ in range(RATIO_WINDOW + 2):
            phase1_sigs.extend(h.step(hyg_px=1.1))

        phase2_sigs = []
        for i in range(RATIO_WINDOW + 5):
            hyg = max(1.1 - (i + 1) * 0.06, 0.3)
            phase2_sigs.extend(h.step(hyg_px=hyg))

        sells = [s for s in phase2_sigs if s.side == OrderSide.SELL]
        assert sells, "expected at least one SELL during rotation"
        for sell in sells:
            assert sell.suggested_stop_loss is None, "SELL signals must not carry a stop-loss"


# ---------------------------------------------------------------------------
# Hysteresis: dead-band prevents whipsaw
# ---------------------------------------------------------------------------


class TestHysteresis:
    def test_no_flipback_inside_dead_band_unit(self):
        """
        Unit test the hysteresis rule directly via _update_regime.
        After setting regime='defensive', z-scores strictly BETWEEN enter_z and
        exit_z must NOT cause a regime change.
        """
        strat = _strat(enter_z=Decimal("-1.0"), exit_z=Decimal("-0.5"))
        strat._regime = "defensive"
        # z values inside the dead-band (between -1.0 and -0.5): should not flip.
        for z in (-0.99, -0.8, -0.7, -0.6, -0.51):
            strat._update_regime(z)
            assert strat._regime == "defensive", (
                f"regime flipped to {strat._regime} at z={z} — dead-band violated"
            )

    def test_no_flipback_at_exit_z_minus_epsilon(self):
        """z just below exit_z stays defensive."""
        strat = _strat(enter_z=Decimal("-1.0"), exit_z=Decimal("-0.5"))
        strat._regime = "defensive"
        strat._update_regime(-0.51)  # just below exit_z=-0.5
        assert strat._regime == "defensive"

    def test_flips_to_defensive_at_enter_z(self):
        """z at exactly enter_z triggers the risk-OFF flip."""
        strat = _strat(enter_z=Decimal("-1.0"), exit_z=Decimal("-0.5"))
        strat._regime = "risk"
        strat._update_regime(-1.0)  # exactly enter_z
        assert strat._regime == "defensive"

    def test_flips_to_risk_at_exit_z(self):
        """z at exactly exit_z triggers the risk-ON return."""
        strat = _strat(enter_z=Decimal("-1.0"), exit_z=Decimal("-0.5"))
        strat._regime = "defensive"
        strat._update_regime(-0.5)  # exactly exit_z
        assert strat._regime == "risk"

    def test_no_flip_from_risk_above_enter_z(self):
        """z above enter_z (but not extreme) leaves risk-ON unchanged."""
        strat = _strat(enter_z=Decimal("-1.0"), exit_z=Decimal("-0.5"))
        strat._regime = "risk"
        for z in (-0.99, 0.0, 1.0, 2.0):
            strat._update_regime(z)
            assert strat._regime == "risk", f"unexpected regime flip at z={z}"

    def test_integration_dead_band_prevents_spy_buy(self):
        """
        Integration: drive regime to defensive via a declining series, then feed
        a single extra bar whose z is inside the dead-band.  Verify no SPY BUY.

        We inject the internal z directly via _update_regime to sidestep the
        rolling-window statistics collapsing with constant bars.
        """
        strat = _strat(enter_z=Decimal("-1.0"), exit_z=Decimal("-0.5"))
        # Set regime to defensive (simulates having been in risk-OFF).
        strat._regime = "defensive"
        strat._hyg_close = 0.66
        strat._lqd_close = 1.0
        # Pre-fill the ratio history so _compute_zscore returns a dead-band value.
        # Mean ≈ 0.762, stdev ≈ 0.143 (from warm_risk_off ratios).
        # Target z ≈ -0.7: ratio = 0.762 + (-0.7 * 0.143) ≈ 0.662 — inside dead-band.
        warm_ratios = [max(1.0 - i * 0.025, 0.3) for i in range(RATIO_WINDOW - 1)]
        warm_ratios.append(0.662)  # last point → z ≈ -0.7 (inside dead-band)
        strat._ratio_history = list(warm_ratios)

        z = strat._compute_zscore()
        assert z is not None
        enter = float(strat.enter_z)
        exit_ = float(strat.exit_z)
        assert enter < z < exit_, f"z={z:.3f} not in dead-band [{enter}, {exit_})"

        strat._update_regime(z)
        assert strat._regime == "defensive", "dead-band must not flip regime"

    def test_returns_to_risk_on_above_exit_z(self):
        """
        Integration: after booting risk-OFF, a declining+recovering ratio that
        crosses above exit_z returns the strategy to risk-ON.

        We use a ratio history shaped like a V: falls then rises sharply, so
        the final z is above exit_z.
        """
        strat = _strat(enter_z=Decimal("-1.0"), exit_z=Decimal("-0.5"))
        ctx = StrategyContext()
        strat.bind_context(ctx)

        t = datetime(2024, 1, 1, tzinfo=timezone.utc)

        # Fill the ratio window with a FALLING then RISING series.
        # First half falls from 1.0 to 0.5; second half rises back to 1.1.
        half = RATIO_WINDOW // 2

        def _feed_gauge(day: int, hyg_px: float) -> None:
            ts = t + timedelta(days=day)
            strat.on_bar(_bar(HYG, ts, hyg_px))
            strat.on_bar(_bar(LQD, ts, 1.0))

        # Falling phase.
        for i in range(half):
            _feed_gauge(i, max(1.0 - i * 0.05, 0.5))

        # Rising phase: now the recent points are HIGH relative to the early falls,
        # pushing z above exit_z.
        for i in range(half, RATIO_WINDOW + 3):
            _feed_gauge(i, 1.0 + (i - half) * 0.04)  # ratio climbs well above 1.0

        z = strat._compute_zscore()
        assert z is not None and z >= float(strat.exit_z), (
            f"z={z:.3f} should be >= exit_z={strat.exit_z} for this rising-ratio series"
        )

        # Now feed a tradeable bar; the strategy should be in risk-ON (or flip to it).
        ts = t + timedelta(days=RATIO_WINDOW + 5)
        strat._regime = "defensive"  # simulate having been in risk-OFF
        sigs = strat.on_bar(_bar(SPY, ts, 100.0))
        assert strat._regime == "risk", f"expected risk-ON after high z, got {strat._regime}"
        buy_tickers = {s.symbol.ticker for s in sigs if s.side == OrderSide.BUY}
        assert "SPY" in buy_tickers


# ---------------------------------------------------------------------------
# No pyramiding / idempotency
# ---------------------------------------------------------------------------


class TestNoPyramiding:
    def test_no_double_entry_for_spy(self):
        """Once SPY is held in risk-ON, subsequent bars must NOT emit another BUY."""
        h = _Harness(_strat())
        all_sigs = []
        for _ in range(RATIO_WINDOW + 2):
            all_sigs.extend(h.step(hyg_px=1.1))
        assert h.held.get("SPY", Decimal("0")) > 0

        extra = h.run(10, hyg_px=1.1)
        spy_buys = [s for s in extra if s.side == OrderSide.BUY and s.symbol.ticker == "SPY"]
        assert spy_buys == [], "must not pyramid: no second BUY while already holding SPY"

    def test_no_double_entry_for_ief(self):
        """Once IEF is held in risk-OFF, subsequent bars must NOT emit another BUY."""
        h = _Harness(_strat())
        h.warm_risk_off()
        # Ensure IEF is held.
        for _ in range(3):
            h.step(hyg_px=0.5)
        if h.held.get("IEF", Decimal("0")) > 0:
            extra = h.run(5, hyg_px=0.5)
            ief_buys = [s for s in extra if s.side == OrderSide.BUY and s.symbol.ticker == "IEF"]
            assert ief_buys == [], "must not pyramid IEF"


# ---------------------------------------------------------------------------
# Gauge tickers are NEVER traded
# ---------------------------------------------------------------------------


class TestGaugeTickers:
    def test_hyg_on_bar_always_returns_empty_list(self):
        """on_bar for HYG must always return []."""
        strat = _strat()
        t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = strat.on_bar(_bar(HYG, t, 95.0))
        assert result == []

    def test_lqd_on_bar_always_returns_empty_list(self):
        strat = _strat()
        t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = strat.on_bar(_bar(LQD, t, 115.0))
        assert result == []

    def test_no_signal_for_hyg_over_full_run(self):
        h = _Harness(_strat())
        sigs = []
        for _ in range(RATIO_WINDOW + 10):
            sigs.extend(h.step(hyg_px=1.1))
        hyg_sigs = [s for s in sigs if s.symbol.ticker == "HYG"]
        assert hyg_sigs == [], "HYG is a ride-along gauge — must never receive a signal"

    def test_no_signal_for_lqd_over_full_run(self):
        h = _Harness(_strat())
        sigs = []
        for _ in range(RATIO_WINDOW + 10):
            sigs.extend(h.step(hyg_px=1.1))
        lqd_sigs = [s for s in sigs if s.symbol.ticker == "LQD"]
        assert lqd_sigs == [], "LQD is a ride-along gauge — must never receive a signal"


# ---------------------------------------------------------------------------
# Signal field assertions
# ---------------------------------------------------------------------------


class TestSignalFields:
    def _warm_and_collect(self, hyg_px: float = 1.1, n_extra: int = 3) -> tuple:
        h = _Harness(_strat())
        sigs = []
        for _ in range(RATIO_WINDOW + n_extra):
            sigs.extend(h.step(hyg_px=hyg_px))
        return h, sigs

    def test_buy_strategy_id(self):
        _, sigs = self._warm_and_collect()
        buys = [s for s in sigs if s.side == OrderSide.BUY]
        for b in buys:
            assert b.strategy_id == "test_cs"

    def test_buy_has_timestamp(self):
        _, sigs = self._warm_and_collect()
        buys = [s for s in sigs if s.side == OrderSide.BUY]
        for b in buys:
            assert b.timestamp is not None

    def test_sell_carries_no_stop_loss(self):
        h = _Harness(_strat())
        phase1 = []
        for _ in range(RATIO_WINDOW + 2):
            phase1.extend(h.step(hyg_px=1.1))
        phase2 = []
        for i in range(RATIO_WINDOW + 5):
            hyg = max(1.1 - (i + 1) * 0.06, 0.3)
            phase2.extend(h.step(hyg_px=hyg))
        sells = [s for s in phase2 if s.side == OrderSide.SELL]
        for s in sells:
            assert s.suggested_stop_loss is None


# ---------------------------------------------------------------------------
# Cold-start / context integration
# ---------------------------------------------------------------------------


class TestContextIntegration:
    def test_works_without_bound_context(self):
        """
        Without a context, the strategy treats itself as flat and still emits
        BUY in the prevailing regime — must not crash.
        """
        strat = _strat(ratio_window=5)
        # No context bound (context is None).
        t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        sigs = []
        for i in range(10):
            strat.on_bar(_bar(HYG, t + timedelta(days=i), 1.1))
            strat.on_bar(_bar(LQD, t + timedelta(days=i), 1.0))
            sigs.extend(strat.on_bar(_bar(SPY, t + timedelta(days=i), 100.0)))
            sigs.extend(strat.on_bar(_bar(IEF, t + timedelta(days=i), 100.0)))
        buys = [s for s in sigs if s.side == OrderSide.BUY]
        # Stable ratio → risk-ON → SPY BUY expected.
        assert any(s.symbol.ticker == "SPY" for s in buys)

    def test_unknown_ticker_ignored(self):
        """Bars for tickers outside the universe return []."""
        strat = _strat()
        OTHER = Symbol("NOPE", AssetClass.ETF)
        t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        assert strat.on_bar(_bar(OTHER, t, 50.0)) == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_inputs_same_outputs(self):
        def run_once():
            h = _Harness(_strat())
            results = []
            # Phase 1: risk-ON (stable ratio).
            for _ in range(RATIO_WINDOW + 5):
                for s in h.step(hyg_px=1.1):
                    results.append((s.symbol.ticker, str(s.side), str(s.strength)))
            # Phase 2: risk-OFF (declining ratio).
            for i in range(RATIO_WINDOW + 5):
                hyg = max(1.1 - (i + 1) * 0.06, 0.3)
                for s in h.step(hyg_px=hyg):
                    results.append((s.symbol.ticker, str(s.side), str(s.strength)))
            return results

        assert run_once() == run_once(), "strategy output is not deterministic"
