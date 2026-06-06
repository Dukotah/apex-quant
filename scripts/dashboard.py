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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from scripts.status import DEFAULT_STATE_PATH, _read_state, render_status

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


def build_page(status_text: str, shipped: list[tuple[str, str, str]], progress: str) -> str:
    """Pure: assemble the dashboard HTML from the live status block + shipped rows + log."""
    rows = "\n".join(
        f'<tr><td class="t">{html.escape(t)}</td>'
        f'<td><span class="badge">{html.escape(s)}</span></td>'
        f"<td>{html.escape(d)}</td></tr>"
        for t, s, d in shipped
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Apex Quant — Dashboard</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; background:#0d1117; color:#e6edf3; font:14px/1.5 ui-monospace,Menlo,Consolas,monospace; }}
  .wrap {{ max-width: 980px; margin: 0 auto; padding: 24px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  h2 {{ font-size: 14px; text-transform: uppercase; letter-spacing: .08em; color:#7d8590; margin: 28px 0 10px; }}
  .sub {{ color:#7d8590; margin-bottom: 8px; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:10px; padding:16px; }}
  pre {{ margin:0; white-space:pre-wrap; font-size:13px; }}
  table {{ width:100%; border-collapse:collapse; }}
  td {{ padding:8px 10px; border-top:1px solid #21262d; vertical-align:top; }}
  td.t {{ white-space:nowrap; color:#e6edf3; font-weight:600; }}
  .badge {{ background:#1f6feb33; color:#58a6ff; border:1px solid #1f6feb55; border-radius:6px; padding:1px 7px; font-size:12px; }}
  a {{ color:#58a6ff; }}
</style></head>
<body><div class="wrap">
  <h1>⚡ Apex Quant</h1>
  <div class="sub">event-driven algo-trading engine · local dashboard · auto-refresh 30s</div>

  <h2>Live system status</h2>
  <div class="card"><pre>{html.escape(status_text)}</pre></div>

  <h2>Shipped &amp; next</h2>
  <div class="card"><table>{rows}</table></div>

  <h2>Recent progress (PROGRESS.md)</h2>
  <div class="card"><pre>{html.escape(progress)}</pre></div>

  <div class="sub" style="margin-top:20px">
    Full project: VISION.md · ROADMAP.md · WORKLOAD.md · DECISIONS.md ·
    <a href="https://github.com/Dukotah/apex-quant">github.com/Dukotah/apex-quant</a>
  </div>
</div></body></html>
"""


def _render() -> str:
    db_path = Path(os.getenv("APEX_STATE_DB", str(DEFAULT_STATE_PATH)))
    mode = os.getenv("APEX_MODE", "paper")
    state = _read_state(db_path, mode)
    status_text = render_status(state)
    return build_page(status_text, _SHIPPED, _recent_progress())


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
    args = ap.parse_args()

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
