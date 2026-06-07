"""
scripts/backtest_grid.py
========================
Deterministic parameter-grid expander for backtests. Turns a small parameter
grid (each parameter -> a list of candidate values) into the full, ordered list
of concrete parameter combinations you would sweep in a backtest — the cartesian
product, in a stable, reproducible order.

This is the planning half of a parameter sweep: it answers "given these knobs and
these candidate values, exactly which combinations should I run, and in what
order?" without touching data, the network, or the broker. Feed the resulting
combinations into ``scripts.run_backtest`` (or any strategy factory) one at a time.

Run:
    python -m scripts.backtest_grid --param lookback=100,200 --param fast=10,20
    python -m scripts.backtest_grid --param lookback=100,200 --limit 1   # first combo only
    python -m scripts.backtest_grid --param lookback=100,200 --count     # just how many

Determinism (Golden Rule 10): the expansion is a pure function of its inputs.
Parameter order is preserved as given, value order is preserved per parameter, and
the product is enumerated in row-major (last-axis-fastest) order — same grid in,
same combinations out, every time. No ``datetime.now()`` in the core; any label
timestamp is injected. Importing this module has ZERO side effects.

Pure core (tested): ``expand_grid``, ``count_combinations``, ``parse_param_specs``,
``label_combination``. I/O lives only in ``main()``.
"""

from __future__ import annotations

from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence

# A grid maps each parameter name to its ordered list of candidate values.
Grid = Mapping[str, Sequence[object]]
# A single concrete combination: one chosen value per parameter.
Combination = Dict[str, object]


# ----------------------------------------------------------------- pure core


def count_combinations(grid: Grid) -> int:
    """
    Number of combinations the grid expands to — the product of each parameter's
    value-count. An empty grid yields exactly one combination (the empty dict),
    matching the cartesian-product convention. A parameter with no candidate
    values collapses the whole product to zero (nothing to sweep).
    """
    total = 1
    for values in grid.values():
        total *= len(values)
    return total


def expand_grid(grid: Grid) -> List[Combination]:
    """
    Expand a parameter grid into the full ordered list of combinations.

    The result is the cartesian product of the per-parameter value lists, in
    row-major order: parameters are iterated in insertion order and the LAST
    parameter varies fastest. Each combination is a fresh dict mapping every
    parameter name to one chosen value.

    Edge cases (fail closed, never garbage):
      * empty grid                -> ``[{}]``   (one trivial combination)
      * any parameter with []     -> ``[]``     (no combinations to run)

    Pure and deterministic: same grid in -> identical list out.
    """
    names = list(grid.keys())
    value_lists = [list(grid[name]) for name in names]

    # A single empty axis means there is nothing to sweep.
    if any(len(values) == 0 for values in value_lists):
        return []

    combos: List[Combination] = [{}]
    for name, values in zip(names, value_lists):
        combos = [{**partial, name: value} for partial in combos for value in values]
    return combos


def iter_combinations(grid: Grid) -> Iterator[Combination]:
    """
    Streaming form of :func:`expand_grid` — yields combinations one at a time in
    the same deterministic order without materializing the whole product. Useful
    when a sweep is large and each combination is consumed immediately.
    """
    names = list(grid.keys())
    value_lists = [list(grid[name]) for name in names]
    if any(len(values) == 0 for values in value_lists):
        return
    if not names:
        yield {}
        return

    indices = [0] * len(names)
    while True:
        yield {name: value_lists[i][idx] for i, (name, idx) in enumerate(zip(names, indices))}
        # Advance the rightmost (fastest) axis, carrying as needed.
        pos = len(names) - 1
        while pos >= 0:
            indices[pos] += 1
            if indices[pos] < len(value_lists[pos]):
                break
            indices[pos] = 0
            pos -= 1
        else:
            return


