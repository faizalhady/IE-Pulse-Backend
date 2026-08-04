# Server deployment — SMH to SQLite + self-healing ingest

**Target:** `mypenm0iesvr02` → `D:\Application\IE-Pulse\BACKEND`
**Service:** `pulse-backend` (Servy, port 9007)
**Prepared:** 2026-08-04

Two changes ship together:

1. **SMH moves from `.xls` to SQLite** — the pipeline reads the `smh` table, and
   the SMH page gets create/edit/delete.
2. **Self-healing ingest** — the server's mart is missing all of 2026-07-29
   production (W31 reads 49.1% vs local 57.2%) and ~68,000 paid-hours across
   June. Incremental used to skip any date already in the mart, so those gaps
   could never close. It now re-reads the whole share and merges, which repairs
   them on the first run. No special mode, no extra step.

Read §0 before doing anything. One step will take the whole backend down if
skipped.

---

## 0. STOP — the one that breaks everything

`api/routers/smh.py` has a file-upload endpoint. **FastAPI raises at import**
when `python-multipart` is missing — it does not degrade to a broken endpoint,
the application fails to start. Every module goes down, not just SMH.

Checked on 2026-08-04: **the server venv does not have it.**

Install it BEFORE restarting the service:

```powershell
D:\Application\IE-Pulse\BACKEND\venv\Scripts\python.exe -m pip install python-multipart==0.0.32
```

Verify before going further:

```powershell
D:\Application\IE-Pulse\BACKEND\venv\Scripts\python.exe -c "import multipart; print(multipart.__version__)"
```

If that errors, do not restart the service.

> Local currently runs 0.0.21 while `requirements.txt` pins 0.0.32. Install the
> pinned version on the server; the API surface used here is stable across both.

---

## 1. Back up first

The mart holds history the network share no longer has — production from
2026-03-15 and paid hours from 2026-02-28, while the share only goes back to
2026-06-07. If a step goes wrong, that data is not re-creatable.

```powershell
$B = "D:\Application\IE-Pulse\BACKEND"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
Copy-Item "$B\data\mart\ole" "$B\data\_backup_$stamp\mart_ole" -Recurse
Copy-Item "$B\data\operational.db" "$B\data\_backup_$stamp\operational.db"
Get-ChildItem "$B\data\_backup_$stamp" -Recurse | Measure-Object -Property Length -Sum
```

Confirm the backup is non-empty before continuing.

---

## 2. Deploy the code

Files changed or added:

| File | Change |
|---|---|
| `core/database.py` | `smh` + `smh_audit` tables |
| `modules/ole/smh_store.py` | **new** — SMH reads/writes, validation, audit |
| `modules/ole/pipeline/ingest.py` | `ingest_smh()` reads SQLite; incremental no longer skips known dates; coverage self-check |
| `modules/ole/pipeline/refresh.py` | docs for the new incremental behaviour |
| `api/routers/smh.py` | **new** — SMH CRUD |
| `api/routers/ole.py` | removed `GET /api/smh` (moved to the new router) |
| `api/main.py` | registers `smh_router` |
| `scripts/migrate_smh_to_sqlite.py` | **new** — one-time import |
| `scripts/test_smh_store.py` | **new** — self-check |
| `requirements.txt` | `python-multipart` |

> `api/main.py` also imports `api/routers/access.py`, which is untracked in git
> and is separate work. **It must be on the server too** or the app will not
> import. Confirm it deploys with this batch or is already there.

Do not copy `data/`, `logs/`, `venv/`, or `__pycache__`.

---

## 3. Restart, and confirm it came up

```powershell
sc.exe stop pulse-backend ; sc.exe start pulse-backend
Start-Sleep -Seconds 5
Get-Content D:\Application\IE-Pulse\BACKEND\logs\pulse-backend.log -Tail 40
```

Look for the startup banner and `SQLite operational DB ready` — that line means
`init_db()` ran and the `smh` tables now exist.

If the service does not come up, it is almost certainly §0 or the `access.py`
note in §2. Check the log tail, do not retry blindly.

---

## 4. Import SMH into SQLite

The server's `.xls` files were byte-for-byte identical to local on 2026-08-04,
so this should produce the same 32,134 rows.

```powershell
cd D:\Application\IE-Pulse\BACKEND
.\venv\Scripts\python.exe -m scripts.migrate_smh_to_sqlite --dry-run
```

Expected: `42630 rows parsed, 32134 usable, 10496 blank/zero`. If the numbers
differ materially, stop — the server's `.xls` files have diverged and that needs
explaining before importing.

Then for real:

```powershell
.\venv\Scripts\python.exe -m scripts.migrate_smh_to_sqlite
```

