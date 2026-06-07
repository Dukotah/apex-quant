"""
apex.core.option
================
Option instrument models — a self-contained, DRAFT subsystem for the Phase 4
options plumbing. Equities/crypto/futures flow through ``apex.core.models``;
options need a richer instrument identity (underlying + expiry + strike + right)
plus a multi-leg order shape (verticals, etc.), so they get their own models here.

Everything is frozen (immutable) and money is ``Decimal`` — the same invariants
the rest of the core enforces. The OCC-21 symbol is the canonical, deterministic,
reversible string identity for a contract:

    OCC-21 = ROOT(6, left-justified, space-padded)
           + YYMMDD expiry
           + 'C' | 'P'
           + STRIKE * 1000, zero-padded to 8 digits

    e.g.  OptionContract(SPY, 2024-09-20, 450, CALL).occ_symbol == "SPY   240920C00450000"

This file defines the EXACT public interface strategy code is written against, so
the names/shapes here are load-bearing — do not rename without updating callers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from apex.core.models import AssetClass, Symbol


class OptionType(str, Enum):
    """Call or put — the contract's fundamental right."""

    CALL = "call"
    PUT = "put"


class OptionRight(str, Enum):
    """Position intent for a leg: are we BUYing (long) or SELLing (short) it."""

    BUY = "buy"
    SELL = "sell"


# OCC-21 layout: 6-char root, 6-digit yymmdd, C/P, 8-digit strike*1000.
_OCC_RE = re.compile(
    r"^(?P<root>.{6})(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<cp>[CP])(?P<strike>\d{8})$"
)

# Sane bounds for expiry validation — reject obviously corrupt dates, but stay wide
# enough for LEAPS. The framework is brand new (2026), so anything pre-2000 or far
# beyond a decade out is almost certainly a bug, not a real listed contract.
_MIN_EXPIRY_YEAR = 2000
_MAX_EXPIRY_YEAR = 2100


@dataclass(frozen=True)
class OptionContract:
    """
    A single listed option contract: a right (call/put) on ``underlying`` to
    transact at ``strike`` on or before ``expiry``.

    ``underlying`` reuses the core ``Symbol`` so the options subsystem inherits the
    contract_multiplier (typically 100 for equity options) and exchange metadata.
    """

    underlying: Symbol
    expiry: date
    strike: Decimal
    option_type: OptionType

    def __post_init__(self) -> None:
        if not isinstance(self.strike, Decimal):
            raise TypeError(f"OptionContract.strike must be Decimal, got {type(self.strike)}")
        if self.strike <= 0:
            raise ValueError(f"OptionContract.strike must be > 0, got {self.strike}")
        if not isinstance(self.expiry, date):
            raise TypeError(f"OptionContract.expiry must be a date, got {type(self.expiry)}")
        if not (_MIN_EXPIRY_YEAR <= self.expiry.year <= _MAX_EXPIRY_YEAR):
            raise ValueError(
                f"OptionContract.expiry year {self.expiry.year} outside sane "
                f"[{_MIN_EXPIRY_YEAR}, {_MAX_EXPIRY_YEAR}]"
            )
        # OCC root is 6 chars max; a longer underlying ticker can't be encoded.
        if len(self.underlying.ticker) > 6:
            raise ValueError(
                f"OptionContract underlying ticker '{self.underlying.ticker}' exceeds 6 chars "
                "(OCC root limit)"
            )
        if not self.underlying.ticker:
            raise ValueError("OptionContract underlying ticker is empty")
        # Strike must be representable as an integer count of thousandths (OCC encodes
        # strike*1000 as an 8-digit integer). Reject sub-tenth-cent strikes / overflow.
        scaled = self.strike * 1000
        if scaled != scaled.to_integral_value():
            raise ValueError(
                f"OptionContract.strike {self.strike} is finer than $0.001 — not OCC-encodable"
            )
        if scaled >= Decimal("100000000"):  # 8 digits → max 99999.999
            raise ValueError(f"OptionContract.strike {self.strike} too large for OCC 8-digit field")

    @property
    def occ_symbol(self) -> str:
        """The canonical OCC-21 string identity. Deterministic and reversible."""
        root = self.underlying.ticker.upper().ljust(6)
        yymmdd = self.expiry.strftime("%y%m%d")
        cp = "C" if self.option_type is OptionType.CALL else "P"
        strike_int = int((self.strike * 1000).to_integral_value())
        return f"{root}{yymmdd}{cp}{strike_int:08d}"

    @staticmethod
    def parse_occ(
        occ_symbol: str,
        *,
        asset_class: AssetClass = AssetClass.EQUITY,
        contract_multiplier: Decimal = Decimal("100"),
    ) -> "OptionContract":
        """
        Reverse of ``occ_symbol``: parse an OCC-21 string back into a contract.

        The OCC string does not carry the underlying's asset class or multiplier, so
        those are supplied here (defaults match standard US equity options: a 100x
        multiplier). Round-trips exactly for any contract this module produced.
        """
        m = _OCC_RE.match(occ_symbol)
        if m is None:
            raise ValueError(f"Not a valid OCC-21 symbol: {occ_symbol!r}")
        root = m.group("root").strip().upper()
        if not root:
            raise ValueError(f"OCC symbol has empty root: {occ_symbol!r}")
        year = 2000 + int(m.group("yy"))
        expiry = date(year, int(m.group("mm")), int(m.group("dd")))
        option_type = OptionType.CALL if m.group("cp") == "C" else OptionType.PUT
        strike = Decimal(m.group("strike")) / 1000
        underlying = Symbol(
            ticker=root,
            asset_class=asset_class,
            contract_multiplier=contract_multiplier,
        )
        return OptionContract(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
        )


