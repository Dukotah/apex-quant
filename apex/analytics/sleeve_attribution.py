"""
apex.analytics.sleeve_attribution
=================================
Per-sleeve (per-symbol) P&L attribution. A multi-sleeve book reports a single
aggregate equity curve, which can hide one quietly-failing sleeve behind the
others. This module breaks the realized result down PER SYMBOL so a dead or
bleeding sleeve is visible on its own line.

The natural input is the trade history the system already produces: a sequence
of :class:`apex.core.events.FillEvent` (each carries ``symbol``/``side``/
``quantity``/``fill_price``/``commission``). Fills are not themselves trades —
a round-trip trade is an entry matched against a later exit — so we match them
into closed round-trip trades per symbol and book the realized P&L of each.

Matching rule: FIFO (first-in, first-out) per symbol, the standard tax-lot and
broker convention. Within a symbol, opening fills (in the position's current
direction) are pushed onto a lot queue; closing fills (the opposite side) are
matched against the oldest open lots first, and each matched quantity books a
realized trade. A fill that flips the position (closes the whole book AND opens
the other way) is split: the closing part matches existing lots, the remainder
opens a new lot in the new direction. Commission is money-movement, so it is
attributed to realized P&L: an opening lot carries its entry commission forward
pro-rata by matched quantity, and a closing fill's commission is charged to the
trades it closes pro-rata by matched quantity. Open (unclosed) lots at the end
contribute NO realized P&L — only closed round-trips count.

This is exact money math, so — like :mod:`apex.risk.portfolio`, the other
place round-trip P&L is booked — it uses :class:`decimal.Decimal` throughout,
NOT the float convention of the statistical layer (``apex.validation.metrics``).
A sleeve's P&L is dollars and cents, not a ratio.

A trade is a WIN when its realized P&L (net of commission) is strictly > 0, a
LOSS when strictly < 0, and a scratch (breakeven) when exactly 0 — mirroring
the strict ``> 0`` / ``< 0`` partitioning used across the analytics layer.

All functions are pure and deterministic given their inputs: no I/O, no wall
clock, no randomness. Degenerate inputs degrade gracefully — an empty fill list
yields an empty attribution, a symbol with only opening fills yields zero closed
trades, and a zero capital base yields a 0 return contribution rather than a
divide-by-zero. Tested in tests/test_sleeve_attribution.py against hand-computed
values.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Deque, Dict, List, Sequence, Tuple

from apex.core.events import FillEvent
from apex.core.models import OrderSide

_ZERO = Decimal("0")


@dataclass(frozen=True)
class SleeveAttribution:
    """
    Immutable per-symbol (per-sleeve) attribution summary.

    Attributes:
        ticker: the sleeve's instrument symbol.
        realized_pnl: total realized P&L of all closed round-trip trades for this
            sleeve, NET of entry and exit commissions (account currency, Decimal).
        trade_count: number of closed round-trip trades (open lots excluded).
        win_count: closed trades with realized P&L strictly > 0.
        loss_count: closed trades with realized P&L strictly < 0.
        win_rate: ``win_count / trade_count`` (0..1); ``Decimal("0")`` when there
            are no closed trades.
        return_contribution: ``realized_pnl / capital_base`` — this sleeve's
            realized P&L as a fraction of the book's capital base, so the sum
            across sleeves is the book's total realized return on that base.
            ``Decimal("0")`` when the capital base is not positive.
    """

    ticker: str
    realized_pnl: Decimal
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: Decimal
    return_contribution: Decimal


def match_round_trips(fills: Sequence[FillEvent]) -> List[Decimal]:
    """
    Match a single symbol's fills into closed round-trip trades via FIFO and
    return each closed trade's realized P&L (net of commission), in close order.

    Fills must all be for the same symbol; the caller (:func:`attribute_fills`)
    groups by ticker first. Quantities are taken as positive magnitudes (the
    :class:`FillEvent` invariant) and the side determines direction.

    A BUY opens/extends a long lot or closes short lots (oldest first); a SELL
    opens/extends a short lot or closes long lots (oldest first). Each matched
    quantity books one realized trade:

      - long closed by SELL:  ``(exit - entry) * qty * multiplier``
      - short closed by BUY:  ``(entry - exit) * qty * multiplier``

    Commission is folded in: each open lot stores its entry commission and pays
    it forward pro-rata by the quantity matched; the closing fill's commission is
    charged to the trades it closes pro-rata by matched quantity. Open lots left
    over at the end contribute nothing (only round-trips are realized).
    """
    # Each lot: (remaining_qty, entry_price, remaining_entry_commission).
    long_lots: Deque[Tuple[Decimal, Decimal, Decimal]] = deque()
    short_lots: Deque[Tuple[Decimal, Decimal, Decimal]] = deque()
    trades: List[Decimal] = []

    for fill in fills:
        qty = fill.quantity
        if qty <= _ZERO:
            continue
        price = fill.fill_price
        mult = fill.symbol.contract_multiplier
        commission = fill.commission

        if fill.side == OrderSide.BUY:
            # BUY closes shorts first (FIFO), any remainder opens a long lot.
            remaining = _close_lots(
                opposing=short_lots,
                close_qty=qty,
                close_price=price,
                close_commission=commission,
                multiplier=mult,
                is_long_close=False,
                trades=trades,
            )
            if remaining > _ZERO:
                # Commission on the OPENING portion of this fill: pro-rata by the
                # quantity that opened (the rest was spent closing shorts above).
                open_commission = commission * remaining / qty
                long_lots.append((remaining, price, open_commission))
        else:  # SELL closes longs first (FIFO), any remainder opens a short lot.
            remaining = _close_lots(
                opposing=long_lots,
                close_qty=qty,
                close_price=price,
                close_commission=commission,
                multiplier=mult,
                is_long_close=True,
                trades=trades,
            )
            if remaining > _ZERO:
                open_commission = commission * remaining / qty
                short_lots.append((remaining, price, open_commission))

    return trades


def _close_lots(
    *,
    opposing: Deque[Tuple[Decimal, Decimal, Decimal]],
    close_qty: Decimal,
    close_price: Decimal,
    close_commission: Decimal,
    multiplier: Decimal,
    is_long_close: bool,
    trades: List[Decimal],
) -> Decimal:
    """
    Match ``close_qty`` against the oldest opposing lots (FIFO), booking a realized
    trade per matched chunk, and return the unmatched quantity (which opens a new
    lot in the closing direction).

    ``is_long_close`` True means we are SELLing into existing LONG lots; False means
    BUYing to cover existing SHORT lots. The closing commission is allocated pro-rata
    across the quantity actually matched here (the opening remainder is charged by the
    caller), so total commission is conserved exactly.
    """
    remaining = close_qty
    while remaining > _ZERO and opposing:
        lot_qty, entry_price, lot_commission = opposing[0]
        matched = min(remaining, lot_qty)

        # Gross P&L on the matched quantity.
        if is_long_close:
            gross = (close_price - entry_price) * matched * multiplier
        else:
            gross = (entry_price - close_price) * matched * multiplier

        # Entry commission carried by this matched slice of the lot (pro-rata).
        entry_comm_share = lot_commission * matched / lot_qty
        # Exit commission for this matched slice (pro-rata over the whole close).
        exit_comm_share = close_commission * matched / close_qty

        trades.append(gross - entry_comm_share - exit_comm_share)

        leftover_lot = lot_qty - matched
        if leftover_lot > _ZERO:
            opposing[0] = (leftover_lot, entry_price, lot_commission - entry_comm_share)
        else:
            opposing.popleft()
        remaining -= matched

    return remaining


def attribute_fills(
    fills: Sequence[FillEvent],
    capital_base: Decimal = _ZERO,
) -> Dict[str, SleeveAttribution]:
    """
    Break a book's fill history down into per-symbol (per-sleeve) attribution.

    Args:
        fills: the chronological fill history of the whole book (any mix of
            symbols). Fills are grouped by ``symbol.ticker`` and each group is
            matched into closed round-trip trades via FIFO
            (:func:`match_round_trips`). Order within a ticker is preserved, so
            pass fills oldest-first for correct FIFO matching.
        capital_base: the book's capital base (e.g. starting equity) used to
            express each sleeve's realized P&L as a return contribution. When it
            is not strictly positive, every ``return_contribution`` is
            ``Decimal("0")`` (fail closed — no divide-by-zero, no garbage).

    Returns:
        A dict mapping ticker -> :class:`SleeveAttribution`. Every ticker that
        appears in ``fills`` is present, even if it produced no CLOSED trades (in
        which case its counts and realized P&L are zero). Summing
        ``realized_pnl`` over the result gives the book's total realized P&L from
        closed round-trips, and summing ``return_contribution`` gives the book's
        total realized return on ``capital_base``. An empty ``fills`` yields an
        empty dict.
    """
    grouped: Dict[str, List[FillEvent]] = {}
    for fill in fills:
        grouped.setdefault(fill.symbol.ticker, []).append(fill)

    use_base = capital_base > _ZERO

    out: Dict[str, SleeveAttribution] = {}
    for ticker, sym_fills in grouped.items():
        trade_pnls = match_round_trips(sym_fills)
        realized = sum(trade_pnls, _ZERO)
        n = len(trade_pnls)
        wins = sum(1 for p in trade_pnls if p > _ZERO)
        losses = sum(1 for p in trade_pnls if p < _ZERO)
        win_rate = (Decimal(wins) / Decimal(n)) if n else _ZERO
        contribution = (realized / capital_base) if use_base else _ZERO

        out[ticker] = SleeveAttribution(
            ticker=ticker,
            realized_pnl=realized,
            trade_count=n,
            win_count=wins,
            loss_count=losses,
            win_rate=win_rate,
            return_contribution=contribution,
        )
    return out
