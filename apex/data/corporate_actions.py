"""
apex.data.corporate_actions
===========================
Back-adjust an OHLCV ``Bar`` series for stock splits and cash dividends.

Raw price history has discontinuities a strategy must never see: on a 4-for-1
split day a $400 stock "drops" to $100, and on an ex-dividend day the price
gaps down by the dividend amount. Neither is a real return — feeding raw prices
to an indicator (SMA, RSI, returns) manufactures phantom signals. This module
produces a *continuous, total-return* series so that the percentage move from
one bar to the next reflects only genuine market action.

Method — back-adjustment (the industry standard for backtests):
  - The most recent bar is left untouched (its prices are "today's" reality).
  - Every bar strictly *before* an action's effective date is multiplied by a
    cumulative adjustment factor so the series joins smoothly across the gap.
  - **Split factor:** for an ``a``-for-``b`` split (e.g. 4-for-1 → ratio 4), all
    prior prices are divided by the ratio and prior volume multiplied by it.
  - **Dividend factor:** prior prices are scaled by ``(1 - dividend/close_before)``
    where ``close_before`` is the close of the last bar *before* the ex-date —
    the standard CRSP-style proportional dividend adjustment. Volume is untouched.

Factors compound: when several actions precede a bar, that bar carries the
product of all their factors. Adjustment is applied oldest-action-first by
walking the series once per action, which keeps each step independently verifiable.

Purity & determinism: this is the data layer's translation work, so — like the
normalizer — it is pure (no I/O, no clock, no randomness) and operates only on
``Decimal`` money, parsed via ``str()`` first so no binary-float artifact can
enter price math. Bad input fails loud (``ValueError``) so the caller decides
whether to skip-and-count or abort. Insufficient data (empty series, no actions,
or actions outside the series window) degrades gracefully: the series is returned
unchanged rather than garbage.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import List, Sequence

from apex.core.models import Bar


class CorporateActionType(str, Enum):
    """The two corporate actions that distort raw price history."""
    SPLIT = "split"
    DIVIDEND = "dividend"


@dataclass(frozen=True)
class CorporateAction:
    """
    A single corporate action affecting one instrument.

    ``effective_date`` is the ex-date (UTC): the first bar on or after this
    timestamp trades at the *new* (post-action) price, so every bar strictly
    before it is what gets back-adjusted.

    ``value`` carries the action's magnitude:
      - SPLIT: the split ratio as ``new_shares / old_shares``. A 4-for-1 split
        is ``Decimal("4")``; a 1-for-10 reverse split is ``Decimal("0.1")``.
        Must be > 0.
      - DIVIDEND: the cash dividend per share in the instrument's currency.
        Must be > 0.

    For convenience a split may instead be given as ``new`` and ``old`` share
    counts via :meth:`split`; both paths produce the same frozen record.
    """
    action_type: CorporateActionType
    effective_date: datetime
    value: Decimal

    def __post_init__(self) -> None:
        if self.effective_date.tzinfo is None:
            raise ValueError(
                f"CorporateAction effective_date must be timezone-aware (UTC): {self.effective_date!r}"
            )
        if self.value <= 0:
            raise ValueError(
                f"CorporateAction value must be > 0, got {self.value!r} for {self.action_type}"
            )

    @classmethod
    def split(cls, effective_date: object, new: object, old: object = 1) -> "CorporateAction":
        """Build a SPLIT action from ``new``-for-``old`` share counts (e.g. 4-for-1)."""
        ratio = _to_decimal(new, field="split new") / _to_decimal(old, field="split old")
        return cls(CorporateActionType.SPLIT, _to_utc(effective_date), ratio)

    @classmethod
    def dividend(cls, effective_date: object, amount: object) -> "CorporateAction":
        """Build a DIVIDEND action from a cash-per-share ``amount``."""
        return cls(CorporateActionType.DIVIDEND, _to_utc(effective_date), _to_decimal(amount, field="dividend"))


# --------------------------------------------------------------------- scalars

def _to_decimal(value: object, *, field: str = "value") -> Decimal:
    """Coerce to ``Decimal`` via ``str()`` first (no float artifacts). Fails loud."""
    if value is None or value == "":
        raise ValueError(f"{field} is missing/empty")
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} is not a number: {value!r}") from exc


def _to_utc(value: object) -> datetime:
    """Coerce an effective-date of unknown shape into a UTC-aware ``datetime``."""
    if value is None or value == "":
        raise ValueError("effective_date is missing/empty")
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("effective_date is missing/empty")
        if text[-1] in ("Z", "z"):
            text = text[:-1] + "+00:00"   # 3.11-safe Zulu handling
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"unparseable effective_date {value!r}: {exc}") from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)   # naive → assume UTC
    return dt.astimezone(timezone.utc)            # aware → convert to UTC


# ------------------------------------------------------------------- adjustment

def _scale_bar(bar: Bar, price_factor: Decimal, volume_factor: Decimal) -> Bar:
    """Return a copy of ``bar`` with prices and volume scaled by the given factors."""
    return replace(
        bar,
        open=bar.open * price_factor,
        high=bar.high * price_factor,
        low=bar.low * price_factor,
        close=bar.close * price_factor,
        volume=bar.volume * volume_factor,
    )


def _apply_split(bars: List[Bar], effective_date: datetime, ratio: Decimal) -> List[Bar]:
    """Divide pre-ex-date prices by ``ratio`` and multiply their volume by it."""
    price_factor = Decimal("1") / ratio
    out: List[Bar] = []
    for bar in bars:
        if bar.timestamp < effective_date:
            out.append(_scale_bar(bar, price_factor, ratio))
        else:
            out.append(bar)
    return out


def _apply_dividend(bars: List[Bar], effective_date: datetime, amount: Decimal) -> List[Bar]:
    """
    Proportionally scale pre-ex-date prices by ``(1 - amount/close_before)``.

    ``close_before`` is the close of the last bar strictly before the ex-date.
    If there is no such bar (the dividend predates the series) or it is too small
    to absorb the dividend, the series is returned unchanged — fail closed rather
    than produce a non-positive or nonsensical price.
    """
    close_before: Decimal | None = None
    for bar in bars:
        if bar.timestamp < effective_date:
            close_before = bar.close
        else:
            break
    if close_before is None or close_before <= amount:
        return list(bars)

    price_factor = Decimal("1") - amount / close_before
    out: List[Bar] = []
    for bar in bars:
        if bar.timestamp < effective_date:
            out.append(_scale_bar(bar, price_factor, Decimal("1")))   # volume unchanged
        else:
            out.append(bar)
    return out


def adjust_bars(
    bars: Sequence[Bar],
    actions: Sequence[CorporateAction],
) -> List[Bar]:
    """
    Back-adjust ``bars`` for ``actions`` (splits and cash dividends).

    The series is assumed chronological (oldest→newest); it is processed in the
    order given, so pass it sorted (as ``HistoricalDataFeed`` already yields it).
    Returns a new list of new ``Bar`` objects — the inputs (frozen) are untouched.

    Each action scales every bar strictly *before* its ``effective_date``; the
    most recent bars stay at their raw, real-world prices. Multiple actions
    compound naturally because each is applied to the output of the previous one.

    Graceful degradation: an empty ``bars`` or empty ``actions`` returns the bars
    unchanged (as a new list); an action whose ex-date falls outside the series
    is a no-op. Never returns garbage and never mutates the inputs.
    """
    result: List[Bar] = list(bars)
    if not result or not actions:
        return result

    # Apply oldest-action-first so compounding is deterministic and each step is
    # independently checkable. Stable sort keeps same-date actions in input order.
    ordered = sorted(actions, key=lambda a: a.effective_date)
    for action in ordered:
        if action.action_type is CorporateActionType.SPLIT:
            result = _apply_split(result, action.effective_date, action.value)
        elif action.action_type is CorporateActionType.DIVIDEND:
            result = _apply_dividend(result, action.effective_date, action.value)
        else:  # pragma: no cover - enum is exhaustive; fail closed on the impossible
            raise ValueError(f"unknown corporate action type: {action.action_type!r}")
    return result
