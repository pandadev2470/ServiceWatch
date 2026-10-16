# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository, plus a log
of the code review performed on 2026-10-16.

## Project overview

Service Watch is a Flask + Waitress dashboard that polls Windows services via
pywin32, auto-restarts stopped ones (with cooldown / attempt cap / stability
reset), emails alerts over SMTP, and keeps uptime history in memory plus an
append-only JSONL file.

| File | Role |
| --- | --- |
| `app.py` | Flask app factory, routes, request guards, startup |
| `monitor.py` | Backends (`WindowsBackend`, `MockBackend`) and the `Monitor` poll/restart loop |
| `history.py` | In-memory deque + JSONL history, pruning, chart/uptime queries |
| `notify.py` | SMTP notifications on a background thread with per-(kind, service) cooldown |
| `templates/index.html` | Single-file dashboard (vanilla JS, no CDN) |
| `config.json` | Watch list and settings |

Run: `uv run python app.py` from an elevated terminal. Demo mode:
`SERVICEWATCH_MOCK=1`. Note that importing `app` starts the monitor thread
and writes to `servicewatch-history.jsonl` / `servicewatch.log`. Point
`history_file` at a temp path when experimenting.

Conventions: every code block changed in the review is tagged with a
`[Claude review 2026-10-16]` comment. Search for that string to find them all.

---

## Review log: 2026-10-16

### Security fixes

1. **SMTP STARTTLS did not verify certificates** (`notify.py`, `_send`)
   `smtp.starttls()` with no context uses an unverified SSL context, so a
   network attacker could intercept the connection and capture the SMTP password.
   Now uses `ssl.create_default_context()` (cert + hostname verification).
   A warning is also logged if credentials are sent with `use_tls: false`.

2. **DNS-rebinding exposure** (`app.py`, `check_host_header`)
   The dashboard has no authentication and relied only on the `X-ServiceWatch`
   header. A malicious site can rebind its domain to `127.0.0.1`. The browser
   then treats requests as same-origin, so the header check no longer protects
   anything and the attacker's page could start services. Added a Host-header
   allowlist:
   - Default when bound to loopback: `127.0.0.1`, `localhost`, `::1`.
   - New optional config key `allowed_hosts` (list) for other bindings.
   - Disallowed hosts get `403 Host not allowed`.

3. **Startup warning when bound to a non-loopback address** (`app.py`)
   Logs a warning that the dashboard is unauthenticated, plus a second warning
   if `allowed_hosts` is not set (the Host check is then disabled).

4. **Clickjacking / hardening headers** (`app.py`, `security_headers`)
   Added `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`,
   `X-Content-Type-Options: nosniff`, and `Referrer-Policy: no-referrer`.

5. **CSRF guard covered POST only** (`app.py`, `require_header_for_writes`)
   It now covers every method except GET/HEAD/OPTIONS, so future
   PUT/PATCH/DELETE routes are protected automatically.

### Bug fixes

6. **Monitor never ran under `waitress-serve app:app`** (`app.py`)
   `monitor.start()` was only called under `if __name__ == "__main__"`, so the
   documented WSGI entry point served a dashboard that never polled or
   restarted anything. It now starts at module import (the call is idempotent).

7. **History pruning destroyed data on disk** (`history.py`, `prune`)
   `prune()` rewrote the JSONL from the in-memory deques. Those are capped at
   20,000 samples per service (about 55 h at 10 s), so every hourly prune
   deleted everything older than about 55 h, which defeated
   `history_retention_days` (30). It also replaced the real `state` with
   running/stopped. It now stream-filters the file itself by timestamp and
   drops malformed lines.

8. **History persistence could silently stop** (`history.py`, `prune`)
   If the temp-file replace failed (e.g. the file was locked by an editor or AV
   scanner on Windows), `_fh` stayed `None` until restart and no further samples
   were written. The append handle is now reopened in a `finally` block.

9. **"Recovered" email missed on the normal restart path** (`monitor.py`, `check`)
   The recovery alert required `previous in {"stopped", "error"}`, but after
   `StartService` the usual sequence is stopped → starting → running, so no
   email was sent. Added a `Watched.alerted_down` flag that is set when a down
   alert fires and cleared, with a "recovered" email, on the next running state.

