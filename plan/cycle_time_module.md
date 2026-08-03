# Cycle Time Module — Project Documentation

> **Site:** Penang (PEN) | **siteId:** 4 | **Source:** IEDB3.0 API
> **Author:** Solo (Rambo style)
> **Status:** Pipeline built, awaiting Bearer token to run first pull

---

## 1. Background & Goal

The OLE Analyzer backend (`IE-Pulse-Backend`) already handles the OLE module — tracking
Overall Labour Effectiveness via MES production CSVs and eTMS paid hours files.

This initiative adds a **second module: Cycle Time**, which pulls standard process
time data from the Jabil internal IEDB3.0 API. The goal is to:

- Extract **cycle time per process step** for all active assemblies in Penang
- Store it in a queryable Parquet format
- Expose it via the existing FastAPI backend
- Use it for **analysis and reporting** (frontend display is a future phase)

---

## 2. API Discovery

### 2.1 Swagger Page
- **URL:** `https://iedb2api-prd.jblapps.com/swagger/index.html`
- **Spec (JSON):** `https://iedb2api-prd.jblapps.com/swagger/v1/swagger.json`
- **API Title:** IEDB3.0 API v2

### 2.2 Authentication
- **Method:** Okta OAuth2 — Implicit Flow
- **Authorization URL:** `https://jabil.okta.com/oauth2/default/v1/authorize`
- **Client ID:** `0oaujiuao1mblPvOh2p7`
- **Scopes:** `profile`, `email`, `openid`
- **Token Lifespan:** ~1 hour (expires, must be refreshed)
- **Current Approach:** Paste Bearer token manually from browser DevTools into `.env`
- **Future Plan:** Automate token refresh via Playwright (browser automation)

### 2.3 Key API Endpoints Identified

| Endpoint | Purpose |
|---|---|
| `GET /api/DataSync/GetAllSites` | List all sites (used to confirm PEN = siteId 4) |
| `GET /api/Customers/all` | List all customers for a site |
| `GET /api/rawdataapi/v3/GetDetailRawProcessData` | **Primary endpoint — cycle time data** |
| `GET /api/rawdataapi/GetSummaryGroupProcessData` | Summary grouped process data |
| `GET /api/Report/GetGRPSummaryReport` | GRP summary (UPH, cycle, HC) |
| `GET /api/Report/GetGRPAllReport` | All GRP data per assembly |
| `GET /api/mpt/v2/StandardDetails` | Standard details (UPH, HC) |
| `GET /api/grp-standard/ppqt` | PPQT group standards per process |
| `GET /api/Assemblies` | List assemblies by site + customer |

### 2.4 The Primary Endpoint — `GetDetailRawProcessData`

```
GET /api/rawdataapi/v3/GetDetailRawProcessData
```

**Required params:**

| Param | Type | Required | Notes |
|---|---|---|---|
| `SiteCode` | string | ✅ | Use `PEN` for Penang |
| `Customer` | string | ✅ | e.g. `ASP`, `ARISTANETWORKS` |
| `Division` | string | No | e.g. `ASP*` |
| `Assemblies` | string[] | No | Filter to specific assemblies |
| `Revisions` | string[] | No | Filter to specific revisions |
| `Workcenters` | string[] | No | e.g. `SMT` |
| `SubWorkcenters` | string[] | No | e.g. `ASNW SMT P8-4 B831` |
| `BeginDate` | datetime | No | For incremental pulls (filters by `updatedOn`) |
| `EndDate` | datetime | No | |
| `PageNumber` | int | No | Pagination |
| `PageSize` | int | No | Max records per page (we use 500) |

**Response schema (`DetailRawProcessDataV3`) — key fields:**

| API Field (camelCase) | Stored As (snake_case) | Description |
|---|---|---|
| `assembly` | `assembly` | Part/assembly number |
| `revision` | `revision` | Revision code |
| `family` | `family` | Product family |
| `workcenter` | `workcenter` | Manufacturing area (e.g. SMT) |
| `workcenterType` | `workcenter_type` | Type of workcenter |
| `subWorkcenter` | `sub_workcenter` | Specific line (e.g. ASNW SMT P8-4 B831) |
| `process` | `process` | Process step name (BIRTH, SCRB, GLUEB, …) |
| `alias` | `alias` | Process alias |
| `cycleTimePerProcess` | `cycle_time_per_process` | **The time value — this is what we want** |
| `lct` | `lct` | Labour cycle time |
| `mach` | `mach` | Machine time |
| `hc` | `hc` | Head count |
| `grp` | `grp` | GRP grouping |
| `playbook` | `playbook` | Playbook reference |
| `updatedOn` | `updated_on` | Last update timestamp — used for incremental pulls |
| `totalCount` | `total_count` | Total records available — used for pagination |

