"""
apex.strategy.weighting
=======================
Pure portfolio-weighting helpers shared across strategies.

These functions turn a universe (or a per-symbol volatility map) into a set of
NON-NEGATIVE position weights expressed as ``decimal.Decimal`` that sum to 1
(or to a caller-provided ``cap``). They mirror the inverse-volatility / risk-parity
sizing already used inline by
``apex.strategy.library.multi_asset_trend.MultiAssetTrendStrategy`` and factor it
out so any strategy can reuse the exact same, deterministic math.

DESIGN CONTRACT (every function obeys this):
  - Output weights are ``Decimal``, non-negative, and sum EXACTLY to ``cap``
    (default ``Decimal("1")``) — the largest weight absorbs any rounding residue
    so the sum is exact regardless of division remainder.
  - Deterministic: identical inputs → identical outputs. Insertion order of the
    input mapping/sequence is preserved (Python dicts are ordered), so the result
    is reproducible. No wall-clock, no randomness, no I/O.
  - Edge cases are handled gracefully, never with garbage:
      * empty input            → empty dict
      * single asset           → that asset gets the full ``cap``
      * zero / None / NaN vol  → that sleeve is SKIPPED (it cannot be risk-sized);
                                 if EVERY vol is unusable we fall back to equal
                                 weight over the original universe.

ARCHITECTURE NOTE: this is pure logic. It does not size dollars, touch a broker,
or emit orders — strategies feed the resulting weight into a ``SignalEvent``
``strength`` and the RiskManager remains the sole sizer (Golden Rule 2).

Money/weights use ``Decimal`` (Golden Rule 14). Realized-vol inputs are plain
floats (the statistical layer's convention, matching ``apex/validation/metrics.py``
and the ``_vol`` map in ``multi_asset_trend``).
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Dict, Mapping, Optional, Sequence


def _key(symbol: object) -> str:
    """Canonical string key for a symbol: its ``.ticker`` if present, else ``str``."""
    ticker = getattr(symbol, "ticker", None)
    return ticker if isinstance(ticker, str) else str(symbol)


def _usable_vol(vol: Optional[float]) -> bool:
    """A vol is usable for inverse-vol sizing only if it is a finite positive number."""
    if vol is None:
        return False
    try:
        v = float(vol)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0.0


def _distribute(raw: Dict[str, Decimal], cap: Decimal) -> Dict[str, Decimal]:
    """
    Normalize a map of non-negative raw weights to sum EXACTLY to ``cap``.

    The proportional share is computed per key; the rounding residue (cap minus the
    sum of the parts) is added to the LARGEST raw-weight key so the total is exact
    and deterministic. Assumes ``raw`` is non-empty with a positive total.
    """
    total = sum(raw.values(), Decimal("0"))
    if total <= 0:
        # Degenerate — split the cap evenly so we still return a valid distribution.
        return _even(list(raw.keys()), cap)
    out: Dict[str, Decimal] = {k: (cap * w) / total for k, w in raw.items()}
    residue = cap - sum(out.values(), Decimal("0"))
    if residue != 0:
        # Largest raw weight absorbs the rounding remainder (stable: first max wins).
        anchor = max(raw, key=lambda k: raw[k])
        out[anchor] += residue
    return out


def _even(keys: Sequence[str], cap: Decimal) -> Dict[str, Decimal]:
    """Equal split of ``cap`` across ``keys`` with exact-sum residue handling."""
    if not keys:
        return {}
    n = len(keys)
    share = cap / Decimal(n)
    out: Dict[str, Decimal] = {k: share for k in keys}
    residue = cap - share * Decimal(n)
    if residue != 0:
        out[keys[0]] += residue
    return out


def equal_weight(
    symbols: Sequence[object],
    cap: Decimal = Decimal("1"),
) -> Dict[str, Decimal]:
    """
    Equal weight across ``symbols``: each gets ``cap / n``.

    Accepts ``Symbol`` objects or plain ticker strings (keyed by ticker). Duplicate
    tickers collapse to a single key. Empty input → empty dict; single → full cap.
    Weights sum EXACTLY to ``cap``.
    """
    # Preserve order, de-duplicate by ticker.
    keys: list[str] = []
    seen: set[str] = set()
    for s in symbols:
        k = _key(s)
        if k not in seen:
            seen.add(k)
            keys.append(k)
    return _even(keys, cap)


def inverse_vol_weight(
    vol_by_symbol: Mapping[str, Optional[float]],
    cap: Decimal = Decimal("1"),
) -> Dict[str, Decimal]:
    """
    Inverse-volatility weights: weight_i ∝ 1 / vol_i, normalized to ``cap``.

    The lower a sleeve's realized volatility, the larger its weight — the standard
    risk-parity tilt that equalizes risk contribution (mirrors the ``min_vol/vol_i``
    conviction in ``multi_asset_trend``, expressed here as proper portfolio weights).

    Sleeves with zero / None / NaN / negative vol are SKIPPED (they cannot be
    risk-sized). If every sleeve is unusable, falls back to equal weight over the
    original keys. Empty input → empty dict. Result sums EXACTLY to ``cap``.
    """
    if not vol_by_symbol:
        return {}
    raw: Dict[str, Decimal] = {}
    for ticker, vol in vol_by_symbol.items():
        if _usable_vol(vol):
            raw[ticker] = Decimal("1") / Decimal(str(float(vol)))
    if not raw:
        # No usable vol anywhere — degrade gracefully to equal weight.
        return _even(list(vol_by_symbol.keys()), cap)
    return _distribute(raw, cap)


def risk_parity_weight(
    vol_by_symbol: Mapping[str, Optional[float]],
    cap: Decimal = Decimal("1"),
) -> Dict[str, Decimal]:
    """
    Naive (correlation-free) risk-parity weights.

    For a diagonal covariance assumption, equalizing each asset's risk contribution
    reduces to inverse-volatility weighting — so this is the inverse-vol allocation,
    exposed under its portfolio-theory name. Use ``correlation_down_weight`` on top
    when you also want to penalize clustered (highly correlated) sleeves.

    Same edge-case handling and exact-sum guarantee as ``inverse_vol_weight``.
    """
    return inverse_vol_weight(vol_by_symbol, cap=cap)


def correlation_down_weight(
    weights: Mapping[str, Decimal],
    corr_by_symbol: Mapping[str, float],
    *,
    max_penalty: Decimal = Decimal("0.75"),
    cap: Optional[Decimal] = None,
) -> Dict[str, Decimal]:
    """
    Correlation-aware DOWN-weighting helper.

    Given an existing weight map and a per-symbol correlation (e.g. each sleeve's
    correlation to a benchmark or to the rest of the book, as produced by
    ``apex.validation.metrics.correlation``), shrink each weight by how correlated
    it is — diversifying (low/negative-corr) sleeves keep their size; crowded
    (high-corr) sleeves are trimmed. The trimmed weight is then RE-NORMALIZED so the
    book still sums to ``cap``.

    Penalty model (clamped, deterministic):
        factor_i = 1 - max_penalty * clamp(corr_i, 0, 1)
    A correlation of 0 (or negative) keeps the full weight; a correlation of 1
    applies the full ``max_penalty`` haircut. Symbols absent from ``corr_by_symbol``
    (or with NaN/None corr) are treated as uncorrelated (no penalty).

    Args:
        weights: base weights to adjust (e.g. from ``inverse_vol_weight``).
        corr_by_symbol: per-symbol correlation in roughly [-1, 1].
        max_penalty: maximum fractional haircut at corr=1 (0..1).
        cap: target sum after re-normalization; defaults to the input weights' sum
             (so a capped book stays capped, a full book stays full).

    Empty input → empty dict. If every weight collapses to zero, falls back to
    equal weight over the keys. Result sums EXACTLY to the effective cap.
    """
    if not weights:
        return {}
    target = cap if cap is not None else sum(weights.values(), Decimal("0"))
    if target <= 0:
        return {k: Decimal("0") for k in weights}

    mp = max_penalty
    if mp < Decimal("0"):
        mp = Decimal("0")
    if mp > Decimal("1"):
        mp = Decimal("1")

    raw: Dict[str, Decimal] = {}
    for ticker, w in weights.items():
        base = w if w > 0 else Decimal("0")
        corr = corr_by_symbol.get(ticker)
        c = 0.0
        if corr is not None:
            try:
                cv = float(corr)
                if math.isfinite(cv):
                    c = cv
            except (TypeError, ValueError):
                c = 0.0
        # Clamp correlation into [0, 1]: only positive crowding is penalized.
        c_clamped = Decimal(str(max(0.0, min(1.0, c))))
        factor = Decimal("1") - mp * c_clamped
        raw[ticker] = base * factor

    if sum(raw.values(), Decimal("0")) <= 0:
        return _even(list(weights.keys()), target)
    return _distribute(raw, target)
