"""
apex.data.alpaca_options_feed
=============================
AlpacaOptionsFeed: option chain + single-contract quotes from Alpaca.

This mirrors ``apex.data.alpaca_feed``'s design exactly: the ONLY code that
touches the network is a single injectable ``chain_fetcher`` callable built in
``connect()``. Everything else — normalization of raw quotes into ``OptionQuote``,
filtering to a requested expiry, bad-quote skipping, deterministic ordering — is
pure and unit-tested by injecting a fake fetcher. The live alpaca-py path is a
thin, lazily-imported wrapper that needs real keys + network and is verified in
paper, not in CI.

Greeks are OPTIONAL: Alpaca's options snapshot may or may not include them
depending on the data feed/subscription, so ``OptionQuote.greeks`` can be None.

Determinism: ``get_chain`` returns quotes sorted by (strike, call-before-put), so
the same fetched data always yields the same ordering — no hidden ``now()``.

NOTE: this is a DRAFT subsystem on a feature branch — nothing here is wired into
the live ``TradingEngine`` yet. It does not subclass ``BaseDataFeed`` because that
contract is bar/MarketEvent-shaped (``stream() -> Iterator[MarketEvent]``), which
does not fit a request/response option-chain query. See the FINAL REPORT for the
wiring this would need to integrate.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Iterable, List, Mapping, Optional

from apex.core.models import Symbol
from apex.core.option import OptionContract, OptionGreeks, OptionQuote, OptionType

logger = logging.getLogger(__name__)

# A chain fetcher maps (underlying ticker, expiry-or-None) → an iterable of raw
# quote objects/dicts. expiry=None means "all listed expiries". This is the one
# seam the real SDK plugs into; tests inject a fake.
ChainFetcher = Callable[[str, Optional[date]], Iterable[object]]


def _get(raw: object, *names: str) -> object:
    """Read the first present attribute/key from a raw quote (dict or object)."""
    for name in names:
        if isinstance(raw, Mapping):
            if name in raw:
                return raw[name]
        elif hasattr(raw, name):
            return getattr(raw, name)
    raise KeyError(f"none of {names} present on raw quote {raw!r}")


def _opt_get(raw: object, *names: str) -> object:
    """Like ``_get`` but returns None instead of raising when absent."""
    try:
        return _get(raw, *names)
    except KeyError:
        return None


class AlpacaOptionsFeed:
    """
    On-demand option chains + single-contract quotes from Alpaca.

    Typical use::

        feed = AlpacaOptionsFeed(chain_fetcher=my_fetcher)  # or live: no fetcher
        feed.connect()
        chain = feed.get_chain(spy, expiry=date(2024, 9, 20))   # list[OptionQuote]
        quote = feed.get_quote(contract)                        # OptionQuote | None
        feed.disconnect()

    Parameters
    ----------
    api_key / api_secret:
        Alpaca credentials. Default: ALPACA_API_KEY / ALPACA_SECRET_KEY env vars.
    feed_source:
        Alpaca options feed ("indicative" free, "opra" paid). Default "indicative".
    skip_invalid:
        skip + count malformed quotes (True) or raise (False).
    chain_fetcher:
        DEPENDENCY INJECTION for tests — replaces the live SDK call. When provided,
        connect() uses it and never imports alpaca-py.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        feed_source: str = "indicative",
        skip_invalid: bool = True,
        chain_fetcher: Optional[ChainFetcher] = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("ALPACA_API_KEY")
        self.api_secret = api_secret if api_secret is not None else os.getenv("ALPACA_SECRET_KEY")
        self.feed_source = feed_source
        self.skip_invalid = skip_invalid

        self._fetcher: Optional[ChainFetcher] = chain_fetcher
        self._injected_fetcher = chain_fetcher is not None
        self._connected: bool = False
        self.skipped_quotes: int = 0

    # ----------------------------------------------------------------- lifecycle

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        """
        Build the option-data client. With an injected fetcher this is a no-op
        (tests); otherwise it requires credentials and the alpaca-py SDK.
        """
        if self._injected_fetcher:
            self._connected = True
            logger.info("AlpacaOptionsFeed connected (injected fetcher — offline/test mode).")
            return
        if not self.api_key or not self.api_secret:
            raise ConnectionError(
                "Alpaca credentials missing. Set ALPACA_API_KEY / ALPACA_SECRET_KEY "
                "or inject a chain_fetcher for tests."
            )
        self._fetcher = self._build_sdk_fetcher()
        self._connected = True
        logger.info("AlpacaOptionsFeed connected (live SDK, feed=%s).", self.feed_source)

    def disconnect(self) -> None:
        """Tear down. Idempotent."""
        self._connected = False

    def __enter__(self) -> "AlpacaOptionsFeed":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()

    # --------------------------------------------------------------- queries

    def get_chain(self, underlying: Symbol, expiry: Optional[date] = None) -> List[OptionQuote]:
        """
        Fetch the option chain for ``underlying`` (optionally a single ``expiry``)
        as a deterministically-ordered list of ``OptionQuote``.

        Malformed quotes are skipped + counted (or raise, if skip_invalid=False).
        Quotes are sorted by (expiry, strike, calls-before-puts).
        """
        if not self._connected or self._fetcher is None:
            raise RuntimeError("get_chain() called before connect()")

        self.skipped_quotes = 0
        raw_quotes = self._fetcher(underlying.ticker, expiry)
        quotes: List[OptionQuote] = []
        for idx, raw in enumerate(raw_quotes):
            quote = self._to_quote(raw, underlying, idx)
            if quote is None:
                continue
            if expiry is not None and quote.contract.expiry != expiry:
                continue  # fetcher returned a wider window than asked
            quotes.append(quote)

        quotes.sort(
            key=lambda q: (
                q.contract.expiry,
                q.contract.strike,
                0 if q.contract.option_type is OptionType.CALL else 1,
            )
        )
        logger.info(
            "AlpacaOptionsFeed fetched %d quotes for %s (expiry=%s, %d skipped).",
            len(quotes),
            underlying.ticker,
            expiry,
            self.skipped_quotes,
        )
        return quotes

    def get_quote(self, contract: OptionContract) -> Optional[OptionQuote]:
        """
        Fetch a single contract's quote, or None if it isn't in the chain.

        Implemented on top of ``get_chain`` (one network call) and matched by the
        deterministic OCC symbol, so a contract is identified unambiguously.
        """
        chain = self.get_chain(contract.underlying, expiry=contract.expiry)
        target = contract.occ_symbol
        for quote in chain:
            if quote.contract.occ_symbol == target:
                return quote
        logger.info("Contract %s not found in chain.", target)
        return None

    # ----------------------------------------------------------------- internals

    def _to_quote(self, raw: object, underlying: Symbol, idx: int) -> Optional[OptionQuote]:
        """Convert one raw quote (dict or attribute object) into an OptionQuote, or skip."""
        try:
            return self._parse_quote(raw, underlying)
        except (ValueError, TypeError, KeyError) as exc:
            if not self.skip_invalid:
                raise ValueError(
                    f"Invalid option quote #{idx} for {underlying.ticker}: {exc}"
                ) from exc
            self.skipped_quotes += 1
            logger.warning(
                "Skipping invalid option quote #%d for %s: %s", idx, underlying.ticker, exc
            )
            return None

    @staticmethod
    def _parse_quote(raw: object, underlying: Symbol) -> OptionQuote:
        """
        Build an OptionQuote from a raw quote. The contract identity comes from an
        OCC symbol when present (authoritative + reversible); otherwise from explicit
        expiry/strike/type fields.
        """
        occ = _opt_get(raw, "symbol", "occ_symbol", "contract")
        if occ is not None and isinstance(occ, str):
            contract = OptionContract.parse_occ(
                occ,
                asset_class=underlying.asset_class,
                contract_multiplier=underlying.contract_multiplier,
            )
        else:
            expiry_raw = _get(raw, "expiry", "expiration_date", "expiration")
            expiry = expiry_raw if isinstance(expiry_raw, date) else _parse_date(str(expiry_raw))
            strike = _to_decimal(_get(raw, "strike", "strike_price"))
            type_raw = str(_get(raw, "option_type", "type", "right")).lower()
            option_type = OptionType.CALL if type_raw.startswith("c") else OptionType.PUT
            contract = OptionContract(
                underlying=underlying,
                expiry=expiry,
                strike=strike,
                option_type=option_type,
            )

        bid = _to_decimal(_opt_get(raw, "bid", "bid_price") or 0)
        ask = _to_decimal(_opt_get(raw, "ask", "ask_price") or 0)
        last = _to_decimal(_opt_get(raw, "last", "last_price", "close") or 0)
        ts_raw = _get(raw, "timestamp", "ts")
        timestamp = ts_raw if isinstance(ts_raw, datetime) else _parse_datetime(str(ts_raw))

        greeks = AlpacaOptionsFeed._parse_greeks(raw)
        return OptionQuote(
            contract=contract,
            bid=bid,
            ask=ask,
            last=last,
            timestamp=timestamp,
            greeks=greeks,
        )

    @staticmethod
    def _parse_greeks(raw: object) -> Optional[OptionGreeks]:
        """Build OptionGreeks if all sensitivities are present; otherwise None (they're optional)."""
        delta = _opt_get(raw, "delta")
        gamma = _opt_get(raw, "gamma")
        theta = _opt_get(raw, "theta")
        vega = _opt_get(raw, "vega")
        iv = _opt_get(raw, "implied_vol", "implied_volatility", "iv")
        if any(v is None for v in (delta, gamma, theta, vega, iv)):
            return None
        return OptionGreeks(
            delta=float(delta),  # type: ignore[arg-type]
            gamma=float(gamma),  # type: ignore[arg-type]
            theta=float(theta),  # type: ignore[arg-type]
            vega=float(vega),  # type: ignore[arg-type]
            implied_vol=float(iv),  # type: ignore[arg-type]
        )

    def _build_sdk_fetcher(self) -> ChainFetcher:
        """
        Build the real alpaca-py options fetcher. Imported lazily so the module and
        test suite load without the SDK. Verified against paper keys, not in CI.
        """
        try:  # pragma: no cover - requires alpaca-py + network
            from alpaca.data.historical.option import OptionHistoricalDataClient
            from alpaca.data.requests import OptionChainRequest
        except ImportError as exc:  # pragma: no cover
            raise ConnectionError(
                "alpaca-py is required for live options data. `pip install alpaca-py` "
                "or inject a chain_fetcher for offline use."
            ) from exc

        client = OptionHistoricalDataClient(self.api_key, self.api_secret)

        def fetch(underlying_ticker, expiry):  # pragma: no cover - live path
            kwargs = {"underlying_symbol": underlying_ticker, "feed": self.feed_source}
            if expiry is not None:
                kwargs["expiration_date"] = expiry
            request = OptionChainRequest(**kwargs)
            snapshots = client.get_option_chain(request)
            # snapshots is {occ_symbol: OptionsSnapshot}; flatten to one dict per contract.
            out = []
            for occ, snap in snapshots.items():
                quote = getattr(snap, "latest_quote", None)
                trade = getattr(snap, "latest_trade", None)
                greeks = getattr(snap, "greeks", None)
                row = {
                    "symbol": occ,
                    "bid": getattr(quote, "bid_price", 0) if quote else 0,
                    "ask": getattr(quote, "ask_price", 0) if quote else 0,
                    "last": getattr(trade, "price", 0) if trade else 0,
                    "timestamp": getattr(quote, "timestamp", None) if quote else None,
                }
                if greeks is not None:
                    row.update(
                        {
                            "delta": getattr(greeks, "delta", None),
                            "gamma": getattr(greeks, "gamma", None),
                            "theta": getattr(greeks, "theta", None),
                            "vega": getattr(greeks, "vega", None),
                            "implied_vol": getattr(snap, "implied_volatility", None),
                        }
                    )
                out.append(row)
            return out

        return fetch


# ------------------------------------------------------------------- helpers


def _to_decimal(val: object) -> Decimal:
    """Coerce a raw numeric (str/int/float/Decimal) to Decimal via str (no float artifacts)."""
    if isinstance(val, Decimal):
        return val
    return Decimal(str(val))


def _parse_date(s: str) -> date:
    return date.fromisoformat(s[:10])


def _parse_datetime(s: str) -> datetime:
    return datetime.fromisoformat(s)
