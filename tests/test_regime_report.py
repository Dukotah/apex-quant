"""
tests.test_regime_report
========================
Tests for scripts/regime_report.py — the regime-segmented performance report.

Strategy:
  - Feed a synthetic return series with a KNOWN calm stretch and a KNOWN
    high-vol stretch; assert the per-period labelling separates them and that
    the per-regime metrics line up with hand/library computation.
  - Determinism: same input -> same output.
  - Edge cases: empty input (None / fail-closed text), and too-little-data /
    single-regime (everything classifies UNKNOWN).
"""

from __future__ import annotations

import math

import pytest

from apex.strategy.regime import VolatilityRegime
from apex.validation import metrics
from scripts.regime_report import (
    RegimeReport,
    _equities_to_returns,
    _ordered_regimes,
    build_regime_report,
    compute_regime_report,
    label_returns_by_volatility,
)

# ---------------------------------------------------------------------------
# Synthetic series: a calm stretch followed by a turbulent stretch.
# ---------------------------------------------------------------------------


# Classifier params used across the segmentation tests (kept consistent).
_VOL_WINDOW = 10
_LOOKBACK = 50


def _calm_and_turbulent_returns() -> list[float]:
    """
    A DETERMINISTIC series with three known segments:
      - warmup (60): moderate-vol moves so the lookback distribution has spread,
      - calm block (40): small moves (low realized vol),
      - turbulent block (40): large moves (high realized vol).
    Magnitudes are scaled by a sine so the per-step vols are non-degenerate (no
    ties that would collapse the percentile ranking). With these params the calm
    block ranks LOW and the turbulent block ranks HIGH — a clean, known split.
    No randomness: same series every run.
    """
    warmup = [0.02 * math.sin(i * 1.3) for i in range(60)]
    calm = [0.003 * math.sin(i * 1.3) for i in range(40)]
    turbulent = [0.06 * math.sin(i * 1.3) * (1.0 if i % 2 == 0 else -1.1) for i in range(40)]
    return warmup + calm + turbulent


def test_labels_one_per_return():
    rets = _calm_and_turbulent_returns()
    labels = label_returns_by_volatility(rets, vol_window=_VOL_WINDOW, lookback=_LOOKBACK)
    assert len(labels) == len(rets)
    assert all(isinstance(label, VolatilityRegime) for label in labels)


def test_calm_block_low_and_turbulent_block_high():
    rets = _calm_and_turbulent_returns()
    labels = label_returns_by_volatility(rets, vol_window=_VOL_WINDOW, lookback=_LOOKBACK)
    calm_labels = labels[60:100]
    turb_labels = labels[100:140]
    # Calm block is dominated by LOW (its realized vol ranks at the bottom).
    assert calm_labels.count(VolatilityRegime.LOW) > calm_labels.count(VolatilityRegime.HIGH)
    assert VolatilityRegime.LOW in calm_labels
    # Turbulent block is dominated by HIGH.
    assert turb_labels.count(VolatilityRegime.HIGH) > turb_labels.count(VolatilityRegime.LOW)
    assert VolatilityRegime.HIGH in turb_labels


def test_both_regimes_present_with_distinct_metrics():
    rets = _calm_and_turbulent_returns()
    report = compute_regime_report(rets, vol_window=_VOL_WINDOW, lookback=_LOOKBACK)
    assert report is not None
    per = report.per_regime
    assert VolatilityRegime.LOW in per
    assert VolatilityRegime.HIGH in per
    # The high-vol bucket carries a meaningfully larger drawdown than the calm one.
    assert per[VolatilityRegime.HIGH].max_drawdown > per[VolatilityRegime.LOW].max_drawdown
    assert per[VolatilityRegime.HIGH].max_drawdown > 0.0


def test_per_regime_metrics_match_library_on_the_bucket():
    """The report's per-bucket numbers must equal metrics computed directly on
    exactly the returns the labeller assigned to that bucket."""
    rets = _calm_and_turbulent_returns()
    labels = label_returns_by_volatility(rets, vol_window=_VOL_WINDOW, lookback=_LOOKBACK)
    report = compute_regime_report(rets, vol_window=_VOL_WINDOW, lookback=_LOOKBACK)
    assert report is not None

    high_returns = [r for r, label in zip(rets, labels) if label is VolatilityRegime.HIGH]
    high = report.per_regime[VolatilityRegime.HIGH]
    assert high.n_periods == len(high_returns)
    assert high.sharpe_ratio == pytest.approx(metrics.sharpe_ratio(high_returns))
    equity = [1.0]
    for r in high_returns:
        equity.append(equity[-1] * (1.0 + r))
    assert high.total_return == pytest.approx(metrics.total_return(equity))
    assert high.max_drawdown == pytest.approx(metrics.max_drawdown(equity))


