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
    and is used automatically when smtp_port is 465 (or "use_ssl": true).
  * Only some recipients get mail? Look for "rejected recipients" in the log;
    the server refused those addresses but delivered to the rest.
  * Check servicewatch.log for "Notification sent:" or
    "Failed to send notification:" lines.
"""
from __future__ import annotations

import logging
import os
import re
import smtplib
import socket
import ssl  # [Claude review 2026-10-16] for a verifying STARTTLS context
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

# Reuse the app-wide logger so notifications show up alongside monitor events.
log = logging.getLogger("servicewatch")

# [Claude review 2026-10-19] Delivery retries: a send that fails (server
# unreachable at boot, transient 4xx) is retried after these delays (seconds).
RETRY_DELAYS = (15, 60)


def _parse_addrs(value) -> list[str]:
    """[Claude review 2026-10-19] Normalize to_addrs into a de-duplicated list.

    Accepts a JSON list (preferred) or a single string separated by commas or
    semicolons. A plain string used to be joined character by character
    ("a, @, x, ...") by ", ".join(), silently breaking every email.
    """
    if isinstance(value, str):
        value = re.split(r"[;,]", value)
    out: list[str] = []
    seen: set[str] = set()
    for addr in value or []:
        addr = str(addr).strip()
        if addr and addr.lower() not in seen:
            seen.add(addr.lower())
            out.append(addr)
    return out


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

        # [Claude review 2026-10-19] Recipients, normalized once. Any number
        # of inboxes is supported; one message is sent with all of them in To.
        self.to_addrs = _parse_addrs(self.cfg.get("to_addrs", []))
        for addr in self.to_addrs:
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", addr):
                log.warning("Email: '%s' in to_addrs does not look like an address.", addr)

        # Machine name for subjects/bodies so recipients know which device
        # raised the alert. [Claude review 2026-10-19]
        self.hostname = socket.gethostname()

        # Track the last time we sent each (kind, service) pair.
        self._last_sent: dict[tuple[str, str], float] = {}
        # Protects _last_sent from concurrent access by the monitor thread.
        self._lock = threading.Lock()

        # [Claude review 2026-10-16] Fail loudly at startup on an incomplete
        # config instead of only discovering it when the first alert fails.
        if self.enabled and not (self.cfg.get("smtp_host") and self.to_addrs):
            log.warning("Email enabled but smtp_host or to_addrs is missing; "
                        "notifications will fail.")
        # [Claude review 2026-10-19] Same for a missing password env var.
        if (self.enabled and self.cfg.get("username") and self.cfg.get("password_env")
                and not os.environ.get(self.cfg["password_env"])):
            log.warning("Email enabled but environment variable %s is not set; "
                        "SMTP login will be skipped.", self.cfg["password_env"])
        if self.enabled:
            log.info("Email alerts enabled for %d recipient(s).", len(self.to_addrs))

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
    def notify(self, kind: str, service: str, subject: str, body: str) -> bool:
        """Queue a notification if it passes the enabled + cooldown checks.

        Returns immediately. The actual SMTP conversation happens on a
        daemon thread so a slow mail server can't stall the caller.

        [Claude review 2026-10-19] Returns False only when the alert was
        deferred by the cooldown, so the caller can retry it later instead of
        losing it. Returns True when it was queued or intentionally skipped
        (email disabled / kind not in notify_on). `service` is the service
        short name; the cooldown used to be keyed by label, so two services
        sharing a label also shared a cooldown.
        """
        # Feature disabled or event kind not requested -> do nothing.
        if not self.enabled or kind not in self.events_wanted:
            return True

        # Cooldown key is per (kind, service) so a "down" and a "recovered"
        # for the same service don't block each other.
        key = (kind, service)
        now = time.time()
        with self._lock:
            if now - self._last_sent.get(key, 0) < self.cooldown:
                return False  # Still in cooldown window; caller retries.
            self._last_sent[key] = now

        # Fire and forget. If it fails, _send logs the traceback.
        threading.Thread(
            target=self._send, args=(subject, body), name="notify", daemon=True
        ).start()
        return True

    # ------------------------------------------------------------------ #
    # Actual SMTP send
    # ------------------------------------------------------------------ #
    def _send(self, subject: str, body: str) -> None:
        """Send with retries. Any failure is logged, never raised.

        [Claude review 2026-10-19] Previously a single failed attempt (e.g.
        network not up yet after a reboot) silently dropped the alert.
        """
        for attempt, delay in enumerate((0, *RETRY_DELAYS), start=1):
            if delay:
                time.sleep(delay)
            try:
                self._send_once(subject, body)
                return
            except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused,
                    smtplib.SMTPSenderRefused, KeyError, ValueError):
                # Config problems: retrying won't help.
                log.exception("Failed to send notification: %s", subject)
                return
            except Exception:  # noqa: BLE001 - a mail failure must never crash the app
                log.exception("Failed to send notification (attempt %d of %d): %s",
                              attempt, 1 + len(RETRY_DELAYS), subject)

    def _send_once(self, subject: str, body: str) -> None:
        """Do the SMTP dance once. Raises on failure."""
        host = self.cfg["smtp_host"]
        port = int(self.cfg.get("smtp_port", 587))
        # [Claude review 2026-10-19] Implicit TLS for port 465 (or use_ssl).
        use_ssl = bool(self.cfg.get("use_ssl", port == 465))
        use_tls = bool(self.cfg.get("use_tls", True)) and not use_ssl
        user = self.cfg.get("username")
        password = self._password()
        if not self.to_addrs:
            raise ValueError("email.to_addrs is empty")

        # Build the message with the stdlib API (no manual header munging).
        msg = EmailMessage()
        msg["From"] = self.cfg.get("from_addr") or user
        msg["To"] = ", ".join(self.to_addrs)
        # [Claude review 2026-10-19] Host name in the subject, plus Date and
        # Message-ID headers (some relays score mail without them as spam).
        msg["Subject"] = f"[ServiceWatch] {self.hostname}: {subject}"
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.hostname)
        msg.set_content(
            f"{body}\n\n"
            f"Machine: {self.hostname}\n"
            f"Time:    {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}\n"
            f"\n-- \nSent by Service Watch on {self.hostname}.\n"
        )

        context = ssl.create_default_context()
        # 15s timeout prevents a hung server from leaking threads forever.
        if use_ssl:
            smtp_cm = smtplib.SMTP_SSL(host, port, timeout=15, context=context)
        else:
            smtp_cm = smtplib.SMTP(host, port, timeout=15)
        with smtp_cm as smtp:
            if use_tls:
                # [Claude review 2026-10-16] Pass a verifying SSL context.
                # starttls() with no context does NOT check the server
                # certificate or hostname, so a network attacker could
                # intercept the connection and capture the SMTP password.
                smtp.starttls(context=context)
            if user and password:
                if not (use_tls or use_ssl):
                    # [Claude review 2026-10-16] Make plaintext auth visible.
                    log.warning("Sending SMTP credentials without TLS (use_tls is false).")
                smtp.login(user, password)
            # [Claude review 2026-10-19] send_message only raises if EVERY
            # recipient is refused; partial refusals come back as a dict and
            # used to be ignored, so one bad address went unnoticed.
            refused = smtp.send_message(msg, to_addrs=self.to_addrs)

        if refused:
            log.warning("Notification sent, but the server rejected recipients %s: %s",
                        ", ".join(refused), subject)
        log.info("Notification sent to %d recipient(s): %s",
                 len(self.to_addrs) - len(refused), subject)


# End of File: notify.py