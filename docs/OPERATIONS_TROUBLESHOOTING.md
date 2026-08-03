# Operations & Troubleshooting — OLE Backend

Server-side runbook for the OLE backend running under **Servy** (Windows
service) on the production box at `D:\Application\IE-Pulse\BACKEND\`.

Its main job: diagnose the **silent service stops** — the service stopping on
its own with nothing obvious in Servy's captured stdout/stderr.

---

## 1. Deploy / update on the server

After new commits land on `master`:

```powershell
cd D:\Application\IE-Pulse\BACKEND
git pull
venv\Scripts\python.exe -m pip install -r requirements.txt   # picks up new deps (e.g. psutil)
# then restart the Servy service (Services.msc → restart, or `sc stop`/`sc start`)
```

`git pull` updates `requirements.txt` along with the code, so installing right
after pull is all that's needed. `psutil` is the only recent addition — if it
fails to install the app still runs (the heartbeat degrades to uptime-only).

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
| `logs/ole-backend.log` | Primary rotating log (10 × 10 MB). Startup/shutdown banners, signals, heartbeat, all request logs. **Start here.** |
| `logs/faulthandler.log` | Native C-level tracebacks (segfault, stack overflow) and periodic hang dumps. Only matters if there's a fresh entry near a stop. |
| Servy's captured stdout/stderr | Mirror of the console stream. Redundant with `ole-backend.log` now, but Servy may truncate/recycle it — prefer the file log. |

`logs/` is git-ignored and lives only on the box.

---

## 3. Reading a stop — the decision tree

When the service stops, open `logs/ole-backend.log` and look at the **last
20–30 lines**. The pattern tells you the cause:

### A) Clean signalled shutdown — *someone/something asked it to stop*
```
SIGNAL RECEIVED: SIGTERM — initiating graceful shutdown (uptime 4213s)
------------------------------------------------------------------------
OLE-BACKEND SHUTTING DOWN — pid 8228, uptime 4213s
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

Correlate the event `TimeCreated` with the last heartbeat in `ole-backend.log`.

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
Get-Content D:\Application\IE-Pulse\BACKEND\logs\ole-backend.log -Tail 40
```
