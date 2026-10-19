# FILE: monitor.py
# ServiceWatch_V1
# Contact: admin@thepandadev.com
"""Service monitoring logic.

Polls Windows services on an interval, records state changes, and starts any
watched service that has stopped (with a cooldown and attempt limit so a
crash-looping service doesn't get restarted forever).

Troubleshooting checklist:
  * "Automatic start failed: Access is denied." -> run app.py from an
    elevated (Administrator) terminal.
  * "Cannot read status: The specified service does not exist" -> the name in
    config.json is the *service name*, not the display name. Verify with
    `Get-Service | Select Name, DisplayName`.
  * Service flapping every few seconds -> raise restart_cooldown_seconds.
  * Service stuck in "gave_up" -> click "Start service" in the UI (that
    resets attempts and the gave_up flag), or restart the app.
"""
from __future__ import annotations

import logging
import sys
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

# Collaborators used for type hints. [Claude review 2026-10-16] The old
# comment said these were "imported lazily"; they are not — they are normal
# top-level imports. Comment corrected to match the code.
from history import History
from notify import Notifier

log = logging.getLogger("servicewatch")
IS_WINDOWS = sys.platform == "win32"

# Win32 error code returned when QueryServiceStatus can't find the name.
ERROR_SERVICE_DOES_NOT_EXIST = 1060

# [Claude review 2026-10-19] States that trigger a "down" email. not_found
# (service uninstalled or renamed) used to show only in the UI.
DOWN_STATES = {"stopped", "error", "not_found"}


def _now() -> datetime:
    """UTC now. All internal timestamps are timezone-aware UTC."""
    return datetime.now(timezone.utc)


def _iso(t: datetime | None) -> str | None:
    """Serialize a datetime for JSON, or None."""
    return t.isoformat(timespec="seconds") if t else None


# --------------------------------------------------------------------------
# Backends: the only code that talks to the operating system
# --------------------------------------------------------------------------
class WindowsBackend:
    """Reads and starts services through the Windows Service Control Manager."""

    def __init__(self) -> None:
        # Imported here so the module can be imported on non-Windows machines
        # (tests, CI, demo mode) without pywin32 installed.
        import pywintypes
        import win32service
        import win32serviceutil

        self._pywintypes = pywintypes
        self._util = win32serviceutil

        # Map numeric SCM state constants to our string vocabulary.
        self._states = {
            win32service.SERVICE_STOPPED: "stopped",
            win32service.SERVICE_START_PENDING: "starting",
            win32service.SERVICE_STOP_PENDING: "stopping",
            win32service.SERVICE_RUNNING: "running",
            win32service.SERVICE_CONTINUE_PENDING: "starting",
            win32service.SERVICE_PAUSE_PENDING: "stopping",
            win32service.SERVICE_PAUSED: "paused",
        }

    def query(self, name: str) -> str:
        """Return one of our string states, or 'not_found', or raise."""
        try:
            status = self._util.QueryServiceStatus(name)
        except self._pywintypes.error as exc:
            # Missing service is a normal condition; everything else is a
            # real error we want surfaced in the UI.
            if exc.winerror == ERROR_SERVICE_DOES_NOT_EXIST:
                return "not_found"
            raise RuntimeError(exc.strerror or str(exc)) from exc
        # status is a tuple; index 1 is the current state constant.
        return self._states.get(status[1], "unknown")

    def start(self, name: str) -> None:
        """Ask the SCM to start the service. Raises RuntimeError on failure."""
        try:
            self._util.StartService(name)
        except self._pywintypes.error as exc:
            raise RuntimeError(exc.strerror or str(exc)) from exc


class MockBackend:
    """In-memory stand-in so the app can be tried on a non-Windows machine."""

    # Flagged so /api/status can tell the UI it's in demo mode.
    is_mock = True

    def __init__(self, names: list[str]) -> None:
        # Everything starts running so the dashboard looks healthy at boot.
        self.states = {n: "running" for n in names}

    def query(self, name: str) -> str:
        return self.states.get(name, "not_found")

    def start(self, name: str) -> None:
        self.states[name] = "running"

    def stop(self, name: str) -> None:
        """Only used by /api/services/<name>/simulate-stop in demo mode."""
        if name in self.states:
            self.states[name] = "stopped"


