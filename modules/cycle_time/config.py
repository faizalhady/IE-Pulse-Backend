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
# IEDB charges per REQUEST, not per row: a page costs ~18-25s whether it holds 500
# rows or 50,000 (probed 2026-08-13 against KEYSIGHT). At 500 the 10-Aug bulk load
# of 342,848 KEYSIGHT rows needed 686 pages = 3h43m, which overran the nightly task's
# time limit and froze the app's marts for three days. At 10,000 the same pull is
# ~35 pages = ~11 min. Raise further only after re-probing; 50,000 also worked.
PAGE_SIZE   = 10000         # records per API page
API_TIMEOUT = 90            # seconds per request (big customers like KEYSIGHT can be slow)

# Token refresh buffer — fetch a new token this many seconds before expiry.
TOKEN_EXPIRY_BUFFER_S = 60

# ─── Paths ───────────────────────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent.parent.parent          # IE-Pulse-Backend/
CT_MART_DIR  = BASE_DIR / "data" / "mart" / "cycle_time"
EB_MART_DIR  = BASE_DIR / "data" / "mart" / "ebuild"

# ─── What "in demand" means, in ONE place ────────────────────────────────────
# The models we are building or about to build: the 13-week planner sheet UNION
# the MES ~4-week projection. 4,401 models; the two overlap by only 472, so
# either one alone is roughly half the answer.
#
# THIS TUPLE EXISTS BECAUSE THE ANSWER WAS ONCE DEFINED TWICE. The report used
# both marts and labelled the result "Planned 4,401"; the BOM pipeline used a
# scope it called "planner" that read planner_runners alone, 2,454 models. Same
# word, two sets — so BOM coverage was quoted as 75.5% of "planned" when it was
# 42.1% of what the screen calls Planned, with 1,945 models never even fetched.
# Anything that means "in demand" imports this rather than naming the files.
DEMAND_MARTS = ("projection_runners.parquet", "planner_runners.parquet")

