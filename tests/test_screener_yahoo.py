"""
tests/test_screener_yahoo.py
============================
Unit tests for the PURE core of the Yahoo runners screener — parsing a Yahoo
screener envelope, the gap / relative-volume math, and the multi-screen merge +
tagging. None of these touch the network: they feed static JSON-shaped dicts to
the pure functions, so the suite can never go flaky on a Yahoo rate limit.

The thin I/O layer (_default_fetcher / fetch_screens / main) is excluded from
coverage (pragma) and not exercised here — it's the boundary, the core is the logic.
"""

from __future__ import annotations

from decimal import Decimal

from scripts.screener_yahoo import (
    Quote,
    candidate_to_row,
    gap_pct,
    merge_screens,
    parse_quotes,
    relative_volume,
)


def _quote_dict(
    symbol, *, price=None, change=None, prev=None, open_=None, vol=None, avg=None, cap=None
):
    """Build a Yahoo-shaped quote dict, omitting unset fields (as Yahoo does)."""
    d = {"symbol": symbol}
    if price is not None:
        d["regularMarketPrice"] = price
    if change is not None:
        d["regularMarketChangePercent"] = change
    if prev is not None:
        d["regularMarketPreviousClose"] = prev
    if open_ is not None:
        d["regularMarketOpen"] = open_
    if vol is not None:
        d["regularMarketVolume"] = vol
    if avg is not None:
        d["averageDailyVolume3Month"] = avg
    if cap is not None:
        d["marketCap"] = cap
    return d


def _envelope(quotes):
    """Wrap quote dicts in the Yahoo screener result envelope."""
    return {"finance": {"result": [{"quotes": quotes}]}}


# ------------------------------------------------------------------ parse_quotes


def test_parse_quotes_extracts_fields():
    payload = _envelope(
        [
            _quote_dict(
                "AAA", price=12.5, change=8.4, prev=11.0, open_=11.6, vol=2000, avg=1000, cap=5e8
            )
        ]
    )
    quotes = parse_quotes(payload)
    assert len(quotes) == 1
    q = quotes[0]
    assert q.ticker == "AAA"
    assert q.price == Decimal("12.5")
    assert q.change_pct == Decimal("8.4")
    assert q.prev_close == Decimal("11.0")
    assert q.open_price == Decimal("11.6")
    assert q.volume == 2000
    assert q.avg_volume == 1000
    assert q.market_cap == Decimal("500000000")


def test_parse_quotes_unwraps_raw_form():
    # Some Yahoo endpoints wrap numbers as {"raw": x, "fmt": "..."}.
    payload = _envelope([{"symbol": "BBB", "regularMarketPrice": {"raw": 3.21, "fmt": "3.21"}}])
    q = parse_quotes(payload)[0]
    assert q.price == Decimal("3.21")


def test_parse_quotes_skips_symbolless_and_missing_fields_are_none():
    payload = _envelope([{"regularMarketPrice": 5.0}, _quote_dict("CCC", price=5.0)])
    quotes = parse_quotes(payload)
    assert [q.ticker for q in quotes] == ["CCC"]
    assert quotes[0].volume is None
    assert quotes[0].market_cap is None


def test_parse_quotes_malformed_envelope_is_empty():
    assert parse_quotes({}) == []
    assert parse_quotes({"finance": {"result": []}}) == []
    assert parse_quotes({"finance": {"result": [{"quotes": "nope"}]}}) == []


# --------------------------------------------------------------------- the math


def test_gap_pct():
    assert gap_pct(Decimal("105"), Decimal("100")) == Decimal("5")
    assert gap_pct(Decimal("98"), Decimal("100")) == Decimal("-2")
    assert gap_pct(None, Decimal("100")) is None
    assert gap_pct(Decimal("105"), None) is None
    assert gap_pct(Decimal("105"), Decimal("0")) is None  # no divide-by-zero


def test_relative_volume():
    assert relative_volume(2000, 1000) == Decimal("2")
    assert relative_volume(None, 1000) is None
    assert relative_volume(2000, 0) is None  # no divide-by-zero


# ------------------------------------------------------------------ merge_screens


def test_merge_tags_ticker_present_in_multiple_screens():
    shared = Quote("SHR", Decimal("50"), Decimal("4"), None, None, None, None, None)
    only_g = Quote("GNR", Decimal("10"), Decimal("9"), None, None, None, None, None)
    out = merge_screens([("gainer", [shared, only_g]), ("active", [shared])])
    by_ticker = {c.ticker: c for c in out}
    assert by_ticker["SHR"].screens == ("active", "gainer")  # both, sorted
    assert by_ticker["GNR"].screens == ("gainer",)


def test_merge_derives_gapper_tag_on_threshold():
    # Gaps +6% (open 106 vs prev 100) on 3x volume -> qualifies as a gapper.
    runner = Quote(
        "RUN", Decimal("106"), Decimal("6"), Decimal("100"), Decimal("106"), 3000, 1000, None
    )
    # Only +1% gap -> below the 3% default, not a gapper.
    tame = Quote(
        "TAM", Decimal("101"), Decimal("1"), Decimal("100"), Decimal("101"), 3000, 1000, None
    )
    out = {c.ticker: c for c in merge_screens([("gainer", [runner, tame])])}
    assert "gapper" in out["RUN"].screens
    assert out["RUN"].gap_pct == Decimal("6")
    assert out["RUN"].rel_volume == Decimal("3")
    assert "gapper" not in out["TAM"].screens


def test_merge_gapper_respects_price_ceiling():
    pricey = Quote(
        "HII", Decimal("500"), Decimal("8"), Decimal("450"), Decimal("500"), 5000, 1000, None
    )
    out = {c.ticker: c for c in merge_screens([("gainer", [pricey])], price_max=Decimal("20"))}
    assert "gapper" not in out["HII"].screens  # over the $20 ceiling


def test_merge_prefers_first_non_none_field():
    partial = Quote("PRT", None, Decimal("3"), None, None, None, None, None)
    full = Quote("PRT", Decimal("42"), Decimal("3"), Decimal("40"), Decimal("41"), 100, 50, None)
    out = {c.ticker: c for c in merge_screens([("gainer", [partial]), ("active", [full])])}
    assert out["PRT"].price == Decimal("42")  # the real number, not the leading None


def test_merge_sorts_by_change_desc():
    a = Quote("LO", Decimal("10"), Decimal("2"), None, None, None, None, None)
    b = Quote("HI", Decimal("10"), Decimal("20"), None, None, None, None, None)
    c = Quote("MID", Decimal("10"), Decimal("11"), None, None, None, None, None)
    out = merge_screens([("gainer", [a, b, c])])
    assert [x.ticker for x in out] == ["HI", "MID", "LO"]


def test_candidate_to_row_blanks_unknowns():
    q = Quote("ZZZ", None, Decimal("5"), None, None, None, None, None)
    row = candidate_to_row(merge_screens([("gainer", [q])])[0])
    assert row["ticker"] == "ZZZ"
    assert row["price"] == ""  # None -> empty cell
    assert row["screens"] == "gainer"
