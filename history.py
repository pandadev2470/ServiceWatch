# FILE: history.py
# ServiceWatch_V1
# Contact: admin@thepandadev.com
"""History storage without a database.

Two cooperating stores:

  1. An in-memory deque per service — fast, used by the live UI.
  2. An append-only JSONL file — survives restarts, easy to grep/rotate.

Both are trimmed to `retention_days`. There is no schema, no locking drama,
no C extension, and no .db file to corrupt. If the JSONL is deleted, charts
simply start empty.

Troubleshooting checklist:
  * Charts empty after restart? On startup the JSONL is replayed into the
    in-memory deques, so the last `retention_days` of samples should reappear.
    If not, check that the file is readable and not held open by another
    process.
  * "history file not writable" in the log? Check folder permissions.
  * Want to reset history? Stop the app and delete servicewatch-history.jsonl.
  * Want to keep it longer? Raise "history_retention_days" in config.json.
"""
from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("servicewatch")


class History:
    """In-memory + JSONL backed sample store.

    File format: one JSON object per line, newest last.
        {"ts": "2026-10-16T14:22:01+00:00", "service": "Spooler", "state": "running", "up": 1}

    The in-memory deque holds the same tuples so reads are O(1)-ish.
    """

    def __init__(self, file_path: Path, retention_days: int = 30,
                 max_samples_per_service: int = 20_000) -> None:
        self.path = Path(file_path)
        self.retention_days = int(retention_days)
        # Cap per service so a very old file can't blow out RAM. 20k samples
        # at 10s interval is ~55 hours of data. Bump if you widen the window.
        self.max_samples = int(max_samples_per_service)
        self._lock = threading.Lock()
        # service name -> deque[(datetime_utc, up_bool)]
        self._samples: dict[str, deque[tuple[datetime, int]]] = defaultdict(
            lambda: deque(maxlen=self.max_samples)
        )

        # Replay any existing file so charts survive a restart.
        self._load_from_disk()

        # Open the file once in append mode. Text mode so we get \n handling.
        try:
            self._fh = self.path.open("a", encoding="utf-8")
        except OSError as exc:
            log.warning("History file not writable (%s); running in-memory only.", exc)
            self._fh = None

    # ------------------------------------------------------------------ #
    # Loading / persistence
    # ------------------------------------------------------------------ #
    def _load_from_disk(self) -> None:
        """Read existing JSONL into memory, ignoring anything malformed."""
        if not self.path.exists():
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        loaded = 0
        skipped = 0
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        ts = datetime.fromisoformat(row["ts"])
                        # Ignore anything past retention so startup is fast.
                        if ts < cutoff:
                            continue
                        self._samples[row["service"]].append((ts, int(row["up"])))
                        loaded += 1
                    except Exception:  # noqa: BLE001 - skip bad lines, keep going
                        skipped += 1
        except OSError as exc:
            log.warning("Could not read history file: %s", exc)
            return
        if loaded or skipped:
            log.info("History loaded: %d samples (%d lines skipped).", loaded, skipped)

    def _append_line(self, row: dict) -> None:
        """Write one sample to the JSONL file, silently on failure."""
        if self._fh is None:
            return
        try:
            self._fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            # Flush often enough that a crash doesn't lose the last minute.
            # Full fsync would be overkill for this workload.
            self._fh.flush()
        except OSError as exc:
            log.warning("Failed to append history: %s", exc)

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def record(self, service: str, state: str) -> None:
        """Record one sample. Called from Monitor.check() on every poll."""
        ts = datetime.now(timezone.utc)
        up = 1 if state == "running" else 0
        with self._lock:
            self._samples[service].append((ts, up))
            self._append_line({
                "ts": ts.isoformat(timespec="seconds"),
                "service": service,
                "state": state,
                "up": up,
            })

    def prune(self) -> None:
        """Drop in-memory samples past retention, and rewrite the JSONL.

        Called roughly hourly by the monitor. Rewriting is cheap for the
        scale of this app (a few dozen services, ≤ 30 days of samples).
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        with self._lock:
            # Trim in-memory deques.
            for name, dq in self._samples.items():
                while dq and dq[0][0] < cutoff:
                    dq.popleft()

            # Rewrite the file without pruned rows. Do the rewrite via a
            # temp file + replace so a crash mid-write can't corrupt history.
            if self._fh is None:
                # [Claude review 2026-10-16] No writable handle means the file
                # was never writable; don't attempt a rewrite.
                return
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

            # [Claude review 2026-10-16] Stream-filter the FILE rather than
            # dumping the in-memory deques. The deques are capped at
            # max_samples (~55h at 10s), so the old rewrite silently threw
            # away everything older than that on every prune, defeating
            # history_retention_days. It also lost the real "state" value.
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                with self.path.open("r", encoding="utf-8") as src, \
                        tmp.open("w", encoding="utf-8") as dst:
                    for line in src:
                        try:
                            if datetime.fromisoformat(json.loads(line)["ts"]) < cutoff:
                                continue
                        except Exception:  # noqa: BLE001 - drop malformed lines
                            continue
                        dst.write(line if line.endswith("\n") else line + "\n")
                tmp.replace(self.path)
            except OSError as exc:
                log.warning("Failed to prune history file: %s", exc)
            finally:
                # [Claude review 2026-10-16] Always reopen the append handle.
                # Previously a failed replace (e.g. file locked by an editor or
                # AV scanner on Windows) left _fh = None forever, silently
                # stopping all history persistence until restart.
                try:
                    self._fh = self.path.open("a", encoding="utf-8")
                except OSError as exc:
                    log.warning("History file not writable (%s); running in-memory only.", exc)
                    self._fh = None

    # ------------------------------------------------------------------ #
    # Reads (used by /api/history and /api/uptime)
    # ------------------------------------------------------------------ #
    def series(self, service: str, hours: int, buckets: int = 48) -> list[dict]:
        """Return `buckets` evenly-spaced uptime fractions over the last `hours`.

        Each element is {"t": ISO-start-of-bucket, "up": 0.0..1.0 or None}.
        None means "no samples in this bucket" so the UI can render a gap
        instead of pretending the service was down.
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        step = (end - start) / buckets
        with self._lock:
            samples = list(self._samples.get(service, ()))
        # Filter once, then bucket in Python.
        samples = [(t, u) for (t, u) in samples if t >= start]

        # [Claude review 2026-10-16] Single pass: compute each sample's bucket
        # index directly. The old nested loop was O(buckets * samples) and ran
        # for every service on every dashboard poll.
        ups = [0] * buckets
        totals = [0] * buckets
        for t, up in samples:
            i = int((t - start) / step)
            if 0 <= i < buckets:
                totals[i] += 1
                ups[i] += up

        out = []
        for i in range(buckets):
            out.append({
                "t": (start + step * i).isoformat(timespec="seconds"),
                "up": (ups[i] / totals[i]) if totals[i] else None,
            })
        return out

    def uptime_pct(self, service: str, hours: int) -> float | None:
        """Percentage of samples that were 'running' in the last N hours.

        Returns None if there is no data, so the UI can show '—' instead of
        a misleading 0%.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        with self._lock:
            samples = [u for (t, u) in self._samples.get(service, ()) if t >= cutoff]
        if not samples:
            return None
        return 100.0 * sum(samples) / len(samples)

    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        """Flush and close the JSONL handle. Optional; called on shutdown."""
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None


# End of File: history.py