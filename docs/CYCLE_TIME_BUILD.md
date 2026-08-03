# Cycle Time Module — Backend Build Spec

> **IE Pulse | Jabil Penang**
> **Status:** ✅ BE Live (pipeline built — first pull pending Bearer token)
> **Last updated:** June 2026

---

## 1. Module Context

### 1a. What This Module Is

Pulls **standard cycle time per process step** for all 41 active Penang customers
from the IEDB3.0 internal API. Stores as parquet. Exposes via FastAPI for the
IE Pulse frontend.

- **Site:** Penang | SiteId: 4 | SiteCode: `PEN` | UTC+08:00
- **Data scope:** 41 active customers, 143,929+ assemblies
- **Source system:** IEDB3.0 API (`iedb2api-prd.jblapps.com`) — Okta Bearer token auth

### 1b. How It Fits Into IE Pulse Backend

| Aspect                   | Detail                                                          |
| ------------------------ | --------------------------------------------------------------- |
| **Module pattern**       | Follows `modules/<module>/pipeline/` pattern established by OLE |
| **Router registration**  | `api/main.py` — `app.include_router(cycle_time_router)`         |
| **No cross-module deps** | Fully independent; no imports from OLE or other modules         |
| **Storage**              | Parquet mart in `data/mart/cycle_time/` — same as OLE           |
| **SQLite**               | Not used — no user-entered data in this module                  |

---

## 2. Backend Files — What Was Built

```
IE-Pulse-Backend/
├── modules/
│   ├── __init__.py
│   └── cycle_time/
│       ├── __init__.py
│       ├── config.py                  ← IEDB API settings + all 41 customers + mart paths
│       ├── client.py                  ← HTTP client: auth headers, pagination, 401/403 handling
│       └── pipeline/
│           ├── __init__.py
│           ├── ingest.py              ← fetch API → raw.parquet (upsert on PK)
│           ├── transform.py           ← pivot raw.parquet → pivoted.parquet
│           └── refresh.py             ← orchestrator: ingest + transform
│
├── api/
│   └── routers/
│       ├── __init__.py
│       └── cycle_time.py              ← FastAPI router, 5 endpoints, prefix /api/cycle-time
│
├── data/mart/cycle_time/              ← created on first run
│   ├── raw.parquet                    ← (created after first run)
│   ├── pivoted.parquet                ← (created after first run)
│   └── .ingest_state.json             ← tracks last_run_date for incremental
│
└── .env.example                       ← IEDB_BEARER_TOKEN=PASTE_YOUR_TOKEN_HERE
```

### Modified Files

| File               | Change                                                                                                               |
| ------------------ | -------------------------------------------------------------------------------------------------------------------- |
| `api/main.py`      | +2 lines: `from api.routers.cycle_time import router as cycle_time_router` + `app.include_router(cycle_time_router)` |
| `requirements.txt` | Added `requests==2.32.3`, `python-dotenv==1.0.1`                                                                     |

---

## 3. API Discovery — IEDB3.0

### Authentication

| Field             | Value                                                                           |
| ----------------- | ------------------------------------------------------------------------------- |
| Method            | Okta OAuth2 Implicit Flow                                                       |
| Authorization URL | `https://jabil.okta.com/oauth2/default/v1/authorize`                            |
| Client ID         | `0oaujiuao1mblPvOh2p7`                                                          |
| Scopes            | `profile`, `email`, `openid`                                                    |
| Token lifespan    | ~1 hour                                                                         |
| Current approach  | Paste Bearer token into `.env` as `IEDB_BEARER_TOKEN`                           |
| Future            | Playwright auto-refresh (planned) or persistent IT client credentials (pending) |

**How to get token:**

1. Open `https://iedb2api-prd.jblapps.com/swagger/index.html`
2. Log in via Okta SSO
3. F12 → Network → Execute any endpoint → copy `Authorization` header value (without `Bearer ` prefix)
4. Paste into `.env`

### Primary Endpoint

```
GET /api/rawdataapi/v3/GetDetailRawProcessData
```

