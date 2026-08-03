# Rename Runbook — `ole-backend` → `IE-Pulse-Backend`

> **STATUS: COMPLETE — 2026-08-03.** Dev, GitHub and server all done and verified.
> Kept only for the three open findings at the bottom, which are unrelated to the
> rename and still need attention. Delete this file once those are handled.
>
> Outcome: dev folder, GitHub repo, and `D:\Application\IE-Pulse\BACKEND` all
> renamed. Service `pulse-backend` runs `python.exe -m uvicorn` directly — the
> `uvicorn.exe` shim is gone, so it is now one process instead of three. Neither
> venv needed rebuilding. nginx needed no change at all.

| | From | To |
|---|---|---|
| Dev | `C:\Users\4033375\Projects\OLE ANALYZER\ole-backend` | `C:\Users\4033375\Projects\IE-Pulse-Backend` |
| Server | `D:\Application\IE-Pulse\OLE-BACKEND` | `D:\Application\IE-Pulse\BACKEND` |
| GitHub | `faizalhady/ole-backend` | `faizalhady/IE-Pulse-Backend` |

Server target is `BACKEND` (not `IE-Pulse-Backend`) because it already sits inside
`IE-Pulse\` alongside `CORE`, `OLE`, `PPQT`, `IPK`, `LBR`, `CYCLE-TIME` — all bare
uppercase. `IE-Pulse\IE-Pulse-Backend` would stutter.

---

## What does NOT need changing (verified, do not touch)

| Thing | Why |
|---|---|
| **nginx** (all 5 confs in `C:\nginx\conf\`) | Zero references to the backend dir. Reaches it only via `proxy_pass http://127.0.0.1:9007`. The `alias` lines point at *frontend* dist folders, which are separate dirs. **No nginx edit, no nginx restart.** |
| Git history / branches / remotes-as-data | Git stores nothing about the folder name |
| Frontend `src/**` | Hits are UI text "OLE Analyzer" — a product name, not a path |
| `data/`, `logs/` contents | Relative to project root, move with the folder |
| `.env` | No absolute paths |
| Local Windows scheduled tasks | None exist |

---

## Pre-flight — do all of this before touching anything

```powershell
# 1. Commit current work (5 modified + ~20 untracked files are uncommitted right now)
cd "C:\Users\4033375\Projects\OLE ANALYZER\ole-backend"
git add -A
git commit -m "chore: checkpoint before directory rename"

# 2. Back up both Claude stores
$bk = "C:\Users\4033375\Projects\temp\rename-backup-20260803"
New-Item -ItemType Directory -Force $bk
Copy-Item "C:\Users\4033375\.claude.json" "$bk\claude.json.bak"
Copy-Item -Recurse "C:\Users\4033375\.claude\projects\C--Users-4033375-Projects-OLE-ANALYZER-ole-backend" "$bk\claude-project-dir"

# 3. Back up server service config.
#    BEST: in the Servy UI, select `pulse-backend` -> Export -> save the config file.
#    Revert then becomes a one-click Import instead of retyping fields.
#    Belt-and-braces, the whole store is a single SQLite file:
Copy-Item "\\mypenm0iesvr02\c$\ProgramData\Servy\db\Servy.db" "$bk\Servy.db.bak"

# 4. Dump server scheduled tasks
Invoke-Command -ComputerName mypenm0iesvr02 -ScriptBlock {
  Get-ScheduledTask -TaskName 'IEPulse-*' | Export-Clixml D:\Application\IE-Pulse\SERVICE-BACKUP\tasks-pre-rename.xml
}
```

**Close VS Code and every terminal sitting in the old directory** — Windows will
refuse the rename if anything holds a handle.

---

## Phase 1 — Local code edits (do these BEFORE renaming)

Two files hardcode the absolute path. Everything else is comments/log-names.

`scripts/mes_crack.py:24` and `scripts/mes_settle.py:22`:

```python
# before
sys.path.insert(0, r"C:\Users\4033375\Projects\OLE ANALYZER\ole-backend")
# after — resolve from the file's own location, never breaks again
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
```

**Check:**
```powershell
venv\Scripts\python.exe -c "from pathlib import Path; print(Path('scripts/mes_crack.py').resolve().parents[1])"
# must print the project root
grep -rn "OLE ANALYZER" api core modules scripts    # must return nothing
```

Note: both scripts `import mes_sweep`, which **does not exist** in `scripts/` —
they already fail at import, independent of this change. They're untracked
scratch files from the MES exploration. The path fix is still correct; it just
can't be checked by running them. Consider deleting both.

**Status: DONE** — applied and verified 2026-08-03.

---

## Phase 2 — Local directory rename