10. **`/api/history` returned 500 on bad input** (`app.py`)
    `int(request.args.get(...))` raised `ValueError` for e.g. `?hours=abc`. It now uses
    `request.args.get(..., type=int)`, which falls back to the default.

11. **Prune cadence assumed a 10 s interval** (`monitor.py`)
    The hard-coded 360 ticks has been replaced with `3600 // interval`.

12. **Relative `SERVICEWATCH_CONFIG` resolved against the CWD** (`app.py`)
    `.env` ships `SERVICEWATCH_CONFIG=config.json`. Launched from another
    directory (e.g. Task Scheduler without "Start in"), the config was not found.
    Relative paths now resolve against the project folder.

13. **Uptime bars vanished after clicking a button** (`templates/index.html`, `act`)
    `render()` was called without history. It now passes `cachedHistory`.

14. **Overlapping dashboard refreshes** (`templates/index.html`, `refresh`)
    A slow server let the 3 s `setInterval` stack requests that could finish out
    of order. Added an in-flight guard.

### Performance

15. **`History.series` was O(buckets × samples)** (`history.py`)
    It is now a single pass that computes each sample's bucket index directly.

### Robustness / docs

16. `notify.py`: logs a startup warning if email is enabled but `smtp_host` or
    `to_addrs` is missing.
17. `monitor.py`: corrected a misleading comment claiming history/notify were
    "imported lazily".
18. `app.py` `/api/uptime`: documented that 7d/30d figures only cover the
    in-memory window (about 55 h at a 10 s interval).
19. `README.md`:
    - Fixed the contact email typo (`oguejoforc` → `oguejioforc`).
    - Corrected the troubleshooting row that claimed a pywin32 import failure
      shows the demo banner. The app actually fails at startup.
    - Documented `allowed_hosts` and the new `403 Host not allowed` response.

### Verified

Smoke-tested in demo mode with the project venv:
- the index serves the security headers
- a bad Host header gets 403 and `[::1]:8080` is accepted
- junk query params get 200
- POST without the header gets 400 and with it gets 200
- the monitor thread is alive after import
- the down→recovered email pair fires
- prune keeps in-retention rows beyond the memory cap, preserves `state`, drops expired and malformed rows, and appending still works afterwards

The test run's rows were removed from `servicewatch-history.jsonl` and
`servicewatch.log`. Not tested against real Windows services or a real SMTP
server.

### Known limitations / follow-ups (not changed)

- **No authentication.** Keep `host` at `127.0.0.1`. Exposing the dashboard on
  a network needs real auth (e.g. a reverse proxy with SSO) before anything else.
- **Local `.env` is stale.** It is a copy of an older `.env.example` and says
  "Nothing here is auto-loaded", which is no longer true. It was not edited
  because it may hold a real secret. Compare it with the current `.env.example`.
- ~~**`config.json` auto-restarts critical services**~~. Resolved on
  2026-10-16, see "Config update" below.
- **Port 465 (implicit TLS) is unsupported.** Use 587 + STARTTLS.
- **History file growth.** About 12 services × 8,640 samples/day × 30 days is roughly
  3M lines (about 250 MB). Startup parses the whole file. Consider lowering
  retention or sampling only on state change if this becomes slow.
- The notifier cooldown is keyed by service **label**, so two services that
  share a label also share a cooldown.
- Manual starts increment `total_restarts`, so the "Auto-restarts" tile also
  counts manual starts.

---

## Config update: 2026-10-16 (watch list reduced to 4 services)

The user edited `config.json` to remove critical OS services from the watch
list. Claude reviewed and verified the change.

**Removed (8):** `Dnscache` (DNS Client), `Dhcp` (DHCP Client),
`LanmanServer` (Server/SMB), `LanmanWorkstation` (Workstation/SMB),
`EventLog` (Windows Event Log), `Winmgmt` (WMI), `TermService`
(Remote Desktop Services), `AudioSrv` (Windows Audio).

**Now monitored (4):**

| Service | Label | auto_restart | Local StartType at review |
| --- | --- | --- | --- |
| `Spooler` | Print Spooler | true | Automatic |
| `W32Time` | Windows Time | true | Manual (trigger-start) |
| `wuauserv` | Windows Update | false | Manual (trigger-start) |
| `Schedule` | Task Scheduler | true | Automatic |

