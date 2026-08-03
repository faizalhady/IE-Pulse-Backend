# OLE Module - Build & Context Reference

> Part of **IE Pulse**, Jabil Penang's unified Industrial Engineering platform.
> This document is the single source of truth for the **OLE (Overall Labor Effectiveness)** module.
> It is saved to both repos: `IE-Pulse/docs/OLE_BUILD.md` and `IE-Pulse-Backend/docs/OLE_BUILD.md`.

**Module:** OLE (Overall Labor Effectiveness)
**Frontend status:** Live
**Backend status:** Live
**Owner:** IE team, Jabil Penang
**OLE target:** 61%

---

## 1. Module Context Document

### a. What this module is

OLE measures how effectively direct labor hours are turned into useful output. In plain terms: for every paid hour a direct worker is on the floor, how much standard work actually got produced.

- **Business problem:** Plants pay for direct labor hours. Some of those hours produce sellable units, some are lost to idle time, line imbalance, support tasks, or low yield. OLE puts a single percentage on how well paid labor converts into earned standard output, so the IE team and managers can see where labor is being lost.
- **Who uses it:** IE engineers (daily, deep analysis) and operations managers (weekly, high-level review).
- **When they use it:** Weekly performance reviews, workcell-level loss investigation, and quarterly target tracking against the 61% goal.
- **Scope:** Penang site, broken down by plant (Plant 1, Plant 2, Batu Kawan) and by workcell.

Active workcells: AOP1, WABTEC, ARISTA NETWORKS, ARISTA NETWORKS HLA, ASP, KEYSIGHT HLA, UTAS, BECKMAN COULTER, IMED.

> BECKMAN COULTER is temporarily excluded from all data hooks via `TEMP_EXCLUDED_WORKCELLS` in `oleConstants.ts` because of anomalous OLE values caused by incorrect SMH lookup data. It is re-included once the SMH matrix for that workcell is corrected.

### b. How it fits into IE Pulse

OLE was the first module and set the conventions the rest of the platform follows.

| Shared with platform | What is shared |
|---|---|
| Workcell taxonomy | The workcell names and the `_WORKCELL_MAP` cost-center mapping are reused by future labor modules (LBR, DL Sizing). |
| Shift convention | Shifts 1, 2, 3 are the standard everywhere. |
| Paid-hours source | `PEN_PaidHours_Raw_*` (`TPHDirect`) is the common paid-labor input. LBR and DL Sizing will read the same raw source. |
| SMH matrix | Standard Man Hours per unit is an IE-maintained input shared with any module that needs earned standard hours. |
| Date handling | ISO date normalization (`normalizeDates()`) is the shared frontend convention. |
| Design system | Sticky-header dashboards, underline tabs, card patterns, emerald primary line color. Other modules copy these. |

**Dependencies:** OLE depends on MES (production output), the payroll/HR paid-hours extract, and the IE-maintained SMH matrix. It does not depend on any other module's computed output.

### c. All variables

**Raw inputs (production) - `PEN_TotalProduction_*.csv`**

| Field | Type | Plain English |
|---|---|---|
| Site | string | Always `PEN` for Penang. |
| Workcell | string | Workcell name (often blank in raw; resolved by assembly mapping). |
| SubWorkcell | string | Sub-line within a workcell. |
| AssemblyNumber | string | Part/assembly built. Key used to look up SMH per unit. |
| Qty | integer | Units produced. |
| StartDate / EndDate | string (date) | Production day. |
| Shift | integer | 1, 2, or 3. |

**Raw inputs (paid hours) - `PEN_PaidHours_Raw_*.csv`**

| Field | Type | Plain English |
|---|---|---|
| Site | string | Always `PEN`. |
| CostCenter | integer | Cost center code. |
| WorkCell | string | Cost-center code prefixed with `*` (e.g. `*710246`). Mapped to a workcell name via `_WORKCELL_MAP`. |
| SubWorkCell | string | Sub-line code. |
| THCDirect | string | Employee ID. |
| **TPHDirect** | float | **Paid direct hours for that row. This is the OLE denominator input.** |
| Startdate / EndDate | string (date) | Paid day. |
| Shift | integer | 1, 2, or 3. |
| Position | string | Job grade (e.g. SDL I). |
| Name | string | Employee name. |
| Category | string | `M0` or `XA`. |
| DateJoined / DayJoined | string / int | Tenure info (not used in OLE compute). |

