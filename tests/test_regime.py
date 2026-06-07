"""
tests.test_regime
=================
Tests for the VolatilityRegimeClassifier gate component.

Strategy:
  - Hand-computed realized_volatility against a tiny known series.
  - A genuinely low-vol-now series classifies LOW; a high-vol-now series HIGH.
  - Boundary behavior at low_pct / high_pct.
  - Insufficient data → UNKNOWN with None percentile/vol and risk-off.
  - Determinism and Decimal-input acceptance.
"""
from __future__ import annotations

import math
from decimal import Decimal

import pytest

from apex.strategy.regime import (
    RegimeResult,
    VolatilityRegime,
    VolatilityRegimeClassifier,
    realized_volatility,
)

# ---------------------------------------------------------------------------
# realized_volatility — hand-computed
# ---------------------------------------------------------------------------

def test_realized_vol_insufficient_data_returns_none():
    # window=3 needs 3 returns => 4 closes. Only 3 closes here.
    assert realized_volatility([100.0, 101.0, 102.0], window=3) is None


def test_realized_vol_zero_for_constant_prices():
    # Constant prices => zero log returns => zero stdev.
    assert realized_volatility([50.0] * 10, window=5) == 0.0


def test_realized_vol_hand_computed():
    # closes -> log returns of last `window` (=2):
    # closes = [100, 110, 121] -> rets = [ln(1.1), ln(1.1)] (both equal)
    # stdev of two identical values = 0.
    assert realized_volatility([100.0, 110.0, 121.0], window=2) == pytest.approx(0.0)

    # A series with two *different* returns; compute pstdev by hand.
    closes = [100.0, 110.0, 99.0]
    r1 = math.log(110.0 / 100.0)
    r2 = math.log(99.0 / 110.0)
    mean = (r1 + r2) / 2
    var = ((r1 - mean) ** 2 + (r2 - mean) ** 2) / 2
    expected = math.sqrt(var)
    assert realized_volatility(closes, window=2) == pytest.approx(expected)


def test_realized_vol_uses_only_most_recent_window():
    # A noisy early segment then a calm tail; window covers only the calm tail.
    closes = [100.0, 130.0, 90.0, 100.0, 100.0, 100.0]  # last 3 returns ~ 0
    # window=2 -> last two returns are both 0 (100->100, 100->100).
    assert realized_volatility(closes, window=2) == pytest.approx(0.0)


def test_realized_vol_rejects_nonpositive_window():
    with pytest.raises(ValueError):
        realized_volatility([1.0, 2.0, 3.0], window=0)


# ---------------------------------------------------------------------------
# Classifier — construction validation
# ---------------------------------------------------------------------------

def test_classifier_rejects_bad_params():
    with pytest.raises(ValueError):
        VolatilityRegimeClassifier(vol_window=0)
    with pytest.raises(ValueError):
        VolatilityRegimeClassifier(lookback=0)
    with pytest.raises(ValueError):
        VolatilityRegimeClassifier(low_pct=0.8, high_pct=0.2)  # low > high
    with pytest.raises(ValueError):
        VolatilityRegimeClassifier(low_pct=-0.1)
    with pytest.raises(ValueError):
        VolatilityRegimeClassifier(high_pct=1.5)


def test_min_closes_property():
    clf = VolatilityRegimeClassifier(vol_window=5, lookback=10)
    assert clf.min_closes == 15


# ---------------------------------------------------------------------------
# Insufficient data → UNKNOWN
# ---------------------------------------------------------------------------

def test_insufficient_data_returns_unknown():
    clf = VolatilityRegimeClassifier(vol_window=5, lookback=10)  # needs 15
    result = clf.classify([100.0] * 14)
    assert result.regime is VolatilityRegime.UNKNOWN
    assert result.percentile is None
    assert result.volatility is None
    assert result.is_risk_on() is False  # UNKNOWN is risk-off (fail-closed)


def test_empty_input_returns_unknown():
    clf = VolatilityRegimeClassifier(vol_window=2, lookback=3)
    result = clf.classify([])
    assert result.regime is VolatilityRegime.UNKNOWN


# ---------------------------------------------------------------------------
# LOW vs HIGH classification on engineered series
# ---------------------------------------------------------------------------

def _series_with_quiet_tail(n_history: int, quiet_len: int) -> list[float]:
    """
    Build closes: a turbulent history (alternating big jumps) followed by a
    *short* calm tail of small, non-flat moves. The current realized vol (over
    the small-move tail) is much smaller than the turbulent-history vols, so it
    ranks near the bottom of the distribution => LOW.

    The tail uses tiny alternating moves (not perfectly flat): a flat tail would
    produce many identical zero-vol samples that, under the "<=" percentile
    convention, would all rank current vol at 1.0. Small distinct moves keep the
    current vol a strict low outlier.
    """
    closes = [100.0]
    # turbulent history: large alternating +/- swings
    for i in range(n_history):
        prev = closes[-1]
        closes.append(prev * (1.15 if i % 2 == 0 else 0.87))
    # short calm tail: tiny alternating moves (~0.1%), clearly low-vol but nonzero
    for i in range(quiet_len):
        prev = closes[-1]
        closes.append(prev * (1.001 if i % 2 == 0 else 0.999))
    return closes


def _series_with_turbulent_tail(n_history: int, turb_len: int) -> list[float]:
    """
    A calm history (flat) followed by a turbulent tail of big alternating
    swings. Current vol is the highest in the distribution => HIGH.
    """
    closes = [100.0] * (n_history + 1)
    for i in range(turb_len):
        prev = closes[-1]
        closes.append(prev * (1.20 if i % 2 == 0 else 0.83))
    return closes


