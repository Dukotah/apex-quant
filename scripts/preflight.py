"""
scripts/preflight.py
====================
Pre-flight health check — run before trusting a live or paper session.

Prints a PASS / WARN / FAIL line per check, then exits 0 if no FAIL or 1 if
any FAIL was reported.

Usage:
    python -m scripts.preflight

Each individual check returns a (name, status, detail) 3-tuple. All but one are
PURE functions, unit-tested offline with no broker/network/DB. The exception is
``check_broker_reachable`` — the single network round-trip — which takes an
injectable ``engine_factory`` so it too is tested offline with a fake engine.

IMPORTANT: this script NEVER prints the value of any secret env var.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional

# ----------------------------------------------------------------- constants

STATUS_PASS = "PASS"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"

CheckResult = tuple[str, str, str]  # (name, status, detail)

# Default locations that mirror scripts/run_once.py
_DEFAULT_STATE_DIR = Path("state")
_DEFAULT_DB_PATH = _DEFAULT_STATE_DIR / "apex_state.db"
_DEFAULT_DATA_DIR = Path("data")


# ----------------------------------------------------------------- pure checks


def check_config_loads() -> CheckResult:
    """
    Try to instantiate AppConfig from the current environment.
    Returns PASS if it succeeds, FAIL with the error message otherwise.
    """
    name = "config.loads"
    try:
        from apex.core.config import AppConfig

        AppConfig.from_env()
        return name, STATUS_PASS, "AppConfig.from_env() succeeded"
    except Exception as exc:  # noqa: BLE001
        return name, STATUS_FAIL, f"AppConfig.from_env() raised: {exc}"


def check_env_vars(environ: Optional[dict[str, str]] = None) -> CheckResult:
    """
    For APEX_MODE=live or APEX_MODE=paper, verify that ALPACA_API_KEY and
    ALPACA_SECRET_KEY are set.  Reports PRESENT/MISSING — NEVER the value.

    If APEX_MODE is not set or is 'backtest', no keys are required (PASS).
    """
    name = "env.credentials"
    env = environ if environ is not None else dict(os.environ)

    mode = env.get("APEX_MODE", "backtest").strip().lower()
    if mode not in ("live", "paper"):
        return name, STATUS_PASS, f"mode={mode!r} — no broker credentials required"

    missing: list[str] = []
    for var in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        if not env.get(var, "").strip():
            missing.append(var)

    if missing:
        return (
            name,
            STATUS_FAIL,
            f"mode={mode!r} but missing env var(s): {', '.join(missing)}",
        )

    # Both present — report presence only, never the values.
    return name, STATUS_PASS, f"mode={mode!r} — ALPACA_API_KEY=PRESENT, ALPACA_SECRET_KEY=PRESENT"


def check_halt_state(environ: Optional[dict[str, str]] = None) -> CheckResult:
    """
    Check the APEX_HALT kill switch.

    - Not set / falsy → PASS (system is armed).
    - Truthy           → WARN (orders will be blocked this cycle — human may have set it
                              deliberately, but the operator should know).
    """
    name = "halt.state"
    env = environ if environ is not None else dict(os.environ)
    raw = env.get("APEX_HALT", "").strip().lower()
    is_halted = raw in ("1", "true", "yes", "on")
    if is_halted:
        return name, STATUS_WARN, "APEX_HALT is active — all orders will be blocked"
    return name, STATUS_PASS, "APEX_HALT not set — kill switch is OFF"


def check_dirs_and_db(
    state_dir: Path = _DEFAULT_STATE_DIR,
    db_path: Path = _DEFAULT_DB_PATH,
    data_dir: Path = _DEFAULT_DATA_DIR,
) -> CheckResult:
    """
    Verify that:
      - data/ directory exists
      - state/ directory exists
      - state/apex_state.db is present (only a WARN if missing; it is created
        on first run, so a brand-new install is not a hard failure)

    Returns FAIL only if a directory that must already exist is absent.
    Returns WARN if the DB file doesn't exist yet.
    Returns PASS otherwise.
    """
    name = "dirs.state_db"
    problems: list[str] = []
    warnings: list[str] = []

    for d, label in ((data_dir, "data"), (state_dir, "state")):
        if not d.exists():
            problems.append(f"{label}/ dir not found ({d})")

    if not problems:
        # Only check the DB if the directory is present.
        if not db_path.exists():
            warnings.append(f"state DB not found at {db_path} (will be created on first run)")

    if problems:
        return name, STATUS_FAIL, "; ".join(problems)
    if warnings:
        return name, STATUS_WARN, "; ".join(warnings)
    return name, STATUS_PASS, f"data/ and state/ present; DB at {db_path}"


def check_risk_config(risk_cfg=None) -> CheckResult:
    """
    Validate that the active RiskConfig is sane for production use:
      - require_stop_loss must be True
      - max_drawdown_pct must be in (0, 1]
      - max_daily_loss_pct must be in (0, 1]

    If `risk_cfg` is None, a default RiskConfig() is constructed and checked.
    Returns FAIL for any hard constraint violation, PASS otherwise.
    """
    name = "risk.config_sane"
    try:
        from apex.risk.risk_manager import RiskConfig

        cfg = risk_cfg if risk_cfg is not None else RiskConfig()
        problems: list[str] = []

        if not cfg.require_stop_loss:
            problems.append("require_stop_loss is False — mandatory stop-loss disabled")

        dd = Decimal(str(cfg.max_drawdown_pct))
        if not (Decimal("0") < dd <= Decimal("1")):
            problems.append(f"max_drawdown_pct={dd} not in (0, 1]")

        dl = Decimal(str(cfg.max_daily_loss_pct))
        if not (Decimal("0") < dl <= Decimal("1")):
            problems.append(f"max_daily_loss_pct={dl} not in (0, 1]")

        if problems:
            return name, STATUS_FAIL, "; ".join(problems)

        return (
            name,
            STATUS_PASS,
            f"require_stop_loss=True, drawdown={dd:.0%}, daily={dl:.0%}",
        )
    except Exception as exc:  # noqa: BLE001
        return name, STATUS_FAIL, f"could not instantiate RiskConfig: {exc}"


def check_broker_reachable(
    environ: Optional[dict[str, str]] = None,
    engine_factory: Optional[Callable[[], object]] = None,
) -> CheckResult:
    """
    Verify the broker is actually REACHABLE with the configured credentials by
    doing one real account round-trip (``get_account_equity()`` → Alpaca's
    ``get_account()``). This is the one check that touches the network.

    Why it exists: every other credential check only confirms a key is *present*.
    A present-but-stale/revoked key (or a broker outage) sails past those and
    fails deep inside the trading cycle — *after* reconciliation has already run,
    leaving a half-applied state. This check fails fast, before any order logic.

    Behaviour by mode/broker:
      - mode not in {paper, live}                  → PASS (skipped — no broker)
      - broker not a real venue (alpaca / crypto)  → PASS (skipped — simulator)
      - real broker, round-trip succeeds, equity>0 → PASS
      - real broker, round-trip succeeds, equity≤0 → WARN (reachable but unfunded)
      - real broker, round-trip raises             → FAIL (unreachable / bad key)

    Offline-testable: ``engine_factory`` is injected in tests to return a fake
    engine implementing ``connect()`` / ``get_account_equity()`` / ``disconnect()``.
    In production it defaults to building the real engine from ``AppConfig``.

    NEVER prints any secret. The exception text is the broker's own (auth/network)
    message; this function never interpolates credential values into the detail.
    """
    name = "broker.reachable"
    env = environ if environ is not None else dict(os.environ)

    mode = env.get("APEX_MODE", "backtest").strip().lower()
    if mode not in ("live", "paper"):
        return name, STATUS_PASS, f"mode={mode!r} — no live broker to reach"

    broker = env.get("APEX_BROKER", "simulated").strip().lower()
    if broker not in ("alpaca", "alpaca_crypto"):
        return name, STATUS_PASS, f"broker={broker!r} — simulated, nothing to reach"

    try:
        if engine_factory is not None:
            engine = engine_factory()
        else:
            from apex.core.config import AppConfig
            from apex.execution.factory import make_execution_engine

            engine = make_execution_engine(AppConfig.from_env())

        try:
            engine.connect()  # type: ignore[attr-defined]
            equity = Decimal(str(engine.get_account_equity()))  # type: ignore[attr-defined]
        finally:
            # Best-effort teardown; disconnect must never mask the real result.
            try:
                engine.disconnect()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        return (
            name,
            STATUS_FAIL,
            f"mode={mode!r} broker={broker!r} — account round-trip failed: {exc}",
        )

    if equity <= 0:
        return (
            name,
            STATUS_WARN,
            f"broker reachable (mode={mode!r}) but account equity is {equity} — unfunded",
        )
    return name, STATUS_PASS, f"broker reachable (mode={mode!r}) — account equity={equity}"


# ----------------------------------------------------------------- runner


def run_all_checks() -> list[CheckResult]:
    """
    Execute every preflight check in order and return the full list of results.
    This is split from main() so it can be called programmatically without side
    effects (no printing, no sys.exit).
    """
    checks: list[CheckResult] = []

    # 1. Config loads
    checks.append(check_config_loads())

    # 2. Env vars for the current mode
    checks.append(check_env_vars())

    # 3. APEX_HALT kill-switch state
    checks.append(check_halt_state())

    # 4. Directories and state DB
    checks.append(check_dirs_and_db())

    # 5. Risk config sanity
    #    Load from AppConfig if config is healthy; fall back to default RiskConfig.
    risk_cfg = None
    try:
        from apex.core.config import AppConfig

        app = AppConfig.from_env()
        risk_cfg = app.risk
    except Exception:  # noqa: BLE001
        pass  # check_config_loads already reported this; use the default
    checks.append(check_risk_config(risk_cfg))

    # 6. Broker reachability (the one network round-trip; skipped unless a real
    #    broker is configured in paper/live mode).
    checks.append(check_broker_reachable())

    return checks


def _print_result(name: str, status: str, detail: str) -> None:
    width = 24
    padded = name.ljust(width)
    print(f"  {padded} [{status:4}]  {detail}")


def main() -> int:
    """
    Run all checks, print a report, return 0 if no FAIL or 1 if any FAIL.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    print("=" * 60)
    print("APEX QUANT — PRE-FLIGHT CHECK")
    print("=" * 60)

    results = run_all_checks()

    has_fail = False
    for name, status, detail in results:
        _print_result(name, status, detail)
        if status == STATUS_FAIL:
            has_fail = True

    print("-" * 60)
    if has_fail:
        print("  VERDICT  [FAIL]  One or more checks failed — do NOT start live/paper.")
        return 1

    has_warn = any(status == STATUS_WARN for _, status, _ in results)
    if has_warn:
        print("  VERDICT  [WARN]  All checks passed with warnings — review before trading.")
    else:
        print("  VERDICT  [PASS]  All checks passed — system looks healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
