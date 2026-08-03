# Operations & Troubleshooting — IE Pulse Backend

Server-side runbook for the backend running under **Servy** (Windows
service) on the production box at `D:\Application\IE-Pulse\BACKEND\`.

Its main job: diagnose the **silent service stops** — the service stopping on
its own with nothing obvious in Servy's captured stdout/stderr.

---

## 1. Deploy / update on the server

> **The server is NOT a git checkout.** There is no `.git` in
> `D:\Application\IE-Pulse\BACKEND` — deploys are a file copy. `git pull` there
> will fail. (Making it a checkout would simplify this; not done yet.)

Deploy = copy `api/`, `core/`, `modules/` and `requirements.txt` over, then:

```powershell
cd D:\Application\IE-Pulse\BACKEND
venv\Scripts\python.exe -m pip install -r requirements.txt   # picks up new deps
sc.exe stop pulse-backend ; sc.exe start pulse-backend
```

The service runs `venv\Scripts\python.exe -m uvicorn` **directly** — not the
`uvicorn.exe` shim, which bakes an absolute interpreter path into its binary and
breaks if the folder ever moves.

**Always confirm the env after a deploy** — `.env` is git-ignored and does not
travel with a copy. The startup banner reports `iedb key found : True/False`.

**Always confirm the env after a fresh deploy:**

```powershell
type D:\Application\IE-Pulse\BACKEND\.env   # must contain IEDB_CLIENT_KEY=...
```

`.env` is git-ignored, so it does NOT come down with a pull — it must already
exist on the server. The startup banner (below) reports `iedb key found : True/False`.

---

## 2. Where the logs are

| File | What it holds |
|---|---|
| `logs/pulse-backend.log` | Primary rotating log (10 × 10 MB). Startup/shutdown banners, signals, heartbeat, all request logs. **Start here.** |
| `logs/<module>.log` | Per-module view — `ole`, `cycle-time`, `ppqt`, `ipk`, `lbr` (10 MB × 5 each). Same lines as the main log, filtered to one module. **This is where scheduled pipeline runs land.** |
| `logs/faulthandler.log` | Native C-level tracebacks (segfault, stack overflow) and periodic hang dumps. Only matters if there's a fresh entry near a stop. |
| Servy's captured stdout/stderr | Mirror of the console stream. Redundant with `pulse-backend.log` now, but Servy may truncate/recycle it — prefer the file log. |

`logs/` is git-ignored and lives only on the box.

---

## 3. Reading a stop — the decision tree

When the service stops, open `logs/pulse-backend.log` and look at the **last
20–30 lines**. The pattern tells you the cause:

### A) Clean signalled shutdown — *someone/something asked it to stop*
```
SIGNAL RECEIVED: SIGTERM — initiating graceful shutdown (uptime 4213s)
------------------------------------------------------------------------
IE PULSE BACKEND SHUTTING DOWN — pid 8228, uptime 4213s
------------------------------------------------------------------------
```
→ **Not a crash.** A signal was delivered: Servy stop, console window close,
machine logoff/restart, or a deploy. Check **which signal**:
- `SIGINT` / `SIGBREAK` → typically a console Ctrl-C / Ctrl-Break or Servy
  sending a console stop.
- `SIGTERM` → a service-control / process stop request.

Then check **Servy + Windows** for who issued it (see §4).

### B) Heartbeat stops with NO shutdown banner — *hard kill*
```
heartbeat — uptime 3600s, rss 1820 MB
heartbeat — uptime 3660s, rss 1944 MB
heartbeat — uptime 3720s, rss 2090 MB      ← last line, then silence
```
→ **Hard kill, no graceful shutdown.** No signal was honoured. Likely causes:
- **OOM-kill** — note whether `rss` was climbing toward the box's RAM ceiling.
- `taskkill /F`, a crash in native code, or the machine losing power.

Cross-check `logs/faulthandler.log` for a fresh dump, and the Windows Event Log
(see §4).

### C) Fresh entry in `faulthandler.log` — *native crash or hang*
A `Fatal Python error` / `Windows fatal exception` block, or a
`dump_traceback_later` dump showing every thread wedged in the same place
→ native crash (bad C extension state) or a deadlock/hung request. The thread
stacks point at where it was stuck.

### D) `UNHANDLED EXCEPTION on thread …` / `on main thread`
→ A background worker (e.g. the refresh pipeline) or the main thread died on an
exception. Full traceback is inline. This no longer vanishes silently.

---

## 4. What the app logs CANNOT tell you (need server access)

If §3 points to a **signalled stop (A)** or **hard kill (B)**, the *origin* is
outside the Python process. Collect this on the server (PowerShell):

```powershell
$svc = 'OLE-BE'   # ← replace with the real service name:
                  #   Get-Service | ? { $_.DisplayName -match 'ole|pulse|9007|servy' }