def test_low_vol_series_classifies_low():
    clf = VolatilityRegimeClassifier(vol_window=5, lookback=60, low_pct=0.25, high_pct=0.75)
    # lookback=60 spans mostly turbulent history with a short calm tail (8), so
    # the current (calm) vol is a low outlier within the distribution.
    closes = _series_with_quiet_tail(n_history=80, quiet_len=8)
    result = clf.classify(closes)
    assert result.regime is VolatilityRegime.LOW
    assert result.percentile is not None
    assert result.percentile < clf.low_pct
    assert result.is_risk_on() is True


def test_high_vol_series_classifies_high():
    clf = VolatilityRegimeClassifier(vol_window=5, lookback=30, low_pct=0.25, high_pct=0.75)
    closes = _series_with_turbulent_tail(n_history=60, turb_len=40)
    result = clf.classify(closes)
    assert result.regime is VolatilityRegime.HIGH
    assert result.percentile is not None
    assert result.percentile >= clf.high_pct
    assert result.is_risk_on() is False


# ---------------------------------------------------------------------------
# Percentile / boundary behavior — controlled distribution
# ---------------------------------------------------------------------------

def test_percentile_and_boundaries_with_known_distribution():
    """
    Construct closes whose per-step realized vol is a clean increasing ramp so
    the percentile of the current (last) value is exactly 1.0, then verify the
    band thresholds respond to changing high_pct.
    """
    # vol_window=1 makes realized vol over one return = abs(log return) since
    # population stdev of a single value is 0... so use vol_window=1 carefully:
    # with one return, mean == that return, variance == 0 -> vol 0 always.
    # Instead use vol_window=2 and craft returns.
    clf = VolatilityRegimeClassifier(vol_window=2, lookback=4, low_pct=0.25, high_pct=0.75)
    # Need vol_window + lookback = 6 closes. The LAST window has the biggest
    # swing so current vol is the max => percentile 1.0 => HIGH.
    closes = [100.0, 100.5, 100.0, 101.0, 100.0, 103.0]
    result = clf.classify(closes)
    assert result.percentile == pytest.approx(1.0)
    assert result.regime is VolatilityRegime.HIGH

    # If high_pct is raised above 1.0-equivalent... percentile 1.0 always >= any
    # high_pct <= 1.0, so it stays HIGH. Lowering low_pct/high_pct keeps HIGH.
    clf2 = VolatilityRegimeClassifier(vol_window=2, lookback=4, low_pct=0.1, high_pct=1.0)
    assert clf2.classify(closes).regime is VolatilityRegime.HIGH


def test_normal_when_percentile_between_thresholds():
    """
    All-equal vol distribution => every value's percentile is 1.0 (<=) ...
    that lands HIGH. To get NORMAL we need the current value to sit strictly
    between low_pct and high_pct. Build a ramp and pick a middle current value.
    """
    # Distribution of vols where current ranks in the middle.
    # vol_window=2, lookback=4 => 6 closes. Make the final window a mid-size swing.
    clf = VolatilityRegimeClassifier(vol_window=2, lookback=4, low_pct=0.25, high_pct=0.75)
    # Returns engineered so the per-step vol series (4 samples) is increasing,
    # but the LAST window is the 2nd-smallest -> percentile = 0.5 -> NORMAL.
    # Per-step vols depend on consecutive return pairs; construct directly:
    # closes -> log returns. We want vol samples roughly: [big, big, big, mid].
    closes = [100.0, 120.0, 100.0, 120.0, 100.0, 101.0]
    result = clf.classify(closes)
    assert result.percentile is not None
    assert clf.low_pct <= result.percentile < clf.high_pct
    assert result.regime is VolatilityRegime.NORMAL
    assert result.is_risk_on() is True


def test_low_pct_boundary_is_strict_below():
    # percentile exactly == low_pct should NOT be LOW (LOW is strictly below).
    # Build a distribution where current percentile == 0.25 exactly.
    # lookback=4 distribution, current is the smallest -> percentile counts how
    # many <= current. If current is unique smallest, count=1 -> 1/4 = 0.25.
    clf = VolatilityRegimeClassifier(vol_window=2, lookback=4, low_pct=0.25, high_pct=0.75)
    # Craft so the final vol is the strict minimum of 4 samples.
    closes = [100.0, 130.0, 100.0, 130.0, 100.0, 100.1]
    result = clf.classify(closes)
    assert result.percentile == pytest.approx(0.25)
    # 0.25 is NOT < 0.25, and not >= 0.75 -> NORMAL.
    assert result.regime is VolatilityRegime.NORMAL


# ---------------------------------------------------------------------------
# Determinism & Decimal acceptance
# ---------------------------------------------------------------------------

def test_deterministic_repeated_calls():
    clf = VolatilityRegimeClassifier(vol_window=5, lookback=20)
    closes = _series_with_turbulent_tail(n_history=40, turb_len=30)
    a = clf.classify(closes)
    b = clf.classify(closes)
    assert a == b


def test_accepts_decimal_input():
    clf = VolatilityRegimeClassifier(vol_window=3, lookback=5)  # needs 8 closes
    floats = [100.0, 101.0, 99.0, 102.0, 98.0, 103.0, 97.0, 104.0, 96.0, 105.0]
    decimals = [Decimal(str(x)) for x in floats]
    rf = clf.classify(floats)
    rd = clf.classify(decimals)
    assert rf.regime is rd.regime
    assert rf.percentile == pytest.approx(rd.percentile)
    assert rf.volatility == pytest.approx(rd.volatility)


def test_result_is_frozen():
    result = RegimeResult(VolatilityRegime.LOW, 0.1, 0.01)
    with pytest.raises(Exception):
        result.regime = VolatilityRegime.HIGH  # type: ignore[misc]
