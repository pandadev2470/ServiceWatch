<#
FILE: install-task.ps1
ServiceWatch_V1
Contact: oguejioforc@concordnc.gov

[Claude review 2026-10-19] Registers Service Watch as a Windows scheduled task
that starts at boot and is relaunched if it ever exits.

Run from an ELEVATED PowerShell in the project folder:

    .\install-task.ps1                     # install (runs as you, no password stored)
    .\install-task.ps1 -RunAs SYSTEM       # install as LocalSystem instead
    .\install-task.ps1 -StartNow           # install and start immediately
    .\install-task.ps1 -Uninstall          # stop and remove the task

If scripts are blocked:  powershell -ExecutionPolicy Bypass -File .\install-task.ps1

What the task does, and why (each is a Task Scheduler default that would
otherwise break a long-running monitor):
  * Trigger 1: at startup, after a short delay so "Automatic (Delayed Start)"
    services have had time to start (avoids false "stopped" alerts at boot).
  * Trigger 2: every 5 minutes, as a watchdog. If Service Watch is already
    running the run is ignored (MultipleInstances IgnoreNew); if it crashed
    or was killed, it is started again.
  * No execution time limit. The default "stop after 3 days" would silently
    kill the monitor.
  * Runs on battery and keeps running when switching to battery.
  * Highest privileges: required to start services.

Troubleshooting checklist:
  * Task shows "Running" but no dashboard -> check servicewatch.log.
  * Last Run Result 0x1 -> the app exited with an error; see servicewatch.log
    (bad config.json, port in use, another instance already running).
  * To stop Service Watch for maintenance, DISABLE the task first, then end
    it; otherwise the watchdog trigger starts it again within 5 minutes.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "ServiceWatch",
    [ValidateSet("CurrentUser", "SYSTEM")]
    [string]$RunAs = "CurrentUser",
    [ValidateRange(0, 60)]
    [int]$StartupDelayMinutes = 2,
    [switch]$StartNow,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Run this script from an elevated (Run as Administrator) PowerShell."
}

# ---- Uninstall ---------------------------------------------------------- #
if ($Uninstall) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) { Write-Host "Task '$TaskName' is not installed."; return }
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
    Stop-ScheduledTask -TaskName $TaskName
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed task '$TaskName'."
    Write-Host "If a python.exe for app.py is still running, end it in Task Manager."
    return
}

# ---- Locate the app ----------------------------------------------------- #
$ProjectDir = $PSScriptRoot
$Python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$App = Join-Path $ProjectDir "app.py"
if (-not (Test-Path $Python)) { throw "Not found: $Python. Run 'uv sync' in $ProjectDir first." }
if (-not (Test-Path $App)) { throw "Not found: $App" }

# Sanity-check config.json before installing something that runs hidden.
try { Get-Content (Join-Path $ProjectDir "config.json") -Raw | ConvertFrom-Json | Out-Null }
catch { throw "config.json is not valid JSON: $($_.Exception.Message)" }

if ($RunAs -eq "SYSTEM" -and $ProjectDir -like "$env:SystemDrive\Users\*") {
    Write-Warning ("The project is inside a user profile ($ProjectDir). Running it as SYSTEM " +
        "lets anyone who can edit these files run code as SYSTEM. Move it to e.g. " +
        "C:\ServiceWatch (writable by Administrators only) or use -RunAs CurrentUser.")
}

# ---- Build the task ----------------------------------------------------- #
$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$App`"" -WorkingDirectory $ProjectDir

$atStartup = New-ScheduledTaskTrigger -AtStartup
if ($StartupDelayMinutes -gt 0) { $atStartup.Delay = "PT$($StartupDelayMinutes)M" }
# No RepetitionDuration = repeat indefinitely.
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 5)

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

if ($RunAs -eq "SYSTEM") {
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
} else {
    # S4U = "Run whether user is logged on or not" without storing a password.
    # Outbound SMTP still works; only Windows-authenticated network shares don't.
    $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType S4U -RunLevel Highest
}

$task = New-ScheduledTask -Action $action -Trigger @($atStartup, $watchdog) `
    -Settings $settings -Principal $principal `
    -Description "Service Watch: monitors Windows services, restarts them, emails alerts. Dashboard: see config.json host/port."

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Write-Host "Installed task '$TaskName' (runs as $($principal.UserId), highest privileges)."
Write-Host "  Program:  $Python"
Write-Host "  Argument: $App"
Write-Host "  Start in: $ProjectDir"

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started. Dashboard should be up in a few seconds; logs: $ProjectDir\servicewatch.log"
} else {
    Write-Host "It will start at next boot (or within 5 minutes via the watchdog trigger)."
    Write-Host "To start now: Start-ScheduledTask -TaskName $TaskName"
}

# End of File: install-task.ps1
