# PPQT — Backend Build Document

Model: the official PPQT template **EM-IE80-00003-B**, as used in
`8.0 PPQT LAMRES Aug26,Sep26,Oct26.xlsx` (reference workbook, 2026-08-21).
Formula diff vs the retired Wabtec model and the page storyline:
`IE-Pulse/docs/PPQT_LAMRES_DIFF.md`.

## Data flow (Excel first)

```
data/raw/ppqt/<workbook>.xlsx          one workbook = one workcell; drop files here
        │  modules/ppqt/pipeline/ingest.py   (visible sheets only, ~8 s for 12 MB)
        ▼
data/mart/ppqt/
  workbooks.parquet    file, workcell, areas, periods, ingested_at
  periods.parquet      workcell × period: weeks, PCA/HLA volume, actual DL, NVA DL, non-mfg DL, NVA target
  stations.parquet     workcell × area × period × station: hours_per_day, days, co_per_day, co_min,
                       fpy, eff, eq_avail, line_group, is_bottleneck, issues, sheet_need_allow (golden)
  assemblies.parquet   workcell × area × period × assembly: demand, lead_time_sec, model, family
  cycle_times.parquet  long: assembly × station → ct_sec (ct > 0 only)
  bays.parquet         Exe Summaries rows: bay × period: crew, NPI, available, sheet values (golden)
        │  modules/ppqt/compute.py   (request time, cached on mart mtime)
        ▼
api/routers/ppqt.py    /api/ppqt/*
```

Mart integration (IEDB cycle time, planner demand) comes later: it only has to
produce the same tables.

## What the ingest reads

| Sheet | How it is found | What is taken |
|---|---|---|
| PPQT sheet (one per Area × Period) | visible, `G1 == "PPQT Template"` | C7 workcell · title token → area (`SMT`/`BE`/`HLA`, else C8) · G12 date → period · row 13 headers H.. up to the last `BOTTLENECK` column (line groups end at each BOTTLENECK) · rows 14..footer−1 assemblies (A part no, C model, G demand, CT per station) · footer rows **by label** (crew hours, days, CO/day, CO min, FPY, efficiency, equipment available, Resources NEEDED) |
| Exe Summaries | visible, title starts with `Exe` | header row (`Area` in col B) · period blocks where row 6 starts with `Line` · bay rows (area, bay, type, DL/line, crew, NPI, available + sheet results) · scalars under the DL column: `Actual … DL`, `NVA`, `Non Mfg DL`, `… NVA Target` (% parsed) |

Hidden sheets are never read. Error cells (`#REF!` …) in a parameter row are
recorded in `stations.issues` and treated as 0 — exactly what the sheet does.

## Formulas (compute.py)

```
demand_through = Σ demand where CT > 0                 per station
sum_dem_ct     = Σ demand × CT
avail_sec      = days × (hours_per_day×60 − co_per_day×co_min) × 60
WCT            = sum_dem_ct / demand_through
Takt           = avail_sec  / demand_through
need           = WCT / Takt                            fractional
need_allow     = need × (1 + (1 − FPY×Eff))            "Resources NEEDED … with Allowances"

line group     = same formulas on MAX(CT over the group) per assembly (+ CTI / PFTR block)

report (bay)   ttl_req  = ROUNDUP(need_allow + NPI)
               variance = available − ttl_req          negative = short
               dl_req   = need_allow × crew
totals         Σ DL · actual DL · NVA ratio = NVA ÷ Σ DL · allowable NVA = VA × t/(1−t)
```

A station whose parameter row is broken (`issues`) or has zero available time
gets `variance = util = None` — no verdict rather than a false "spare".

## Golden check

`python -m modules.ppqt.compute` recomputes every station column and every
Exe Summaries bay and compares with the workbook's own cells
(`sheet_need_allow`, `sheet_demand_through`, `sheet_line_req`). Tolerance 1e-6.
Run it after any change to ingest or compute. Status 2026-08-21: **OK** —
261 station columns × 3 periods, 44 bays × 3 periods.

## Endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/api/ppqt/health` | marts present, workbooks in raw dir |
| GET | `/api/ppqt/workcells` | landing rows (latest period: volume, DL req vs actual, bays short, NVA ratio) |
| GET | `/api/ppqt/{workcell}` | areas, periods, source files |
| GET | `/api/ppqt/{workcell}/summary` | Exe Summaries: bays × periods + totals |
| GET | `/api/ppqt/{workcell}/stations?area&period` | per-station metrics, line groups, area totals |
| GET | `/api/ppqt/{workcell}/stations/{station}?area&period&top` | assemblies loading one station |
| GET | `/api/ppqt/{workcell}/assemblies?area&period&all` | assemblies × stations CT grid |
| GET | `/api/ppqt/{workcell}/inputs` | every parameter, raw |
| POST | `/api/ppqt/refresh` | re-parse `data/raw/ppqt/*.xlsx` (background) |

Legacy (Wabtec model, `modules/ppqt/capacity.py`) is mounted at
`/api/ppqt-legacy` for the `/ppqt-legacy` pages only.

## Known data facts in the LAMRES workbook

- SMT **SEP'26 and OCT'26**: the `Days in the Period` row is `#REF!` for all
  26 wave-line stations → the sheet sizes them at 0. Flagged per station.
- `Total Demand by process=` label is missing on some sheets; the values sit on
  the footer's first row, which the ingest falls back to.
- Exe Summaries bay `INSP` (HLA) maps to station `XFNI VIP` (alias in config).
- One HLA `PROGRAM` row has `2` in the Area column (typo) — forward-filled.
- 883 of 1,258 assemblies carry no demand; only `ct > 0` cells are stored.