All other settings were unchanged.

**Verification performed:**
- `config.json` parses as valid JSON. There are 4 services and no duplicate names.
- `Get-Service` confirms all 4 exist on this machine and were Running.
- The app loaded the new config with the real `WindowsBackend` (not demo
  mode). `/api/status` reported all 4 as `running` with no errors, and
  `/api/history` returned series for exactly these 4. The test used a scratch
  history file and suppressed file logging, so `servicewatch-history.jsonl` and
  `servicewatch.log` were not touched. Only read-only status queries were made.
  No service was started or stopped.

**Notes:**
- `W32Time` is a *trigger-start* service. On machines that are not
  domain-joined, Windows may stop it by itself when idle, which would cause
  "stopped" alerts and auto-restarts. If you see it flapping, set its
  `auto_restart` to `false` or drop it.
- `wuauserv` is also trigger-start and often stops by design. Keeping
  `auto_restart: false` on it (as configured) is correct.
- `Schedule` (Task Scheduler) cannot normally be stopped or started by
  administrators, so an auto-restart attempt would likely fail with
  "Access is denied". That is harmless, but don't rely on Service Watch to
  recover it.
- Existing rows for the 8 removed services remain in
  `servicewatch-history.jsonl`. They are loaded into memory at startup but never
  displayed (the API only iterates watched services), and they age out after
  `history_retention_days` (30). To clear them now, stop the app and delete the
  file.

---

## Config update 2: 2026-10-16 (replaced with 5 non-critical services)

At the user's request, Claude removed the 4 services from the previous update
and replaced them with 5 non-critical services. The notes on `W32Time`,
`wuauserv` and `Schedule` in the section above no longer apply.

**Removed (4):** `Spooler` (Print Spooler), `W32Time` (Windows Time),
`wuauserv` (Windows Update), `Schedule` (Task Scheduler).

**Now monitored (5):**

| Service | Label | auto_restart | What it does / impact if down |
| --- | --- | --- | --- |
| `Themes` | Themes | true | Visual styles. When down, the UI falls back to the classic look. |
| `WSearch` | Windows Search | true | File/Start-menu indexing. When down, searches are slower. |
| `SysMain` | SysMain (Superfetch) | true | App preloading / memory optimization. Performance only. |
| `TrkWks` | Distributed Link Tracking Client | true | Keeps shortcuts valid when NTFS files move. |
| `stisvc` | Windows Image Acquisition (WIA) | true | Scanner/camera image acquisition. |

**Selection criteria:**
- The OS, networking, logging, management (WMI), and remote access do not
  depend on any of them, so they are safe to stop and restart.
- They are Automatic-start services that should stay running, so a "stopped"
  state is a real event, not a trigger-start service going idle by design. This
  avoids the false alerts noted for `W32Time` / `wuauserv` above.
- `Get-Service` shows they exist and were Running on this machine at review time.

The candidates were rejected for these reasons:
- Running but more system-relevant: `FontCache`, `CDPSvc`, `DiagTrack`, `PcaSvc`.
- Trigger-start or manual: `lfsvc`, `SSDPSRV`, `WerSvc`, `Fax`.
- Already stopped here: `MapsBroker`, `WbioSrvc`.

All other settings were unchanged.

**Verification performed:**
- `config.json` parses as valid JSON. There are 5 services and no duplicate names.
- The app loaded the new config with the real `WindowsBackend`. `/api/status`
  reported all 5 as `running` with no errors, and `/api/history` returned series
  for exactly these 5. The test used a scratch history file with file logging
  suppressed, and only read-only queries were made. No service was started or
  stopped.

**Notes:**
- Some hardened or VDI images disable `SysMain` or `WSearch` on purpose. On
  such a machine, a disabled service will show "Stopped" and auto-restart will
  fail. Remove it from the list there.
- `stisvc` may be absent or unneeded on servers without imaging devices. Swap
  it out if so.
- History rows for previously watched services remain in
  `servicewatch-history.jsonl` until they age out (30 days). Delete the file
  while the app is stopped to clear them.

---

## Application & security analysis: 2026-10-16 (current state)

