## What this changes

<!-- One or two sentences. What and why. -->

## Roadmap phase

<!-- Which ROADMAP.md phase does this advance? Tick one. -->

- [ ] Phase 1 — Core event system & data models
- [ ] Phase 2 — Data feed layer
- [ ] Phase 3 — Strategy layer & indicators
- [ ] Phase 4 — Risk manager & portfolio
- [ ] Phase 5 — Execution layer & integration
- [ ] Phase 6 — Live operations & strategy expansion
- [ ] Validation Gauntlet (cross-cutting)
- [ ] CI / tooling / docs only

## Golden Rules checklist

<!-- See CLAUDE.md. Confirm every box that applies. -->

- [ ] `Decimal` used for all money/prices/quantities (no `float`)
- [ ] `from __future__ import annotations` + full type hints
- [ ] New/changed data models are frozen (immutable)
- [ ] Risk checks fail closed (uncertainty/error => reject / no trade)
- [ ] Deterministic — no `datetime.now()` in logic, no unseeded randomness, no I/O in strategy logic
- [ ] Strategies emit signals only (never orders); modules talk via events
- [ ] No secrets committed; secrets read from env vars only
- [ ] New module ships with tests; math asserted against hand-computed values

## Verification

- [ ] `ruff check` clean
- [ ] `ruff format --check` clean
- [ ] `mypy apex/` reviewed
- [ ] `pytest` green (note count: ___ tests)

## Notes for reviewers

<!-- Anything CI/reviewers should watch for. -->
