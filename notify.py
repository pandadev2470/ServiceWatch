# FILE: notify.py
# ServiceWatch_V1
# Contact: oguejioforc@concordnc.gov
"""Email notifications for service state changes.

Uses only the standard library (smtplib + email.message). If email is not
configured, every call is a cheap no-op so the monitor loop never depends on
it. Sends are dispatched on a background thread so SMTP latency never blocks
the Flask request thread or the monitor loop.

Troubleshooting checklist:
  * Nothing is arriving? Set "email.enabled": true in config.json.
  * Auth failing? Make sure the env var named in "email.password_env" exists
    before launching the app. Gmail/Outlook require an "app password".
  * 587 vs 465? 587 uses STARTTLS (use_tls: true). 465 uses implicit TLS
    and is NOT handled here -- switch to port 587 for app passwords.
  * Check servicewatch.log for "Notification sent:" or
    "Failed to send notification:" lines.
"""
from __future__ import annotations

import logging
import os
import smtplib
import threading
import time
from email.message import EmailMessage

# Reuse the app-wide logger so notifications show up alongside monitor events.
log = logging.getLogger("servicewatch")


class Notifier:
    """Thin wrapper around smtplib with per-(event, service) cooldown."""

    def __init__(self, cfg: dict) -> None:
        # `cfg` is the "email" block from config.json. May be empty dict.
        self.cfg = cfg or {}
        self.enabled = bool(self.cfg.get("enabled"))

        # Minimum seconds between two emails of the same kind for the same
        # service. Prevents a flapping service from flooding the inbox.
        self.cooldown = int(self.cfg.get("cooldown_seconds", 300))

        # Which event kinds should trigger an email.
        # Recognized kinds: "down", "recovered", "gave_up".
        self.events_wanted = set(
            self.cfg.get("notify_on", ["down", "recovered", "gave_up"])
        )

        # Track the last time we sent each (kind, service) pair.
        self._last_sent: dict[tuple[str, str], float] = {}
        # Protects _last_sent from concurrent access by the monitor thread.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Credentials
    # ------------------------------------------------------------------ #
    def _password(self) -> str | None:
        """Resolve the SMTP password.

        Prefer the environment variable named by "password_env" so the secret
        never lands in config.json. Fall back to a plaintext "password" key
        for local testing only.
        """
        env = self.cfg.get("password_env")
        if env:
            return os.environ.get(env)
        return self.cfg.get("password")

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    def notify(self, kind: str, service_label: str, subject: str, body: str) -> None:
        """Queue a notification if it passes the enabled + cooldown checks.

        Returns immediately. The actual SMTP conversation happens on a
        daemon thread so a slow mail server can't stall the caller.
        """
        # Feature disabled or event kind not requested -> do nothing.
        if not self.enabled or kind not in self.events_wanted:
            return

        # Cooldown key is per (kind, service) so a "down" and a "recovered"
        # for the same service don't block each other.
        key = (kind, service_label)
        now = time.time()
        with self._lock:
            if now - self._last_sent.get(key, 0) < self.cooldown:
                return  # Still in cooldown window, skip.
            self._last_sent[key] = now

        # Fire and forget. If it fails, _send logs the traceback.
        threading.Thread(
            target=self._send, args=(subject, body), name="notify", daemon=True
        ).start()

    # ------------------------------------------------------------------ #
    # Actual SMTP send
    # ------------------------------------------------------------------ #
    def _send(self, subject: str, body: str) -> None:
        """Do the SMTP dance. Any failure is logged, never raised."""
        try:
            host = self.cfg["smtp_host"]
            port = int(self.cfg.get("smtp_port", 587))
            use_tls = bool(self.cfg.get("use_tls", True))
            user = self.cfg.get("username")
            password = self._password()

            # Build the message with the stdlib API (no manual header munging).
            msg = EmailMessage()
            msg["From"] = self.cfg.get("from_addr") or user
            msg["To"] = ", ".join(self.cfg.get("to_addrs", []))
            msg["Subject"] = f"[ServiceWatch] {subject}"
            msg.set_content(body)

            # 15s timeout prevents a hung server from leaking threads forever.
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                if use_tls:
                    smtp.starttls()          # Upgrade the plain connection.
                if user and password:
                    smtp.login(user, password)
                smtp.send_message(msg)

            log.info("Notification sent: %s", subject)
        except Exception:  # noqa: BLE001 - a mail failure must never crash the app
            log.exception("Failed to send notification: %s", subject)


# End of File: notify.py