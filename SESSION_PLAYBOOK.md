# Apex Quant — Session Playbook

How to prompt Claude in future sessions to build this out **one module at a time
without context-window overload or code regressions.**

The #1 risk in multi-session building is **context drift** — Claude forgetting
earlier decisions and silently rewriting working code. This playbook prevents it.

---

## The Three Iron Rules

1. **One module per session.** Build it, test it, commit it, stop. Never two.
2. **Frozen files are reference, not editing targets.** When you paste an existing
   file, say it's frozen. Claude outputs NEW files or diffs, never full rewrites
   of working code.
3. **Test-first handoff.** A module is done only when its test is green. End each
   session by running the test yourself.

---

## Session-Opening Prompt Template

Copy-paste this at the start of every build session, filling in the brackets:

```
We're building Apex Quant, an event-driven Python trading framework.
The base classes and core are already built and FROZEN — do not rewrite them.

Here is the project context:
[paste CLAUDE.md]

Here is what we've decided so far:
[paste DECISIONS.md]

Here are the frozen files this module depends on:
[paste only the files from the dependency table below]

TODAY'S TASK: implement [ONE module, e.g. "apex/core/event_bus.py"].

Constraints:
- Match the existing event/model signatures EXACTLY.
- Decimal for money, frozen dataclasses for data, type hints throughout.
- No I/O in logic. Determinism required. Fail closed on errors.
- Include a pytest test file with known-correct assertions.
- Output ONLY the new file(s). Do not rewrite any frozen file.

Build only this module. Stop when its test passes.
```

---

## What to Paste Per Module (Dependency Table)

Paste ONLY what the new module imports. Pasting the whole repo wastes context
and invites drift.

| Building | Paste these frozen files |
|----------|--------------------------|
| `event_bus.py` | `events.py` |
| `clock.py` | (nothing — standalone) |
| `historical_feed.py` | `base_feed.py`, `events.py`, `models.py` |
| `alpaca_feed.py` | `base_feed.py`, `events.py`, `models.py`, `config.py` |
| `indicators.py` | `models.py` (for Decimal usage patterns) |
| Any strategy | `base_strategy.py`, `events.py`, `models.py`, `indicators.py` |
| `portfolio.py` | `events.py`, `models.py` |
| `risk_manager.py` (edits) | `events.py`, `models.py`, current `risk_manager.py` |
| `simulated.py` (execution) | `base_execution.py`, `events.py`, `config.py` |
| `alpaca.py` (execution) | `base_execution.py`, `events.py`, `config.py` |
| `engine.py` (orchestrator) | ALL four base classes + `config.py` + `event_bus.py` |

---

## Session-Closing Ritual (do this every time)

Before ending, ask Claude:

```
We're done with this module. Now:
1. Write 3-5 bullet points for DECISIONS.md describing what we built and any
   design choices worth remembering.
2. Give me the exact git commands to commit this (with a conventional commit msg).
3. Tell me which ROADMAP item is next.
```

Then YOU:
- Run the test (`pytest tests/test_<module>.py -v`).
- If green, commit.
- Paste the new DECISIONS.md bullets into the file.
- Update the status emoji in ROADMAP.md (🔲 → ✅).

---

## When a Session Goes Sideways

**If output quality drops or Claude seems "lost"** — that's context saturation.
Don't push through. Say:

```
Summarize what we built this session in 5 bullets for DECISIONS.md, then stop.
We'll continue fresh next session.
```

Start the next session clean with the updated DECISIONS.md as the anchor.

**If Claude rewrites a frozen file** — reject it immediately:

```
That file is frozen — I pasted it as reference only. Output only the new module
and leave the frozen file untouched.
```

**If you hit a regression** — you committed after every green test, so:
```
git log --oneline        # find the last good commit
git revert <bad-commit>  # undo it safely
```

---

## Recommended Build Order (one per session)

```
Session 1:  event_bus.py + clock.py + model/event tests   → finishes Phase 1
Session 2:  historical_feed.py + test                      → Phase 2
Session 3:  indicators.py + tests                          → Phase 3
Session 4:  library/sma_crossover.py + test                → Phase 3
Session 5:  portfolio.py + test                            → Phase 4
Session 6:  risk_manager tests (formalize smoke test)      → Phase 4
Session 7:  simulated.py (execution) + slippage test       → Phase 5
Session 8:  factory.py + engine.py (the orchestrator)      → Phase 5
Session 9:  scripts/run_once.py + integration test         → Phase 5
Session 10: Full backtest end-to-end, verify P&L report    → Integration
Session 11: alpaca_feed.py + alpaca.py (execution)         → live-ready
Session 12: Flip APEX_MODE=paper, run against Alpaca paper → paper live!
```

After Session 12 you have a working paper-trading bot on free infrastructure.
Live trading is then a config flag away — after 30 days of proven paper results.

---

## Generating Strategies via Chat (the $0 AI path)

To add a strategy without paying for the API, do it in a normal Claude chat:

```
Generate a new Apex Quant strategy: [describe it — e.g. "RSI mean-reversion:
buy when RSI(14) < 30 and price is above the 200-day SMA, exit when RSI > 55"].

It must subclass BaseStrategy [paste base_strategy.py], use indicators from
apex.strategy.indicators [paste indicators.py], emit SignalEvents with a
suggested stop-loss, and include a pytest test. Output the strategy file and
its test only.
```

Paste the result into `apex/strategy/library/`. Zero API cost.
