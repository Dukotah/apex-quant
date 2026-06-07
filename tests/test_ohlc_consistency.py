"""Tests for apex.data.ohlc_consistency — full-path import, self-contained."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from apex.core.models import AssetClass, Bar, Symbol
from apex.data.ohlc_consistency import (
    OHLCReport,
    OHLCViolation,
    Violation,
    check_bar,
    check_series,
)

SYM = Symbol(ticker="AAPL", asset_class=AssetClass.EQUITY)
SYM2 = Symbol(ticker="MSFT", asset_class=AssetClass.EQUITY)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bar(
    o="100", h="110", lo="90", c="105", v="1000",
    *, ts=T0, symbol=SYM,
) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
        volume=Decimal(v),
        timeframe="1Day",
    )


# ----------------------------------------------------------------- single bar

def test_clean_bar_has_no_violations():
    assert check_bar(_bar()) == []


def test_high_below_open_flagged():
    # open 120 > high 110 — Bar allows it (high>=low holds), we catch it.
    v = check_bar(_bar(o="120"))
    kinds = {x.kind for x in v}
    assert Violation.HIGH_NOT_MAX in kinds
    assert v[0].index == 0


def test_high_below_close_flagged():
    v = check_bar(_bar(c="115"))
    assert any(x.kind is Violation.HIGH_NOT_MAX for x in v)


def test_low_above_close_flagged():
    v = check_bar(_bar(c="80"))  # close 80 < low 90
    assert any(x.kind is Violation.LOW_NOT_MIN for x in v)


def test_low_above_open_flagged():
    v = check_bar(_bar(o="85"))  # open 85 < low 90
    assert any(x.kind is Violation.LOW_NOT_MIN for x in v)


def test_zero_price_flagged():
    # Bar permits 0 (only rejects < 0); we flag non-positive.
    v = check_bar(_bar(lo="0", o="0"))
    assert any(x.kind is Violation.NON_POSITIVE_PRICE for x in v)


def test_zero_volume_with_range_flagged():
    v = check_bar(_bar(v="0"))  # high 110 != low 90
    assert any(x.kind is Violation.ZERO_VOLUME_WITH_RANGE for x in v)


def test_zero_volume_flat_bar_ok():
    # No range → zero volume is fine (e.g. an illiquid flat bar).
    v = check_bar(_bar(o="100", h="100", lo="100", c="100", v="0"))
    assert v == []


def test_high_lt_low_detected_when_constructed_directly():
    # Construct a Bar that violates high>=low. Bar.__post_init__ would normally
    # block this, so we patch around it via object.__setattr__ on a frozen copy.
    b = _bar()
    object.__setattr__(b, "high", Decimal("80"))  # now high 80 < low 90
    v = check_bar(b)
    kinds = {x.kind for x in v}
    assert Violation.HIGH_LT_LOW in kinds


def test_index_propagates():
    v = check_bar(_bar(o="200"), index=7)
    assert all(x.index == 7 for x in v)
    assert v[0].bar.open == Decimal("200")


# ----------------------------------------------------------------- series

def test_empty_series_is_consistent():
    r = check_series([])
    assert isinstance(r, OHLCReport)
    assert r.is_consistent
    assert r.checked == 0
    assert not r  # falsy when clean


def test_single_bad_bar_series():
    r = check_series([_bar(o="999")])
    assert not r.is_consistent
    assert r.checked == 1
    assert bool(r) is True  # truthy when problems exist


def test_clean_multi_bar_series():
    bars = [_bar(ts=T0 + timedelta(days=i)) for i in range(5)]
    r = check_series(bars)
    assert r.is_consistent
    assert r.checked == 5


def test_non_increasing_timestamp_flagged():
    bars = [_bar(ts=T0), _bar(ts=T0)]  # duplicate timestamp
    r = check_series(bars)
    kinds = r.kinds()
    assert kinds.get(Violation.NON_INCREASING_TIME) == 1
    # The violation is recorded against the *later* bar (index 1).
    nit = [x for x in r.violations if x.kind is Violation.NON_INCREASING_TIME][0]
    assert nit.index == 1


def test_out_of_order_timestamp_flagged():
    bars = [_bar(ts=T0 + timedelta(days=1)), _bar(ts=T0)]
    r = check_series(bars)
    assert Violation.NON_INCREASING_TIME in r.kinds()


def test_per_symbol_timestamps_compared_independently():
    # Interleaved symbols sharing timestamps must NOT trip the time check —
    # each symbol is compared only against its own previous bar.
    bars = [
        _bar(ts=T0, symbol=SYM),
        _bar(ts=T0, symbol=SYM2),
        _bar(ts=T0 + timedelta(days=1), symbol=SYM),
        _bar(ts=T0 + timedelta(days=1), symbol=SYM2),
    ]
    r = check_series(bars)
    assert r.is_consistent


def test_excessive_gap_off_by_default():
    bars = [_bar(c="100", ts=T0), _bar(o="200", ts=T0 + timedelta(days=1))]
    r = check_series(bars)  # no max_gap → gaps ignored
    assert Violation.EXCESSIVE_GAP not in r.kinds()


def test_excessive_gap_flagged_when_enabled():
    # prev close 100, open 200 → ratio 1.0 > 0.5 threshold.
    bars = [_bar(c="100", ts=T0), _bar(o="200", ts=T0 + timedelta(days=1))]
    r = check_series(bars, max_gap=0.5)
    assert r.kinds().get(Violation.EXCESSIVE_GAP) == 1


def test_gap_within_threshold_ok():
    # prev close 100, open 140 → ratio 0.4 <= 0.5.
    bars = [_bar(c="100", ts=T0), _bar(o="140", h="150", c="145",
                                       ts=T0 + timedelta(days=1))]
    r = check_series(bars, max_gap=0.5)
    assert Violation.EXCESSIVE_GAP not in r.kinds()


def test_gap_exact_threshold_not_flagged():
    # ratio exactly 0.5 is NOT > 0.5 → allowed (boundary is inclusive of OK).
    bars = [_bar(c="100", ts=T0), _bar(o="150", ts=T0 + timedelta(days=1))]
    r = check_series(bars, max_gap=Decimal("0.5"))
    assert Violation.EXCESSIVE_GAP not in r.kinds()


def test_negative_max_gap_rejected():
    with pytest.raises(ValueError):
        check_series([_bar()], max_gap=-0.1)


def test_kinds_counts_multiple():
    # Two bad bars of different kinds plus a duplicate-timestamp.
    bars = [
        _bar(o="200", ts=T0),                       # HIGH_NOT_MAX
        _bar(v="0", ts=T0),                         # ZERO_VOLUME_WITH_RANGE + NON_INCREASING_TIME
    ]
    r = check_series(bars)
    counts = r.kinds()
    assert counts[Violation.HIGH_NOT_MAX] == 1
    assert counts[Violation.ZERO_VOLUME_WITH_RANGE] == 1
    assert counts[Violation.NON_INCREASING_TIME] == 1


def test_violation_is_frozen():
    v = check_bar(_bar(o="200"))[0]
    assert isinstance(v, OHLCViolation)
    with pytest.raises(Exception):
        v.index = 5  # frozen dataclass


def test_determinism_same_input_same_output():
    bars = [_bar(o="200", ts=T0 + timedelta(days=i)) for i in range(3)]
    r1 = check_series(bars, max_gap=0.1)
    r2 = check_series(bars, max_gap=0.1)
    assert [(x.index, x.kind, x.detail) for x in r1.violations] == \
           [(x.index, x.kind, x.detail) for x in r2.violations]
