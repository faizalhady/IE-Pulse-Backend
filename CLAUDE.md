# IE Pulse — Backend
# C:\Users\4033375\Projects\IE-Pulse-Backend\

## What This Is

This is the **IE Pulse backend** — a FastAPI monorepo serving all IE analytical
modules for Jabil Penang. The product is **IE Pulse**.

> Renamed from `OLE ANALYZER\ole-backend` on 2026-08-03 — the old name only ever
> described one module. The GitHub repo is `faizalhady/IE-Pulse-Backend`. The
> production folder on the server is still `D:\Application\IE-Pulse\OLE-BACKEND`
> until that phase runs; see `docs/RENAME_RUNBOOK.md`.

Single backend, multiple modules. Each module is self-contained inside its own
`modules/<name>/` folder. Routers are thin — all business logic lives in
`modules/`.

**This platform will keep growing.** New modules are added regularly. Every new
module follows the same recipe documented in section 5. Do not invent new
patterns — always read existing modules first.

The paired frontend lives at:
  C:\Users\4033375\Projects\PRODUCTION DASHBOARD\IE-Pulse\

---

## Stack

- Python / FastAPI + uvicorn
- pandas + pyarrow — all computed data stored as parquet (the "mart")
- duckdb — analytical queries over parquet files
- openpyxl / beautifulsoup4 / lxml — Excel + HTML-disguised XLS parsing
- SQLite (`core/database.py`) — user-entered operational data only
- statsmodels — OLE forecasting / prediction endpoint
- requests + python-dotenv — external API clients (e.g. Cycle Time → IEDB3.0)

---

## Repo Structure

```
IE-Pulse-Backend/
│
├── CLAUDE.md                       # this file — platform orientation
├── docs/                           # per-module deep-dive specs
│   ├── OLE_BUILD.md
│   ├── CYCLE_TIME_BUILD.md
│   └── IPK_BUILD.md
│
├── api/
│   ├── main.py                     # FastAPI app — registers all module routers
│   ├── deps.py                     # shared dependencies (auth, db session)
│   └── routers/                    # one file per module
│       ├── ole.py                  ✅ live
│       ├── downtime.py             ✅ live
│       ├── transfers.py            ✅ live
│       ├── cycle_time.py           ✅ live
│       ├── ppqt.py                 🔲 stubbed
│       ├── lbr.py                  🔲 stubbed
│       └── ipk.py                  🔲 stubbed
│
├── core/                           # shared across ALL modules — never module-specific
│   ├── database.py                 # SQLite for operational data
│   ├── paths.py                    # PROJECT_ROOT, DATA_RAW_DIR, DATA_MART_DIR
│   └── naming.py                   # shared naming helpers
│
├── modules/                        # one folder per module — all business logic
│   ├── ole/                        ✅ live
│   │   ├── config.py
│   │   └── pipeline/
│   │       ├── ingest.py           # pull from \\penhomev10\OLE network share
│   │       ├── compute.py
│   │       ├── compute_mh.py
│   │       ├── compute_weekly.py
│   │       └── refresh.py
│   │
│   ├── cycle_time/                 ✅ live
│   │   ├── config.py
│   │   ├── auth.py                 # external IEDB3.0 auth
│   │   ├── client.py               # HTTP client
│   │   ├── keep_awake.py
│   │   └── pipeline/
│   │       ├── ingest.py
│   │       ├── transform.py
│   │       └── refresh.py
│   │
│   ├── lbr/                        🔲 stubbed
│   │   ├── config.py
│   │   └── pipeline/refresh.py
│   │
│   ├── ppqt/                       🔲 stubbed
│   │   ├── config.py
│   │   └── pipeline/refresh.py
│   │
│   └── ipk/                        🔲 stubbed
│       ├── config.py
│       └── pipeline/refresh.py
│
├── data/
│   ├── raw/                        # SMH files, uploaded Excel inputs
│   ├── mart/                       # computed parquet per module
│   │   ├── ole/
│   │   ├── cycle_time/
│   │   └── (ipk/, ppqt/, lbr/ created when built)
│   └── operational.db              # SQLite — downtime_logs, transfer_logs
│
└── scripts/
    ├── diagnose.py
    └── setup_scheduled_tasks.ps1
```

---

## Module Status

| Module | Status | Spec |
|---|---|---|
| OLE | ✅ Live | docs/OLE_BUILD.md |
| Cycle Time | ✅ Live | docs/CYCLE_TIME_BUILD.md |
| Downtime | ✅ Live | (router-only — operational SQLite, no modules/ folder) |
| Transfers | ✅ Live | (router-only — operational SQLite, no modules/ folder) |
| PPQT | 🔲 Stubbed — full pipeline pending | (FE has PPQT_BUILD.md; BE spec to be created when built) |
| IPK | 🔲 Stubbed — full pipeline pending | docs/IPK_BUILD.md |
| LBR | 🔲 Stubbed — full pipeline pending | — (BE spec to be created when built) |

For module-specific business logic, formulas, API endpoints, and parquet
schemas — always read `docs/<MODULE>_BUILD.md`. The root CLAUDE.md only carries
platform-wide context.

