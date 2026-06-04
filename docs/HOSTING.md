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
