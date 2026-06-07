"""
apex.analytics.monthly_returns_table
====================================
Returns-by-month and by-year matrix from a dated return series — the classic
"monthly returns table" you see on every tearsheet: one row per calendar year,
twelve month columns (Jan..Dec) plus a year-total column, each cell a compounded
return for that month (or year).

A flat list of daily returns answers "what was the Sharpe"; this answers "*when*
did the strategy make and lose money", which is what actually builds (or
destroys) confidence in an edge. Seeing a single month that carried the whole
year, or a quietly bleeding stretch of red, is impossible from a headline number.

This is statistical/reporting code, so it follows the float convention of
``apex.validation.metrics`` rather than Decimal: the inputs are already-computed
fractional returns (0.01 = +1%), not money.

All functions are pure and deterministic given their inputs. There is no I/O and
no wall-clock access — dates are supplied by the caller. Months/years with no
observations are reported as ``None`` (not 0.0) so callers can distinguish "flat"
from "no data" and fail closed. Tested in tests/test_monthly_returns_table.py
against hand-computed values.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

# (year, month) -> compounded fractional return for that calendar month.
MonthKey = Tuple[int, int]

MONTH_ABBRS: Tuple[str, ...] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _compound(returns: Sequence[float]) -> Optional[float]:
    """
    Geometrically compound a sequence of fractional returns into one return.

    (1 + r1)(1 + r2)... - 1. Returns ``None`` for an empty sequence so the
    caller can tell "no observations" apart from a genuine 0.0 return.
    """
    if not returns:
        return None
    growth = 1.0
    for r in returns:
        growth *= 1.0 + r
    return growth - 1.0


def monthly_returns(
    dated_returns: Sequence[Tuple[date, float]],
) -> Dict[MonthKey, float]:
    """
    Compound a dated return series into one return per calendar month.

    Args:
        dated_returns: iterable of ``(date, return)`` pairs. ``return`` is a
            fraction (0.01 = +1%). Order does not matter — returns within the
            same month are compounded in chronological (date, then original
            input) order so the result is deterministic regardless of how the
            caller sorted the input. Multiple returns sharing a date are
            compounded in their original relative order.

    Returns:
        ``{(year, month): compounded_return}`` for every month that has at least
        one observation. Months with no observations are simply absent from the
        dict (callers should treat absence as "no data", not 0.0).
    """
    # Bucket by (year, month), preserving a stable chronological order within
    # each bucket. We sort by (date, original_index) so same-date returns keep
    # their input order and the compounding is deterministic.
    indexed = sorted(
        enumerate(dated_returns),
        key=lambda pair: (pair[1][0], pair[0]),
    )
    buckets: Dict[MonthKey, List[float]] = {}
    for _, (d, r) in indexed:
        buckets.setdefault((d.year, d.month), []).append(r)

    out: Dict[MonthKey, float] = {}
    for key, rets in buckets.items():
        compounded = _compound(rets)
        if compounded is not None:
            out[key] = compounded
    return out


def yearly_returns(
    dated_returns: Sequence[Tuple[date, float]],
) -> Dict[int, float]:
    """
    Compound a dated return series into one return per calendar year.

    This compounds the *monthly* returns (which themselves compound the
    underlying observations), so a year total is the true geometric chaining of
    its months and is internally consistent with :func:`monthly_returns`.

    Returns ``{year: compounded_return}`` for every year with at least one
    observation; years with no data are absent.
    """
    months = monthly_returns(dated_returns)
    by_year: Dict[int, List[float]] = {}
    # Compound months in calendar order for determinism (Jan..Dec).
    for year, month in sorted(months):
        by_year.setdefault(year, []).append(months[(year, month)])

    out: Dict[int, float] = {}
    for year, rets in by_year.items():
        compounded = _compound(rets)
        if compounded is not None:
            out[year] = compounded
    return out


def monthly_returns_table(
    dated_returns: Sequence[Tuple[date, float]],
) -> Dict[int, List[Optional[float]]]:
    """
    Build the full year x month matrix.

    Returns a dict mapping each calendar year present in the data to a 13-element
    row::

        [Jan, Feb, Mar, Apr, May, Jun, Jul, Aug, Sep, Oct, Nov, Dec, YearTotal]

    Each of the twelve month cells is that month's compounded return, or
    ``None`` if there were no observations in that month. The 13th element is the
    year's compounded total (``None`` only if the year has no data at all, which
    cannot happen for a year that appears as a key).

    Years are *not* gap-filled: only years that actually appear in the input are
    keys. Use :func:`year_range` if you need a contiguous span for rendering.
    """
    months = monthly_returns(dated_returns)
    years = yearly_returns(dated_returns)

    table: Dict[int, List[Optional[float]]] = {}
    for year in years:
        row: List[Optional[float]] = [months.get((year, m)) for m in range(1, 13)]
        row.append(years[year])
        table[year] = row
    return table


def year_range(
    dated_returns: Sequence[Tuple[date, float]],
) -> List[int]:
    """
    Contiguous list of calendar years from the earliest to the latest observed
    year, inclusive — including years with no observations in between.

    Useful for rendering a gap-free table: zip these years against
    :func:`monthly_returns_table` and supply an all-``None`` row for any year the
    table does not contain. Returns ``[]`` for empty input.
    """
    if not dated_returns:
        return []
    years = [d.year for d, _ in dated_returns]
    return list(range(min(years), max(years) + 1))


def format_table(
    dated_returns: Sequence[Tuple[date, float]],
    *,
    fill_year_gaps: bool = True,
    na: str = "",
    decimals: int = 2,
) -> str:
    """
    Render the monthly returns table as a fixed-width text grid (percentages).

    A convenience for logs/tearsheets. Each cell is shown as a percentage with
    ``decimals`` places (0.0123 -> ``"1.23"``); empty months render as ``na``.
    The header row is ``Year Jan Feb ... Dec Total``.

    Args:
        fill_year_gaps: if True, include every year in :func:`year_range` (so the
            block has no missing years); otherwise only years with data.
        na: placeholder string for months with no observations.
        decimals: decimal places for the percentage values.

    Returns ``""`` for empty input. This is the only function that produces a
    string; it does no I/O (the caller decides whether to print or log it).
    """
    table = monthly_returns_table(dated_returns)
    if not table:
        return ""

    if fill_year_gaps:
        years = year_range(dated_returns)
    else:
        years = sorted(table)

    empty_row: List[Optional[float]] = [None] * 13

    def cell(value: Optional[float]) -> str:
        if value is None:
            return na
        return f"{value * 100.0:.{decimals}f}"

    headers = ["Year", *MONTH_ABBRS, "Total"]
    rows: List[List[str]] = []
    for year in years:
        row = table.get(year, empty_row)
        rows.append([str(year), *(cell(v) for v in row)])

    # Compute per-column widths across header and all rows.
    n_cols = len(headers)
    widths = [len(headers[c]) for c in range(n_cols)]
    for row in rows:
        for c in range(n_cols):
            if len(row[c]) > widths[c]:
                widths[c] = len(row[c])

    def fmt_line(cells: Sequence[str]) -> str:
        return "  ".join(cells[c].rjust(widths[c]) for c in range(n_cols))

    lines = [fmt_line(headers)]
    lines.extend(fmt_line(row) for row in rows)
    return "\n".join(lines)
