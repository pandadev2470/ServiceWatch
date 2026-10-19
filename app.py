# FILE: app.py
# ServiceWatch_V1
# Contact: admin@thepandadev.com
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
  * "Another Service Watch instance is already running" -> the scheduled task
    (or another console) already has it open. See install-task.ps1.
  * "Invalid config.json" -> the listed keys are missing or have wrong types.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import sys
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
# [Claude review 2026-10-16] Resolve a relative SERVICEWATCH_CONFIG against
# the project folder, not the current working directory, so .env's
# "SERVICEWATCH_CONFIG=config.json" works when launched from elsewhere
# (e.g. Task Scheduler without "Start in").
if not CONFIG_PATH.is_absolute():
    CONFIG_PATH = BASE_DIR / CONFIG_PATH

# [Claude review 2026-10-16] Hostnames accepted by default when bound to loopback.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _hostname(host_header: str) -> str:
    """[Claude review 2026-10-16] Strip the port from a Host header value.

    Handles bracketed IPv6 ("[::1]:8080" -> "::1").
    """
    host_header = (host_header or "").strip().lower()
    if host_header.startswith("["):
        return host_header[1:].split("]", 1)[0]
    return host_header.rsplit(":", 1)[0] if ":" in host_header else host_header


def validate_config(cfg: dict) -> None:
    """[Claude review 2026-10-19] Fail fast with a readable message.

    A missing services[].name or a wrong type used to surface as a raw
    KeyError/TypeError traceback, which is easy to miss under Task Scheduler.
    """
    errors: list[str] = []
    services = cfg.get("services")
    if not isinstance(services, list) or not services:
        errors.append("'services' must be a non-empty list.")
    else:
        seen: set[str] = set()
        for i, s in enumerate(services):
            name = s.get("name") if isinstance(s, dict) else None
            if not isinstance(name, str) or not name.strip():
                errors.append(f"services[{i}] needs a non-empty 'name'.")
            elif name.lower() in seen:
                errors.append(f"services[{i}]: '{name}' is listed twice.")
            else:
                seen.add(name.lower())
    minimums = {"port": 1, "check_interval_seconds": 1, "restart_cooldown_seconds": 0,
                "max_restart_attempts": 0, "stable_after_seconds": 0,
                "history_retention_days": 1}
    for key, minimum in minimums.items():
        val = cfg.get(key)
        if val is not None and (isinstance(val, bool) or not isinstance(val, int) or val < minimum):
            errors.append(f"'{key}' must be a whole number >= {minimum}.")
    email = cfg.get("email", {})
    if not isinstance(email, dict):
        errors.append("'email' must be an object.")
    elif email.get("enabled") and not isinstance(email.get("to_addrs"), (list, str)):
        errors.append("'email.to_addrs' must be a list of addresses.")
    if errors:
        raise SystemExit("Invalid config.json (%s):\n  - %s" % (CONFIG_PATH, "\n  - ".join(errors)))


