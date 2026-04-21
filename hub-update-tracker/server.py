#!/usr/bin/env python3
"""
Minimal local server — enables the Rerun button on dashboard.html.

Usage:
    python3 server.py          # or: double-click run.command

Serves static files from this folder and exposes one API:
    POST /api/rescan   — runs scan.py, regenerates dashboard.html + jv-dashboard.html

The dashboards remain fully standalone — this server is only needed if you want
the Rerun button inside the dashboard to work. If you just open dashboard.html
directly via file://, the button shows a helpful message.

Stop with Ctrl+C.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
os.chdir(SCRIPT_DIR)
sys.path.insert(0, str(SCRIPT_DIR))

import scan as scan_mod  # noqa: E402

HOST = "127.0.0.1"
PORT = int(os.environ.get("HUB_TRACKER_PORT", "8765"))


class Handler(SimpleHTTPRequestHandler):
    # Serve from SCRIPT_DIR regardless of where it was invoked from
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SCRIPT_DIR), **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[server] {self.command} {self.path} — {fmt % args}\n")

    def do_POST(self):
        if self.path == "/api/rescan":
            try:
                t0 = time.time()
                status = scan_mod.scan()
                (SCRIPT_DIR / "dashboard.html").write_text(scan_mod.render_dashboard(status))
                (SCRIPT_DIR / "jv-dashboard.html").write_text(scan_mod.render_jv_dashboard(status))
                (SCRIPT_DIR / "hub-status-report.html").write_text(scan_mod.render_report(status))
                dt = round(time.time() - t0, 2)
                self._send_json({
                    "ok": True,
                    "scanned_at": status["scanned_at"],
                    "duration_sec": dt,
                    "summary": status["summary"],
                })
            except Exception as e:
                traceback.print_exc()
                self._send_json({"ok": False, "error": str(e)}, status=500)
            return
        self.send_error(404)

    def do_GET(self):
        # Redirect root to dashboard
        if self.path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/dashboard.html")
            self.end_headers()
            return
        super().do_GET()

    def end_headers(self):
        # Discourage caching so Rerun's post-reload shows fresh content
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def ensure_dashboards_exist() -> None:
    if not (SCRIPT_DIR / "dashboard.html").exists() or not (SCRIPT_DIR / "jv-dashboard.html").exists():
        status = scan_mod.scan()
        (SCRIPT_DIR / "dashboard.html").write_text(scan_mod.render_dashboard(status))
        (SCRIPT_DIR / "jv-dashboard.html").write_text(scan_mod.render_jv_dashboard(status))


def main() -> int:
    ensure_dashboards_exist()
    url = f"http://{HOST}:{PORT}/dashboard.html"
    server = ThreadingHTTPServer((HOST, PORT), Handler)

    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    print()
    print("━" * 60)
    print("  Endocrine Hub Update Tracker")
    print(f"  Dashboard: {url}")
    print(f"  JV tool:   http://{HOST}:{PORT}/jv-dashboard.html")
    print("  Press Ctrl+C to stop.")
    print("━" * 60)
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