---

## Module Pattern

Every BE module follows the same recipe. To add a new module:

### File Structure

```
modules/<module>/
├── config.py                       # mart paths, constants, workcell settings
├── pipeline/
│   ├── refresh.py                  # ENTRY POINT — called by router via BackgroundTasks
│   ├── ingest.py                   # pull/parse raw data
│   ├── compute.py                  # run calculations
│   └── export.py                   # write to parquet mart
└── engine/                         # OPTIONAL — for calculation-heavy modules (e.g. IPK)
    ├── __init__.py
    └── *.py                        # pure calculation functions, zero I/O

api/routers/<module>.py             # FastAPI router, prefix /api/<module>
```

### Router Pattern (api/routers/<module>.py)

Routers are thin. Logic lives in `modules/`. Standard endpoints:

```python
GET  /api/<module>/health           # always include — checks mart file existence
POST /api/<module>/refresh          # triggers pipeline as BackgroundTask
GET  /api/<module>/data             # serves mart data to frontend
```

The router calls `from modules.<module>.pipeline.refresh import run` — that's
the only coupling between router and pipeline.

### Registration (api/main.py)

```python
from api.routers.<module> import router as <module>_router
app.include_router(<module>_router)
```

### Steps To Add A New Module

1. Create `modules/<module>/` with `config.py` and `pipeline/refresh.py`
2. Create `api/routers/<module>.py` with prefix `/api/<module>`
3. Register router in `api/main.py`
4. Add parquet output directory under `data/mart/<module>/`
5. Build the pipeline: `ingest.py` → `compute.py` → `export.py` → `refresh.py`
6. Create `docs/<MODULE>_BUILD.md` for module documentation
7. Update this CLAUDE.md status table

---

## Storage Conventions

| Data Type | Storage |
|---|---|
| Raw uploaded files (Excel, CSV) | `data/raw/` |
| Computed analytical data | Parquet under `data/mart/<module>/` |
| User-entered operational data | SQLite (`core/database.py`) — only for downtime, transfers |
| External API state (e.g. last_run_date) | JSON file in module mart folder |

**Rule:** Computed data → parquet. NEVER SQLite. SQLite is reserved for
user-entered operational data only.

---

## Pipeline Conventions

- Background tasks via FastAPI BackgroundTasks (see OLE router pattern)
- Pipeline entry point is always `modules/<m>/pipeline/refresh.py::run(mode)`
- Mode parameter is typically `'incremental' | 'full'`
- Pipeline functions log to standard logger — no print statements
- Pipeline failures should log + return False — do not crash the API
- All endpoints under `/api/<module>/`
- Always include `GET /api/<module>/health` endpoint

---

## Deployment Notes (Windows + nginx)

- Server: `mypenm0iesvr02.corp.jabil.org`, port 443, nginx in front
- FastAPI on port 9007
- App served at `/ietools/<module>/`, API proxied at `/ietools/<module>/api/`
- Vite proxy (`/ole-api`) is dev-only. Nginx proxy is production-only.
  Never conflate the two.
- Before assuming a clean nginx restart: `sc stop nginx`, then
  `tasklist | findstr nginx`, then kill stale workers in Task Manager.
  Stale workers intercept requests silently.

---

## The Docs System

Two kinds of markdown files exist in this repo:

### Permanent — `docs/<MODULE>_BUILD.md`

Read on every Claude Code session about that module. Contains:
- What the module is, business logic, formulas
- All variables
- Pipeline structure
- API endpoints
- Parquet schemas
- Current status, known gaps

### Temporary — `<MODULE>_TAKEOFF.md` (at repo root, when needed)

Read ONCE during initial scaffolding. Contains step-by-step build instructions,
exact formulas in Python, file-by-file specs. **Delete after build is complete.**

When working on a module:
1. Read `CLAUDE.md` (this file) for platform context
2. Read `docs/<MODULE>_BUILD.md` for the module
3. If a `<MODULE>_TAKEOFF.md` exists at root, follow its build order
4. Otherwise read existing module files and match the pattern

---

## Hard Rules

- Routers stay thin — business logic lives in `modules/`, never in routers
- Computed data → parquet mart (never SQLite)
- SQLite → user-entered operational data only
- No cross-module imports (`modules/ole/` never imports from `modules/ipk/`)
- `core/` is shared — never add module-specific logic to it
- Every module has its own `config.py` for paths and constants
- Background tasks via FastAPI BackgroundTasks
- All endpoints under `/api/<module>/` prefix
- Always include `/api/<module>/health` endpoint
- Date columns returned as ISO timestamps — frontend handles normalisation

---

## Quick Reference

| Need to | Read |
|---|---|
| Understand the platform | This file |
| Build a new module | This file (section 5) + an existing module |
| Work on an existing module | `docs/<MODULE>_BUILD.md` |
| Scaffold an unbuilt module | `<MODULE>_TAKEOFF.md` (if it exists) |
| Find platform conventions | This file (sections 5, 6, 7) |
| Find the frontend | `C:\Users\4033375\Projects\PRODUCTION DASHBOARD\IE-Pulse\` |