**IE-maintained input - SMH matrix**

| Field | Type | Plain English |
|---|---|---|
| AssemblyNumber | string | Join key to production. |
| SMH per unit | float | Standard Man Hours to build one unit. Maintained by IE from the PPT matrix. |

**Computed values**

| Value | Formula | Plain English |
|---|---|---|
| Earned standard hours | `Qty × SMH_per_unit` | Standard work credited for what was produced. |
| Paid hours | `SUM(TPHDirect)` | Total paid direct labor hours. |
| OLE% | `SUM(Qty × SMH_per_unit) / SUM(TPHDirect) × 100` | Effectiveness percentage. |
| Loss categories | `100% - OLE%` split across Paynter categories | Where the non-productive paid hours went. |

### d. Business logic (step by step)

```
1. INGEST production rows from PEN_TotalProduction_*.csv
2. INGEST paid-hours rows from PEN_PaidHours_Raw_*.csv
     - Map WorkCell code (*710246) -> workcell name via _WORKCELL_MAP
     - Map workcell-name variants (e.g. ARISTA PCA -> ARISTA NETWORKS)
     - DO NOT drop_duplicates: every employee row is a valid paid-hours row
     - DO NOT filter by value_type
3. LOOK UP SMH per unit by AssemblyNumber for each production row
4. COMPUTE earned hours      = Qty × SMH_per_unit          (summed)
5. COMPUTE paid hours        = SUM(TPHDirect)               (summed, all rows)
6. COMPUTE OLE%              = earned / paid × 100
7. AGGREGATE by week, workcell, shift, plant, site
8. DERIVE loss breakdown so OLE% + all loss categories = 100%
9. EXPORT to parquet mart
```

**Hard truths about the pipeline (confirmed, do not re-litigate):**

- Input hours = `SUM(tph_direct)` across all rows in `PEN_PaidHours_Raw_*`. No deduplication, no value_type filtering.
- `PEN_TotalPaidHours_*` and `ingest_support_hours()` are dead code paths. They are not used in compute or in any API response.
- `drop_duplicates` on `[workcell, name, date, shift, value_type]` silently drops valid employee rows. It must never be used in the paid-hours ingest.
- Workcell name variants in raw CSVs cause silent row exclusion if not mapped in `_WORKCELL_MAP`.
- OLE% plus all Paynter loss categories always sum to 100% of total paid hours.

### e. Data sources

| Source | System | File / location |
|---|---|---|
| Production output | MES | `PEN_TotalProduction_*.csv` |
| Paid direct hours | Payroll / HR (eTMS) | `PEN_PaidHours_Raw_*.csv` |
| SMH per unit | IE (PPT matrix) | SMH lookup, maintained by IE |
| Raw drop location | Network share | `\\penhomev10\OLE\RawData\` (not reachable via MCP) |
| Live extraction | MESWebApi | `https://mypenm0soap03.corp.jabil.org/meswebapi` (max query window 59 minutes, datetime `yyyy-MM-ddTHH:mm:ss.fffZ`) |

### f. Current status

| Layer | Status | Notes |
|---|---|---|
| Frontend | Live | In production at `/ietools/ole/`. Primary homepage is OLEHome4. |
| Backend | Live | FastAPI on port 9007, parquet mart, served behind nginx. |
| Deployment | Live | `mypenm0iesvr02.corp.jabil.org:443`, nginx, API proxied at `/ietools/ole/api/`. |

---

## 2. Frontend Enhancement Spec (FE is Live)

### a. Pages that exist

> Always read `App.tsx` first to confirm active routes before touching any component.

| Route | Component | What it does |
|---|---|---|
| `/map` | MapPage | Penang site map with plant-level OLE cards (MapLibre / mapcn). |
| `/ole/home4` | OLEHome4 | Current primary homepage. Site and workcell OLE overview. |
| `/ole/wc4/:workcell` | OLEWorkcell4 | Per-workcell drill-down. |
| `/ole/analysis` | OLEWoWAnalysis | Week-over-week OLE analysis. |
| `/ole/4q` | FourQGenerator | 4Q / Paynter loss report generator. |
| `/ole/smh-status` | SMHStatus | SMH matrix coverage and status per workcell. |

