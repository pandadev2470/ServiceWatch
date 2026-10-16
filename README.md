<!--
FILE: README.md
Project: ServiceWatch_V1
Contact: oguejoforc@concordnc.gov
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
    "to_addrs": ["you@example.com"],
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
- `cooldown_seconds` applies to each event/service pair to help prevent repeated notifications when a service repeatedly changes state.

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
| `history_file` | Path to the JSONL history file. |
| `history_retention_days` | Number of days to retain history; older rows are pruned. |
| `email.*` | SMTP settings for email notifications. See [Email Notifications](#email-notifications). |
| `services[].name` | Windows service short name. |
| `services[].label` | Friendly name displayed in the dashboard. |
| `services[].auto_restart` | Set to `false` to monitor a service without automatically restarting it. |

## Run at Startup with Task Scheduler

To start Service Watch automatically when Windows starts:

1. Open **Task Scheduler** and create a task.
2. Configure the task to run at startup.
3. Select **Run with highest privileges**.
4. Set the program/script to the project's `.venv\Scripts\python.exe`.
5. Set the argument to `app.py`.
6. Set **Start in** to the project directory.

If email notifications are enabled, make sure the task can access the SMTP password environment variable. For example, to set it as a user environment variable in PowerShell:

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
| `static/logo.png` | logo displayed in the dashboard. |
| `servicewatch.log` | Rotating log file created on first run. |
| `servicewatch-history.jsonl` | Append-only history file created on first run. |

## Troubleshooting

| Symptom | Likely cause or fix |
| --- | --- |
| `Access is denied` when starting a service | Run the application from an Administrator terminal. |
| `The specified service does not exist` | Check `config.json` and use the service's short name. |
| Demo-mode banner appears on Windows | `SERVICEWATCH_MOCK=1` may be set, or `pywin32` may have failed to import. Check the application log. |
| Uptime charts are empty | Check that `servicewatch-history.jsonl` is writable. The file may also be empty if the app has just started or the history file was deleted. |
| Email notifications are not sent | Confirm `email.enabled` is `true`, verify `SERVICEWATCH_SMTP_PASSWORD` is available in `.env` or the process environment, and check whether your provider requires an app password or specific SMTP settings. |
| Logo is missing | Place `logo.png` in `static/` and restart the application. |
| `400` response on a POST request | The `X-ServiceWatch` header may be missing. The bundled dashboard is expected to send this header. |
| `Address already in use` | Another instance may be running, or the port in `config.json` may need to be changed. |

---

*Service Watch — 2026
