<!--
FILE: README.md
Project: ServiceWatch_V1
Contact: admin@thepandadev.com
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


## Setup

```powershell
uv venv
.venv\Scripts\activate
uv sync                      # or: pip install -r requirements.txt
Copy-Item .env.example .env  # set SERVICEWATCH_SMTP_PASSWORD
```

Add services to `config.json` using each service's **short name**:

```powershell
Get-Service | Select-Object Name, DisplayName
```

## Run

From an **Administrator** PowerShell:

```powershell
uv run python app.py
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080). On macOS/Linux, or with `SERVICEWATCH_MOCK=1`, it runs in demo mode.

## Run at startup

```powershell
.\install-task.ps1 -StartNow   # install and start
.\install-task.ps1 -Uninstall  # remove
```

Disable the task before stopping Service Watch for maintenance, or the watchdog restarts it within 5 minutes.

## Logs and history

- `servicewatch.log` — application log
- `servicewatch-history.jsonl` — uptime history (delete to reset)