| Param            | Required | Type     | Notes                                                    |
| ---------------- | -------- | -------- | -------------------------------------------------------- |
| `SiteCode`       | ✅       | string   | Always `PEN` for Penang                                  |
| `Customer`       | ✅       | string   | e.g. `ASP`, `ARISTANETWORKS`                             |
| `Division`       | No       | string   | e.g. `ASP*` — passed from customer config                |
| `Assemblies`     | No       | string[] | Filter by part numbers (not used in bulk pull)           |
| `Revisions`      | No       | string[] | Filter by revision (not used in bulk pull)               |
| `Workcenters`    | No       | string[] | e.g. `SMT`                                               |
| `SubWorkcenters` | No       | string[] | Specific line                                            |
| `BeginDate`      | No       | datetime | Used for incremental: filter by `updatedOn >= BeginDate` |
| `EndDate`        | No       | datetime | Not used currently                                       |
| `PageNumber`     | No       | int      | Pagination: 1-based                                      |
| `PageSize`       | No       | int      | Records per page — default 500 in config                 |

**Response row shape:**

```json
{
  "assembly": "ASPCA-01133-03",
  "revision": "02",
  "family": "GLACIER/GRINNELL-D",
  "workcenter": "SMT",
  "subWorkcenter": "ASNW SMT P8-4 B831",
  "process": "BIRTH",
  "cycleTimePerProcess": 12.5,
  "customer": "ASP",
  "updatedOn": "2024-11-15T08:30:00",
  "totalCount": 4820
}
```

### Other Useful Endpoints

| Endpoint                                   | Purpose                                           |
| ------------------------------------------ | ------------------------------------------------- |
| `GET /api/Customers/all?userId=1&siteId=4` | List all customers (use to refresh customer list) |
| `GET /api/DataSync/GetAllSites`            | Confirm site codes                                |
| `GET /api/Assemblies`                      | List assemblies by site+customer                  |
| `GET /api/grp-standard/ppqt`               | PPQT group standards per process                  |
| `GET /api/Report/GetGRPSummaryReport`      | UPH, cycle, HC summary                            |
| `GET /api/mpt/v2/StandardDetails`          | Standard times (UPH, HC)                          |

---

## 4. Pipeline Details

### 4a. config.py

```python
BASE_URL    = "https://iedb2api-prd.jblapps.com"
SITE_CODE   = "PEN"
PAGE_SIZE   = 500
API_TIMEOUT = 30  # seconds

# Mart paths
CT_MART_DIR   = PROJECT_ROOT / "data" / "mart" / "cycle_time"
CT_MART = {
    "raw":     CT_MART_DIR / "raw.parquet",
    "pivoted": CT_MART_DIR / "pivoted.parquet",
}
CT_STATE_FILE = CT_MART_DIR / ".ingest_state.json"

# All 41 active Penang customers (from /api/Customers/all?userId=1&siteId=4)
CT_CUSTOMERS = [
    {"customer": "KEYSIGHT",       "division": "KEYSIGHT*",       "customer_id": 355,  "assembly_count": 59539},
    {"customer": "ARISTANETWORKS", "division": "ARISTANETWORKS*", "customer_id": 374,  "assembly_count": 15151},
    # ... all 41 — full list in modules/cycle_time/config.py
]
```

### 4b. client.py — Fetch Logic

```python
def fetch_page(customer, division, page, page_size, begin_date=None) -> list[dict]:
    # Load IEDB_BEARER_TOKEN from .env
    # GET /api/rawdataapi/v3/GetDetailRawProcessData
    # Raise PermissionError on 401 (token expired), PermissionError on 403
    # Return [] if empty response

def fetch_all_pages(customer, division, page_size, begin_date=None) -> list[dict]:
    # Paginate until:
    #   - totalCount (from first record) is reached, OR
    #   - page is not full (fallback empty-page detection)
```

### 4c. ingest.py — Raw Parquet

**Full mode:**

- No `BeginDate` filter
- Fetch all pages for all 41 customers
- Normalise camelCase → snake_case
- Overwrite `raw.parquet`

**Incremental mode:**

- Load `last_run_date` from `.ingest_state.json`
- Set `BeginDate = last_run_date`
- Fetch only records where `updated_on >= last_run_date`
- Upsert into existing `raw.parquet` on PK: `(assembly, revision, sub_workcenter, process, customer)`
- Write new `last_run_date` to state file

**raw.parquet schema:**

```
assembly              string
revision              string
family                string
workcenter            string
sub_workcenter        string
process               string
cycle_time_per_process float
customer              string
division              string
updated_on            datetime
```

### 4d. transform.py — Pivoted Parquet

