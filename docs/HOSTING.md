# Hosting Apex Quant for Free

Three free ways to run the engine. Pick based on your strategy's timeframe.

---

## Option A — GitHub Actions cron (recommended to start)

**Best for:** daily, hourly, or 30-min strategies. Wake → evaluate → exit.

**Cost:** $0. Public repos get unlimited Actions minutes.

**How it works:** `.github/workflows/trade.yml` triggers on a cron schedule,
spins up an Ubuntu runner, runs `python -m scripts.run_once`, commits updated
state back to the repo, and exits. State persists between runs via SQLite files
committed to the `state/` directory.

**Setup:**
1. Push this repo to GitHub (public for unlimited minutes).
2. Repo Settings → Secrets and variables → Actions → add:
   - Secret `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` (paper keys to start)
   - Variable `APEX_MODE=paper`, `APEX_BROKER=alpaca`
3. The workflow runs automatically on schedule. Test it now with the
   "Run workflow" button (workflow_dispatch).

**Limitation:** GitHub's terms prohibit using Actions as an always-on server
(infinite loops). Scheduled wake/run/exit is the correct, allowed pattern. Cron
timing is approximate (~5-min minimum granularity, not guaranteed exact), so
don't use this for sub-minute strategies.

---

## Option B — Oracle Cloud Always-Free VM

**Best for:** a true 24/7 persistent process, intraday strategies needing a
live websocket connection, or anything that must never sleep.

**Cost:** $0 forever (Oracle's Always-Free tier — a capable ARM VM).

**How it works:** a small Linux VM runs the engine as a systemd service or in a
screen/tmux session, holding a persistent connection to Alpaca's data stream.

**Setup sketch:**
1. Create an Oracle Cloud account → launch an Always-Free Ampere ARM instance.
2. SSH in, clone the repo, `pip install -r requirements.txt`.
3. Run the engine as a systemd service (auto-restart on failure).
4. Logs to disk; optionally push state to the repo or a free Postgres (Neon/Supabase).

**Limitation:** more setup than Actions; you manage the VM. Sign-up can be
finicky in some regions.

---

## Option C — PythonAnywhere free tier

**Best for:** simplest possible setup, browser-based, scheduled tasks.

**Cost:** $0 on the free tier (includes scheduled tasks).

**Limitation:** free tier has limited CPU seconds/day and a restricted outbound
network allowlist — Alpaca's domains may need to be on the allowed list (check
their current policy). Fine for light daily strategies.

---

## State persistence (all options)

The engine keeps state (positions, equity, peak equity, daily start) in a SQLite
file under `state/`. For Actions, the workflow commits this file back after each
run. For a VM, it lives on disk. On startup, the Alpaca execution engine also
reconciles against the broker's truth, so even if local state drifts, the broker
is the authority.

## Free notifications

`ntfy.sh` gives free push notifications with no account:
1. Pick an unguessable topic name, set it as the `NTFY_TOPIC` secret.
2. Subscribe to that topic in the ntfy mobile app.
3. The workflow pings it on failures; the engine can ping it on fills.

## External dead-man's-switch (truly independent liveness)

The in-repo `watchdog.yml` alerts when `state/status.json` goes stale, but it runs on
the **same GitHub Actions scheduler it polices** — if GitHub auto-disables scheduled
workflows (e.g. after 60 days of repo inactivity) or the schedule simply never fires,
the watchdog is silenced too. That is the blind spot behind the June 2026 three-day
outage.

An **off-GitHub** monitor closes it. `run_once` pings `APEX_HEARTBEAT_URL` on every
successful cycle (and `<url>/fail` on an errored one); the external service emails you
when the pings stop. Because the success ping fires only from inside a genuinely
completed cycle, a preflight skip, an errored run, and a schedule that never fired all
collapse to the same observable — **no ping → you get alerted.**

Setup (free, ~2 minutes):
1. Create a check at [healthchecks.io](https://healthchecks.io) (free tier, no card).
   Set its **period** to your cron cadence and a **grace** window (e.g. period 1 day,
   grace 2 hours for the weekday 19:50 UTC slot).
2. Copy the check's ping URL (e.g. `https://hc-ping.com/<uuid>`).
3. Set it as the `APEX_HEARTBEAT_URL` secret/env var for the trade cron.
4. Unset = disabled. Any check-in service using the `/fail` suffix convention works.