Scope: all tracked source (`app.py`, `monitor.py`, `history.py`, `notify.py`,
`templates/index.html`), configuration (`config.json`, `.env.example`,
`pyproject.toml`, `requirements.txt`, `uv.lock`) and docs, *after* the fixes
and config changes logged above. Assumed deployment: a single Windows host,
dashboard bound to `127.0.0.1`, run elevated by an administrator.

### Scores

| | Score | Rating |
| --- | --- | --- |
| **Security** | **61 / 100** | Acceptable for a single-admin, loopback-only tool. **Not acceptable if exposed on a network.** |
| **Overall application** | **63 / 100** | Clean, well-documented, functional. Held back by no tests, no auth, and ops gaps. |

These scores depend on context. Binding to a non-loopback address without
the S-1 / S-2 improvements would drop security to about **35 / 100**.
Implementing the Priority 1 items below would raise it to about **80 / 100**.

#### Security score breakdown

| Category | Weight | Score /10 | Points | Rationale |
| --- | --- | --- | --- | --- |
| Network exposure | 15 | 8 | 12.0 | Loopback by default. Host allowlist blocks DNS rebinding. A warning is logged on non-loopback binds. |
| Authentication / authorization | 20 | 3 | 6.0 | None. Any process or user on the host can call the API. |
| Web protections (CSRF / XSS / clickjacking) | 15 | 8.5 | 12.75 | Custom-header CSRF guard. DOM built with `textContent` only (no `innerHTML`). Frame-ancestors / XFO / nosniff are set. No `script-src` CSP because the script is inline. |
| Secrets management | 10 | 6 | 6.0 | Password lives in an env var or `.env` (plaintext on disk). STARTTLS is now verified. A plaintext `password` config fallback still exists. |
| Least privilege | 15 | 4 | 6.0 | The whole web server runs as full Administrator just to start a few services. |
| Input validation | 10 | 8 | 8.0 | Service name must be on the watch list. Query params are typed and clamped. Config is not schema-validated. |
| Dependencies / supply chain | 10 | 7 | 7.0 | `uv.lock` pins current versions (Flask 3.1.3, Werkzeug 3.1.9, Waitress 3.0.2, Jinja2 3.1.6, pywin32 312). No known advisories at review time, but this was an offline review, not a live CVE scan. `requirements.txt` uses unpinned `>=`. No automated audit. |
| Audit logging | 5 | 6 | 3.0 | Manual starts are logged, but not *who* or *from where*. Logs are not access-controlled. |
| **Total** | **100** | | **60.75 ≈ 61** | |

#### Application score breakdown

| Category | Weight | Score /10 | Points | Rationale |
| --- | --- | --- | --- | --- |
| Functionality | 15 | 8 | 12.0 | Monitoring, auto-restart with cooldown, attempt cap and stability reset, email alerts, uptime bars, demo mode. |
| Reliability / correctness | 20 | 7 | 14.0 | Loop is exception-safe. Earlier data-loss and alerting bugs are fixed. No graceful shutdown. Single-instance assumption is unenforced. |
| Code quality / maintainability | 15 | 8 | 12.0 | Small, readable modules with a clear backend abstraction. Well commented, with type hints. |
| Testing | 15 | 1 | 1.5 | No automated tests or CI. Verification so far was manual smoke testing. |
| Performance / scalability | 10 | 6 | 6.0 | Fine at about 5 services. Startup parses the whole JSONL. A sample is written every poll even with no state change. The file can reach hundreds of MB at scale. |
| Observability | 10 | 6 | 6.0 | Rotating log and in-UI event feed. No health endpoint. The event feed is lost on restart. |
| Documentation | 10 | 8.5 | 8.5 | Thorough README, inline troubleshooting checklists, and this file. |
| Deployability / operations | 5 | 5 | 2.5 | Task Scheduler instructions only. It is not a real Windows service and has no installer. |
| **Total** | **100** | | **62.5 ≈ 63** | |

### Security findings (current state)

