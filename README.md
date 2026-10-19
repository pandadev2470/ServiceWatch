<!--
FILE: README.md
Project: ServiceWatch_V1
Contact: oguejioforc@concordnc.gov
-->

# Service Watch

A Python web application that monitors Windows services, automatically restarts them when they stop, sends email alerts, and charts uptime over time.


## Features

- Monitor any number of Windows services using each service's short name and a friendly dashboard label.
- Automatically restart stopped services, with a cooldown, a maximum-attempt limit, and a stability period that resets the attempt count after a service runs reliably.
- Send optional email notifications when a service goes **down**, **recovers**, or the monitor **gives up** restarting it.
- View 24-hour uptime bars for each service.
- Keep history in memory and in an append-only JSONL file. **No database is required.**
- Use demo mode on macOS and Linux to explore the dashboard without Windows services.
- Write application logs to a rotating `servicewatch.log` file.

## Requirements

- Python
- [uv](https://docs.astral.sh/uv/) (recommended) or `pip`
- Windows for monitoring and restarting real Windows services

On macOS and Linux, the application uses simulated services in demo mode.

## Installation and Setup (Windows)

### 1. Create the environment and install dependencies

Open PowerShell in the project directory:

```powershell
cd ServiceWatch_V1
uv venv
.venv\Scripts\activate
uv sync
```

`uv sync` installs the dependencies declared in `pyproject.toml`, including Flask, Waitress, `python-dotenv`, and `pywin32`.

If you prefer `pip`, install the dependencies listed in `requirements.txt`.

### 2. Configure the services to monitor

Edit `config.json` and add the services you want to watch. Use each service's **short name**, not its display name.

To list running services and their names, run:

```powershell
Get-Service | Where-Object Status -eq Running | Select-Object Name, DisplayName
```

> **Caution:** Be careful when enabling automatic restarts for critical Windows services such as `Dhcp`, `Dnscache`, `EventLog`, or `Winmgmt`. Stopping or restarting these services can destabilize the operating system. Consider monitoring them with `"auto_restart": false`.

### 3. Start the application

Open an **Administrator PowerShell terminal** because starting Windows services requires elevated permissions. Run:

```powershell
uv run python app.py
```

Then open [http://127.0.0.1:8080](http://127.0.0.1:8080) in your browser.

### Demo mode

On macOS and Linux, the app starts in demo mode with simulated services, allowing you to explore the dashboard without a Windows machine.

To force demo mode on Windows, run:

```powershell
$env:SERVICEWATCH_MOCK = "1"
uv run python app.py
```

## Environment Variables

The application loads `.env` automatically at startup using `python-dotenv`.

Create a local `.env` file from the example:

```powershell
Copy-Item .env.example .env
```

Environment variables set outside `.env` take precedence over values in the file. For example, a Windows Task Scheduler task can use a system or user environment variable named `SERVICEWATCH_SMTP_PASSWORD`.

Do not commit `.env` or real credentials to source control.

## Email Notifications

Configure the `email` section in `config.json`:

```json
{
  "email": {
    "enabled": true,
    "smtp_host": "smtp.example.com",
    "smtp_port": 587,
    "use_tls": true,
    "username": "alerts@example.com",
    "password_env": "SERVICEWATCH_SMTP_PASSWORD",
    "from_addr": "alerts@example.com",
    "to_addrs": [
      "first@example.com",
      "second@example.com",
      "third@example.com",
      "fourth@example.com",
      "fifth@example.com"
    ],
    "cooldown_seconds": 300,
    "notify_on": ["down", "recovered", "gave_up"]
  }
}
```

Set the SMTP password in `.env`:

```dotenv
SERVICEWATCH_SMTP_PASSWORD=your-app-password
```

**Notes:**

- The password is read from the environment variable named by `password_env`, so it does not need to be stored in `config.json`.
- Many email providers, including Gmail and Outlook, may require an app password or provider-specific SMTP configuration.
- `to_addrs` takes any number of recipients. One message is sent with every address in `To:`. Duplicates are removed. If the server rejects some addresses, the rest still receive the message and the rejected ones are logged.
- `cooldown_seconds` applies to each event/service pair to help prevent repeated notifications when a service repeatedly changes state. An alert held back by the cooldown is **delayed, not dropped**. If the service is still down when the cooldown ends, the email is sent then. If it recovered in the meantime, nothing is sent.
- Port 465 (implicit TLS) is supported. Set `"smtp_port": 465` and it is used automatically. Port 587 uses STARTTLS.
- Subjects include the machine name, for example `[ServiceWatch] PC-NAME: Print Spooler stopped`. Failed sends are retried twice.
- A "down" email is sent when a service is stopped, its status can't be read, or it is no longer installed, including when it is already down when Service Watch starts.

## History and Uptime

Service Watch does not use a database. It uses two cooperating history stores:

- **In-memory deque per service:** Fast data storage used by the live dashboard.
- **`servicewatch-history.jsonl`:** Append-only history that persists across application restarts and can be searched or rotated manually.

Each monitoring poll writes a history line. The dashboard uses these endpoints:

- `GET /api/history?hours=24&buckets=48` — returns bucketed uptime fractions.
- `GET /api/uptime` — returns uptime percentages per service for the last 1 hour, 24 hours, 7 days, and 30 days.

Entries older than `history_retention_days` are pruned roughly hourly by the monitor loop. The JSONL file is rewritten using a temporary file and an atomic replacement.

To reset history, stop the application and delete `servicewatch-history.jsonl`.

## Branding and Appearance

The dashboard header displays `/static/logo.png`. Add your logo at that path. A transparent PNG approximately 400 pixels wide is recommended. The logo is displayed on a white background chip to keep it legible in dark mode.

Brand colors are defined near the top of `templates/index.html` in the `:root` CSS block:

| Variable | Purpose | Default |
| --- | --- | --- |
| `--brand` | red used for buttons, accents, and tile stripes | `#c8102e` |
| `--bad` | Down state; intentionally darker than the brand color | `#8a0e1f` |
| `--ok` | Running state | `#1c7a4b` |
| `--warn` | Transitional states | `#8a5a00` |

Dark-mode overrides are defined in the `@media (prefers-color-scheme: dark)` block.

## Configuration Reference

| Setting | Description |
| --- | --- |
| `host`, `port` | Address and port where the dashboard listens. Keep the host at `127.0.0.1` unless authentication and appropriate network protections are added. |
| `check_interval_seconds` | How often each service is checked. |
| `restart_cooldown_seconds` | Minimum wait between automatic restart attempts. |
| `max_restart_attempts` | Maximum restart attempts before the monitor gives up. A manual start resets the count. |
| `stable_after_seconds` | How long a service must remain running before its restart-attempt count resets. |
| `allowed_hosts` | Optional list of hostnames accepted in the HTTP `Host` header (DNS-rebinding protection). Defaults to `127.0.0.1`, `localhost`, `::1` when bound to loopback. Set it if you bind to another address, e.g. `["servicewatch.corp.local"]`. |
| `history_file` | Path to the JSONL history file. |
| `history_retention_days` | Number of days to retain history; older rows are pruned. |
| `email.*` | SMTP settings for email notifications. See [Email Notifications](#email-notifications). |
| `services[].name` | Windows service short name. |
| `services[].label` | Friendly name displayed in the dashboard. |
| `services[].auto_restart` | Set to `false` to monitor a service without automatically restarting it. |

## Run at Startup with Task Scheduler

Use the included script from an **elevated** PowerShell in the project folder:

```powershell
.\install-task.ps1 -StartNow        # install and start now
.\install-task.ps1 -Uninstall       # stop and remove
```

If script execution is blocked, run `powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -StartNow`.

The task runs as your account with highest privileges, whether or not you are logged on. No password is stored (S4U logon). Use `-RunAs SYSTEM` to run it as LocalSystem instead, but only after moving the folder somewhere only Administrators can write to, such as `C:\ServiceWatch`. It sets up:

- A **startup trigger with a 2-minute delay**, so delayed-start services aren't reported as stopped at boot.
- A **watchdog trigger every 5 minutes**. If Service Watch crashed or was killed, it is started again. If it is already running, the trigger does nothing.
- **No time limit.** Task Scheduler's default stops tasks after 3 days.
- It keeps running on battery power.

Only one instance can run at a time. A second copy, for example a manual `python app.py` while the task is running, exits with "Another Service Watch instance is already running".

To stop Service Watch for maintenance, **disable** the task first. Otherwise the watchdog restarts it within 5 minutes.

The `.env` file in the project folder is loaded at startup, so the task picks up `SERVICEWATCH_SMTP_PASSWORD` from it. You can also set it as an environment variable for the account the task runs as:

```powershell
[Environment]::SetEnvironmentVariable(
    "SERVICEWATCH_SMTP_PASSWORD",
    "your-app-password",
    "User"
)
```

Use a secure secret-management approach appropriate to your environment, and avoid placing real passwords in scripts or source control.

## Project Files

| File or path | Purpose |
| --- | --- |
| `pyproject.toml` | Project metadata and dependencies for `uv`. |
| `requirements.txt` | Dependencies for `pip` users. |
| `.env.example` | Example environment-variable configuration. |
| `.env` | Local environment variables; should not be committed. |
| `.gitignore` | Excludes `.env`, `.venv`, logs, and history from Git. |
| `app.py` | Web server and API. |
| `monitor.py` | Service monitoring and restart logic. |
| `history.py` | In-memory and JSONL history handling. |
| `notify.py` | SMTP email notifications. |
| `config.json` | Service watch list and application settings. |
| `templates/index.html` | Dashboard template. |
| `install-task.ps1` | Installs or removes the Windows scheduled task. |
| `static/logo.png` | logo displayed in the dashboard. |
| `servicewatch.log` | Rotating log file created on first run. |
| `servicewatch-history.jsonl` | Append-only history file created on first run. |

## Troubleshooting

| Symptom | Likely cause or fix |
| --- | --- |
| `Access is denied` when starting a service | Run the application from an Administrator terminal. |
| `The specified service does not exist` | Check `config.json` and use the service's short name. |
| Demo-mode banner appears on Windows | `SERVICEWATCH_MOCK=1` is set (check `.env` and the process environment). |
| `ModuleNotFoundError: pywintypes` / `win32service` at startup | `pywin32` is not installed in the active environment. Run `uv sync` (or `pip install pywin32`). The app does not silently fall back to demo mode. |
| Uptime charts are empty | Check that `servicewatch-history.jsonl` is writable. The file may also be empty if the app has just started or the history file was deleted. |
| Email notifications are not sent | Confirm `email.enabled` is `true`, verify `SERVICEWATCH_SMTP_PASSWORD` is available in `.env` or the process environment, and check whether your provider requires an app password or specific SMTP settings. |
| Logo is missing | Place `logo.png` in `static/` and restart the application. |
| `400` response on a POST request | The `X-ServiceWatch` header may be missing. The bundled dashboard is expected to send this header. |
| `403 Host not allowed` | The browser's `Host` header is not in `allowed_hosts`. Browse to `127.0.0.1`/`localhost`, or add the hostname to `allowed_hosts`. |
| `Address already in use` | Another instance may be running, or the port in `config.json` may need to be changed. |
| `Another Service Watch instance is already running` | The scheduled task is already running it. Use the dashboard, or disable the task first. |
| `Invalid config.json` | The message lists each problem (missing `name`, duplicate service, wrong type). Errors are also written to `servicewatch.log`. |
| Scheduled task "Last Run Result" is `0x1` | The app exited with an error. Check `servicewatch.log`. |
| Some recipients get no email | Search `servicewatch.log` for "rejected recipients". |

---

*Service Watch — 2026
