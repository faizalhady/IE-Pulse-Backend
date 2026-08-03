# =============================================================================
# Cycle Time Module — Config
# Source : IEDB3.0 API  (https://iedb2api-prd.jblapps.com)
# Site   : Penang | SiteCode=PEN | siteId=4
# =============================================================================

import os
from pathlib import Path

from dotenv import load_dotenv

# Anchor .env to the repo root (not process CWD) so env vars load for BOTH the
# FastAPI server and standalone `python -m ...` runs. Idempotent; won't override
# vars already set in the environment.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# ─── API ─────────────────────────────────────────────────────────────────────
BASE_URL    = "https://iedb2api-prd.jblapps.com"
TOKEN_URL   = "https://jabil.okta.com/oauth2/default/v1/token"
SITE_CODE   = "PEN"
PAGE_SIZE   = 500           # records per API page
API_TIMEOUT = 90            # seconds per request (big customers like KEYSIGHT can be slow)

# Token refresh buffer — fetch a new token this many seconds before expiry.
TOKEN_EXPIRY_BUFFER_S = 60

# ─── Paths ───────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent.parent.parent          # IE-Pulse-Backend/
CT_MART_DIR  = BASE_DIR / "data" / "mart" / "cycle_time"

CT_MART = {
    "raw":     CT_MART_DIR / "raw.parquet",       # one row per (assembly, revision, sub_workcenter, process)
    "pivoted": CT_MART_DIR / "pivoted.parquet",   # one row per assembly/rev/sub_wc — processes as columns (Image 2 view)
    "assembly_summary": CT_MART_DIR / "assembly_summary.parquet",  # one row per (customer, assembly): list-page fields + precomputed SMH (+ eff)
    "eff_by_line":      CT_MART_DIR / "eff_by_line.parquet",       # (customer, sub_workcenter) → efficiency goal, from GetSummaryGroupProcessData
    "assembly_catalog": CT_MART_DIR / "assembly_catalog.parquet",  # one row per (customer, assembly) FULL IEDB catalogue + has_data flag (incl. no-data)
    "mes_assembly_map": CT_MART_DIR / "mes_assembly_map.parquet",  # (customer, number, revision, assembly_id, customer_id) — MES name→id translator for completion status
    "mes_process_map": CT_MART_DIR / "mes_process_map.parquet",    # (customer, step_instance, iedb_alias) — MES step ↔ IEDB alias dict, from MNS workbook
    "completion_status": CT_MART_DIR / "completion_status.parquet",# per top-100 model: status + missing steps + coverage
    "completion_steps":  CT_MART_DIR / "completion_steps.parquet", # per model, tidy MES-route + IEDB-route rows (FE side-by-side)
    "line_metrics":      CT_MART_DIR / "line_metrics.parquet",     # per model, LBR% + IPK trolleys (from IEDB route; complete models only)
    # ── completion v2 (parallel marts — v1 above is left untouched as fallback) ──
    "completion_status_v2": CT_MART_DIR / "completion_status_v2.parquet",
    "completion_steps_v2":  CT_MART_DIR / "completion_steps_v2.parquet",
    "mes_serial_index":     CT_MART_DIR / "mes_serial_index.parquet",  # (customer, assembly, serial) from #94
}

# Cached MES pulls — written once, never re-fetched. mes_scans is one parquet per
# customer-DAY (#21) so a re-run only pulls days it doesn't already have.
CT_MES_SCAN_DIR  = CT_MART_DIR / "mes_scans"        # <cust>/<YYYY-MM-DD>.parquet
CT_MES_BOARD_DIR = CT_MART_DIR / "mes_board_steps"  # <cust>.parquet  (#132)

# MNS process-mapping workbook (curated MES StepInstance ↔ IEDB Alias, per customer).
# Source for the completion-status feature's naming bridge. Override via env if moved.
MES_PROCESS_MAP_XLSX = os.getenv("MES_PROCESS_MAP_XLSX",
    r"C:\Users\4033375\Projects\PRODUCTION DASHBOARD\MNS - Process Mapping (Centralized) (1).xlsx")

# ─── MES JEMS Web API — route/assembly-id lookups (completion-status feature) ──
# Different system from the eBuild MES_SQL_* buildplan: this is the REST WebApi
# (POST + APIKey header). Docs: docs/MES/MESWebApi_REFERENCE.md. Set MES_WEBAPI_KEY
# in .env — without it every call raises (no live data).
MES_WEBAPI_BASE    = os.getenv("MES_WEBAPI_BASE", "https://mypenm0soap03.corp.jabil.org/meswebapi")
MES_WEBAPI_KEY     = os.getenv("MES_WEBAPI_KEY", "")
MES_WEBAPI_TIMEOUT = int(os.getenv("MES_WEBAPI_TIMEOUT", "30"))

CT_STATE_FILE = CT_MART_DIR / ".ingest_state.json"

# ─── DuckDB query guardrails ─────────────────────────────────────────────────
# Each request opens a fresh in-memory DuckDB. `memory_limit` caps a single
# query's peak RAM; `temp_directory` is what actually lets a large query SPILL
# to disk instead of raising OutOfMemoryException. The previous config set a
# limit but NO temp dir, so a heavy query hit the ceiling and threw 500s (the
# `_duckdb.OutOfMemoryException ... 954 MiB/953.6 MiB used` seen in prod). With a
# temp dir set, the same query spills and completes. `threads` caps CPU and the
# per-thread memory overhead on the shared box (OLE + Cycle Time are one process).
# All three are env-overridable so the server can be tuned without a redeploy.
CT_DUCKDB_MEMORY_LIMIT = os.getenv("CT_DUCKDB_MEMORY_LIMIT", "2GB")
CT_DUCKDB_THREADS      = int(os.getenv("CT_DUCKDB_THREADS", "2"))
CT_DUCKDB_TEMP_DIR     = Path(os.getenv("CT_DUCKDB_TEMP_DIR", str(BASE_DIR / "data" / "tmp" / "duckdb")))