**Stack:** React 18, TypeScript, Vite, Tailwind, shadcn/ui, react-query, recharts, lucide-react, MapLibre GL.

**Key frontend constants and helpers:**

| Item | File | Purpose |
|---|---|---|
| `BASE = '/ietools/ole/api'` | `oleApi.ts` | Production API base. |
| `normalizeDates()` | frontend util | Strips time component from ISO timestamps before date comparison. |
| `TEMP_EXCLUDED_WORKCELLS` | `oleConstants.ts` | Currently holds BECKMAN COULTER. |
| `_WORKCELL_MAP` | shared | Maps cost-center codes and name variants to canonical workcell names. |
| `seededRand`, `PAYNTER_CATS`, `buildPaynterRow` | 4Q logic | Deterministic Paynter values, stable across refreshes. |
| Router `basename` | App config | Must equal `/ietools/ole` to match the nginx-served subpath. |
| `import.meta.env.BASE_URL` | asset paths | Required for all static asset paths (logos, images) under the production base path. |

### b. Known gaps / improvements needed

- Multiple OLEHome variants were built while choosing a manager-facing layout. Only OLEHome4 is wired as primary. The unused variants should be removed or archived once the layout is final.
- BECKMAN COULTER is hard-excluded. Needs a cleaner re-inclusion path once its SMH data is fixed, rather than editing a constant.
- Manager homepage layout selection is still open (the reason the variants exist).

### c. New features to consider

- A single "data freshness" indicator showing the latest ingested date, since the raw drop is manual.
- Re-inclusion toggle for excluded workcells driven by backend health rather than a frontend constant.

### d. Platform-convention alignment (target)

When OLE is folded into the unified platform conventions, it should be registered in `src/config/apps.ts`, keep its pages under `src/pages/ole/`, hooks under `src/hooks/ole/`, and constants under `src/lib/ole/oleConstants.ts`. It already follows the dashboard, card, and badge patterns the other modules copy.

---

## 3. Backend Enhancement Spec (BE is Live)

### a. What exists

**Stack:** Python, FastAPI, uvicorn, pandas, pyarrow (parquet), DuckDB.

**Current layout (as deployed):**

```
IE-Pulse-Backend/
  api/
    main.py            FastAPI entry, registers OLE routes
  pipeline/
    ingest.py          parse raw production and paid-hours CSVs
    compute.py         OLE and loss calculations
    export.py          write parquet mart
  data/
    mart/              computed parquet files
```

**Endpoints (live):**

| Endpoint | Returns |
|---|---|
| `GET /api/ole/weekly` | Weekly OLE per workcell / shift. |
| `GET /api/ole/mh-breakdown` | Man-hour breakdown (paid vs earned vs loss). |
| `GET /api/ole/pareto` | Paynter / Pareto loss categories. |
| `GET /api/workcells` | Active workcell list. |
| `GET /api/shifts` | Shift list (1, 2, 3). |

**Compute rule (the one that matters):**

```python
paid_hours   = paid_df["TPHDirect"].sum()          # all rows, no dedup, no filter
earned_hours = (prod_df["Qty"] * prod_df["smh_per_unit"]).sum()
ole_pct      = earned_hours / paid_hours * 100
```

API returns dates as ISO timestamps. The frontend normalizes them before any date comparison.

### b. Known gaps

- `ingest_support_hours()` cleanup is deferred to a future session. It is dead code and should be removed, not extended.
- `PEN_TotalPaidHours_*` handling is also dead. Confirm nothing reads it, then remove.
- The `drop_duplicates` pitfall is a permanent trap. Any future change to ingest must not reintroduce row deduplication on paid-hours.
- Workcell name-variant mapping lives in code. Additional SMH files are pending, which will expand coverage and likely add new variants to map.

### c. Improvements needed

- Remove dead code paths (`ingest_support_hours`, `PEN_TotalPaidHours_*`) once verified unused.
- Add `GET /api/ole/health` to match the platform convention (every module exposes a health endpoint).
- Migrate toward the unified backend layout: `modules/ole/config.py` + `modules/ole/pipeline/(refresh, ingest, compute, export)` with a thin `routers/ole.py`.
- Consider the hybrid architecture noted earlier: keep Python FastAPI for ETL and heavy processing, add a lighter dashboard API layer for read performance.

