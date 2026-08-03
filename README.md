# IE-Pulse-Backend

FastAPI backend for **IE Pulse** — serves every IE analytical module (OLE,
Cycle Time, Downtime, Transfers, PPQT, IPK, LBR), not just OLE.

See `CLAUDE.md` for the current layout and module conventions. The folder tree
below is out of date.

## Folder structure

```
IE-Pulse-Backend/
├── data/
│   ├── raw/          ← place SMH .xls files here
│   └── mart/         ← parquet outputs (auto-generated)
├── pipeline/
│   ├── ingest.py     ← normalise MES + eTMS CSVs + SMH files → parquet
│   ├── compute.py    ← DuckDB JOIN + OLE calculation → ole_computed.parquet
│   └── refresh.py    ← entry point: runs ingest then compute
├── api/
│   └── main.py       ← FastAPI app
├── config.py         ← all paths, workcell config, constants
├── requirements.txt
└── README.md
```

## Setup

```bash
cd C:\Users\4033375\Projects\IE-Pulse-Backend

# Create virtual environment
python -m venv venv
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

## SMH files

Place the SMH XLS files in `data/raw/`:

```
data/raw/
├── ASP_SMH.xls
├── AOP1_SMH.xls
├── KEYSIGHT_PCA_SMH.xls
└── KEYSIGHT_HLA_SMH.xls
```

## Pipeline scripts and how to run them

End-to-end via the entry point:

```bash
python pipeline/refresh.py                # incremental (DEFAULT - SAFE)
python pipeline/refresh.py --incremental  # same as above, explicit
python pipeline/refresh.py --full         # nuke + rebuild (DANGER)
```

### Modes — when to use which

| Mode | Reads | Marts | Historical rows | When to use |
|---|---|---|---|---|
| `--incremental` *(default)* | Only files newer than `.ingest_state.json` watermark | Appends | **PRESERVED** even if source CSVs were retention-deleted from the share | Routine runs, scheduled jobs, normal catch-up |
| `--full` | Everything currently on the share | Overwrites | **LOST** if a CSV is no longer on the share | Disaster recovery, schema migration |

The state file `data/mart/.ingest_state.json` is the watermark — it records the last successful ingest date per source.

### Step order inside `refresh.py`

1. **ingest** — read MES/eTMS CSVs + SMH files, normalise, write `production.parquet`, `paid_hours.parquet`, `smh_lookup.parquet`
2. **compute** — DuckDB JOIN production × paid hours × SMH, compute OLE → `ole_computed.parquet`
3. **compute_weekly** — ISO-week aggregation of `ole_computed` → `ole_weekly.parquet`
4. **compute_mh** — per-shift man-hours distribution from `ole_computed` → `mh_distribution.parquet`

### Running individual steps directly

Useful when you've only changed the math in one step and don't want to re-ingest.

| Command | What it rebuilds | When |
|---|---|---|
| `python pipeline/ingest.py` *(if exposed)* — usually called via refresh | Raw → normalised parquet | Rarely standalone |
| `python pipeline/compute.py` | `ole_computed.parquet` from raw parquet | OLE math changed |
| `python pipeline/compute_weekly.py` | `ole_weekly.parquet` from `ole_computed.parquet` | Weekly aggregation changed |
| `python pipeline/compute_mh.py` | `mh_distribution.parquet` from `ole_computed.parquet` | Man-hours formula (NVA, Lunch, MFG DT, Downtime, MFG Hour Lost) changed |

All single-step commands read existing upstream parquet and rewrite only their own output. None touch raw data, ingest state, or other marts.

### Forcing a clean reset

```bash
# Re-ingest everything visible on the share + recompute all marts:
python pipeline/refresh.py --full

# Drop the watermark and rebuild incrementally from DATE_FROM (config.py):
rm data/mart/.ingest_state.json
python pipeline/refresh.py
```

## Start the API

```bash
python api/main.py
```

API runs at http://localhost:8000

## API endpoints

| Method | Endpoint            | Description                        |
|--------|---------------------|------------------------------------|
| GET    | /api/health         | Check mart status                  |
| GET    | /api/workcells      | Workcell config reference          |
| GET    | /api/ole            | OLE results (filterable)           |
| GET    | /api/ole/summary    | Per-workcell OLE summary           |
| GET    | /api/production     | Raw MES production data            |
| GET    | /api/paid-hours     | Raw eTMS paid hours data           |
| GET    | /api/smh            | SMH lookup table                   |
| POST   | /api/refresh        | Trigger full pipeline refresh      |

Interactive docs: http://localhost:8000/docs

## Workcell config

Defined in `config.py`. Each workcell maps to its final scan stage:

| Workcell     | Final Scan Stage | SMH Column used     |
|--------------|------------------|---------------------|
| ASP          | Backend          | SMH/ Unit - Backend |
| AOP1         | SMT              | SMH/ Unit - SMT     |
| KEYSIGHT PCA | SMT              | SMH/ Unit - SMT     |
| KEYSIGHT HLA | BoxBuild         | SMH/ Unit - BoxBuild|

To add a new workcell, add one entry to `WORKCELL_CONFIG` and one entry
to `SMH_FILES` in config.py. No other changes needed.