| ID | Severity | Finding | Location |
| --- | --- | --- | --- |
| S-1 | **High** (Medium on a single-user workstation) | **No authentication.** `127.0.0.1` is not a user boundary. On a multi-user host (RDS, jump box, shared server), any logged-in user or local malware can call `POST /api/services/<name>/start` and `/api/check`, which execute with the app's Administrator rights. Impact is limited to *starting* watched services, but it is still a privilege boundary crossing. | `app.py` routes |
| S-2 | **Medium** | **Over-privileged process.** The Flask/Waitress server, the template engine and the SMTP client all run as full Administrator. A future bug in any of them (or a dependency) runs elevated. Only `SERVICE_START` + `SERVICE_QUERY_STATUS` on 5 specific services is actually needed. | deployment |
| S-3 | **Medium** | **Plaintext secret at rest.** The SMTP password sits in `.env` (or a user env var) readable by anyone with access to the profile or project folder. `notify.py` still accepts a plaintext `password` key in `config.json`. | `.env`, `notify.py` `_password` |
| S-4 | Low | **No audit trail of who acted.** Manual starts log "Start requested manually." with no client IP or user, so they can't be attributed. | `monitor.py` `start_now` |
| S-5 | Low | **Internal error text returned to client.** `str(exc)` from pywin32 goes to the UI. This is mostly harmless SCM text, but it is information disclosure if exposed. | `app.py` `start_service` |
| S-6 | Low | **No `script-src` CSP.** XSS is currently mitigated by safe DOM construction, but the inline `<script>`/`<style>` prevent a strict CSP as a second line of defence. | `templates/index.html` |
| S-7 | Low | **Unpinned `requirements.txt`.** `pip install -r requirements.txt` takes whatever is newest, bypassing `uv.lock`. There is no `pip-audit` / Dependabot. | `requirements.txt` |
| S-8 | Low | **Log and history files are not ACL-restricted.** They inherit the project folder's ACLs (currently under a user's Downloads). | deployment |
| S-9 | Info | **No rate limiting.** `POST /api/check` triggers SCM queries for every service. This is negligible at 5 services. | `app.py` `check_all` |
| S-10 | Info | **Stale local `.env`** with outdated guidance (noted earlier). It may contain a real secret. | `.env` |

**Strengths:**
- Safe defaults: loopback bind, email disabled, demo routes only in mock mode.
- No `innerHTML` and no `eval`. All HTML comes from Flask/Jinja auto-escaping or `textContent`.
- CSRF guard, Host allowlist, anti-framing headers.
- Verified TLS for SMTP. No shell execution, no SQL, no deserialization of untrusted data.
- Secrets are kept out of `config.json` and git (`.gitignore` covers `.env`).

### Application findings (current state)

| ID | Area | Finding |
| --- | --- | --- |
| A-1 | Testing | There are no unit or integration tests. `MockBackend` makes them easy to write: restart policy, alert transitions, history prune and series bucketing, request guards. |
| A-2 | Reliability | **No graceful shutdown.** `monitor.stop()` and `history.close()` are never called on exit (no `atexit` or signal handler). With a flush on every write the data risk is low, but shutdown is abrupt. |
| A-3 | Reliability | **Single-instance assumption.** Two instances (e.g. Task Scheduler plus a manual run) would both poll, both restart, and interleave writes to the same JSONL. "Address already in use" only protects the port if both use the same one. |
| A-4 | Reliability | **No config validation.** A missing `services[].name`, wrong types, or `max_restart_attempts: 0` fail with a raw `KeyError` or odd behaviour. |
| A-5 | Performance | **Writes a sample on every poll** (5 services × 8,640/day). Recording only state changes plus a periodic heartbeat would cut file size by about 99%. Startup currently parses the whole file. |
| A-6 | Correctness | **Long-window uptime is approximate.** The in-memory cap (20k samples per service, about 55 h) means `/api/uptime` 7d/30d and `/api/history?hours>55` only reflect the last ~55 h. |
| A-7 | Alerting | `not_found` (service uninstalled or renamed) produces a UI warning but **no email**. `gave_up` is emailed only once. There is no repeat or reminder. |
| A-8 | Alerting | The notifier cooldown is keyed by label, not service name. Each email spawns an unbounded thread (fine at this scale). Port 465 (implicit TLS) is unsupported. |
| A-9 | Observability | There is no `/healthz` endpoint and no "monitor loop last ran at" heartbeat, so a stuck loop is invisible to external monitoring. The event feed is in-memory only. |
| A-10 | UX | The "Auto-restarts" tile also counts manual starts. Only the 24 h window is shown even though the API supports others. |
| A-11 | Operations | It runs as a console app under Task Scheduler, not as a Windows service. Nothing restarts Service Watch itself if it crashes. The project lives in `Downloads`. |