CT_MART = {
    "raw":     CT_MART_DIR / "raw.parquet",       # one row per (assembly, revision, sub_workcenter, process)
    "pivoted": CT_MART_DIR / "pivoted.parquet",   # one row per assembly/rev/sub_wc — processes as columns (Image 2 view)
    "assembly_summary": CT_MART_DIR / "assembly_summary.parquet",  # one row per (customer, assembly): list-page fields + precomputed SMH (+ eff)
    "eff_by_line":      CT_MART_DIR / "eff_by_line.parquet",       # (customer, sub_workcenter) → efficiency goal, from GetSummaryGroupProcessData
    "assembly_catalog": CT_MART_DIR / "assembly_catalog.parquet",  # one row per (customer, assembly) FULL IEDB catalogue + has_data flag (incl. no-data)
    "mes_assembly_map": CT_MART_DIR / "mes_assembly_map.parquet",  # (customer, number, revision, assembly_id, bom_id, customer_id) — MES name→id translator for completion status
    # One row per (bom_id, material). Keyed on BOM_ID, NOT on the model: MES
    # shares one BOM across an assembly's revisions (E5052-66516 revs 003/004/106
    # are all BOM 7433), so keying on the model would store the same 194 rows 3x.
    # Join through mes_assembly_map.bom_id to get from a model to its materials.
    "bom_material": CT_MART_DIR / "bom_material.parquet",
    "mes_process_map": CT_MART_DIR / "mes_process_map.parquet",    # (customer, step_instance, iedb_alias) — MES step ↔ IEDB alias dict, from MNS workbook
    "completion_status": CT_MART_DIR / "completion_status.parquet",# per top-100 model: status + missing steps + coverage
    "completion_steps":  CT_MART_DIR / "completion_steps.parquet", # per model, tidy MES-route + IEDB-route rows (FE side-by-side)
    "line_metrics":      CT_MART_DIR / "line_metrics.parquet",     # per model, LBR% + IPK trolleys (from IEDB route; complete models only)
    # ── completion v2 (parallel marts — v1 above is left untouched as fallback) ──
    "completion_status_v2": CT_MART_DIR / "completion_status_v2.parquet",
    "completion_steps_v2":  CT_MART_DIR / "completion_steps_v2.parquet",
    # One row per (iso_week, plant, workcell, status, reason) with models + units.
    # The status marts above are SNAPSHOTS - each run overwrites them - so there
    # was no way to answer "are we getting better?". This is the only cycle-time
    # mart that accumulates.
    "completion_history":   CT_MART_DIR / "completion_history.parquet",
    "mes_serial_index":     CT_MART_DIR / "mes_serial_index.parquet",  # (customer, assembly, serial) from #94
    # IEDB CustomerStatus coverage report, snapshotted by the pipeline instead of
    # fetched live per request. The live call cost 3.6s on every cold start and
    # made the endpoint fail whenever IEDB was down. Snapshotting also makes it
    # CONSISTENT with the daily marts shown beside it, rather than a live number
    # next to day-old data.
    "customer_status":      CT_MART_DIR / "customer_status.parquet",
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

# ─── Chat (local LLM over Ollama) ────────────────────────────────────────────
# The GPU is the only part of the chat that needs its own process, and Ollama
# already is one — so where the card lives is a URL, not an architecture. Point
# this at a server when the RTX 2000 Ada moves off a workstation.
#
# llama3.1:8b by measurement, not preference: on 7 realistic routing questions it
# scored 6/7 at a 0.9s median, against 3/7 at 8.0s for qwen3.5. gemma4 is 9.6 GB
# and does not fit an 8 GB card — it spills to system RAM and crawls. Reasoning
# models (deepseek-r1) burn seconds thinking before a call that needs no thought.
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
OLLAMA_NUM_CTX  = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
OLLAMA_TIMEOUT  = int(os.getenv("OLLAMA_TIMEOUT", "180"))

# The chat's kill-switch. Testing-phase: set CT_CHAT_ENABLED=0 in the service
# env on 02 and every /chat* endpoint answers instantly WITHOUT importing the
# chat module or touching Ollama — no probes, no retries, nothing to time out.
# Default ON so local dev needs no setup. The 02 frontend build hides the chat
# UI independently (VITE_CHAT_ENABLED=0), so this is the second lock, not the
# only one.
CHAT_ENABLED = os.getenv("CT_CHAT_ENABLED", "1").strip().lower() not in ("0", "false", "no")

# ─── Chat provider (local Ollama vs OpenAI-compatible cloud) ─────────────────
# Company policy is currently OPEN for AI experimentation, so the model behind
# the chat is an env decision, not an architecture one. "openai" here means the
# PROTOCOL — Groq, Gemini (OpenAI-compat endpoint), OpenRouter, Cerebras and
# NVIDIA NIM all speak it, so one client covers every alternative under test.
#
#   CHAT_PROVIDER=ollama                                   (default — local 8B)
#   CHAT_PROVIDER=openai
#   CHAT_API_BASE=https://api.groq.com/openai/v1           (Groq example)
#   CHAT_API_KEY=gsk_...
#   CHAT_MODEL=llama-3.3-70b-versatile
#
# Other bases: Gemini https://generativelanguage.googleapis.com/v1beta/openai
#              OpenRouter https://openrouter.ai/api/v1
# Defaults are Groq + llama-3.3-70b (the decided primary), so going cloud is
# TWO env values: CHAT_PROVIDER=openai and CHAT_API_KEY=gsk_... The local 8B
# stays as automatic fallback either way (see chat/llm.py).
CHAT_PROVIDER  = os.getenv("CHAT_PROVIDER", "ollama").strip().lower()
CHAT_API_BASE  = os.getenv("CHAT_API_BASE", "https://api.groq.com/openai/v1").rstrip("/")
CHAT_API_KEY   = os.getenv("CHAT_API_KEY", "")
CHAT_MODEL     = os.getenv("CHAT_MODEL", "llama-3.3-70b-versatile")

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