sc.exe qc        $svc      # start type + the exact binary/args Servy launches
sc.exe qfailure  $svc      # recovery policy — does SCM auto-restart on failure?

# Service Control Manager start/stop events (last 7 days)
Get-WinEvent -FilterHashtable @{ LogName='System'; ProviderName='Service Control Manager'; StartTime=(Get-Date).AddDays(-7) } |
  Where-Object { $_.Message -match $svc } |
  Select-Object TimeCreated, Id, Message | Format-List

# App-level faults / WER (OOM, access violations) near the stop time
Get-WinEvent -FilterHashtable @{ LogName='Application'; StartTime=(Get-Date).AddDays(-7) } |
  Where-Object { $_.LevelDisplayName -in 'Error','Critical' -and $_.Message -match 'python|uvicorn|9007|OLE|0xc0000005' } |
  Select-Object TimeCreated, Id, ProviderName, LevelDisplayName, Message | Format-List
```

Key event IDs (System log, *Service Control Manager*):
- **7036** — service entered Running / Stopped (normal state changes).
- **7031 / 7034** — service **terminated unexpectedly**, with a restart count →
  confirms a crash vs a deliberate stop.
- **7024** — service stopped **with a specific error code**.

Correlate the event `TimeCreated` with the last heartbeat in `pulse-backend.log`.

---

## 5. Known issues already fixed (context)

These were live bugs found in the server logs (commit `ec7dabe`):

- **DuckDB `OutOfMemoryException`** on heavy `/api/cycle-time/*` queries — the
  per-request connection had a `memory_limit` but no `temp_directory`, so it
  couldn't spill and threw at the ceiling. Fixed: spill enabled + thread cap.
  Tunable without redeploy via env: `CT_DUCKDB_MEMORY_LIMIT` (default `2GB`),
  `CT_DUCKDB_THREADS` (default `2`), `CT_DUCKDB_TEMP_DIR`.
  *This caused 500s, not the process stop — but heavy memory pressure is a
  plausible trigger for an OOM-kill (pattern B), so watch the heartbeat RSS.*

- **`IEDB_CLIENT_KEY is not set`** despite `.env` existing — `auth.py` loaded
  `.env` relative to the process CWD, which Servy doesn't set to the backend
  root. Fixed: `.env` is now loaded from the repo root regardless of CWD. If you
  still see this error, the key genuinely isn't in `D:\…\BACKEND\.env`.

---

## 6. Quick health check

```powershell
# Is it up and serving?
Invoke-WebRequest http://127.0.0.1:9007/api/cycle-time/health -UseBasicParsing | Select StatusCode

# Last lifecycle lines
Get-Content D:\Application\IE-Pulse\BACKEND\logs\pulse-backend.log -Tail 40
```

---

## Did a scheduled pipeline run?

Every line carries a `[module]` tag, derived from the logger name. Each module
also gets its own file, so a module log reads as a run history:

```
2026-08-03 02:00:01  INFO    [cycle-time] refresh: RUN START  mode=incremental trigger=scheduled
2026-08-03 02:04:17  INFO    [cycle-time] refresh: RUN OK     256.3s
```

A failure logs the traceback and re-raises, so the process still exits non-zero
and Task Scheduler records `LastTaskResult != 0`:

```
2026-08-03 10:45:03  ERROR   [ole       ] refresh: RUN FAILED after 12.4s — FileNotFoundError: ...
```

Quick checks:

```powershell
# every run of one module, newest last
Select-String -Path logs\cycle-time.log -Pattern "RUN "

# only the failures, across all modules
Select-String -Path logs\*.log -Pattern "RUN FAILED"

# what the scheduler itself thinks
Get-ScheduledTask -TaskName 'IEPulse-*' | Get-ScheduledTaskInfo |
    Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime
```

> Before 2026-08-03 the pipeline entrypoints called `logging.basicConfig`, which
> writes to a console. Task Scheduler captures no console, so **scheduled runs
> left no trace at all** — a failed ingest was invisible until the data went
> stale. They now use `core.logging_setup`, so runs are on disk.