**Until this runs, the pipeline will refuse to compute.** `ingest_smh()` aborts
on an empty table rather than publishing a plant-wide 0% OLE. That is
deliberate — a loud stop beats a plausible wrong number.

---

## 5. Purge the decimal-place errors

51 rows carry values like `4.761905e-10` — 1/21 with a slipped decimal. They
look populated but earn nothing, so they are worse than a missing value.

```powershell
.\venv\Scripts\python.exe -c "from modules.ole import smh_store; print(len(smh_store.purge_below_minimum(by='cleanup-decimal-error')))"
```

Expected: `51` (50 LAM RESEARCH, 1 ARISTA NETWORKS HLA), leaving 32,083. Each
delete is audited in `smh_audit`, so the old values are recoverable.

---

## 6. Refresh — this is also the repair

A plain refresh now re-reads every file in the share and merges: the share
window is rebuilt from source, older history is preserved. The gaps close on
this run.

Do NOT use `--full`. It re-reads only what the share currently holds
(2026-06-07 onward) and would delete ~63,000 production rows and ~165,000
paid-hours rows from before that date, which exist nowhere else.

```powershell
.\venv\Scripts\python.exe -m modules.ole.pipeline.refresh
```

Local took **~18 seconds** end to end. Watch for two lines:

- `Coverage OK -- all N share dates present in the mart`
- `Pipeline complete`

If instead you see `COVERAGE GAP -- ... missing from the mart`, stop and read
which dates. That is the new self-check; it means a share file failed to parse
or the share went unreachable mid-run.

---

## 7. Verify

Run from a machine that can reach both. Server and local should now agree.

```python
import duckdb
c = duckdb.connect()
S = '//mypenm0iesvr02/d$/Application/IE-Pulse/BACKEND/data/mart/ole/ole_computed.parquet'
L = 'data/mart/ole/ole_computed.parquet'
q = """SELECT strftime(date,'%Y-W%V') wk,
       ROUND(SUM(effective_output_smh)/SUM(total_input_hours)*100,1) ole
       FROM read_parquet(?) GROUP BY 1"""
l = c.execute(q,[L]).df().set_index('wk')
s = c.execute(q,[S]).df().set_index('wk')
m = l.join(s, lsuffix='_loc', rsuffix='_srv', how='outer').sort_index(ascending=False)
m['diff'] = (m.ole_loc - m.ole_srv).round(1)
print(m.head(12).to_string())
```

Pass criteria:

- **W31 diff ≈ 0** (was 8.1). This is the headline check — it means 2026-07-29
  production is now present.
- All weeks from 2026-W24 onward within ~0.1.

Then check the API is serving, and that the server now reports its own
coverage:

```powershell
curl.exe -s http://localhost:9007/api/smh/health          # {"status":"ok","rows":32083}
curl.exe -s http://localhost:9007/api/health              # coverage_ok must be true
```

`/api/health` is the thing to watch from now on. It reports:

```json
{ "status": "ok", "coverage_ok": true, "missing_days": [] }
```

`status` flips to `degraded` and `missing_days` names the dates whenever the
mart is missing a day the share still holds. That is the check that was absent
while this drift went unnoticed — `mart_ready` only ever meant "the files
exist", which stayed true the whole time the server served wrong numbers.

---

## 8. Known residue — not fixable here

Paid hours **before 2026-06-07** still differ: local 168,460 rows vs server
164,667. Those source files have been deleted from the share by retention, so
nothing can reconcile them. The difference is permanent
unless the CSVs exist in another backup.

It affects weeks up to 2026-W23. Weeks from W24 on will match after §6.

---

## Rollback

```powershell
sc.exe stop pulse-backend
# restore the code from the previous deploy
Copy-Item "$B\data\_backup_<stamp>\mart_ole\*" "$B\data\mart\ole\" -Force
Copy-Item "$B\data\_backup_<stamp>\operational.db" "$B\data\operational.db" -Force
sc.exe start pulse-backend
```

The old code reads SMH from the `.xls` files, which are untouched throughout —
so rolling back the code alone restores the previous behaviour. The `smh` tables
being left behind in SQLite is harmless; nothing reads them.

---

## Frontend

Separate deploy from `C:\Users\4033375\Projects\PRODUCTION DASHBOARD\IE-Pulse`.
Changed: `OLESmh.tsx` (CRUD + URL filters), `OlePlantReport.tsx` (Total Output
column, day-grain date filtering), `smhApi.ts` (new), `oleCalculations.ts`.

Build is clean. Ship it **after** the backend, since the page calls
`/api/smh/can-edit` and the CRUD endpoints.

Write access is limited to NTIDs `4033375` and `1268287`
(`SMH_EDITORS` in `api/routers/smh.py`). Everyone else sees a read-only page.
Note the new `user_access` table now exists — once populated, that allowlist
should read from it rather than staying hardcoded.