```powershell
Move-Item "C:\Users\4033375\Projects\OLE ANALYZER\ole-backend" `
          "C:\Users\4033375\Projects\IE-Pulse-Backend"
```

**Do NOT delete the venv.** `python.exe` is relocatable — it finds `pyvenv.cfg`
next to itself at runtime, and `home` in that file points at the base Python
install, which hasn't moved. Only the console-script shims (`pip.exe`,
`uvicorn.exe`) and the `activate` scripts bake in the old path, and `-m` form
sidesteps all of them.

Test the moved venv first:

```powershell
cd C:\Users\4033375\Projects\IE-Pulse-Backend
venv\Scripts\python.exe -c "import fastapi, duckdb, pandas; print('venv OK')"
venv\Scripts\python.exe -c "from api.main import app; print('routes:', len(app.routes))"
```

Passes → done, no rebuild. Use `venv\Scripts\python.exe -m pip ...` and
`-m uvicorn ...` from here on instead of the shims.

Only if it fails, rebuild — with the old venv still present as fallback:

```powershell
Rename-Item venv venv-old          # keep it until the new one is proven
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
# proven? then: Remove-Item -Recurse -Force venv-old
```

> Deleting the venv before proving pip can reach the corporate proxy was the only
> step in this plan that could leave you stranded. It isn't necessary — don't.

**Revert:** `Move-Item` back. The venv works in either location, so there is
nothing to undo.

---

## Phase 3 — Local Claude stores

Two separate stores. Missing either loses something.

```powershell
# 3a. Session transcripts + memory/
Move-Item "C:\Users\4033375\.claude\projects\C--Users-4033375-Projects-OLE-ANALYZER-ole-backend" `
          "C:\Users\4033375\.claude\projects\C--Users-4033375-Projects-IE-Pulse-Backend"
```

```powershell
# 3b. Re-key the projects entry in .claude.json (allowedTools, MCP, trust flag)
python - <<'PY'
import json, pathlib
p = pathlib.Path.home() / ".claude.json"
d = json.loads(p.read_text(encoding="utf-8"))
old = "C:/Users/4033375/Projects/OLE ANALYZER/ole-backend"
new = "C:/Users/4033375/Projects/IE-Pulse-Backend"
assert old in d["projects"], "old key not found — already renamed?"
d["projects"][new] = d["projects"].pop(old)
p.write_text(json.dumps(d, indent=2), encoding="utf-8")
print("re-keyed ->", new)
PY
```

Mangling rule for the folder name: full path, every non-alphanumeric → `-`.
`C:\Users\4033375\Projects\IE-Pulse-Backend` → `C--Users-4033375-Projects-IE-Pulse-Backend`

**Check:** start Claude Code in the new dir. You should get **no trust dialog**,
and `/resume` should list the old sessions.

**Revert:** move the folder back; restore `$bk\claude.json.bak` over `~/.claude.json`.

---

## Phase 4 — Workspace file

`C:\Users\4033375\Projects\IE-Pulse.code-workspace` line 7:

```jsonc
// before
"path": "OLE ANALYZER/ole-backend",
// after
"path": "IE-Pulse-Backend",
```

**Check:** open the workspace — the folder resolves, no "missing folder" warning.

---

## Phase 5 — GitHub

1. GitHub UI → repo Settings → rename `ole-backend` → `IE-Pulse-Backend`
2. ```powershell
   git remote set-url origin https://github.com/faizalhady/IE-Pulse-Backend.git
   git fetch origin && git status
   ```

GitHub permanently redirects the old URL, so this is low-risk and reversible by
renaming back.

---

## Phase 6 — Server (the only phase with downtime)

### The critical detail

Servy launches the service as:

```
ExecutablePath   : D:\Application\IE-Pulse\OLE-BACKEND\venv\Scripts\uvicorn.exe
StartupDirectory : D:\Application\IE-Pulse\OLE-BACKEND
Parameters       : api.main:app --host 127.0.0.1 --port 9007   (stored encrypted)
```

`uvicorn.exe` is a console-script shim with the interpreter path **baked into its
binary header**. It will not survive the rename.

**Do not rebuild the server venv** (needs pip through the corporate proxy — avoidable
risk). Instead switch to the interpreter directly. `python.exe` is relocatable: it
finds `pyvenv.cfg` next to itself at runtime.

```
ExecutablePath   : D:\Application\IE-Pulse\BACKEND\venv\Scripts\python.exe
StartupDirectory : D:\Application\IE-Pulse\BACKEND
Parameters       : -m uvicorn api.main:app --host 127.0.0.1 --port 9007
```

Same result, no venv rebuild, and it makes future moves free.

### Steps