```python
pivot = raw_df.pivot_table(
    index=['assembly', 'revision', 'family', 'workcenter', 'sub_workcenter', 'customer'],
    columns='process',
    values='cycle_time_per_process',
    aggfunc='first'   # each (assembly, process) should be unique
).reset_index()
pivot.columns.name = None
pivot.to_parquet(CT_MART['pivoted'], index=False)
```

Result: one row per `(assembly, revision, sub_workcenter, customer)`, one column per process name.

### 4e. refresh.py — Orchestrator

```bash
python -m modules.cycle_time.pipeline.refresh             # incremental (default)
python -m modules.cycle_time.pipeline.refresh --full      # full re-fetch
python -m modules.cycle_time.pipeline.refresh --incremental  # explicit
```

Steps:

1. `ingest.run(mode)` → raw.parquet
2. `transform.run()` → pivoted.parquet

---

## 5. API Endpoints

| Method | Endpoint                                         | Description                                |
| ------ | ------------------------------------------------ | ------------------------------------------ |
| `GET`  | `/api/cycle-time/health`                         | Parquet file status + row counts           |
| `POST` | `/api/cycle-time/refresh?mode=incremental\|full` | Trigger pipeline via API                   |
| `GET`  | `/api/cycle-time/customers`                      | All 41 configured customers                |
| `GET`  | `/api/cycle-time/data`                           | Pivoted data (Image 2 layout) with filters |
| `GET`  | `/api/cycle-time/raw`                            | Raw row-per-process data, paginated        |

### /api/cycle-time/data — Filter Params

| Param            | Match       | Description                      |
| ---------------- | ----------- | -------------------------------- |
| `customer`       | Exact       | Filter by customer name          |
| `assembly`       | ILIKE `%x%` | Partial match on assembly number |
| `revision`       | Exact       | Filter by revision               |
| `workcenter`     | Exact       | e.g. `SMT`                       |
| `sub_workcenter` | Exact       | e.g. `ASNW SMT P8-4 B831`        |
| `family`         | ILIKE `%x%` | Partial match on family          |

### /api/cycle-time/raw — Filter Params

Same as above, plus:

| Param       | Match | Description                                |
| ----------- | ----- | ------------------------------------------ |
| `process`   | Exact | Filter by process name e.g. `BIRTH`        |
| `page`      | —     | Page number (default: 1)                   |
| `page_size` | —     | Records per page (default: 500, max: 2000) |

---

## 6. Penang Customer List

41 active customers (IsActive=1, assembly_count > 0):

| Customer                | Division                        | CustomerId | Assemblies |
| ----------------------- | ------------------------------- | ---------- | ---------- |
| KEYSIGHT                | KEYSIGHT\*                      | 355        | 59,539     |
| ARISTANETWORKS          | ARISTANETWORKS\*                | 374        | 15,151     |
| Tellabs                 | Tellabs\*                       | 362        | 13,618     |
| INFINERA                | INFINERA\*                      | 367        | 9,926      |
| LAMRESEARCH             | LAMRESEARCH\*                   | 370        | 9,502      |
| K_CTEC                  | K_CTEC\*                        | 398        | 9,101      |
| LTX                     | LTX\*                           | 366        | 7,228      |
| BECKMAN COULTER         | BECKMAN COULTER\*               | 391        | 2,554      |
| DYSON                   | DYSON\*                         | 881        | 2,199      |
| MICRON SIG              | MICRON SIG\*                    | 128        | 2,068      |
| Motorola                | Mobile Devices\*                | 1225       | 1,864      |
| TMO                     | TMO\*                           | 376        | 1,796      |
| FORTALEZA               | SEMICAP\*                       | 2448       | 1,596      |
| Masimo                  | Masimo\*                        | 371        | 1,084      |
| ARISTA_NETWORKS_GLACIER | ARISTA_NETWORKS_GLACIER\*       | 403        | 920        |
| BEDFORD                 | BEDFORD\*                       | 361        | 811        |
| AFC                     | AFC\*                           | 359        | 731        |
| ASP                     | ASP\*                           | 373        | 682        |
| WABTEC                  | WABTEC\*                        | 421        | 579        |
| Nokia Optics            | Nokia Optics\*                  | 423        | 516        |
| BD                      | BECTON, DICKINSON AND COMPANY\* | 379        | 389        |
| UTAS                    | UTAS\*                          | 382        | 303        |
| ILLUMINA                | ILLUMINA\*                      | 2463       | 212        |
| INTEL OPTICS            | INTEL OPTICS\*                  | 1187       | 197        |
| ResMed                  | ResMed\*                        | 380        | 192        |
| HMB                     | HMB\*                           | 2645       | 190        |
| ADVANTEST               | ADVANTEST#                      | 2522       | 183        |
| ELENION TECHNOLOGIES    | ELENION TECHNOLOGIES\*          | 404        | 156        |
| LAMMEC                  | LAMMEC#                         | 2505       | 151        |
| AKAMAI                  | AKAMAI\*                        | 2544       | 144        |
| ADVA                    | ADVA\*                          | 418        | 114        |
| Medtronic               | Medtronic\*                     | 1272       | 73         |
| ENDURANCE               | ENDURANCE\*                     | 2642       | 41         |
| LAMGB                   | LAMGB#                          | 2523       | 39         |
| AMAT                    | AMAT\*                          | 1753       | 27         |
| LIFE360                 | SVS\*                           | 2701       | 20         |
| TERRA SANA              | TERRA SANA\*                    | 2525       | 10         |
| GOPRO                   | GOPRO\*                         | 2681       | 9          |
| BARCO                   | Healthcare & Entertainment\*    | 2708       | 6          |
| Skydio                  | Mobile Devices\*                | 2535       | 5          |
| GO                      | GO\*                            | 2526       | 3          |