### 2.5 Data Shape — Raw vs Pivoted

The API returns **one row per process step per assembly**. The target table
(Image 2) shows **one row per assembly** with each process step as a column.

**Raw (from API):**
```
assembly      | revision | sub_workcenter      | process | cycle_time_per_process
ASPCA-01133-03 | B0       | ASNW SMT P8-4 B831 | BIRTH   | 24.34
ASPCA-01133-03 | B0       | ASNW SMT P8-4 B831 | SCRB    | 30.1
ASPCA-01133-03 | B0       | ASNW SMT P8-4 B831 | GLUEB   | 11.01
...
```

**Pivoted (target — Image 2 layout):**
```
assembly      | revision | family            | workcenter | sub_workcenter      | BIRTH | SCRB  | GLUEB | SPIB  | SMTB  | ...
ASPCA-01133-03 | B0       | GLACIER/GRINNELL-D | SMT       | ASNW SMT P8-4 B831 | 24.34 | 30.1  | 11.01 | 20.15 | 16.86 | ...
```

---

## 3. Penang Customer Data

### 3.1 Site Info

| Field | Value |
|---|---|
| SiteId | 4 |
| SiteName | Penang |
| SiteCode | PEN |
| Sector | EMS |
| TimeZone | UTC +08:00 |

### 3.2 Customer Summary

- **Total entries in system:** 158
- **Inactive (IsActive=2):** 115 — excluded
- **Active with 0 assemblies:** 2 — excluded
- **Active with assemblies (our scope):** **41 customers**
- **Total assemblies across all 41:** **143,929**

### 3.3 Active Customers — Full List

| Customer | Division | CustomerId | Assemblies |
|---|---|---|---|
| KEYSIGHT | KEYSIGHT* | 355 | 59,539 |
| ARISTANETWORKS | ARISTANETWORKS* | 374 | 15,151 |
| Tellabs | Tellabs* | 362 | 13,618 |
| INFINERA | INFINERA* | 367 | 9,926 |
| LAMRESEARCH | LAMRESEARCH* | 370 | 9,502 |
| K_CTEC | K_CTEC* | 398 | 9,101 |
| LTX | LTX* | 366 | 7,228 |
| BECKMAN COULTER | BECKMAN COULTER* | 391 | 2,554 |
| DYSON | DYSON* | 881 | 2,199 |
| MICRON SIG | MICRON SIG* | 128 | 2,068 |
| Motorola | Mobile Devices* | 1225 | 1,864 |
| TMO | TMO* | 376 | 1,796 |
| FORTALEZA | SEMICAP* | 2448 | 1,596 |
| Masimo | Masimo* | 371 | 1,084 |
| ARISTA_NETWORKS_GLACIER | ARISTA_NETWORKS_GLACIER* | 403 | 920 |
| BEDFORD | BEDFORD* | 361 | 811 |
| AFC | AFC* | 359 | 731 |
| ASP | ASP* | 373 | 682 |
| WABTEC | WABTEC* | 421 | 579 |
| Nokia Optics | Nokia Optics* | 423 | 516 |
| BD | BECTON, DICKINSON AND COMPANY* | 379 | 389 |
| UTAS | UTAS* | 382 | 303 |
| ILLUMINA | ILLUMINA* | 2463 | 212 |
| INTEL OPTICS | INTEL OPTICS* | 1187 | 197 |
| ResMed | ResMed* | 380 | 192 |
| HMB | HMB* | 2645 | 190 |
| ADVANTEST | ADVANTEST# | 2522 | 183 |
| ELENION TECHNOLOGIES | ELENION TECHNOLOGIES* | 404 | 156 |
| LAMMEC | LAMMEC# | 2505 | 151 |
| AKAMAI | AKAMAI* | 2544 | 144 |
| ADVA | ADVA* | 418 | 114 |
| Medtronic | Medtronic* | 1272 | 73 |
| ENDURANCE | ENDURANCE* | 2642 | 41 |
| LAMGB | LAMGB# | 2523 | 39 |
| AMAT | AMAT* | 1753 | 27 |
| LIFE360 | SVS* | 2701 | 20 |
| TERRA SANA | TERRA SANA* | 2525 | 10 |
| GOPRO | GOPRO* | 2681 | 9 |
| BARCO | Healthcare & Entertainment* | 2708 | 6 |
| Skydio | Mobile Devices* | 2535 | 5 |
| GO | GO* | 2526 | 3 |

