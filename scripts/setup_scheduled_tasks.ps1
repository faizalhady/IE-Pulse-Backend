<#
.SYNOPSIS
  Register the daily IE Pulse pipeline scheduled tasks (OLE + Cycle Time).

.DESCRIPTION
  Creates / updates Windows scheduled tasks:

    IEPulse-OLE-Ingest-PaidHours    10:45  - 5 min after paid-hours upload (10:40)
    IEPulse-OLE-Ingest-Production   13:05  - 5 min after production upload (13:00)
    IEPulse-CycleTime-Ingest        02:00  - overnight incremental IEDB refresh

  OLE tasks read BOTH sources (paid_hours + production). The state file +
  date-stitching make it safe to run twice a day:
    - State "since" mark skips files already processed
    - seen_dates seed prevents row duplication
    - _merge_with_existing absorbs any overlap

  Cycle Time runs incremental (delta + upsert): fetches only IEDB records
  changed since last run, upserts into raw.parquet by primary key (no dupes,
  no data loss, no cross-workcell changes), then rebuilds pivoted /
  assembly_summary / eBuild runner mart. Safe to run daily.

  Idempotent - re-run this script any time to update timings; existing
  tasks with the same names are replaced.

.NOTES
  - This script lives in scripts/ and resolves the project root one level up.
  - Run from an ELEVATED PowerShell prompt:
        .\scripts\setup_scheduled_tasks.ps1
  - Logon mode: runs as SYSTEM, whether anyone is logged on or not. No
    password is prompted for or stored, so nothing expires. (Previously this
    stored a user password, which silently died on every rotation - see the
    comment above $Principal.)
#>

$ErrorActionPreference = "Stop"

# Resolve project root: scripts/ lives one level under the project root.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root      = Split-Path -Parent $ScriptDir
$PythonExe = Join-Path $Root "venv\Scripts\python.exe"

if (-not (Test-Path $PythonExe)) { throw "venv python not found: $PythonExe" }

# ponytail: run as SYSTEM, not a named user. A stored user password silently
# expires (Jabil rotates every 6 months) and Task Scheduler then fails every
# run with 0x8007052E ERROR_LOGON_FAILURE while still reporting State=Ready --
# which is exactly what happened 2026-07-05..07-27: 22 consecutive missed runs
# that looked healthy. SYSTEM has no password, so nothing to expire.
# Verified 2026-07-27 on MYPENM0IESVR02: SYSTEM (i.e. the machine account
# MYPENM0IESVR02$) CAN read \\penhomev10\OLE (119 items) and \OLE\RawData
# (56 items), and pulse-backend already runs as LocalSystem and writes the
# same mart -- so this grants nothing the box didn't already have.
$Principal = New-ScheduledTaskPrincipal `
    -UserId 'SYSTEM' `
    -LogonType ServiceAccount `
    -RunLevel Highest

function Register-PipelineTask {
    param(
        [string]$Name,
        [string]$Time,
        [string]$Argument,               # e.g. "-m modules.ole.pipeline.refresh"
        [string]$Description,
        [int]$TimeLimitMinutes = 30
    )

    $action = New-ScheduledTaskAction `
        -Execute $PythonExe `
        -Argument $Argument `
        -WorkingDirectory $Root

    $trigger = New-ScheduledTaskTrigger -Daily -At $Time

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DontStopOnIdleEnd `
        -ExecutionTimeLimit (New-TimeSpan -Minutes $TimeLimitMinutes) `
        -MultipleInstances IgnoreNew

    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "Replaced existing: $Name"
    }

    Register-ScheduledTask `
        -TaskName $Name `
        -Description $Description `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $Principal | Out-Null

    Write-Host "Registered: $Name at $Time daily"
}

# ── OLE ──────────────────────────────────────────────────────────────────────
Register-PipelineTask `
    -Name "IEPulse-OLE-Ingest-PaidHours" `
    -Time "10:45" `
    -Argument "-m modules.ole.pipeline.refresh" `
    -Description "Daily OLE pipeline refresh after paid hours upload."

Register-PipelineTask `
    -Name "IEPulse-OLE-Ingest-Production" `
    -Time "13:05" `
    -Argument "-m modules.ole.pipeline.refresh" `
    -Description "Daily OLE pipeline refresh after production upload."

# ── Cycle Time ───────────────────────────────────────────────────────────────
# Incremental (delta + upsert): fetches only IEDB records changed since the last
# run and upserts into raw.parquet, then chains transform -> assembly_summary ->
# eBuild runner mart. Safe to run daily (upsert by key: no dupes, no data loss,
# no cross-workcell changes). Runs overnight; 4h limit covers a large delta +
# the 24-month eBuild runner pull. NOTE: incremental can't heal old missed rows
# (watermark blind spot) — run a manual --backfill periodically for that.
Register-PipelineTask `
    -Name "IEPulse-CycleTime-Ingest" `
    -Time "02:00" `
    -Argument "-m modules.cycle_time.pipeline.refresh" `
    -Description "Daily Cycle Time incremental refresh (IEDB -> raw.parquet + transform/assembly_summary/eBuild runner)." `
    -TimeLimitMinutes 240

Write-Host ""
Write-Host "Done. Useful commands:"
Write-Host "  Get-ScheduledTask -TaskName 'IEPulse-*'"
Write-Host "  Start-ScheduledTask   -TaskName 'IEPulse-CycleTime-Ingest'   # run it now"
Write-Host "  Get-ScheduledTaskInfo -TaskName 'IEPulse-CycleTime-Ingest'   # last run result"
