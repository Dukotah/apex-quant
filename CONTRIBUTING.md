# Contributing to Apex Quant

Thanks for working on Apex Quant. This is a **live trading engine**, so the bar
for changes is correctness and safety first. Read `CLAUDE.md` (the Golden Rules)
before writing any code — they are non-negotiable.

---

## Quick start

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
pre-commit install        # installs the local git hooks (lint/format/secrets)
pytest                    # run the full suite (offline, no keys needed)
```

That's the whole local loop. `pre-commit install` wires the hooks once; from then
on every `git commit` auto-runs ruff, ruff-format, and a secrets scan. Run them
manually any time with `pre-commit run --all-files`.

---

## Quality gates (these run in CI on every push / PR)

| Gate | Command | Notes |
|------|---------|-------|
| Lint | `ruff check apex/ tests/ scripts/` | catches undefined names, unused imports, import order |
| Format | `ruff format --check apex/ tests/ scripts/` | run `ruff format` to fix |
| Types | `mypy apex/` | respects `[tool.mypy]` in `pyproject.toml` |
| Tests + coverage | `pytest` | coverage + `--cov-fail-under` floor are in `pyproject.toml` |
| Secrets | gitleaks + `detect-secrets` | no API keys may ever be committed |

CI runs on a **Python 3.11 and 3.12 matrix**. The suite is fully offline — it uses
dependency-injection seams (injectable fetchers, broker clients, and clocks), so
it never touches Alpaca or the network and **requires no secrets**.

---

## The rules that matter most here

- **Decimal for all money/prices/quantities** — never `float`.
- **`from __future__ import annotations`** at the top of every module; full type hints.
- **Data models are frozen/immutable.**
- **Risk checks fail closed** — any uncertainty or error means reject / no trade.
- **Determinism is sacred** — no `datetime.now()` in logic (inject the `Clock`),
  no unseeded randomness, no I/O in strategy logic.
- **Modules communicate only through events.** Strategies emit signals, never orders.
- **Every new module ships with tests.** Indicator/math tests assert against
  hand-computed known-correct values.
- **Secrets come from environment variables only.** Never hardcode a key, never
  commit `.env`.

---

## Commit convention

```
feat(phase-N): <module> — <short description>
fix(risk): <description>
test(strategy): <description>
docs: <description>
ci: <description>
```

Keep commits small and conventional. One logical change per commit. Never commit
secrets — the pre-commit hook and CI secret-scan will block them.

---

## Pull requests

Use the PR template (it appears automatically). Confirm the checklist: tests pass,
lint/format/types are clean, no secrets, and the relevant Golden Rules are upheld.
Tie the PR to the roadmap phase it advances (see `ROADMAP.md`).
