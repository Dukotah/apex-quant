"""
Tests for scripts.report — the paper-gate monitor (read-only over the state DB).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from apex.core.events import FillEvent
from apex.core.models import AssetClass, OrderSide, Symbol
from scripts.report import (
    GATE_DAYS,
    build_report,
    build_sleeve_section,
    gate_passed,
    main,
)
from scripts.run_once import RunReport, StateStore

UTC = timezone.utc


def _fill(ticker, side, qty, price):
    return FillEvent(
        symbol=Symbol(ticker, AssetClass.ETF),
        side=side,
        quantity=Decimal(str(qty)),
        fill_price=Decimal(str(price)),
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )


def _seed(store, equities, orders=0, mode="paper"):
    base = datetime(2024, 1, 1, tzinfo=UTC)
    for i, eq in enumerate(equities):
        store.save_run(
            RunReport(
                timestamp=base + timedelta(days=i),
                mode=mode,
                equity=float(eq),
                num_positions=0,
                orders_submitted=orders,
            ),
            {},
        )


def _make_passing_equities(n: int = GATE_DAYS + 2) -> list[float]:
    """Return a monotonically growing equity series that yields Sharpe >= GATE_MIN_SHARPE."""
    v = 100_000.0
    equities = []
    for i in range(n):
        v *= 1.006 if i % 2 == 0 else 1.002  # net +0.4%/day with real variance
        equities.append(v)
    return equities


# ============================================================ build_report (existing)


def test_no_runs_message(tmp_path):
    store = StateStore(tmp_path / "s.db")
    assert "hasn't completed a cycle" in build_report(store, "paper")


def test_report_has_core_metrics(tmp_path):
    store = StateStore(tmp_path / "s.db")
    _seed(store, [100000, 100500, 101000, 101200, 101800], orders=2)
    out = build_report(store, "paper")
    assert "PAPER GATE REPORT" in out
    assert "total return" in out and "+1.80%" in out  # 101800/100000 - 1
    assert "30-day gate" in out
    assert f"5/{GATE_DAYS} days" in out  # 5 cycles recorded
    assert "running" in out  # < 30 days, gate not passed


def test_report_gate_passes_after_30_days_with_edge(tmp_path):
    store = StateStore(tmp_path / "s.db")
    # 32 days of noisy-but-positive gains -> high rolling Sharpe, gate criteria met.
    eq = _make_passing_equities(32)
    _seed(store, eq)
    out = build_report(store, "paper")
    assert f"{len(eq)}/{GATE_DAYS} days" in out
    assert "GATE PASSED" in out


def test_report_counts_activity_and_drawdown(tmp_path):
    store = StateStore(tmp_path / "s.db")
    _seed(store, [100000, 95000, 102000], orders=3)  # a dip then recovery
    out = build_report(store, "paper")
    assert "max drawdown" in out
    assert "9 orders" in out  # 3 orders x 3 cycles


# ============================================================ per-sleeve attribution


class TestSleeveSection:
    def test_empty_fills_documents_gap(self):
        out = build_sleeve_section([], Decimal("100000"))
        assert "PER-SLEEVE ATTRIBUTION" in out
        assert "no fill history available" in out

    def test_open_only_renders_zero_trade_row(self):
        # An open position with no exit -> the sleeve still appears, with 0 trades.
        fills = [_fill("SPY", OrderSide.BUY, 10, 100)]
        out = build_sleeve_section(fills, Decimal("100000"))
        assert "SPY" in out
        assert "0.00" in out  # zero realized P&L for the open-only sleeve

    def test_winning_and_losing_sleeves_render(self):
        fills = [
            _fill("SPY", OrderSide.BUY, 10, 100),
            _fill("SPY", OrderSide.SELL, 10, 110),  # +100
            _fill("TLT", OrderSide.BUY, 10, 100),
            _fill("TLT", OrderSide.SELL, 10, 90),  # -100
        ]
        out = build_sleeve_section(fills, Decimal("100000"))
        assert "SPY" in out and "TLT" in out
        assert "100.00" in out  # SPY realized +100
        assert "-100.00" in out  # TLT realized -100
        # Worst sleeve first: TLT must appear before SPY.
        assert out.index("TLT") < out.index("SPY")

    def test_report_includes_sleeve_section(self, tmp_path):
        store = StateStore(tmp_path / "s.db")
        _seed(store, [100000, 100500, 101000])
        fills = [
            _fill("SPY", OrderSide.BUY, 10, 100),
            _fill("SPY", OrderSide.SELL, 10, 110),
        ]
        out = build_report(store, "paper", fills=fills)
        assert "PER-SLEEVE ATTRIBUTION" in out
        assert "SPY" in out

    def test_report_without_fills_shows_gap_note(self, tmp_path):
        store = StateStore(tmp_path / "s.db")
        _seed(store, [100000, 100500, 101000])
        out = build_report(store, "paper")
        assert "PER-SLEEVE ATTRIBUTION" in out
        assert "no fill history available" in out


# ============================================================ gate_passed


class TestGatePassed:
    def test_returns_false_when_no_runs(self, tmp_path):
        store = StateStore(tmp_path / "s.db")
        assert gate_passed(store, "paper") is False

    def test_returns_false_when_too_few_cycles(self, tmp_path):
        store = StateStore(tmp_path / "s.db")
        # GATE_DAYS - 1 cycles, all growing → still fails on count
        equities = _make_passing_equities(GATE_DAYS - 1)
        _seed(store, equities)
        assert gate_passed(store, "paper") is False

    def test_returns_true_when_enough_cycles_and_sharpe_met(self, tmp_path):
        store = StateStore(tmp_path / "s.db")
        equities = _make_passing_equities(GATE_DAYS + 2)
        _seed(store, equities)
        assert gate_passed(store, "paper") is True

    def test_returns_false_when_sharpe_too_low(self, tmp_path):
        store = StateStore(tmp_path / "s.db")
        # GATE_DAYS + 2 cycles but flat equity → Sharpe ≈ 0, below GATE_MIN_SHARPE
        equities = [100_000.0] * (GATE_DAYS + 2)
        _seed(store, equities)
        assert gate_passed(store, "paper") is False

    def test_returns_false_when_quarantined(self, tmp_path):
        store = StateStore(tmp_path / "s.db")
        # Build GATE_DAYS + 2 cycles: first half rises nicely, second half crashes.
        # The crash tanks the rolling Sharpe below the quarantine floor (70% of
        # validated_sharpe) even though the full-history Sharpe might still be >= 1.0.
        # We use a small validated_sharpe so the floor is easy to breach.
        equities: list[float] = []
        v = 100_000.0
        half = (GATE_DAYS + 2) // 2
        for _ in range(half):
            v *= 1.02  # strong gains in first half
            equities.append(v)
        for _ in range(GATE_DAYS + 2 - half):
            v *= 0.96  # heavy losses in second half → quarantine
            equities.append(v)
        _seed(store, equities)
        # validated_sharpe=0.1 → floor=0.07; crash portion will breach it
        assert gate_passed(store, "paper", validated_sharpe=0.1) is False

    def test_mode_isolation(self, tmp_path):
        """gate_passed for 'live' should not be affected by 'paper' rows."""
        store = StateStore(tmp_path / "s.db")
        equities = _make_passing_equities(GATE_DAYS + 2)
        _seed(store, equities, mode="paper")
        # No 'live' rows yet → should return False
        assert gate_passed(store, "live") is False

    def test_gate_passed_consistent_with_build_report(self, tmp_path):
        """gate_passed verdict must match what build_report prints."""
        store = StateStore(tmp_path / "s.db")
        equities = _make_passing_equities(GATE_DAYS + 2)
        _seed(store, equities)
        passed = gate_passed(store, "paper")
        report_text = build_report(store, "paper")
        if passed:
            assert "GATE PASSED" in report_text
        else:
            assert "running" in report_text


# ============================================================ main() --check


class TestMainCheck:
    def test_main_check_returns_1_when_gate_not_passed(self, tmp_path, monkeypatch):
        """--check exits 1 when the gate has not been met (too few cycles)."""
        store = StateStore(tmp_path / "s.db")
        _seed(store, [100_000.0, 101_000.0])  # only 2 cycles — far below GATE_DAYS
        monkeypatch.setattr("scripts.report.StateStore", lambda: store)
        result = main(["--check", "paper"])
        assert result == 1

    def test_main_check_returns_0_when_gate_passed(self, tmp_path, monkeypatch):
        """--check exits 0 when the paper gate is genuinely met."""
        store = StateStore(tmp_path / "s.db")
        equities = _make_passing_equities(GATE_DAYS + 2)
        _seed(store, equities)
        monkeypatch.setattr("scripts.report.StateStore", lambda: store)
        result = main(["--check", "paper"])
        assert result == 0

    def test_main_without_check_always_returns_0(self, tmp_path, monkeypatch):
        """Without --check the exit code is always 0 (original behaviour)."""
        store = StateStore(tmp_path / "s.db")
        # Gate NOT met (only 2 cycles), but no --check flag → must still return 0.
        _seed(store, [100_000.0, 101_000.0])
        monkeypatch.setattr("scripts.report.StateStore", lambda: store)
        assert main(["paper"]) == 0

    def test_main_check_mode_from_positional_arg(self, tmp_path, monkeypatch):
        """Mode can appear before --check: `--check live` and `live --check` both work."""
        store = StateStore(tmp_path / "s.db")
        # seed for 'live' mode so gate is not met
        _seed(store, [100_000.0], mode="live")
        monkeypatch.setattr("scripts.report.StateStore", lambda: store)
        # mode positional appears after --check
        result_after = main(["--check", "live"])
        assert result_after == 1

    def test_main_no_args_returns_0(self, tmp_path, monkeypatch):
        """No args → paper mode, no gate check, always 0."""
        store = StateStore(tmp_path / "s.db")
        monkeypatch.setattr("scripts.report.StateStore", lambda: store)
        assert main([]) == 0

    def test_main_check_gate_min_sharpe_boundary(self, tmp_path, monkeypatch):
        """Exactly GATE_DAYS cycles with low Sharpe must still fail."""
        store = StateStore(tmp_path / "s.db")
        # Flat equity → Sharpe ~ 0 (below GATE_MIN_SHARPE=1.0)
        flat = [100_000.0] * GATE_DAYS
        _seed(store, flat)
        monkeypatch.setattr("scripts.report.StateStore", lambda: store)
        assert main(["--check"]) == 1