### d. Deployment notes (Windows + nginx)

- Server: `mypenm0iesvr02.corp.jabil.org`, port 443, nginx in front, FastAPI on port 9007.
- App served at `/ietools/ole/`, API proxied at `/ietools/ole/api/`.
- Vite proxy (`/ole-api`) is dev-only. The nginx proxy is production-only. Never conflate the two.
- Restart trap: a Windows service auto-restarts nginx worker processes, and stale workers from old configs intercept requests silently. Before assuming a fresh restart, run `sc stop nginx`, then `tasklist | findstr nginx`, then kill leftover workers via Task Manager.

---

## 4. CLAUDE.md Contribution

> Append this section to the root `CLAUDE.md` in both repos.

```markdown
## Module: OLE (Overall Labor Effectiveness)

Status: Frontend Live, Backend Live. OLE target = 61%.

### What it is
Measures how effectively paid direct labor hours convert into earned standard
output, per workcell, shift, plant, and site (Penang).

### Core formula
OLE% = SUM(Qty x SMH_per_unit) / SUM(TPHDirect) x 100
  - Qty, AssemblyNumber          <- PEN_TotalProduction_*.csv  (MES)
  - SMH_per_unit                 <- IE SMH matrix, keyed by AssemblyNumber
  - paid hours = SUM(TPHDirect)  <- PEN_PaidHours_Raw_*.csv     (payroll)

### Pipeline rules (do not break)
- Paid hours = SUM(TPHDirect) over ALL rows. No dedup. No value_type filter.
- NEVER drop_duplicates on paid-hours rows; it silently deletes valid employees.
- ingest_support_hours() and PEN_TotalPaidHours_* are DEAD code. Do not extend.
- Map workcell cost-center codes (*710246) and name variants (ARISTA PCA ->
  ARISTA NETWORKS) via _WORKCELL_MAP or rows are silently excluded.
- OLE% + all Paynter loss categories = 100% of total paid hours.

### Active workcells
AOP1, WABTEC, ARISTA NETWORKS, ARISTA NETWORKS HLA, ASP, KEYSIGHT HLA, UTAS,
BECKMAN COULTER (temp excluded via TEMP_EXCLUDED_WORKCELLS), IMED.

### Frontend (IE-Pulse)
- Read App.tsx first to confirm active routes.
- Routes: /map (MapPage), /ole/home4 (OLEHome4, primary), /ole/wc4/:workcell
  (OLEWorkcell4), /ole/analysis (OLEWoWAnalysis), /ole/4q (FourQGenerator),
  /ole/smh-status (SMHStatus).
- API base: BASE = '/ietools/ole/api' in oleApi.ts.
- Router basename must equal /ietools/ole. Static assets use import.meta.env.BASE_URL.
- normalizeDates() strips time from ISO dates before comparison.
- Constants in oleConstants.ts. Paynter values are deterministic via seededRand.

### Backend (IE-Pulse-Backend)
- FastAPI on port 9007. Parquet mart in data/mart/.
- Endpoints: /api/ole/weekly, /api/ole/mh-breakdown, /api/ole/pareto,
  /api/workcells, /api/shifts. (Add /api/ole/health to match platform convention.)
- API returns dates as ISO timestamps.

### Deployment (Windows + nginx)
- mypenm0iesvr02.corp.jabil.org:443, nginx -> FastAPI 9007, app at /ietools/ole/.
- Before assuming a clean nginx restart: sc stop nginx, tasklist | findstr nginx,
  then kill stale workers in Task Manager. Stale workers intercept requests silently.
- Vite proxy /ole-api is dev-only; nginx proxy is production-only. Never conflate.

### On the horizon
- Remove dead code (ingest_support_hours, PEN_TotalPaidHours_*).
- Additional SMH files pending -> expands coverage, may add workcell variants.
- Re-include BECKMAN COULTER once SMH lookup is corrected.
- Possible migration to unified backend layout (modules/ole/...) and a hybrid
  Python ETL + lighter dashboard API layer.
```

---

*End of OLE_BUILD.md*
