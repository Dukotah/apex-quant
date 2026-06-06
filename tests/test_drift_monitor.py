"""
Tests for apex.validation.drift_monitor.

The live-vs-backtest alpha-decay kill switch: warms up, stays ACTIVE while live
performance tracks the validated edge, AUTO-QUARANTINES (stickily) when the
rolling Sharpe decays below the floor, and only a manual reset() lifts it.
"""

from __future__ import annotations

from apex.validation.drift_monitor import DriftMonitor, DriftState


# Deterministic return streams (no RNG).
def _healthy(n):
    # Positive mean, small variance → high Sharpe, well above any sane floor.
    pattern = [0.010, 0.006, 0.012, 0.008, 0.011, 0.007]
    return [pattern[i % len(pattern)] for i in range(n)]


def _decayed(n):
    # Near-zero/negative mean with noise → Sharpe far below the floor.
    pattern = [0.004, -0.006, -0.001, 0.002, -0.005, -0.003]
    return [pattern[i % len(pattern)] for i in range(n)]


def test_requires_positive_validated_sharpe():
    for bad in (0.0, -1.0):
        try:
            DriftMonitor("s", validated_sharpe=bad)
            assert False, "expected ValueError"
        except ValueError:
            pass


def test_warming_up_before_min_observations():
    mon = DriftMonitor("s", validated_sharpe=1.5, window=30)
    reading = None
    for r in _healthy(10):  # fewer than the 30-obs minimum
        reading = mon.record_return(r)
    assert reading.state == DriftState.WARMING_UP
    assert not reading.is_quarantined
    assert reading.observations == 10


def test_stays_active_when_tracking_validated_edge():
    mon = DriftMonitor("s", validated_sharpe=1.5, window=30)
    reading = None
    for r in _healthy(30):
        reading = mon.record_return(r)
    assert reading.state == DriftState.ACTIVE
    # Healthy stream's rolling Sharpe clears the 0.70*1.5 = 1.05 floor easily.
    assert reading.rolling_sharpe > reading.floor
    assert reading.drift_ratio > 0


def test_auto_quarantines_on_decay():
    mon = DriftMonitor("s", validated_sharpe=2.0, window=30)
    reading = None
    for r in _decayed(30):
        reading = mon.record_return(r)
    assert reading.state == DriftState.QUARANTINED
    assert reading.is_quarantined
    assert reading.rolling_sharpe < reading.floor
    assert "alpha decay" in reading.reason


def test_quarantine_is_sticky():
    """Once quarantined, a run of good returns must NOT auto-reactivate it."""
    mon = DriftMonitor("s", validated_sharpe=2.0, window=30)
    for r in _decayed(30):
        mon.record_return(r)
    assert mon.is_quarantined
    last = None
    for r in _healthy(30):  # would otherwise look great
        last = mon.record_return(r)
    assert last.state == DriftState.QUARANTINED  # still quarantined


def test_reset_lifts_quarantine():
    mon = DriftMonitor("s", validated_sharpe=2.0, window=30)
    for r in _decayed(30):
        mon.record_return(r)
    assert mon.is_quarantined
    mon.reset(clear_history=True)
    assert not mon.is_quarantined
    # After reset + fresh healthy data it returns to ACTIVE.
    reading = None
    for r in _healthy(30):
        reading = mon.record_return(r)
    assert reading.state == DriftState.ACTIVE


def test_floor_is_70pct_of_validated():
    mon = DriftMonitor("s", validated_sharpe=1.4)
    assert abs(mon.floor - 0.70 * 1.4) < 1e-9


def test_record_equity_derives_returns():
    mon = DriftMonitor("s", validated_sharpe=1.5, window=5, min_observations=4)
    # Steadily rising equity → positive returns → ACTIVE once enough points.
    reading = None
    equity = 100_000.0
    for r in _healthy(8):
        equity *= 1.0 + r
        reading = mon.record_equity(equity)
    assert reading.observations >= 4
    assert reading.state in (DriftState.ACTIVE, DriftState.QUARANTINED)
    # Healthy rising equity should keep it active.
    assert reading.state == DriftState.ACTIVE


def test_deterministic():
    a = DriftMonitor("s", validated_sharpe=1.8, window=20)
    b = DriftMonitor("s", validated_sharpe=1.8, window=20)
    ra = rb = None
    for r in _decayed(25):
        ra = a.record_return(r)
        rb = b.record_return(r)
    assert ra.state == rb.state
    assert ra.rolling_sharpe == rb.rolling_sharpe


def test_from_gauntlet_report_recovers_validated_sharpe():
    class FakeReport:
        strategy_name = "demo"
        quarantine_sharpe_floor = 0.70 * 1.6  # floor = 0.70 * validated(1.6)

    mon = DriftMonitor.from_gauntlet_report(FakeReport())
    assert abs(mon.validated_sharpe - 1.6) < 1e-9
    assert mon.strategy_id == "demo"