> Full list with IDs also in `modules/cycle_time/config.py`.

---

## 7. Pending / Known Unknowns

| Item                                    | Status        | Notes                                                                      |
| --------------------------------------- | ------------- | -------------------------------------------------------------------------- |
| **First live pull**                     | ⬜ Pending    | Needs Bearer token in `.env`                                               |
| **Live response shape verification**    | ⬜ Pending    | Schema mapped from swagger.json — confirm field names match real responses |
| **Process column names from live data** | ⬜ Pending    | BIRTH, SCRB etc. assumed from Image 2 — confirm exact names                |
| **BeginDate filter behaviour**          | ⬜ Pending    | Assumed to filter by `updatedOn` — confirm on first incremental run        |
| **totalCount reliability**              | ⬜ Pending    | Pagination fallback in place; confirm totalCount is present in responses   |
| **userId=1 param**                      | ⚠️ Unclear    | Works for `/api/Customers/all` — not fully understood, keep as-is          |
| **Token auto-refresh**                  | 🔲 Planned    | Playwright-based headless login — deferred                                 |
| **IT persistent token**                 | 🔲 Pending IT | Okta service account / client credentials — follow up with IT              |
| **Large customer pagination**           | ⚠️ Risk       | KEYSIGHT has 59k assemblies — memory and time tested on first full pull    |

---

## 8. How to Run

```bash
# 1. Install deps (if not done)
pip install requests==2.32.3 python-dotenv==1.0.1

# 2. Create .env from template
copy .env.example .env
# Edit: IEDB_BEARER_TOKEN=eyJ...your_token...

# 3. First full pull
python -m modules.cycle_time.pipeline.refresh --full

# 4. Check results
curl http://localhost:8000/api/cycle-time/health
curl "http://localhost:8000/api/cycle-time/data?customer=ASP"

# 5. Subsequent runs (incremental)
python -m modules.cycle_time.pipeline.refresh --incremental
```

---

## 9. Module vs OLE Comparison

| Aspect               | OLE Module                              | Cycle Time Module                         |
| -------------------- | --------------------------------------- | ----------------------------------------- |
| **Data source**      | Network share CSVs (`\\penhomev10\OLE`) | IEDB3.0 REST API                          |
| **Auth**             | None (network share)                    | Okta Bearer token                         |
| **Ingest trigger**   | File date watermark                     | API `BeginDate` param                     |
| **Raw storage**      | `data/mart/raw_production.parquet`      | `data/mart/cycle_time/raw.parquet`        |
| **Computed storage** | `data/mart/ole_computed.parquet`        | `data/mart/cycle_time/pivoted.parquet`    |
| **State tracking**   | `data/mart/.ingest_state.json`          | `data/mart/cycle_time/.ingest_state.json` |
| **API prefix**       | `/api/ole/...`                          | `/api/cycle-time/...`                     |
| **Pipeline entry**   | `pipeline/refresh.py`                   | `modules/cycle_time/pipeline/refresh.py`  |
| **SQLite**           | Yes (downtime, transfers)               | No                                        |

---

_Last updated: June 2026_
