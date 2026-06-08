"""
tests/test_architecture_fitness.py
==================================
Architecture-fitness test for the project's #1 structural invariant (CLAUDE.md
golden rules 1-2): **a strategy can never reach the broker.** Strategies express
intent (SignalEvents) and read state through the injected StrategyContext — they
must NOT import the execution engines, the RiskManager / Portfolio internals, or
any broker SDK. The decoupling is enforced by design, but until now nothing
machine-checked it: a strategy that imported `apex.execution` or
`apex.risk.risk_manager` would pass every other gate. This test fails the build
the moment a library strategy gains such an import.

Advisory helpers (e.g. `apex.risk.position_sizing`) are intentionally allowed —
a strategy may read them to inform a signal's `strength`; only the order-placing
and position-truth modules are forbidden.
"""

from __future__ import annotations

import ast
from pathlib import Path

_LIBRARY = Path(__file__).resolve().parents[1] / "apex" / "strategy" / "library"

# Modules a strategy must NEVER import — the order-placing / broker-reaching paths.
_FORBIDDEN_PREFIXES = (
    "apex.execution",  # execution engines — the path to the broker
    "apex.risk.risk_manager",  # the sole order producer; strategies cannot touch it
    "apex.risk.portfolio",  # position/cash truth — read via context, never imported
    "alpaca",  # Alpaca broker SDK
    "ibapi",  # IBKR broker SDK
)


def _imported_modules(path: Path) -> set[str]:
    """Top-level absolute module names imported by a source file (via AST)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module)
    return mods


def _strategy_files() -> list[Path]:
    return [p for p in _LIBRARY.glob("*.py") if p.name != "__init__.py"]


def test_library_directory_resolves():
    assert _strategy_files(), f"no strategy files found under {_LIBRARY}"


def test_no_strategy_imports_the_broker_path():
    violations: list[str] = []
    for path in _strategy_files():
        for mod in _imported_modules(path):
            if any(mod == pre or mod.startswith(pre + ".") for pre in _FORBIDDEN_PREFIXES):
                violations.append(f"{path.name} imports {mod}")
    assert not violations, (
        "Strategies must emit signals only and never reach the broker "
        "(CLAUDE.md golden rules 1-2). Forbidden imports found:\n  " + "\n  ".join(violations)
    )
