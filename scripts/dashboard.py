"""
scripts/dashboard.py
====================
A tiny LOCAL web dashboard to view the project at a glance — live system status plus what's
been shipped and the headline results. Stdlib only (http.server); no Flask, no build step, no
new dependencies — consistent with the $0-infra principle. Not a deployed service; you run it
locally and open the URL.

  python -m scripts.dashboard            # serves http://127.0.0.1:8787
  python -m scripts.dashboard --port 9000

It reuses scripts/status.py for the live read (mode/equity/positions/paper-gate) and renders
the shipped phases + key metrics + the recent PROGRESS.md log as a single auto-refreshing page.
Read-only: it never writes to the repo or the state DB.
"""

from __future__ import annotations

import argparse
import html
import os
import sys
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from scripts.status import DEFAULT_STATE_PATH, GATE_DAYS, _read_state

_REPO = Path(__file__).resolve().parent.parent

# Verified headline results from the merged phases (see DECISIONS.md S22-S29).
_SHIPPED = [
    (
        "Phase F1 — validate edge",
        "MERGED",
        "Value edge real: survivorship 0.67@2%/yr · temporal 16/17 yrs · universe 0.70 / 100%.",
    ),
    (
        "Phase F2 — operator UX",
        "MERGED",
        "status / preflight CLIs · make check (local CI parity) · README quickstart.",
    ),
    (
        "Phase F3 — second edge",
        "MERGED",
        "20% value + 80% trend -> Sharpe 0.82 to 0.99 (+21%), drawdown flat 7%, corr +0.24.",
    ),
    (
        "Deployed: multi-asset trend",
        "LIVE (paper)",
        "7-sleeve inverse-vol, Gauntlet grade A (OOS 1.34), GitHub Actions cron.",
    ),
    (
        "Next: F3.3 allocation engine",
        "READY",
        "Build the ~20/80 allocator (backtest mode); live value gated on W8 paid data.",
    ),
]


def _recent_progress(limit: int = 28) -> str:
    """First `limit` non-empty lines of PROGRESS.md (the running log)."""
    path = _REPO / "PROGRESS.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "(PROGRESS.md not found)"
    return "\n".join(lines[:limit])


# Polished card UI — same design language as the Overseer board (GitHub-dark
# tokens, card grid, status badges). Kept in a plain string so the f-string body
# below doesn't have to escape every brace.
_CSS = """
:root{--bg:#0d1117;--card:#161b22;--line:#30363d;--ink:#e6edf3;--muted:#8b949e;
--grn:#2ea043;--yel:#d29922;--red:#f85149;--cyn:#58a6ff;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 18px;font-size:13px}
.sub a{color:var(--cyn);text-decoration:none}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
margin:28px 0 12px;border-bottom:1px solid var(--line);padding-bottom:6px}
.bar{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:4px}
.chip{background:var(--card);border:1px solid var(--line);border-radius:999px;
padding:5px 12px;font-size:12.5px;color:var(--muted)}
.chip b{color:var(--ink);font-weight:600}
.chip.ok b{color:var(--grn)}.chip.red b{color:var(--red)}.chip.cyn b{color:var(--cyn)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
.grid.ship{grid-template-columns:repeat(auto-fill,minmax(330px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.stat .k{color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.05em}
.stat .v{font-size:22px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
.scard{border-left:4px solid var(--muted)}
.scard.ok{border-left-color:var(--grn)}.scard.work{border-left-color:var(--yel)}
.scard.behind{border-left-color:var(--red)}
.scard .h{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.scard h3{margin:0;font-size:15px}
.scard p{margin:8px 0 0;color:var(--muted);font-size:12.5px}
.badge{font-size:11.5px;padding:2px 9px;border-radius:999px;border:1px solid var(--line);
white-space:nowrap;color:var(--muted)}
.badge.ok{color:var(--grn);border-color:#1f6f33}
.badge.work{color:var(--yel);border-color:#7a5b14}
.badge.behind{color:var(--red);border-color:#7a2620}
.barwrap{background:#0d1117;border:1px solid var(--line);border-radius:999px;height:12px;
overflow:hidden;margin:12px 0 8px}
.barfill{height:100%;background:linear-gradient(90deg,#1f6feb,#58a6ff)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{text-align:right;color:var(--muted);font-size:11px;text-transform:uppercase;
letter-spacing:.04em;padding:6px 10px;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
td{padding:7px 10px;border-top:1px solid var(--line);font-size:13px}
.pos{color:var(--grn)}.neg{color:var(--red)}
pre{margin:0;white-space:pre-wrap;font:12.5px/1.55 ui-monospace,Menlo,Consolas,monospace;color:var(--muted)}
.halt{background:#2a1513;border:1px solid #7a2620;color:#ffb4ac;border-radius:10px;
padding:12px 16px;margin:14px 0;font-weight:600}
.foot{color:var(--muted);font-size:12px;margin-top:32px;border-top:1px solid var(--line);padding-top:14px}
.foot a{color:var(--cyn);text-decoration:none}
"""


