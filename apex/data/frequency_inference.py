"""
apex.data.frequency_inference
=============================
Infer the bar timeframe / cadence from a sequence of bar timestamps.

A feed (a CSV file, an SDK response, a hand-built fixture) hands us a column of
timestamps but does not always tell us *what cadence* those bars are — is this a
one-minute stream, hourly bars, or daily bars? Several Apex components reason in
terms of an explicit timeframe string ("1Min" / "5Min" / "1Hour" / "1Day"),
so this module recovers that label from the data itself.

The approach is deliberately simple and deterministic:

  - Sort the (deduplicated) timestamps and take the consecutive *gaps* between
    them. The bar cadence is the **modal** (most common) gap — robust to the
    holes a real calendar leaves: weekends, holidays, and the occasional missing
    bar all show up as *larger* multiples of the true step, never smaller, so
    they cannot outvote the true cadence as long as most adjacent bars are one
    step apart.
  - That modal gap (a ``timedelta``) is then rendered back into the canonical
    Apex timeframe spelling so the result round-trips through the data layer's
    ``parse_timeframe`` (e.g. a 300-second modal gap → ``"5Min"``).

This module is pure: no I/O, no network, no clock, no randomness. The same
timestamps always yield the same answer, so it is fully unit-testable offline.

Insufficient data fails *soft*: fewer than two distinct timestamps means there
is no gap to measure, so the inference functions return ``None`` rather than
guessing. Callers decide whether a missing cadence is fatal.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Iterable, Optional

# Canonical unit spellings → the duration of one of that unit, matched against a
# modal gap to choose the coarsest exact unit. Ordered coarse→fine so that, e.g.,
# a one-day gap renders as "1Day" rather than "24Hour" or "1440Min". Kept in sync
# with the rest of the data layer's accepted spellings ("1Min"/"1Hour"/"1Day").
_UNIT_ORDER: tuple[tuple[str, timedelta], ...] = (
    ("Week", timedelta(weeks=1)),
    ("Day", timedelta(days=1)),
    ("Hour", timedelta(hours=1)),
    ("Min", timedelta(minutes=1)),
    ("Sec", timedelta(seconds=1)),
)


def _sorted_unique(timestamps: Iterable[datetime]) -> list[datetime]:
    """Return the distinct timestamps in ascending order (duplicates collapsed)."""
    return sorted(set(timestamps))


def consecutive_gaps(timestamps: Iterable[datetime]) -> list[timedelta]:
    """
    The positive gaps between consecutive distinct, sorted timestamps.

    Duplicates are collapsed and ordering is normalized first, so the gaps are
    always strictly positive regardless of the input order. Fewer than two
    distinct timestamps yields an empty list (there is nothing to measure).
    """
    ordered = _sorted_unique(timestamps)
    return [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]


def modal_gap(timestamps: Iterable[datetime]) -> Optional[timedelta]:
    """
    The most common gap between consecutive distinct timestamps, or ``None`` when
    there are fewer than two distinct timestamps.

    Ties are broken toward the **smaller** gap: when two gap sizes occur equally
    often, the finer cadence wins, since the true step can only ever be matched or
    exceeded (calendar holes never produce a *smaller-than-true* gap), so the
    smallest contender is the safest estimate of the underlying bar step.
    """
    gaps = consecutive_gaps(timestamps)
    if not gaps:
        return None
    counts = Counter(gaps)
    best_count = max(counts.values())
    # Among the most-frequent gaps, prefer the smallest duration.
    return min(g for g, c in counts.items() if c == best_count)


def timedelta_to_timeframe(delta: timedelta) -> Optional[str]:
    """
    Render a positive ``timedelta`` into the canonical Apex timeframe spelling
    (e.g. ``timedelta(minutes=5)`` → ``"5Min"``, ``timedelta(days=1)`` →
    ``"1Day"``).

    The coarsest unit that divides the duration evenly is chosen, so a whole
    number of days renders as ``"NDay"`` rather than in hours or minutes. A
    non-positive duration, or one not expressible as a whole number of any known
    unit (e.g. 90 seconds, which is neither a whole minute nor a whole hour but
    *is* a whole 90 seconds), falls through to the finest unit (seconds). Returns
    ``None`` only for a non-positive duration.
    """
    if delta <= timedelta(0):
        return None
    total_seconds = delta.total_seconds()
    for label, unit in _UNIT_ORDER:
        unit_seconds = unit.total_seconds()
        amount = total_seconds / unit_seconds
        # Exact whole-number multiple of this unit?
        if amount == int(amount):
            return f"{int(amount)}{label}"
    # Sub-second residue (shouldn't happen for market bars) — express in seconds.
    return None


def infer_timeframe(timestamps: Iterable[datetime]) -> Optional[str]:
    """
    Infer the bar timeframe string from a sequence of bar timestamps.

    Returns the canonical Apex spelling (``"1Min"``, ``"5Min"``, ``"1Hour"``,
    ``"1Day"``, ...) matching the modal gap between consecutive distinct,
    chronologically-sorted timestamps. Input order does not matter; duplicates
    are ignored.

    Returns ``None`` when the cadence cannot be determined — fewer than two
    distinct timestamps, or a modal gap that is not a whole number of any known
    unit. Never guesses: an undeterminable cadence is reported as ``None``, not
    fabricated.
    """
    delta = modal_gap(timestamps)
    if delta is None:
        return None
    return timedelta_to_timeframe(delta)