# --------------------------------------------------------------------------
# Monitor
# --------------------------------------------------------------------------
@dataclass
class Watched:
    """Per-service runtime state. One instance per entry in config.json."""
    name: str                       # Windows service short name
    label: str                      # Friendly name shown in the UI
    auto_restart: bool              # Should we try to start it if stopped?
    state: str = "unknown"          # Last observed state
    since: datetime | None = None   # When the current state began
    last_check: datetime | None = None
    attempts: int = 0               # Consecutive restart attempts (reset on stable)
    total_restarts: int = 0         # Lifetime count shown in the UI
    last_attempt: datetime | None = None
    error: str | None = None        # Last backend error, if any
    gave_up: bool = False           # Latched true after max_attempts
    # [Claude review 2026-10-16] True once a "down" alert has been sent and
    # not yet answered by a "recovered" alert. Lets recovery be detected even
    # when the service passes through "starting" on its way back to running.
    alerted_down: bool = False
    # [Claude review 2026-10-19] Alert (kind, subject, body) that is owed but
    # was deferred by the notifier cooldown. Retried on every poll so an
    # outage is never silently swallowed by the cooldown.
    pending_alert: tuple[str, str, str] | None = None


class Monitor:
    """Owns the poll loop, restart policy, event log, history, and emails."""

    def __init__(self, cfg: dict, backend,
                 history: History | None = None,
                 notifier: Notifier | None = None) -> None:
        self.backend = backend

        # Clamp interval to something sane; a 0s interval would spin.
        self.interval = max(2, int(cfg.get("check_interval_seconds", 10)))
        self.max_attempts = int(cfg.get("max_restart_attempts", 3))
        self.cooldown = int(cfg.get("restart_cooldown_seconds", 30))
        self.stable_after = int(cfg.get("stable_after_seconds", 60))

        # Optional collaborators. If None, those features are simply skipped.
        self.history = history
        self.notifier = notifier

        # Build the watch list from config. Keys are the service short names.
        self.services: dict[str, Watched] = {
            s["name"]: Watched(
                name=s["name"],
                label=s.get("label") or s["name"],
                auto_restart=bool(s.get("auto_restart", True)),
            )
            for s in cfg["services"]
        }

        # Bounded in-memory event log (newest first) for the UI.
        self.events: deque[dict] = deque(maxlen=200)

        # RLock so check() can call _event() and _try_restart() while holding.
        self._lock = threading.RLock()
        self._stop_flag = threading.Event()
        self._thread: threading.Thread | None = None

        # Counts ticks so we only prune the DB about once an hour.
        self._prune_counter = 0
        # [Claude review 2026-10-16] Derive ticks-per-hour from the real
        # interval. The old hard-coded 360 assumed a 10s interval, so a 60s
        # interval pruned only every 6 hours.
        self._prune_every = max(1, 3600 // self.interval)

    # -- events ------------------------------------------------------------
    def _event(self, level: str, service: str, message: str) -> None:
        """Append to the in-memory log and echo to the rotating log file."""
        self.events.appendleft(
            {"time": _iso(_now()), "level": level, "service": service, "message": message}
        )
        # Route to the matching logger method; keeps levels consistent.
        getattr(log, {"info": "info", "warning": "warning", "error": "error"}[level])(
            "%s: %s", service, message
        )

    def _notify(self, kind: str, svc: "Watched", subject: str, body: str) -> None:
        """Queue an alert for `svc` and try to send it now.

        [Claude review 2026-10-19] Previously the notifier's cooldown dropped
        alerts outright. Sequence: down (emailed) -> recovered (emailed) ->
        down again within cooldown_seconds -> suppressed, so recipients'
        last email said "recovered" while the service stayed down. The alert
        is now held in `pending_alert` and retried every poll until the
        cooldown allows it, or until it no longer applies.
        """
        svc.pending_alert = (kind, subject, body)
        self._flush_alert(svc)

    def _flush_alert(self, svc: "Watched") -> None:
        """Send `svc.pending_alert` if the notifier accepts it now."""
        if svc.pending_alert is None:
            return
        kind, subject, body = svc.pending_alert
        if self.notifier is None or self.notifier.notify(kind, svc.name, subject, body):
            svc.pending_alert = None
            # Track what recipients were last told about this service.
            svc.alerted_down = kind != "recovered"

    def _down_alert(self, svc: "Watched") -> tuple[str, str]:
        """Subject and body for a 'down' alert in the current state."""
        if svc.state == "error":
            return (f"{svc.label} status unreadable",
                    f"Could not read status for {svc.label} ({svc.name}): {svc.error}")
        if svc.state == "not_found":
            return (f"{svc.label} not found",
                    f"{svc.label} ({svc.name}) was not found on this machine. "
                    "It may have been uninstalled or renamed.")
        return (f"{svc.label} stopped",
                f"{svc.label} ({svc.name}) is stopped."
                + (" Auto-restart is enabled." if svc.auto_restart
                   else " Auto-restart is disabled."))

    # -- checking ----------------------------------------------------------
    def check(self, name: str) -> None:
        """Poll one service, update state, record history, maybe restart."""
        with self._lock:
            svc = self.services[name]
            now = _now()

            # --- Query the backend. Never let a backend error kill the loop.
            try:
                state = self.backend.query(name)
                svc.error = None
            except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
                state = "error"
                svc.error = str(exc)

            # --- Handle state transitions (log + email).
            if state != svc.state:
                previous = svc.state
                svc.state = state
                svc.since = now

                if previous == "unknown":
                    # First-ever observation; just note it.
                    self._event("info" if state == "running" else "warning", svc.label,
                                f"Monitoring started, status is {state.replace('_', ' ')}.")
                elif state == "running":
                    self._event("info", svc.label, "Service is running.")
                elif state == "error":
                    self._event("error", svc.label, f"Cannot read status: {svc.error}")
                else:
                    self._event("warning", svc.label,
                                f"Service is {state.replace('_', ' ')}.")

                # --- Alerts. [Claude review 2026-10-19] Decided separately
                # from the event log. A service that is already down when
                # monitoring starts (e.g. it failed at boot) used to get no
                # email at all because previous == "unknown" was skipped.
                if state in DOWN_STATES:
                    if svc.alerted_down:
                        # Recipients already think it's down (e.g. stopped ->
                        # not_found); drop any unsent "recovered".
                        svc.pending_alert = None
                    else:
                        self._notify("down", svc, *self._down_alert(svc))
                elif state == "running":
                    # [Claude review 2026-10-16] Keyed off alerted_down, not
                    # `previous`, so stopped -> starting -> running (the
                    # normal path after StartService) still sends it.
                    if svc.alerted_down:
                        self._notify(
                            "recovered", svc,
                            f"{svc.label} recovered",
                            f"{svc.label} ({svc.name}) is running again.",
                        )
                    else:
                        # A brief outage whose "down" email was still held by
                        # the cooldown: nothing worth telling anyone.
                        svc.pending_alert = None
            svc.last_check = now

            # --- Persist a sample for charts/uptime.
            if self.history is not None:
                self.history.record(svc.name, state)

            # --- If it's been running long enough, reset the attempt counter.
            # This is what stops a service that flaps every few minutes from
            # eventually latching into gave_up.
            if (
                state == "running"
                and svc.attempts
                and (now - svc.since).total_seconds() >= self.stable_after
            ):
                svc.attempts = 0
                svc.gave_up = False
                self._event("info", svc.label,
                            "Recovered and stable; restart counter reset.")

            # --- Attempt restart if stopped and allowed.
            if state == "stopped" and svc.auto_restart:
                self._try_restart(svc, now)

            # --- Retry an alert the cooldown held back. [Claude review 2026-10-19]
            self._flush_alert(svc)

    def _try_restart(self, svc: Watched, now: datetime) -> None:
        """Start the service if policy allows. Called under self._lock."""
        # Latched: we already gave up. Only a manual start clears this.
        if svc.gave_up:
            return

        # Cooldown: don't hammer a service that just failed to start.
        if svc.last_attempt and (now - svc.last_attempt).total_seconds() < self.cooldown:
            return

        # Hit the attempt cap: latch gave_up and email once.
        if svc.attempts >= self.max_attempts:
            svc.gave_up = True
            self._event(
                "error",
                svc.label,
                f"Stopped restarting after {self.max_attempts} attempts. "
                "Start it manually to resume monitoring.",
            )
            self._notify(
                "gave_up", svc,
                f"Giving up on {svc.label}",
                f"{svc.label} ({svc.name}) is still down after "
                f"{self.max_attempts} attempts. Manual intervention required.",
            )
            return

        # Count the attempt and try to start. Note: we increment before the
        # call so a start that throws still counts toward max_attempts.
        svc.attempts += 1
        svc.last_attempt = now
        try:
            self.backend.start(svc.name)
            svc.total_restarts += 1
            self._event(
                "warning",
                svc.label,
                f"Start requested automatically (attempt {svc.attempts} of {self.max_attempts}).",
            )
        except Exception as exc:  # noqa: BLE001
            svc.error = str(exc)
            self._event("error", svc.label, f"Automatic start failed: {exc}")

    def check_all(self) -> None:
        """Poll every watched service once."""
        for name in list(self.services):
            self.check(name)

    # -- manual actions ----------------------------------------------------
    def start_now(self, name: str) -> None:
        """Manual start from the UI. Resets attempts and the gave_up latch."""
        with self._lock:
            svc = self.services[name]
            svc.attempts = 0
            svc.gave_up = False
            try:
                self.backend.start(name)
                svc.total_restarts += 1
                self._event("info", svc.label, "Start requested manually.")
            except Exception as exc:  # noqa: BLE001
                svc.error = str(exc)
                self._event("error", svc.label, f"Manual start failed: {exc}")
                raise  # Re-raise so app.py can return a 500 to the UI.
        # Refresh state immediately so the UI doesn't wait for the next tick.
        self.check(name)

    # -- lifecycle ---------------------------------------------------------
    def _run(self) -> None:
        """The monitor thread's main loop."""
        while not self._stop_flag.is_set():
            try:
                self.check_all()

                # Prune roughly once an hour (see self._prune_every).
                self._prune_counter += 1
                if self.history is not None and self._prune_counter >= self._prune_every:
                    self.history.prune()
                    self._prune_counter = 0
            except Exception:  # noqa: BLE001
                log.exception("Unexpected error in monitor loop")
            # Wait on the stop flag so stop() returns promptly.
            self._stop_flag.wait(self.interval)

    def start(self) -> None:
        """Start the background thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the loop to exit on its next tick."""
        self._stop_flag.set()

    # -- reporting ---------------------------------------------------------
    def snapshot(self) -> dict:
        """JSON-serializable summary used by /api/status."""
        with self._lock:
            return {
                "mock": bool(getattr(self.backend, "is_mock", False)),
                "interval": self.interval,
                "max_attempts": self.max_attempts,
                "services": [
                    {
                        "name": s.name,
                        "label": s.label,
                        "state": s.state,
                        "since": _iso(s.since),
                        "last_check": _iso(s.last_check),
                        "auto_restart": s.auto_restart,
                        "attempts": s.attempts,
                        "total_restarts": s.total_restarts,
                        "gave_up": s.gave_up,
                        "error": s.error,
                    }
                    for s in self.services.values()
                ],
                # Only the newest 60 events are sent to the UI.
                "events": list(self.events)[:60],
            }


# End of File: monitor.py