### Suggested improvements (prioritized)

Effort: S = under 2 h, M = half day to 1 day, L = several days.

**Priority 1: do before any shared or multi-user deployment**

| # | Improvement | Addresses | Effort |
| --- | --- | --- | --- |
| 1 | **Add authentication to write endpoints.** The simplest is a random bearer token generated at first run, stored DPAPI-protected, and entered once in the UI (kept in `sessionStorage`). Better on a domain: Windows Integrated Auth via a reverse proxy (e.g. IIS) in front of Waitress, restricted to an AD group. | S-1 | M |
| 2 | **Drop admin rights.** Run under a dedicated low-privilege account (or a virtual service account). Grant only `SERVICE_START` / `SERVICE_QUERY_STATUS` on the 5 watched services via `sc sdset` (or GPO). Remove "Run with highest privileges". | S-2 | M |
| 3 | **Move the SMTP secret to Windows Credential Manager** (`keyring` package, DPAPI-backed) and remove the plaintext `password` fallback from `notify.py`. Delete or refresh the stale `.env`. | S-3, S-10 | S |
| 4 | **Add a test suite** (pytest + Flask test client + `MockBackend`), covering restart policy, alert transitions, prune, host and CSRF guards. Run it in CI (e.g. GitHub Actions on `windows-latest`). | A-1 | M |

**Priority 2: hardening and reliability**

| # | Improvement | Addresses | Effort |
| --- | --- | --- | --- |
| 5 | Log the client address (and user, once auth exists) on every write request. Return a generic error to the client and keep the details in the log. | S-4, S-5 | S |
| 6 | Move the JS and CSS to `static/` files and add a strict CSP (`default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'`). | S-6 | S |
| 7 | Pin `requirements.txt` (`uv export --format requirements-txt`) and add `pip-audit` (or Dependabot) to CI. | S-7 | S |
| 8 | Validate `config.json` at startup: required keys, types, ranges, unique names. Fail fast with a clear message. | A-4 | S |
| 9 | Graceful shutdown: `atexit` / `signal` handler that calls `monitor.stop()`, joins the thread, and calls `history.close()`. | A-2 | S |
| 10 | Single-instance lock: a named mutex (`win32event.CreateMutex`) or a lock file held for the process lifetime. | A-3 | S |
| 11 | Install as a real Windows service (NSSM, WinSW, or a pywin32 `ServiceFramework`) with recovery actions set to "Restart". Move it out of `Downloads` to e.g. `C:\Program Files\ServiceWatch` with locked-down ACLs. | A-11, S-8 | M |

**Priority 3: functionality and efficiency**

| # | Improvement | Addresses | Effort |
| --- | --- | --- | --- |
| 12 | Record history on **state change plus an hourly heartbeat** instead of every poll. Compute uptime from durations. This shrinks the file by about 99% and makes 7d/30d uptime exact. | A-5, A-6 | M |
| 13 | Add `/healthz` (returns the last loop tick time and fails if it is stale). Persist the event feed (last N events) to disk. | A-9 | S |
| 14 | Alert on `not_found`. Send an optional reminder email while `gave_up` persists. Key the cooldown by service name. Support implicit TLS (`SMTP_SSL`) for port 465. | A-7, A-8 | S |
| 15 | Split "Auto-restarts" and "Manual starts" counters. Add a 24h / 7d / 30d selector to the uptime bars. | A-10 | S |
| 16 | Optional: Teams/Slack webhook notifier alongside SMTP. Watch multiple hosts via remote SCM. | feature | M–L |

**Expected effect:**
- Items 1–4: security about 80 / 100, application about 72 / 100.
- Adding items 5–11: security about 88 / 100, application about 80 / 100.
- Adding items 12–15: application about 85 / 100.

### Method and limits of this analysis

This was a manual review of every source file plus runtime smoke tests in demo
mode and against real services (read-only). The dependency assessment
used versions pinned in `uv.lock`. **No live CVE database scan was run**, so
run `pip-audit` to confirm. Not tested: real SMTP delivery, actual
restart of a stopped service, behaviour under Task Scheduler, and
load or long-duration runs.