def test_fractions_sum_to_one():
    rets = _calm_and_turbulent_returns()
    report = compute_regime_report(rets, vol_window=_VOL_WINDOW, lookback=_LOOKBACK)
    assert report is not None
    total = sum(m.fraction for m in report.per_regime.values())
    assert total == pytest.approx(1.0)


def test_n_classified_counts_non_unknown():
    rets = _calm_and_turbulent_returns()
    labels = label_returns_by_volatility(rets, vol_window=_VOL_WINDOW, lookback=_LOOKBACK)
    report = compute_regime_report(rets, vol_window=_VOL_WINDOW, lookback=_LOOKBACK)
    assert report is not None
    expected = sum(1 for label in labels if label is not VolatilityRegime.UNKNOWN)
    assert report.n_classified == expected
    assert report.n_returns == len(rets)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_report_text_contains_header_and_regimes():
    rets = _calm_and_turbulent_returns()
    text = build_regime_report(rets, label="synthetic", vol_window=_VOL_WINDOW, lookback=_LOOKBACK)
    assert "REGIME-SEGMENTED REPORT (synthetic)" in text
    assert "regime" in text
    assert "Sharpe" in text
    assert "high" in text  # the turbulent regime is present


def test_report_regimes_ordered_calm_to_turbulent():
    rets = _calm_and_turbulent_returns()
    report = compute_regime_report(rets, vol_window=_VOL_WINDOW, lookback=_LOOKBACK)
    assert report is not None
    ordered = [str(r.value) for r in report.regimes()]
    # Whatever subset is present, it must follow the low<normal<high<unknown order.
    canonical = ["low", "normal", "high", "unknown"]
    indices = [canonical.index(r) for r in ordered if r in canonical]
    assert indices == sorted(indices)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_determinism_same_input_same_output():
    rets = _calm_and_turbulent_returns()
    a = build_regime_report(rets, vol_window=_VOL_WINDOW, lookback=_LOOKBACK)
    b = build_regime_report(rets, vol_window=_VOL_WINDOW, lookback=_LOOKBACK)
    assert a == b
    ra = compute_regime_report(rets, vol_window=_VOL_WINDOW, lookback=_LOOKBACK)
    rb = compute_regime_report(rets, vol_window=_VOL_WINDOW, lookback=_LOOKBACK)
    assert ra == rb


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_returns_is_none_and_failclosed_text():
    assert compute_regime_report([]) is None
    text = build_regime_report([])
    assert "not enough data" in text


def test_too_little_data_all_unknown_single_regime():
    """With fewer returns than the classifier's minimum history, every period is
    UNKNOWN — a single-regime report, not garbage."""
    rets = [0.01, -0.01, 0.02, -0.02, 0.01]  # 5 returns, far below min history
    report = compute_regime_report(rets, vol_window=5, lookback=40)
    assert report is not None
    assert report.n_returns == 5
    assert report.n_classified == 0
    assert set(report.per_regime.keys()) == {VolatilityRegime.UNKNOWN}
    unknown = report.per_regime[VolatilityRegime.UNKNOWN]
    assert unknown.n_periods == 5
    assert unknown.fraction == pytest.approx(1.0)


def test_too_little_data_renders_without_error():
    rets = [0.01, -0.01, 0.02]
    text = build_regime_report(rets, vol_window=5, lookback=40)
    assert "REGIME-SEGMENTED REPORT" in text
    assert "unknown" in text


def test_report_is_a_frozen_dataclass():
    rets = _calm_and_turbulent_returns()
    report = compute_regime_report(rets, vol_window=_VOL_WINDOW, lookback=_LOOKBACK)
    assert isinstance(report, RegimeReport)
    with pytest.raises(Exception):
        report.n_returns = 0  # type: ignore[misc]


def test_single_return_classifies_unknown():
    report = compute_regime_report([0.05])
    assert report is not None
    assert report.n_returns == 1
    assert report.n_classified == 0
    assert report.per_regime[VolatilityRegime.UNKNOWN].total_return == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


def test_equities_to_returns_matches_metrics_layer():
    equities = [100.0, 110.0, 99.0]
    rets = _equities_to_returns(equities)
    assert rets == pytest.approx([0.10, -0.10])


def test_ordered_regimes_puts_canonical_first_unexpected_last():
    # Mix canonical enum bands with an unexpected raw string label: the canonical
    # ones order calm->turbulent, the unexpected one sorts after them.
    labels = [VolatilityRegime.HIGH, "weird", VolatilityRegime.LOW]
    ordered = _ordered_regimes(labels)
    keys = [str(getattr(label, "value", label)) for label in ordered]
    assert keys == ["low", "high", "weird"]
