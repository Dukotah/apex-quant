"""
Tests for apex.data.alpaca_options_feed.

The live SDK call is isolated behind an injectable ``chain_fetcher``, so chain
fetching, OCC-based + field-based quote parsing, optional greeks, bad-quote
skipping, deterministic ordering, expiry filtering, and the single-contract lookup
are all exercised offline. No alpaca-py, no network, no real keys.
"""

from __future__ import annotations

from datetime import date, timezone
from decimal import Decimal

import pytest

from apex.core.models import AssetClass, Symbol
from apex.core.option import OptionContract, OptionType
from apex.data.alpaca_options_feed import AlpacaOptionsFeed

UTC = timezone.utc
SPY = Symbol("SPY", AssetClass.ETF, contract_multiplier=Decimal("100"))
EXPIRY = date(2024, 9, 20)


def _occ_row(strike, cp="C", bid="5.00", ask="5.20", last="5.10", **extra):
    contract = OptionContract(
        SPY, EXPIRY, Decimal(str(strike)), OptionType.CALL if cp == "C" else OptionType.PUT
    )
    row = {
        "symbol": contract.occ_symbol,
        "bid": bid,
        "ask": ask,
        "last": last,
        "timestamp": "2024-09-01T15:00:00+00:00",
    }
    row.update(extra)
    return row


def _feed(fetcher, **kw):
    feed = AlpacaOptionsFeed(chain_fetcher=fetcher, **kw)
    feed.connect()
    return feed


# ----------------------------------------------------------------- lifecycle


def test_connect_without_keys_raises(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    feed = AlpacaOptionsFeed()
    with pytest.raises(ConnectionError):
        feed.connect()


def test_injected_fetcher_connects_offline():
    feed = _feed(lambda u, e: [])
    assert feed.is_connected


def test_get_chain_before_connect_raises():
    feed = AlpacaOptionsFeed(chain_fetcher=lambda u, e: [])
    with pytest.raises(RuntimeError):
        feed.get_chain(SPY)


# ----------------------------------------------------------------- chain parsing


def test_get_chain_parses_occ_rows():
    feed = _feed(lambda u, e: [_occ_row(450), _occ_row(455, cp="P")])
    chain = feed.get_chain(SPY, expiry=EXPIRY)
    assert len(chain) == 2
    assert chain[0].contract.strike == Decimal("450")
    assert chain[0].bid == Decimal("5.00")
    assert chain[0].ask == Decimal("5.20")
    assert chain[0].mid == Decimal("5.10")


def test_get_chain_is_deterministically_ordered():
    # Fed out of order; calls before puts at the same strike, then ascending strike.
    feed = _feed(
        lambda u, e: [
            _occ_row(455, cp="C"),
            _occ_row(450, cp="P"),
            _occ_row(450, cp="C"),
        ]
    )
    chain = feed.get_chain(SPY, expiry=EXPIRY)
    keys = [(q.contract.strike, q.contract.option_type) for q in chain]
    assert keys == [
        (Decimal("450"), OptionType.CALL),
        (Decimal("450"), OptionType.PUT),
        (Decimal("455"), OptionType.CALL),
    ]


def test_get_chain_parses_field_based_rows():
    # No OCC symbol — parse from explicit fields instead.
    def fetcher(u, e):
        return [
            {
                "strike": "120.5",
                "option_type": "call",
                "expiry": "2024-09-20",
                "bid": "1.0",
                "ask": "1.2",
                "last": "1.1",
                "timestamp": "2024-09-01T15:00:00+00:00",
            }
        ]

    chain = _feed(fetcher).get_chain(SPY, expiry=EXPIRY)
    assert len(chain) == 1
    assert chain[0].contract.strike == Decimal("120.5")
    assert chain[0].contract.option_type == OptionType.CALL


def test_get_chain_parses_optional_greeks():
    row = _occ_row(450, delta="0.55", gamma="0.10", theta="-0.02", vega="0.30", implied_vol="0.25")
    chain = _feed(lambda u, e: [row]).get_chain(SPY, expiry=EXPIRY)
    g = chain[0].greeks
    assert g is not None
    assert g.delta == 0.55
    assert g.implied_vol == 0.25


def test_get_chain_greeks_none_when_partial():
    # Missing vega → greeks omitted entirely (they're all-or-nothing, and optional).
    row = _occ_row(450, delta="0.55", gamma="0.10", theta="-0.02", implied_vol="0.25")
    chain = _feed(lambda u, e: [row]).get_chain(SPY, expiry=EXPIRY)
    assert chain[0].greeks is None


def test_get_chain_filters_to_requested_expiry():
    other = OptionContract(SPY, date(2024, 10, 18), Decimal("450"), OptionType.CALL)
    rows = [
        _occ_row(450),  # EXPIRY
        {
            "symbol": other.occ_symbol,
            "bid": "5",
            "ask": "5.2",
            "last": "5.1",
            "timestamp": "2024-09-01T15:00:00+00:00",
        },
    ]
    chain = _feed(lambda u, e: rows).get_chain(SPY, expiry=EXPIRY)
    assert len(chain) == 1
    assert chain[0].contract.expiry == EXPIRY


# ----------------------------------------------------------------- bad quotes


def test_skip_invalid_skips_and_counts():
    rows = [
        _occ_row(450),
        {"symbol": "garbage", "bid": "1", "ask": "1", "last": "1", "timestamp": "x"},
    ]
    feed = _feed(lambda u, e: rows)
    chain = feed.get_chain(SPY, expiry=EXPIRY)
    assert len(chain) == 1
    assert feed.skipped_quotes == 1


def test_skip_invalid_false_raises():
    rows = [{"symbol": "garbage", "bid": "1", "ask": "1", "last": "1", "timestamp": "x"}]
    feed = _feed(lambda u, e: rows, skip_invalid=False)
    with pytest.raises(ValueError):
        feed.get_chain(SPY, expiry=EXPIRY)


# ----------------------------------------------------------------- single contract


def test_get_quote_found():
    feed = _feed(lambda u, e: [_occ_row(450), _occ_row(455)])
    contract = OptionContract(SPY, EXPIRY, Decimal("455"), OptionType.CALL)
    quote = feed.get_quote(contract)
    assert quote is not None
    assert quote.contract.strike == Decimal("455")


def test_get_quote_missing_returns_none():
    feed = _feed(lambda u, e: [_occ_row(450)])
    contract = OptionContract(SPY, EXPIRY, Decimal("999"), OptionType.CALL)
    assert feed.get_quote(contract) is None