def acquire_single_instance_lock():
    """[Claude review 2026-10-19] Refuse to run twice on the same machine.

    Two instances (e.g. the scheduled task plus a manual run) would both
    restart services, both send every email, and interleave writes to the
    same JSONL/log. The port bind only failed *after* the monitor thread had
    already started. Returns a handle that must stay referenced.
    """
    if not IS_WINDOWS:
        return None
    import pywintypes
    import win32api
    import win32event
    import winerror

    try:
        handle = win32event.CreateMutex(None, False, "Global\\ServiceWatch_V1")
        already = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    except pywintypes.error as exc:
        # Access denied means another account (e.g. SYSTEM) owns the mutex.
        if exc.winerror != winerror.ERROR_ACCESS_DENIED:
            raise
        handle, already = None, True
    if already:
        raise SystemExit("Another Service Watch instance is already running on this machine.")
    return handle


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
    # [Claude review 2026-10-19] Validate, and log config errors as well as
    # raising them: under Task Scheduler nobody sees stderr.
    try:
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Cannot load config {CONFIG_PATH}: {exc}") from exc
        validate_config(cfg)
    except SystemExit as exc:
        logging.getLogger("servicewatch").error("%s", exc)
        raise

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

    # [Claude review 2026-10-16] Host-header allowlist to block DNS-rebinding.
    # Without it, a malicious site can rebind its own domain to 127.0.0.1;
    # the browser then treats requests as same-origin, so the X-ServiceWatch
    # header check below no longer helps. Set "allowed_hosts" in config.json
    # when binding to a non-loopback address.
    bind_host = str(cfg.get("host", "127.0.0.1"))
    allowed_hosts = {h.lower() for h in cfg.get("allowed_hosts", [])}
    if not allowed_hosts and bind_host in LOOPBACK_HOSTS:
        allowed_hosts = set(LOOPBACK_HOSTS)
    if bind_host not in LOOPBACK_HOSTS:
        logging.getLogger("servicewatch").warning(
            "Dashboard bound to %s with NO authentication. Anyone who can reach "
            "this port can start services. Prefer 127.0.0.1.", bind_host
        )
        if not allowed_hosts:
            logging.getLogger("servicewatch").warning(
                "No allowed_hosts configured; Host-header check is disabled."
            )

    @app.before_request
    def check_host_header():
        """Reject requests whose Host header isn't on the allowlist."""
        if allowed_hosts and _hostname(request.host) not in allowed_hosts:
            return jsonify(error="Host not allowed"), 403

    @app.before_request
    def require_header_for_writes():
        """Reject state-changing requests that don't carry our custom header.

        A custom header triggers a CORS preflight that a random cross-site
        page can't satisfy, so this blocks CSRF-style form posts.
        """
        # [Claude review 2026-10-16] Cover every unsafe method, not just POST,
        # so any future PUT/PATCH/DELETE route is protected automatically.
        if (request.method not in ("GET", "HEAD", "OPTIONS")
                and request.headers.get("X-ServiceWatch") != "1"):
            return jsonify(error="Missing X-ServiceWatch header"), 400

    @app.after_request
    def security_headers(resp):
        """[Claude review 2026-10-16] Baseline hardening headers.

        frame-ancestors / X-Frame-Options stop clickjacking of the
        "Start service" button; nosniff stops MIME sniffing of JSON.
        """
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

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
        # [Claude review 2026-10-16] type=int falls back to the default on
        # junk input (e.g. ?hours=abc) instead of raising ValueError -> 500.
        hours = request.args.get("hours", 24, type=int)
        buckets = request.args.get("buckets", 48, type=int)
        hours = max(1, min(hours, 24 * 30))
        buckets = max(6, min(buckets, 240))
        data = {
            name: monitor.history.series(name, hours=hours, buckets=buckets)
            for name in monitor.services
        }
        return jsonify(hours=hours, buckets=buckets, series=data)

    @app.get("/api/uptime")
    def uptime():
        """Uptime percentage per service over a few fixed windows.

        [Claude review 2026-10-16] Note: computed from the in-memory cache,
        which holds at most `max_samples_per_service` samples (~55h at a 10s
        interval). The 7d/30d figures therefore cover only that span.
        """
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


# [Claude review 2026-10-19] Taken before anything polls or writes files.
_instance_lock = acquire_single_instance_lock()

# Module-level app so `waitress-serve app:app` also works.
app, monitor, config = create_app()

# [Claude review 2026-10-16] Start the poll loop at import time. Previously
# this only ran under __main__, so `waitress-serve app:app` served a dashboard
# whose monitor never polled or restarted anything. start() is idempotent.
monitor.start()


@atexit.register
def _shutdown() -> None:
    """[Claude review 2026-10-19] Stop polling and close the history file on
    a normal exit (Ctrl+C, startup failure such as the port being in use)."""
    monitor.stop()
    if monitor.history is not None:
        monitor.history.close()
    logging.getLogger("servicewatch").info("Service Watch stopped.")

if __name__ == "__main__":

    host = config.get("host", "127.0.0.1")
    port = int(config.get("port", 8080))

    try:
        from waitress import serve
    except ImportError:
        # Fallback for environments without waitress installed.
        app.run(host=host, port=port)
    else:
        # [Claude review 2026-10-19] The import is no longer wrapped together
        # with serve(): an ImportError raised *inside* waitress at runtime used
        # to fall through to Flask's development server.
        logging.getLogger("servicewatch").info("Dashboard at http://%s:%s", host, port)
        try:
            serve(app, host=host, port=port)
        except OSError as exc:
            # e.g. port already in use. Log it so it shows up in
            # servicewatch.log when running hidden under Task Scheduler.
            logging.getLogger("servicewatch").error("Cannot serve on %s:%s: %s", host, port, exc)
            sys.exit(1)


# End of File: app.py