"""
scripts/config_audit.py
=======================
Pre-flight config doctor. Validates env/config sanity BEFORE the cron fires —
mode/broker coherence, credentials present, and risk caps within sane bounds —
and prints a human-readable list of issues. The point is to catch a
mis-configured deployment (e.g. ``APEX_MODE=live`` with empty keys, or a risk
cap typo'd to 1600% instead of 16%) on the ground instead of mid-flight.

Run:  python -m scripts.config_audit            # audit the live env
      python -m scripts.config_audit --json      # machine-readable issues

SECURITY: this is read-only and never touches the network. It checks whether a
secret is PRESENT (and roughly well-formed by length), but NEVER prints, logs,
or returns the secret value itself — only booleans and lengths cross the
boundary into the pure core. No secret can leak through the audit output.

Design (Golden Rule 12): the whole judgement lives in the PURE, deterministic
``audit_config`` core, which takes a plain ``ConfigFacts`` snapshot (no env, no
I/O, no clock) and returns a sorted list of ``Issue``s. ``main()`` is the only
place that reads real env/keys, lazily, and turns them into that snapshot.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import List, Optional, Sequence

# --------------------------------------------------------------------- severity


class Severity(str, Enum):
    """How bad an issue is. ERROR = do not deploy; WARNING = look before you leap."""
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


# An ERROR present makes the audit "fail" (non-zero exit); WARNING/INFO do not.
_FAILING = (Severity.ERROR,)


@dataclass(frozen=True)
class Issue:
    """One audit finding. ``code`` is a stable machine id; ``message`` is human-facing."""
    severity: Severity
    code: str
    message: str

    def render(self) -> str:
        mark = {Severity.ERROR: "[X]", Severity.WARNING: "[!]", Severity.INFO: "[i]"}[self.severity]
        return f"{mark} {self.code}: {self.message}"


# ---------------------------------------------------------------- config facts

# A secret shorter than this is almost certainly a paste error / placeholder.
MIN_SECRET_LEN = 8


@dataclass(frozen=True)
class ConfigFacts:
    """
    A pure, secret-free snapshot of the configuration to audit.

    NOTE the deliberate absence of any secret VALUE: only whether each credential
    is present and its length cross this boundary, so the core literally cannot
    leak a key. ``main()`` builds this from the real (frozen) AppConfig.
    """
    mode: str                                  # "backtest" | "paper" | "live" | other
    broker: str                                # "simulated" | "alpaca" | "ibkr" | other
    initial_capital: Decimal = Decimal("0")

    alpaca_key_present: bool = False
    alpaca_key_len: int = 0
    alpaca_secret_present: bool = False
    alpaca_secret_len: int = 0

    # Risk caps (as fractions: 0.16 = 16%). Mirrors apex.risk.risk_manager.RiskConfig.
    max_position_size_pct: Decimal = Decimal("0")
    max_total_exposure_pct: Decimal = Decimal("0")
    max_leverage: Decimal = Decimal("1")
    max_drawdown_pct: Decimal = Decimal("0")
    max_daily_loss_pct: Decimal = Decimal("0")
    max_open_positions: int = 0
    require_stop_loss: bool = True


# ----------------------------------------------------------------- pure core

# Recognised enum values (kept local so the core has ZERO apex imports / side effects).
_KNOWN_MODES = ("backtest", "paper", "live")
_KNOWN_BROKERS = ("simulated", "alpaca", "ibkr")
_REAL_BROKERS = ("alpaca", "ibkr")   # brokers that actually need credentials


def audit_config(facts: ConfigFacts) -> List[Issue]:
    """
    Pure, deterministic audit. Same ``facts`` -> same issues, every time. No env,
    no I/O, no wall-clock. Returns issues sorted ERROR-first then by code, so the
    output ordering is stable and reproducible.

    The checks, grouped:
      * mode / broker recognised and mutually coherent (the LIVE+simulated trap);
      * credentials present and plausibly-shaped when a real broker is selected;
      * risk caps inside sane bounds, with cross-cap coherence.
    Fail closed: an unknown/garbage value is flagged, never silently accepted.
    """
    issues: List[Issue] = []
    issues += _audit_mode_broker(facts)
    issues += _audit_credentials(facts)
    issues += _audit_risk(facts)

    sev_rank = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
    issues.sort(key=lambda i: (sev_rank[i.severity], i.code))
    return issues


def _audit_mode_broker(f: ConfigFacts) -> List[Issue]:
    out: List[Issue] = []
    if f.mode not in _KNOWN_MODES:
        out.append(Issue(Severity.ERROR, "mode.unknown",
                         f"APEX_MODE '{f.mode}' is not one of {_KNOWN_MODES}."))
    if f.broker not in _KNOWN_BROKERS:
        out.append(Issue(Severity.ERROR, "broker.unknown",
                         f"APEX_BROKER '{f.broker}' is not one of {_KNOWN_BROKERS}."))

    # The headline safety trap (Golden Rule 15): live money against a fake broker.
    if f.mode == "live" and f.broker == "simulated":
        out.append(Issue(Severity.ERROR, "mode.live_simulated",
                         "LIVE mode requires a real broker, not 'simulated'."))
    if f.mode == "live":
        out.append(Issue(Severity.WARNING, "mode.live",
                         "LIVE mode uses REAL money — confirm the 30-day paper gate passed."))
    if f.mode == "backtest" and f.broker in _REAL_BROKERS:
        out.append(Issue(Severity.INFO, "mode.backtest_real_broker",
                         f"backtest mode ignores the '{f.broker}' broker (simulated fills are used)."))
    return out


def _audit_credentials(f: ConfigFacts) -> List[Issue]:
    out: List[Issue] = []
    # Only a real broker in a non-backtest mode actually needs live credentials.
    needs_keys = f.broker in _REAL_BROKERS and f.mode in ("paper", "live")
    if not needs_keys:
        return out

    sev = Severity.ERROR if f.mode == "live" else Severity.WARNING
    if not f.alpaca_key_present:
        out.append(Issue(sev, "creds.key_missing",
                         f"ALPACA_API_KEY is not set but {f.mode} mode needs it."))
    elif f.alpaca_key_len < MIN_SECRET_LEN:
        out.append(Issue(Severity.WARNING, "creds.key_short",
                         f"ALPACA_API_KEY looks too short ({f.alpaca_key_len} chars) — placeholder?"))

    if not f.alpaca_secret_present:
        out.append(Issue(sev, "creds.secret_missing",
                         f"ALPACA_SECRET_KEY is not set but {f.mode} mode needs it."))
    elif f.alpaca_secret_len < MIN_SECRET_LEN:
        out.append(Issue(Severity.WARNING, "creds.secret_short",
                         f"ALPACA_SECRET_KEY looks too short ({f.alpaca_secret_len} chars) — placeholder?"))
    return out


def _audit_risk(f: ConfigFacts) -> List[Issue]:
    out: List[Issue] = []
    one = Decimal("1")
    zero = Decimal("0")

    # Each pct cap must be a fraction in (0, 1] (0.16 = 16%). A value > 1 is the
    # classic "typed 16 meaning 16%" bug that would uncap risk entirely.
    pct_caps = (
        ("risk.max_position_size_pct", f.max_position_size_pct),
        ("risk.max_total_exposure_pct", f.max_total_exposure_pct),
        ("risk.max_drawdown_pct", f.max_drawdown_pct),
        ("risk.max_daily_loss_pct", f.max_daily_loss_pct),
    )
    for code, val in pct_caps:
        if val <= zero:
            out.append(Issue(Severity.ERROR, code,
                             f"{code} is {val}; must be a positive fraction (e.g. 0.16 = 16%)."))
        elif val > one:
            out.append(Issue(Severity.ERROR, code,
                             f"{code} is {val} (> 1.0); did you mean {val / 100} ({val}%)?"))

    if f.max_leverage < one:
        out.append(Issue(Severity.ERROR, "risk.max_leverage",
                         f"max_leverage is {f.max_leverage}; must be >= 1.0 (1.0 = no leverage)."))
    elif f.max_leverage > one:
        out.append(Issue(Severity.WARNING, "risk.max_leverage",
                         f"max_leverage is {f.max_leverage} (> 1.0) — leverage amplifies losses."))

    if f.max_open_positions <= 0:
        out.append(Issue(Severity.ERROR, "risk.max_open_positions",
                         f"max_open_positions is {f.max_open_positions}; must be >= 1."))

    # Cross-cap coherence: a single position cannot be allowed to exceed total exposure.
    if (f.max_position_size_pct > zero and f.max_total_exposure_pct > zero
            and f.max_position_size_pct > f.max_total_exposure_pct):
        out.append(Issue(Severity.WARNING, "risk.position_gt_exposure",
                         f"max_position_size_pct ({f.max_position_size_pct}) exceeds "
                         f"max_total_exposure_pct ({f.max_total_exposure_pct})."))

    if not f.require_stop_loss:
        out.append(Issue(Severity.WARNING, "risk.no_stop_loss",
                         "require_stop_loss is False — Golden Rule 7 wants a mandatory stop."))

    if f.initial_capital <= zero:
        out.append(Issue(Severity.ERROR, "config.capital",
                         f"initial_capital is {f.initial_capital}; must be > 0."))
    return out


# ------------------------------------------------------------- report helpers


def has_errors(issues: Sequence[Issue]) -> bool:
    """True if any issue is failing-severity (ERROR) — the deploy-blocker check."""
    return any(i.severity in _FAILING for i in issues)


def render_report(facts: ConfigFacts, issues: Sequence[Issue]) -> str:
    """Human-readable audit. Shows config shape (no secrets) + the issue list."""
    lines = [
        "APEX QUANT — CONFIG AUDIT",
        "=" * 56,
        f"  mode {facts.mode}   broker {facts.broker}   capital ${facts.initial_capital:,}",
        f"  alpaca key {'present' if facts.alpaca_key_present else 'MISSING'}"
        f"   secret {'present' if facts.alpaca_secret_present else 'MISSING'}",
        "-" * 56,
    ]
    if not issues:
        lines.append("  OK — no issues found.")
    else:
        errs = sum(1 for i in issues if i.severity is Severity.ERROR)
        warns = sum(1 for i in issues if i.severity is Severity.WARNING)
        infos = sum(1 for i in issues if i.severity is Severity.INFO)
        for issue in issues:
            lines.append("  " + issue.render())
        lines.append("-" * 56)
        lines.append(f"  {errs} error(s), {warns} warning(s), {infos} info.")
    lines.append("-" * 56)
    verdict = "FAILED — fix errors before deploying." if has_errors(issues) else "PASSED."
    lines.append(f"  verdict      {verdict}")
    return "\n".join(lines)


def issues_to_json(issues: Sequence[Issue]) -> str:
    """Machine-readable issue list (no secrets — only codes/messages/severities)."""
    payload = [{"severity": i.severity.value, "code": i.code, "message": i.message}
               for i in issues]
    return json.dumps(payload, indent=2)


# ------------------------------------------------------------- env -> facts

def _present(value: Optional[str]) -> bool:
    """A credential counts as present only if it is a non-blank string."""
    return bool(value and value.strip())


def facts_from_config(config) -> ConfigFacts:  # pragma: no cover - thin adapter over live config
    """
    Build a secret-free ``ConfigFacts`` from a live AppConfig. The ONLY thing
    derived from the secret values is presence + length; the values themselves
    never escape this function.
    """
    key = config.alpaca_key
    secret = config.alpaca_secret
    risk = config.risk
    return ConfigFacts(
        mode=config.mode.value,
        broker=config.broker.value,
        initial_capital=config.initial_capital,
        alpaca_key_present=_present(key),
        alpaca_key_len=len(key.strip()) if _present(key) else 0,
        alpaca_secret_present=_present(secret),
        alpaca_secret_len=len(secret.strip()) if _present(secret) else 0,
        max_position_size_pct=risk.max_position_size_pct,
        max_total_exposure_pct=risk.max_total_exposure_pct,
        max_leverage=risk.max_leverage,
        max_drawdown_pct=risk.max_drawdown_pct,
        max_daily_loss_pct=risk.max_daily_loss_pct,
        max_open_positions=risk.max_open_positions,
        require_stop_loss=risk.require_stop_loss,
    )


# --------------------------------------------------------------------- main

def _parse_args(argv: Optional[Sequence[str]] = None):
    import argparse
    parser = argparse.ArgumentParser(
        prog="config_audit",
        description="Validate env/config sanity (mode, broker, keys, risk caps) "
                    "without revealing any secret values. Read-only; no network.",
    )
    parser.add_argument("--json", action="store_true",
                        help="emit issues as JSON instead of a human report.")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - reads real env
    # Lazy import: keeps module import side-effect-free (no env read at import time).
    from apex.core.config import AppConfig

    args = _parse_args(argv)
    try:
        config = AppConfig.from_env()
    except Exception as exc:  # noqa: BLE001 — surface a config-load failure as an audit error
        issue = Issue(Severity.ERROR, "config.load", f"AppConfig.from_env() failed: {exc}")
        if args.json:
            print(issues_to_json([issue]))
        else:
            print(issue.render())
        return 1

    facts = facts_from_config(config)
    issues = audit_config(facts)
    if args.json:
        print(issues_to_json(issues))
    else:
        try:
            import sys
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        print(render_report(facts, issues))
    return 1 if has_errors(issues) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