# ─── Active Customers — Penang, IsActive=1, AssemblyCount>0 ─────────────────
# Sorted by assembly count DESC.
# customer  = API query param (Customer)
# division  = API query param (Division)
# customer_id = internal IEDB CustomerId (reference only, not used in API calls)
CT_CUSTOMERS = [
    {"customer": "KEYSIGHT",                "division": "KEYSIGHT*",                     "customer_id": 355,  "assembly_count": 59539},
    {"customer": "ARISTANETWORKS",          "division": "ARISTANETWORKS*",               "customer_id": 374,  "assembly_count": 15151},
    {"customer": "Tellabs",                 "division": "Tellabs*",                      "customer_id": 362,  "assembly_count": 13618},
    {"customer": "INFINERA",                "division": "INFINERA*",                     "customer_id": 367,  "assembly_count": 9926},
    {"customer": "LAMRESEARCH",             "division": "LAMRESEARCH*",                  "customer_id": 370,  "assembly_count": 9502},
    {"customer": "K_CTEC",                  "division": "K_CTEC*",                       "customer_id": 398,  "assembly_count": 9101},
    {"customer": "LTX",                     "division": "LTX*",                          "customer_id": 366,  "assembly_count": 7228},
    {"customer": "BECKMAN COULTER",         "division": "BECKMAN COULTER*",              "customer_id": 391,  "assembly_count": 2554},
    # DYSON removed — workcell EOL 2026-07 (was customer_id 881, 2199 assemblies).
    {"customer": "MICRON SIG",              "division": "MICRON SIG*",                   "customer_id": 128,  "assembly_count": 2068},
    {"customer": "Motorola",                "division": "Mobile Devices*",               "customer_id": 1225, "assembly_count": 1864},
    {"customer": "TMO",                     "division": "TMO*",                          "customer_id": 376,  "assembly_count": 1796},
    {"customer": "FORTALEZA",               "division": "SEMICAP*",                      "customer_id": 2448, "assembly_count": 1596},
    {"customer": "Masimo",                  "division": "Masimo*",                       "customer_id": 371,  "assembly_count": 1084},
    {"customer": "ARISTA_NETWORKS_GLACIER", "division": "ARISTA_NETWORKS_GLACIER*",     "customer_id": 403,  "assembly_count": 920},
    {"customer": "BEDFORD",                 "division": "BEDFORD*",                      "customer_id": 361,  "assembly_count": 811},
    {"customer": "AFC",                     "division": "AFC*",                          "customer_id": 359,  "assembly_count": 731},
    {"customer": "ASP",                     "division": "ASP*",                          "customer_id": 373,  "assembly_count": 682},
    {"customer": "WABTEC",                  "division": "WABTEC*",                       "customer_id": 421,  "assembly_count": 579},
    {"customer": "Nokia Optics",            "division": "Nokia Optics*",                 "customer_id": 423,  "assembly_count": 516},
    {"customer": "BD",                      "division": "BECTON, DICKINSON AND COMPANY*","customer_id": 379,  "assembly_count": 389},
    {"customer": "UTAS",                    "division": "UTAS*",                         "customer_id": 382,  "assembly_count": 303},
    {"customer": "ILLUMINA",                "division": "ILLUMINA*",                     "customer_id": 2463, "assembly_count": 212},
    {"customer": "INTEL OPTICS",            "division": "INTEL OPTICS*",                 "customer_id": 1187, "assembly_count": 197},
    {"customer": "ResMed",                  "division": "ResMed*",                       "customer_id": 380,  "assembly_count": 192},
    {"customer": "HMB",                     "division": "HMB*",                          "customer_id": 2645, "assembly_count": 190},
    {"customer": "ADVANTEST",               "division": "ADVANTEST#",                    "customer_id": 2522, "assembly_count": 183},
    {"customer": "ELENION TECHNOLOGIES",    "division": "ELENION TECHNOLOGIES*",         "customer_id": 404,  "assembly_count": 156},
    {"customer": "LAMMEC",                  "division": "LAMMEC#",                       "customer_id": 2505, "assembly_count": 151},
    {"customer": "AKAMAI",                  "division": "AKAMAI*",                       "customer_id": 2544, "assembly_count": 144},
    {"customer": "ADVA",                    "division": "ADVA*",                         "customer_id": 418,  "assembly_count": 114},
    {"customer": "Medtronic",               "division": "Medtronic*",                    "customer_id": 1272, "assembly_count": 73},
    {"customer": "ENDURANCE",               "division": "ENDURANCE*",                    "customer_id": 2642, "assembly_count": 41},
    {"customer": "LAMGB",                   "division": "LAMGB#",                        "customer_id": 2523, "assembly_count": 39},
    {"customer": "AMAT",                    "division": "AMAT*",                         "customer_id": 1753, "assembly_count": 27},
    {"customer": "LIFE360",                 "division": "SVS*",                          "customer_id": 2701, "assembly_count": 20},
    {"customer": "TERRA SANA",              "division": "TERRA SANA*",                   "customer_id": 2525, "assembly_count": 10},
    {"customer": "GOPRO",                   "division": "GOPRO*",                        "customer_id": 2681, "assembly_count": 9},
    {"customer": "BARCO",                   "division": "Healthcare & Entertainment*",   "customer_id": 2708, "assembly_count": 6},
    {"customer": "Skydio",                  "division": "Mobile Devices*",               "customer_id": 2535, "assembly_count": 5},
    {"customer": "GO",                      "division": "GO*",                           "customer_id": 2526, "assembly_count": 3},
]