```powershell
# 6a. Stop
sc.exe \\mypenm0iesvr02 stop pulse-backend
# confirm nothing holds port 9007 — there are currently 3 processes on it (see Notes)
Invoke-Command -ComputerName mypenm0iesvr02 -ScriptBlock {
  Get-CimInstance Win32_Process -Filter "Name='uvicorn.exe' OR Name='python.exe'" |
    Where-Object CommandLine -match 'OLE-BACKEND' |
    Select-Object ProcessId, CommandLine
}
# kill any stragglers by PID, then:

# 6b. Rename (instant, instantly reversible — NOT copy-then-delete)
Invoke-Command -ComputerName mypenm0iesvr02 -ScriptBlock {
  Rename-Item 'D:\Application\IE-Pulse\OLE-BACKEND' 'BACKEND'
}
```

**6c.** Open the **Servy UI on the server** and edit service `pulse-backend` to the
three values above. Parameters is encrypted at rest, so it must be retyped — the
plaintext is confirmed from the running process:
`-m uvicorn api.main:app --host 127.0.0.1 --port 9007`

**6d.** Re-point the two scheduled tasks. Easiest is to re-run the setup script from
the new location — it resolves its own root:

```powershell
# on the server, ELEVATED
cd D:\Application\IE-Pulse\BACKEND
.\scripts\setup_scheduled_tasks.ps1
```

**6e. Start and verify:**

```powershell
sc.exe \\mypenm0iesvr02 start pulse-backend
Start-Sleep 10

# every module's health endpoint
'ole','cycle-time','ppqt','ipk','lbr' | ForEach-Object {
  $u = "https://mypenm0iesvr02.corp.jabil.org/ietools/ole/api/$_/health"
  "{0,-12} {1}" -f $_, (Invoke-WebRequest $u -UseBasicParsing).StatusCode
}

# startup banner should say the new path
Get-Content \\mypenm0iesvr02\d$\Application\IE-Pulse\BACKEND\logs\ole-backend.log -Tail 40
Get-Content \\mypenm0iesvr02\d$\Application\IE-Pulse\LOGS\ole-be-stderr.log -Tail 20
```

Also load the actual dashboards in a browser — health endpoints passing doesn't
prove nginx→backend routing survived (it will, but confirm).

### Revert (≈2 minutes)

```powershell
sc.exe \\mypenm0iesvr02 stop pulse-backend
Invoke-Command -ComputerName mypenm0iesvr02 -ScriptBlock {
  Rename-Item 'D:\Application\IE-Pulse\BACKEND' 'OLE-BACKEND'
}
# Servy UI: restore the three original values (above), or stop Servy and
# restore $bk\Servy.db.bak over C:\ProgramData\Servy\db\Servy.db
sc.exe \\mypenm0iesvr02 start pulse-backend
```

---

## Phase 7 — Cosmetic text (optional, zero runtime effect)

`README.md`, `CLAUDE.md`, `docs/*.md`, `plan/*.md`, path comments in
`api/routers/ebuild.py`, `modules/cycle_time/config.py`,
`modules/cycle_time/planner_demand.py`.

Leave `core/logging_setup.py` alone unless you want to: renaming
`ole-backend.log` starts a fresh rotation and orphans the existing history, and
`docs/OPERATIONS_TROUBLESHOOTING.md` tells you to look for that exact filename.

---

## Notes found during the sweep — unrelated to the rename, worth a look later

1. **Duplicate Servy entry.** Servy's DB has two rows with identical config:
   `IEPulse-OLE-Backend` (Id 20) and `pulse-backend` (Id 21). Only `pulse-backend`
   exists as a real Windows service; Id 20 is a stale leftover. Harmless, but
   delete it so it can't be started by mistake.

2. **Three processes on port 9007**, not one:
   - `uvicorn.exe` (PID 12548)
   - venv `python.exe` running that shim (PID 94240)
   - **`C:\Program Files\Python312\python.exe` running the same shim (PID 91404)** —
     the *system* Python, not the venv

   That third one is wrong and may well be connected to the silent-stop issue in
   `Obsidian/JABIL/Issues/pulse-backend Service Silently Stops.md`. Switching Servy
   to invoke `python.exe -m uvicorn` (Phase 6) collapses the shim layer and may
   incidentally fix it. Investigate separately.

3. **`IEPulse-CycleTime-Ingest` is not registered on the server.** The setup script
   defines it and `docs/CYCLE_TIME_BUILD.md` assumes 02:00 nightly incremental runs,
   but only the two OLE tasks exist. Cycle Time is likely not auto-refreshing.
   Re-running `setup_scheduled_tasks.ps1` in Phase 6d will create it — expect that,
   and confirm a nightly run is actually wanted before it fires.