def _money(value) -> str:
    return f"${Decimal(str(value)):,.2f}"


def _badge_class(status: str) -> str:
    """Map a status label to a colour class (green / yellow / red / muted)."""
    s = status.upper()
    if any(k in s for k in ("FAIL", "BLOCK", "HALT", "KILLED", "RED")):
        return "behind"
    if any(k in s for k in ("LIVE", "MERGED", "DONE", "PASS", "GREEN", "OK")):
        return "ok"
    if any(k in s for k in ("READY", "WIP", "NEXT", "PROGRESS", "DOING", "CANDIDATE")):
        return "work"
    return "muted"  # neutral: research, gated, archived


def build_page(state: dict, shipped: list[tuple[str, str, str]], progress: str) -> str:
    """Pure: assemble the polished dashboard HTML from a structured state dict."""
    zero = Decimal("0")
    mode = str(state.get("mode", "unknown"))
    broker = str(state.get("broker", "unknown"))
    equity = Decimal(str(state.get("equity", "0")))
    peak = Decimal(str(state.get("peak_equity", "0")))
    cash = Decimal(str(state.get("cash", "0")))
    drawdown = max(zero, (peak - equity) / peak) if peak > zero else zero

    halt_env = str(state.get("apex_halt_env", "")).strip().lower() in ("1", "true", "yes", "on")
    halted = halt_env or bool(state.get("halt_persisted", False))

    total_runs = int(state.get("total_runs", 0))
    first_ts = str(state.get("first_ts", ""))[:10]
    last_ts = str(state.get("last_ts", ""))[:10]
    gate_frac = min(1.0, total_runs / GATE_DAYS) if GATE_DAYS else 0.0
    has_data = total_runs > 0 or equity > zero

    # --- summary chips -------------------------------------------------------
    status_chip = (
        '<span class="chip red">status <b>HALTED</b></span>'
        if halted
        else '<span class="chip ok">status <b>armed</b></span>'
    )
    chips = (
        f'<span class="chip">mode <b>{html.escape(mode)}</b></span>'
        f'<span class="chip">broker <b>{html.escape(broker)}</b></span>'
        f'<span class="chip cyn">equity <b>{_money(equity)}</b></span>'
        f'<span class="chip">drawdown <b>{drawdown * 100:.2f}%</b></span>'
        f'<span class="chip">gate <b>{total_runs}/{GATE_DAYS}</b></span>'
        f"{status_chip}"
    )

    # --- account stat cards --------------------------------------------------
    def _stat(k: str, v: str) -> str:
        return f'<div class="card stat"><div class="k">{k}</div><div class="v">{v}</div></div>'

    stats = (
        _stat("Equity", _money(equity))
        + _stat("Peak equity", _money(peak))
        + _stat("Cash", _money(cash))
        + _stat("Drawdown", f"{drawdown * 100:.2f}%")
    )

    # --- paper-gate progress bar --------------------------------------------
    span = f"{first_ts} .. {last_ts}" if first_ts and last_ts else "n/a"
    if total_runs >= GATE_DAYS:
        gate_note = "30-day window COMPLETE — eligible for the live checklist"
    elif total_runs > 0:
        gate_note = f"{GATE_DAYS - total_runs} more cycle(s) needed · span {span}"
    else:
        gate_note = "no cycles recorded yet"
    gate_card = (
        '<div class="card">'
        f'<div class="barwrap"><div class="barfill" style="width:{gate_frac * 100:.1f}%"></div></div>'
        f"<div>{total_runs}/{GATE_DAYS} days &nbsp;·&nbsp; {html.escape(gate_note)}</div></div>"
    )

    # --- positions table -----------------------------------------------------
    positions: dict = state.get("positions", {}) or {}
    if positions:
        body_rows = []
        for ticker, pos in sorted(positions.items()):
            qty = Decimal(str(pos.get("qty", "0")))
            avg = Decimal(str(pos.get("avg_entry_price", "0")))
            cur = Decimal(str(pos.get("current_price", "0")))
            unreal = (cur - avg) * qty
            cls = "pos" if unreal >= zero else "neg"
            body_rows.append(
                f"<tr><td>{html.escape(ticker)}</td><td>{qty:,.4f}</td>"
                f"<td>{avg:,.2f}</td><td>{cur:,.2f}</td>"
                f'<td class="{cls}">{float(unreal):+,.2f}</td></tr>'
            )
        positions_card = (
            '<div class="card"><table><tr><th>Ticker</th><th>Qty</th><th>Avg entry</th>'
            "<th>Price</th><th>Unreal P&amp;L</th></tr>" + "".join(body_rows) + "</table></div>"
        )
    else:
        positions_card = (
            '<div class="card"><div class="sub" style="margin:0">no open positions</div></div>'
        )

    # --- shipped / roadmap cards --------------------------------------------
    ship_cards = "".join(
        f'<div class="card scard {_badge_class(s)}"><div class="h">'
        f"<h3>{html.escape(t)}</h3>"
        f'<span class="badge {_badge_class(s)}">{html.escape(s)}</span></div>'
        f"<p>{html.escape(d)}</p></div>"
        for t, s, d in shipped
    )

    halt_banner = (
        '<div class="halt">⛔ SYSTEM IS HALTED — no new orders will be placed</div>'
        if halted
        else ""
    )
    no_data = (
        ""
        if has_data
        else '<div class="card"><div class="sub" style="margin:0">No cycles recorded yet — '
        "run <code>python -m scripts.run_once</code>.</div></div>"
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Apex Quant — Dashboard</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
  <h1>⚡ Apex Quant</h1>
  <p class="sub">event-driven algo-trading engine · paper status as of <b>{html.escape(last_ts or "n/a")}</b>
    · <a href="https://github.com/Dukotah/apex-quant">repo ↗</a></p>
  {halt_banner}
  <div class="bar">{chips}</div>

  <h2>Account</h2>
  <div class="grid">{stats}</div>

  <h2>Paper gate (Rule 17 — 30 days)</h2>
  {gate_card}

  <h2>Open positions</h2>
  {positions_card}

  <h2>Shipped &amp; next</h2>
  <div class="grid ship">{ship_cards}</div>
  {no_data}

  <h2>Recent progress (PROGRESS.md)</h2>
  <div class="card"><pre>{html.escape(progress)}</pre></div>

  <div class="foot">
    Apex Quant · VISION.md · ROADMAP.md · DECISIONS.md ·
    <a href="https://github.com/Dukotah/apex-quant">github.com/Dukotah/apex-quant</a>
  </div>
</div></body></html>
"""


def _render() -> str:
    db_path = Path(os.getenv("APEX_STATE_DB", str(DEFAULT_STATE_PATH)))
    mode = os.getenv("APEX_MODE", "paper")
    state = _read_state(db_path, mode) or {"mode": mode, "broker": os.getenv("APEX_BROKER", "")}
    return build_page(state, _SHIPPED, _recent_progress())


def export_to(path: str | Path) -> Path:
    """Render the dashboard to a static HTML file and return the path written.

    This is what the GitHub Pages workflow calls: it produces the same page the
    local server serves, but as a one-shot static artifact (Pages is static-only).
    Read-only — it never touches the broker or the state DB.
    """
    out = Path(path)
    if str(out.parent) not in ("", "."):
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render(), encoding="utf-8")
    return out


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        body = _render().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # silence per-request logging
        pass


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Local web dashboard for Apex Quant.")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument(
        "--export",
        metavar="PATH",
        help="Render the dashboard to a static HTML file and exit (for GitHub Pages).",
    )
    args = ap.parse_args()

    if args.export:
        out = export_to(args.export)
        print(f"Apex Quant dashboard exported → {out}", flush=True)
        return 0

    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Apex Quant dashboard → {url}   (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