> **Scope decision:** Pull ALL 41 active customers. No filtering.

---

## 4. Architecture Decisions

### 4.1 Module Structure Philosophy
The backend is designed to grow. Each new module (OLE, Cycle Time, and future ones)
lives under `modules/` as a self-contained unit. The API router is registered in
`api/main.py` with a single `include_router()` call. No existing OLE code is touched.

### 4.2 Storage Format
- **Parquet** — same as OLE module. Fast, compressed, DuckDB-queryable.
- Two parquet files per module:
  - `raw.parquet` — one row per API record (faithful to source)
  - `pivoted.parquet` — one row per assembly, processes as columns (Image 2 view)

### 4.3 Incremental Updates
- State tracked in `data/mart/cycle_time/.ingest_state.json`
- On each incremental run: `BeginDate` = `last_run_date` from state
- API filters records by `updatedOn >= BeginDate`
- New/updated records are **upserted** into existing raw.parquet
  (primary key: `assembly + revision + sub_workcenter + process + customer`)
- `pivoted.parquet` is always fully re-derived from raw after ingest

### 4.4 Authentication — Current vs Future

| Phase | Method | Status |
|---|---|---|
| **Now** | Paste Bearer token into `.env` file manually | ✅ Implemented |
| **Later** | Auto-refresh via Playwright (headless browser login) | 🔲 Planned |
| **Future** | Persistent token from IT (Okta service account / client credentials) | 🔲 Pending IT |

---

## 5. What Was Built

### 5.1 New Files Created

```
IE-Pulse-Backend/
├── modules/
│   ├── __init__.py
│   └── cycle_time/
│       ├── __init__.py
│       ├── config.py                  ← API settings + all 41 customers
│       ├── client.py                  ← HTTP client (auth, pagination, error handling)
│       └── pipeline/
│           ├── __init__.py
│           ├── ingest.py              ← fetch API → raw.parquet (with upsert)
│           ├── transform.py           ← pivot raw → pivoted.parquet
│           └── refresh.py             ← orchestrator (ingest + transform)
│
├── api/
│   └── routers/
│       ├── __init__.py
│       └── cycle_time.py              ← FastAPI router (5 endpoints)
│
├── data/mart/cycle_time/              ← output folder (parquets written here)
│
└── .env.example                       ← token config template
```

### 5.2 Modified Files

| File | Change |
|---|---|
| `api/main.py` | +2 lines: import router + `app.include_router(cycle_time_router)` |
| `requirements.txt` | Added `requests==2.32.3` and `python-dotenv==1.0.1` |

### 5.3 New API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/cycle-time/health` | Parquet status + row counts |
| `POST` | `/api/cycle-time/refresh?mode=incremental\|full` | Trigger pipeline |
| `GET` | `/api/cycle-time/customers` | List all 41 configured customers |
| `GET` | `/api/cycle-time/data` | **Pivoted data — Image 2 layout** |
| `GET` | `/api/cycle-time/raw` | Raw row-per-process data (paginated) |

#### `/api/cycle-time/data` — Query Params

| Param | Description |
|---|---|
| `customer` | Filter by customer name (exact) |
| `assembly` | Filter by assembly number (partial match) |
| `revision` | Filter by revision (exact) |
| `workcenter` | Filter by workcenter (e.g. `SMT`) |
| `sub_workcenter` | Filter by sub-workcenter (e.g. `ASNW SMT P8-4 B831`) |
| `family` | Filter by product family (partial match) |

---

## 6. How to Run

### 6.1 First-Time Setup

```bash
# Install new dependencies
pip install requests==2.32.3 python-dotenv==1.0.1

# Create your .env file from the template
copy .env.example .env
```

Edit `.env` and paste your Bearer token:
```
IEDB_BEARER_TOKEN=eyJ...your_token_here...
```

