"""
apex.data.ohlc_consistency
==========================
Validate OHLC relationships across a series of ``Bar`` models and report
violations — a data-quality gate that runs *after* normalization, before any
bar reaches a strategy or backtest.

A single ``Bar`` already self-validates the two cheapest invariants at
construction time (``high >= low``; no negative prices/volume) — see
``apex.core.models.Bar.__post_init__``. That leaves a whole class of subtler
defects a *technically-valid* ``Bar`` can still carry, plus every relationship
that only exists *between* bars:

  Intra-bar (within one Bar)
    - high must be the session maximum: ``high >= open, close, low``
    - low must be the session minimum:  ``low <= open, close, high``
    - prices should be strictly positive (a $0 print is a data error, not a
      trade); ``Bar`` allows 0, we flag it
    - volume present but zero on a bar with price movement is suspicious

  Inter-bar (between consecutive bars of one symbol)
    - timestamps must be strictly increasing (no duplicates, no time travel)
    - an oversized close-to-open gap may signal an un-adjusted split/dividend
      or a bad print (reported, never "fixed")

This module is **pure** (no I/O, no clock, no randomness) and deterministic, so
it is fully unit-testable offline. It is also *non-destructive*: it never mutates
or drops a ``Bar`` — it only describes what it found, and the caller decides
whether to skip-and-count or abort (mirroring how ``HistoricalDataFeed`` already
treats a bad row).

Decimal vs float: every comparison here is on prices/volumes, which are
``Decimal`` on a ``Bar``. We stay in ``Decimal`` throughout — no float ever
touches a price comparison — so the check is exact. The single ratio used for
the optional gap test is computed in ``Decimal`` as well.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import List, Optional, Sequence

from apex.core.models import Bar


class Violation(str, Enum):
    """The kinds of OHLC defect this module detects."""

    HIGH_NOT_MAX = "high_not_max"  # high < one of open/low/close
    LOW_NOT_MIN = "low_not_min"  # low > one of open/high/close
    HIGH_LT_LOW = "high_lt_low"  # high < low (Bar normally blocks this)
    NON_POSITIVE_PRICE = "non_positive_price"  # an O/H/L/C <= 0
    NEGATIVE_VOLUME = "negative_volume"  # volume < 0
    ZERO_VOLUME_WITH_RANGE = "zero_volume_with_range"  # vol == 0 but high != low
    NON_INCREASING_TIME = "non_increasing_time"  # ts <= previous ts (same symbol)
    EXCESSIVE_GAP = "excessive_gap"  # |open-prev_close|/prev_close too large


@dataclass(frozen=True)
class OHLCViolation:
    """
    One detected defect, located precisely.

    ``index`` is the position in the *input sequence* (not a sort order — this
    module never reorders). ``bar`` is the offending bar. For inter-bar checks,
    ``bar`` is the *later* bar of the pair (the one whose context is wrong).
    ``detail`` is a human-readable explanation safe to log.
    """

    index: int
    kind: Violation
    bar: Bar
    detail: str


@dataclass(frozen=True)
class OHLCReport:
    """
    The result of validating a series: every violation found, plus light counts.

    ``is_consistent`` is True iff no violations were found. ``checked`` is the
    number of bars examined. Falsy-friendly: ``if not report:`` is True when the
    series is clean.
    """

    checked: int
    violations: tuple[OHLCViolation, ...]

    @property
    def is_consistent(self) -> bool:
        return len(self.violations) == 0

    def __bool__(self) -> bool:
        # A report is "truthy" when it carries violations — so `if report:` reads
        # as "if there are problems". Mirrors how callers branch on findings.
        return len(self.violations) > 0

    def kinds(self) -> dict[Violation, int]:
        """Count of violations grouped by kind (deterministic insertion order)."""
        counts: dict[Violation, int] = {}
        for v in self.violations:
            counts[v.kind] = counts.get(v.kind, 0) + 1
        return counts


# --------------------------------------------------------------------- single bar


def check_bar(bar: Bar, index: int = 0) -> List[OHLCViolation]:
    """
    Validate the intra-bar OHLC relationships of a single ``Bar``.

    Returns a (possibly empty) list of violations. Pure and order-independent —
    the same bar always yields the same violations. ``index`` is recorded on each
    violation so callers can locate it in a larger sequence.

    Note ``Bar`` itself already rejects ``high < low`` and negative prices at
    construction; we still test those here because a caller may hand us a ``Bar``
    built by other means, and a defensive check costs nothing. Detecting a
    problem twice is fine; missing it is not (fail closed).
    """
    out: List[OHLCViolation] = []
    o, h, lo, c, v = bar.open, bar.high, bar.low, bar.close, bar.volume

    if h < lo:
        out.append(
            OHLCViolation(
                index,
                Violation.HIGH_LT_LOW,
                bar,
                f"high {h} < low {lo}",
            )
        )

    # high must dominate open/close/low; low must be dominated by open/close/high.
    above_high = [name for name, p in (("open", o), ("close", c), ("low", lo)) if p > h]
    if above_high:
        out.append(
            OHLCViolation(
                index,
                Violation.HIGH_NOT_MAX,
                bar,
                f"high {h} is below {', '.join(above_high)}",
            )
        )
    below_low = [name for name, p in (("open", o), ("close", c), ("high", h)) if p < lo]
    if below_low:
        out.append(
            OHLCViolation(
                index,
                Violation.LOW_NOT_MIN,
                bar,
                f"low {lo} is above {', '.join(below_low)}",
            )
        )

    non_positive = [
        name for name, p in (("open", o), ("high", h), ("low", lo), ("close", c)) if p <= 0
    ]
    if non_positive:
        out.append(
            OHLCViolation(
                index,
                Violation.NON_POSITIVE_PRICE,
                bar,
                f"non-positive price(s): {', '.join(non_positive)}",
            )
        )

    if v < 0:
        out.append(
            OHLCViolation(
                index,
                Violation.NEGATIVE_VOLUME,
                bar,
                f"negative volume {v}",
            )
        )
    elif v == 0 and h != lo:
        out.append(
            OHLCViolation(
                index,
                Violation.ZERO_VOLUME_WITH_RANGE,
                bar,
                f"zero volume but price range {lo}..{h}",
            )
        )

    return out


# --------------------------------------------------------------------- series


def _gap_ratio(prev_close: Decimal, open_: Decimal) -> Optional[Decimal]:
    """
    Absolute fractional gap |open - prev_close| / prev_close, or None when it is
    undefined (non-positive previous close). Kept in ``Decimal`` for exactness.
    """
    if prev_close <= 0:
        return None
    return abs(open_ - prev_close) / prev_close


def check_series(
    bars: Sequence[Bar],
    *,
    max_gap: Optional[float | Decimal] = None,
) -> OHLCReport:
    """
    Validate a series of bars and return a full ``OHLCReport``.

    Runs every intra-bar check (``check_bar``) on each bar, then the inter-bar
    checks between consecutive bars **of the same symbol**:

      - strictly increasing timestamps (a duplicate or out-of-order timestamp is
        ``NON_INCREASING_TIME``)
      - an optional close-to-open gap test: if ``max_gap`` is given (e.g. ``0.5``
        for 50%), any open that jumps more than that fraction from the prior
        close is flagged ``EXCESSIVE_GAP``. Off by default — gaps are normal;
        the test exists to surface un-adjusted splits/bad prints when a caller
        opts in.

    The series is examined **in the order given** — this module never reorders;
    ordering is the feed's job (``HistoricalDataFeed`` sorts before yielding).
    A series with fewer than two bars trivially passes the inter-bar checks.
    Empty input returns an empty, consistent report (graceful, never garbage).

    ``max_gap`` accepts ``float`` or ``Decimal`` for ergonomic callers; it is
    coerced via ``str()`` so a float literal cannot smuggle a binary artifact
    into the threshold.
    """
    gap_threshold: Optional[Decimal] = None
    if max_gap is not None:
        gap_threshold = Decimal(str(max_gap))
        if gap_threshold < 0:
            raise ValueError(f"max_gap must be non-negative, got {max_gap!r}")

    violations: List[OHLCViolation] = []
    # Per-symbol memory of the previous bar, so interleaved multi-symbol series
    # are compared correctly (each symbol against its own prior bar).
    prev_by_ticker: dict[str, Bar] = {}

    for index, bar in enumerate(bars):
        violations.extend(check_bar(bar, index))

        ticker = bar.symbol.ticker
        prev = prev_by_ticker.get(ticker)
        if prev is not None:
            if bar.timestamp <= prev.timestamp:
                violations.append(
                    OHLCViolation(
                        index,
                        Violation.NON_INCREASING_TIME,
                        bar,
                        f"timestamp {bar.timestamp.isoformat()} <= previous "
                        f"{prev.timestamp.isoformat()} for {ticker}",
                    )
                )
            if gap_threshold is not None:
                ratio = _gap_ratio(prev.close, bar.open)
                if ratio is not None and ratio > gap_threshold:
                    violations.append(
                        OHLCViolation(
                            index,
                            Violation.EXCESSIVE_GAP,
                            bar,
                            f"open {bar.open} gaps {ratio:.4f} from prev close "
                            f"{prev.close} (> {gap_threshold})",
                        )
                    )
        prev_by_ticker[ticker] = bar

    return OHLCReport(checked=len(bars), violations=tuple(violations))