def _coerce(token: str) -> object:
    """
    Best-effort, deterministic scalar coercion for a single CLI token: try int,
    then float, else keep the raw (stripped) string. Pure — no locale/now calls.
    """
    text = token.strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def parse_param_specs(specs: Iterable[str]) -> Dict[str, List[object]]:
    """
    Parse ``name=v1,v2,...`` CLI specs into an ordered grid mapping.

    Tokens are coerced to int/float when they parse cleanly, else kept as strings.
    Duplicate values within one parameter are de-duplicated while preserving first
    appearance, so the swept order stays stable. A later spec for the same name
    REPLACES the earlier one (last write wins) — but the parameter keeps its
    original position in the grid.

    Raises ``ValueError`` (fail closed) on a malformed spec: missing ``=``, an
    empty name, or no values at all.
    """
    grid: Dict[str, List[object]] = {}
    for raw in specs:
        if "=" not in raw:
            raise ValueError(f"bad --param {raw!r}: expected name=v1,v2,...")
        name, _, values_str = raw.partition("=")
        name = name.strip()
        if not name:
            raise ValueError(f"bad --param {raw!r}: empty parameter name")

        values: List[object] = []
        seen: set = set()
        for token in values_str.split(","):
            if token.strip() == "":
                continue
            value = _coerce(token)
            key = (type(value).__name__, value)
            if key in seen:
                continue
            seen.add(key)
            values.append(value)
        if not values:
            raise ValueError(f"bad --param {raw!r}: no values given")
        grid[name] = values  # last write wins; insertion order preserved
    return grid


def label_combination(
    combo: Combination, *, index: Optional[int] = None, timestamp: Optional[str] = None
) -> str:
    """
    Render one combination as a stable, human-readable single line.

    Parameters are emitted in the combination's own (grid) order as ``k=v`` joined
    by spaces. An optional ``index`` prefixes ``#<i>`` and an optional injected
    ``timestamp`` (a string — NEVER computed here, Golden Rule 10) is appended in
    brackets. Pure: identical inputs -> identical label.
    """
    body = " ".join(f"{k}={v}" for k, v in combo.items()) if combo else "(no params)"
    prefix = f"#{index} " if index is not None else ""
    suffix = f"  [{timestamp}]" if timestamp else ""
    return f"{prefix}{body}{suffix}"


def render_grid(grid: Grid, *, limit: Optional[int] = None, timestamp: Optional[str] = None) -> str:
    """
    Build the full report text for a grid: a header line with the combination
    count, then one labeled line per combination (optionally capped at ``limit``).
    Pure string builder — no I/O. ``timestamp``, if given, is injected, not read.
    """
    combos = expand_grid(grid)
    shown = combos if limit is None else combos[: max(0, limit)]
    names = ", ".join(grid.keys()) or "(none)"
    lines = [
        "APEX QUANT — BACKTEST PARAMETER GRID",
        "=" * 48,
        f"  params: {names}",
        f"  combinations: {len(combos)}"
        + (f"  (showing {len(shown)})" if len(shown) != len(combos) else ""),
        "-" * 48,
    ]
    if not combos:
        lines.append("  (empty grid — nothing to sweep)")
    for i, combo in enumerate(shown):
        lines.append("  " + label_combination(combo, index=i, timestamp=timestamp))
    return "\n".join(lines)


# ----------------------------------------------------------------- CLI (I/O only)


def _build_parser():  # pragma: no cover - thin argparse wiring
    import argparse

    parser = argparse.ArgumentParser(
        prog="backtest_grid",
        description="Expand a backtest parameter grid into ordered combinations "
        "(deterministic cartesian product). Read-only; no network.",
    )
    parser.add_argument(
        "--param",
        "-p",
        action="append",
        default=[],
        metavar="NAME=v1,v2,...",
        help="A parameter and its candidate values. Repeat for more parameters. "
        "Values are coerced to int/float when possible, else kept as strings.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Show only the first N combinations (the full count is still reported).",
    )
    parser.add_argument(
        "--count",
        action="store_true",
        help="Print only the number of combinations and exit.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - I/O wiring
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        grid = parse_param_specs(args.param)
    except ValueError as exc:
        parser.error(str(exc))
        return 2  # parser.error exits, but keep the type checker honest

    if args.count:
        print(count_combinations(grid))
        return 0

    print(render_grid(grid, limit=args.limit))
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
