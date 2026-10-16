# FILE: app.py
# ServiceWatch_V1
# Contact: oguejioforc@concordnc.gov
"""Service Watch: a small web dashboard that keeps Windows services running.

Run from an elevated (Administrator) prompt so it is allowed to start services:
    python app.py

Environment:
  SERVICEWATCH_CONFIG         - path to config.json (default: ./config.json)
  SERVICEWATCH_MOCK=1         - force demo mode even on Windows
  SERVICEWATCH_SMTP_PASSWORD  - consumed by notify.py for SMTP auth
  (all of the above can be placed in .env — python-dotenv loads it at startup)

Troubleshooting checklist:
  * 400 "Missing X-ServiceWatch header" on a POST -> the request did not come
    from the dashboard. This is intentional (CSRF-ish protection).
  * 500 on /api/services/<name>/start -> check the logged traceback; the most
    common cause is a non-elevated shell.
  * Charts empty -> verify servicewatch-history.jsonl is writable in the
    project folder, or just let it build up from a fresh start.
  * Emails silent -> see notify.py's docstring.
  * "Address already in use" -> another instance is running, or change port.
"""
from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Load .env BEFORE anything reads os.environ. Missing file is fine.
from dotenv import load_dotenv

from flask import Flask, jsonify, render_template, request

from history import History
from monitor import IS_WINDOWS, MockBackend, Monitor, WindowsBackend
from notify import Notifier

BASE_DIR = Path(__file__).resolve().parent

# Load .env from the project folder if present. Values already set in the
# real environment are NOT overwritten (override=False is the default).
load_dotenv(BASE_DIR / ".env")

CONFIG_PATH = Path(os.environ.get("SERVICEWATCH_CONFIG", BASE_DIR / "config.json"))


def setup_logging() -> None:
    """Attach a rotating file handler and a console handler to 'servicewatch'."""
    handler = RotatingFileHandler(
        BASE_DIR / "servicewatch.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger("servicewatch")
    root.setLevel(logging.INFO)
    # Avoid duplicate handlers if create_app() is called twice (tests).
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
               for h in root.handlers):
        root.addHandler(logging.StreamHandler())


def create_app() -> tuple[Flask, Monitor, dict]:
    """Wire up logging, config, backend, history, notifier, monitor, and routes."""
    setup_logging()
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    # ---- Backend selection -------------------------------------------- #
    # Demo mode if not on Windows, or if explicitly forced via env var.
    use_mock = os.environ.get("SERVICEWATCH_MOCK") == "1" or not IS_WINDOWS
    if use_mock:
        logging.getLogger("servicewatch").warning(
            "Demo mode: using simulated services (not running on Windows, or SERVICEWATCH_MOCK=1)."
        )
        backend = MockBackend([s["name"] for s in cfg["services"]])
    else:
        backend = WindowsBackend()

    # ---- Optional collaborators --------------------------------------- #
    # JSONL history file. Old configs used "history_db"; accept both so an
    # upgrade doesn't break an existing config.json.
    history_file = cfg.get("history_file") or cfg.get("history_db", "servicewatch-history.jsonl")
    history = History(
        BASE_DIR / history_file,
        retention_days=int(cfg.get("history_retention_days", 30)),
    )
    notifier = Notifier(cfg.get("email", {}))

    # ---- Monitor ------------------------------------------------------ #
    monitor = Monitor(cfg, backend, history=history, notifier=notifier)

    # ---- Flask app ---------------------------------------------------- #
    app = Flask(__name__)

    @app.before_request
    def require_header_for_writes():
        """Reject state-changing requests that don't carry our custom header.

        A custom header triggers a CORS preflight that a random cross-site
        page can't satisfy, so this blocks CSRF-style form posts.
        """
        if request.method == "POST" and request.headers.get("X-ServiceWatch") != "1":
            return jsonify(error="Missing X-ServiceWatch header"), 400

    # ---- Read endpoints ---------------------------------------------- #
    @app.get("/")
    def index():
        """Serve the dashboard."""
        return render_template("index.html")

    @app.get("/api/status")
    def status():
        """Current state of every watched service + recent events."""
        return jsonify(monitor.snapshot())

    @app.get("/api/history")
    def history_series():
        """Bucketed uptime fractions per service for the sparkline/bar chart.

        Query params:
          hours   - window size (1..720). Default 24.
          buckets - number of buckets (6..240). Default 48.
        """
        hours = int(request.args.get("hours", 24))
        buckets = int(request.args.get("buckets", 48))
        hours = max(1, min(hours, 24 * 30))
        buckets = max(6, min(buckets, 240))
        data = {
            name: monitor.history.series(name, hours=hours, buckets=buckets)
            for name in monitor.services
        }
        return jsonify(hours=hours, buckets=buckets, series=data)

    @app.get("/api/uptime")
    def uptime():
        """Uptime percentage per service over a few fixed windows."""
        windows = [1, 24, 24 * 7, 24 * 30]
        out = {}
        for name in monitor.services:
            out[name] = {
                f"{h}h": monitor.history.uptime_pct(name, hours=h) for h in windows
            }
        return jsonify(uptime=out)

    # ---- Write endpoints -------------------------------------------- #
    @app.post("/api/check")
    def check_all():
        """Force an immediate poll of every service."""
        monitor.check_all()
        return jsonify(monitor.snapshot())

    @app.post("/api/services/<name>/start")
    def start_service(name: str):
        """Manually start one service. Resets attempts and the gave_up latch."""
        if name not in monitor.services:
            return jsonify(error="That service isn't on the watch list."), 404
        try:
            monitor.start_now(name)
        except Exception as exc:  # noqa: BLE001 - report the reason to the UI
            return jsonify(error=str(exc)), 500
        return jsonify(monitor.snapshot())

    # ---- Demo-only endpoint ----------------------------------------- #
    if use_mock:

        @app.post("/api/services/<name>/simulate-stop")
        def simulate_stop(name: str):
            """Demo helper: force a mock service into 'stopped' so the restart
            flow can be exercised without touching a real machine."""
            if name not in monitor.services:
                return jsonify(error="That service isn't on the watch list."), 404
            backend.stop(name)
            monitor.check(name)
            return jsonify(monitor.snapshot())

    return app, monitor, cfg


# Module-level app so `waitress-serve app:app` also works.
app, monitor, config = create_app()

if __name__ == "__main__":
    # Start the background poll loop before serving requests.
    monitor.start()

    host = config.get("host", "127.0.0.1")
    port = int(config.get("port", 8080))

    try:
        from waitress import serve
        logging.getLogger("servicewatch").info("Dashboard at http://%s:%s", host, port)
        serve(app, host=host, port=port)
    except ImportError:
        # Fallback for environments without waitress installed.
        app.run(host=host, port=port)


# End of File: app.py