**How to get the token:**
1. Open `https://iedb2api-prd.jblapps.com/swagger/index.html`
2. Log in via Okta SSO (Authorize button)
3. Open DevTools (`F12`) → Network tab
4. Click "Execute" on any endpoint in Swagger
5. Click the request in Network → copy the `Authorization` header value
6. Paste into `.env` (without the `Bearer ` prefix)

### 6.2 Run Pipeline

```bash
# Full pull — first time or reset
python -m modules.cycle_time.pipeline.refresh --full

# Incremental — only new/updated records since last run
python -m modules.cycle_time.pipeline.refresh --incremental

# Or trigger via API (server must be running)
curl -X POST "http://localhost:8000/api/cycle-time/refresh?mode=full"
```

### 6.3 Check Results

```bash
# Via API
curl http://localhost:8000/api/cycle-time/health
curl "http://localhost:8000/api/cycle-time/data?customer=ASP"

# Direct parquet inspection (Python)
import pandas as pd
df = pd.read_parquet("data/mart/cycle_time/pivoted.parquet")
print(df.head())
print(df.columns.tolist())
```

---

## 7. Pending / Known Unknowns

| Item | Notes |
|---|---|
| **Live API response shape** | We mapped from swagger.json schema. Actual field names in responses need verification on first real pull. |
| **Process column names** | Exact process step names (BIRTH, SCRB, GLUEB, etc.) need confirmation from live data — pivot column headers depend on these. |
| **BeginDate filter behaviour** | Assumed to filter by `updatedOn`. Needs verification on first incremental run. |
| **totalCount reliability** | Used for pagination. If absent in response, falls back to empty-page detection. |
| **userId=1 parameter** | Works for `/api/Customers/all` — not fully understood. Kept as-is for now. |
| **Token expiry handling** | Currently manual. Auto-refresh via Playwright to be added later. |
| **IT persistent token** | May become available — would replace the manual token flow entirely. |

---

## 8. Roadmap

### Phase 1 — Verify & First Pull ✅ Ready to execute
- [ ] Paste Bearer token into `.env`
- [ ] Run `--full` pipeline
- [ ] Verify raw.parquet shape matches expected schema
- [ ] Verify pivoted.parquet columns match Image 2 (BIRTH, SCRB, GLUEB, etc.)
- [ ] Fix any field name mismatches

### Phase 2 — Stabilise ETL
- [ ] Schedule incremental runs (Windows Task Scheduler or cron)
- [ ] Add retry logic for failed customers in `client.py`
- [ ] Add alerting/logging for token expiry (catch 401, surface clearly)
- [ ] Validate data quality (null rates per process column, row counts per customer)

### Phase 3 — Auth Automation
- [ ] Implement Playwright-based token auto-refresh
- [ ] Run as a background task, inject fresh token before each pipeline run
- [ ] OR obtain persistent client credentials from IT (preferred long-term)

### Phase 4 — Frontend
- [ ] Connect frontend to `/api/cycle-time/data`
- [ ] Build cycle time table view matching Image 2
- [ ] Add filters: customer, workcenter, sub-workcenter, assembly search
- [ ] Add analysis views: process bottleneck, cycle time trends, comparison

### Phase 5 — Future Modules
- Repeat the `modules/<module_name>/` pattern for each new data source
- Add router to `api/main.py` with a single `include_router()` line
- Each module is fully independent — no cross-module dependencies

---

## 9. Module Comparison — OLE vs Cycle Time

| Aspect | OLE Module | Cycle Time Module |
|---|---|---|
| **Data source** | Network share CSVs (`\\penhomev10\OLE`) | IEDB3.0 REST API |
| **Auth** | None (network share) | Okta Bearer token |
| **Ingest trigger** | File date watermark | API `BeginDate` param |
| **Raw storage** | `data/mart/raw_production.parquet` | `data/mart/cycle_time/raw.parquet` |
| **Computed storage** | `data/mart/ole_computed.parquet` | `data/mart/cycle_time/pivoted.parquet` |
| **State tracking** | `data/mart/.ingest_state.json` | `data/mart/cycle_time/.ingest_state.json` |
| **API prefix** | `/api/ole/...` | `/api/cycle-time/...` |
| **Pipeline entry** | `pipeline/refresh.py` | `modules/cycle_time/pipeline/refresh.py` |

---

*Last updated: May 2026*