@dataclass(frozen=True)
class OptionGreeks:
    """Risk sensitivities + implied vol for a contract. All plain floats (model output)."""

    delta: float
    gamma: float
    theta: float
    vega: float
    implied_vol: float


@dataclass(frozen=True)
class OptionQuote:
    """A point-in-time market quote for one contract, with optional greeks."""

    contract: OptionContract
    bid: Decimal
    ask: Decimal
    last: Decimal
    timestamp: datetime
    greeks: Optional[OptionGreeks] = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("OptionQuote timestamp must be timezone-aware (UTC)")
        for name in ("bid", "ask", "last"):
            val = getattr(self, name)
            if not isinstance(val, Decimal):
                raise TypeError(f"OptionQuote.{name} must be Decimal, got {type(val)}")
            if val < 0:
                raise ValueError(f"OptionQuote.{name} must be >= 0, got {val}")
        if self.ask < self.bid:
            raise ValueError(f"OptionQuote ask {self.ask} < bid {self.bid} (crossed quote)")

    @property
    def mid(self) -> Decimal:
        """Mid price between bid and ask."""
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class OptionLeg:
    """One leg of a (possibly multi-leg) option order: a contract, a side, a ratio."""

    contract: OptionContract
    right: OptionRight
    ratio: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.ratio, int) or isinstance(self.ratio, bool):
            raise TypeError(f"OptionLeg.ratio must be int, got {type(self.ratio)}")
        if self.ratio < 1:
            raise ValueError(f"OptionLeg.ratio must be >= 1, got {self.ratio}")


@dataclass(frozen=True)
class OptionOrder:
    """
    A single- or multi-leg option order (e.g. a vertical spread is 2 legs).

    ``quantity`` is the number of *spreads* (contracts of the whole structure) in
    integer contracts. ``limit_price`` is the net debit/credit limit for the whole
    order; None means a market order (defined-risk strategies should prefer limits).
    """

    legs: tuple[OptionLeg, ...]
    quantity: int
    limit_price: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if not isinstance(self.legs, tuple):
            raise TypeError("OptionOrder.legs must be a tuple (it is frozen/hashable)")
        if not self.legs:
            raise ValueError("OptionOrder requires at least one leg")
        if not isinstance(self.quantity, int) or isinstance(self.quantity, bool):
            raise TypeError(f"OptionOrder.quantity must be int, got {type(self.quantity)}")
        if self.quantity <= 0:
            raise ValueError(f"OptionOrder.quantity must be > 0, got {self.quantity}")
        if self.limit_price is not None:
            if not isinstance(self.limit_price, Decimal):
                raise TypeError("OptionOrder.limit_price must be Decimal or None")
        # All legs must share an underlying — a spread is on one name.
        underlyings = {leg.contract.underlying.ticker for leg in self.legs}
        if len(underlyings) > 1:
            raise ValueError(f"OptionOrder legs span multiple underlyings: {sorted(underlyings)}")

    @property
    def is_multi_leg(self) -> bool:
        return len(self.legs) > 1
