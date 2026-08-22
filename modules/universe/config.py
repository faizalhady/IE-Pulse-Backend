"""
modules/universe/config.py
──────────────────────────
The Jabil Universe — dimensions and facts, defined once for the whole plant.
Phase 1 promotes the August 2026 draft registry (Projects\\docs\\registry) into
tested parquet tables under data/mart/universe. Rules: the `jabil-universe`
skill; reasoning: the vault, Universe/Jabil Universe - Foundational Document.
"""

from pathlib import Path

from core.paths import DATA_MART_DIR, PROJECT_ROOT
import os
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")     # MES_WEBAPI_KEY — the same file every module reads

# ─── Sources ──────────────────────────────────────────────────────────────────
# The August draft registry — one-off generator scripts and their outputs. Read
# only; the universe is the tested copy, the registry is the evidence.
REGISTRY_DIR = Path(r"C:\Users\4033375\Projects\docs\registry")

# Faiz's sheet, 2026-08-06: left block = REGION (Penang Island vs Batu Kawan),
# right block = PLANT (P1 / P2 / BK). Confirmed not to be in conflict.
WORKCELL_GROUP_XLSX = Path(r"C:\Users\4033375\Projects\temp\workcell group.xlsx")

# ─── Marts ────────────────────────────────────────────────────────────────────
UNIVERSE_MART_DIR = DATA_MART_DIR / "universe"
UNIVERSE_MART_DIR.mkdir(parents=True, exist_ok=True)

UNIVERSE_MART = {
    "dim_workcell":   UNIVERSE_MART_DIR / "dim_workcell.parquet",   # one row per workcell (= customer)
    "workcell_alias": UNIVERSE_MART_DIR / "workcell_alias.parquet", # one row per (workcell, system, value)
    "workcell_alias_conflict": UNIVERSE_MART_DIR / "workcell_alias_conflict.parquet",  # one row per spelling that points at 2+ workcells — surfaced, never resolved
    "dim_model":          UNIVERSE_MART_DIR / "dim_model.parquet",          # one row per (workcell, assembly)
    "dim_model_revision": UNIVERSE_MART_DIR / "dim_model_revision.parquet", # one row per (model, revision)
    "dim_calendar":   UNIVERSE_MART_DIR / "dim_calendar.parquet",   # one row per date, 2019-09-01 → 2031-12-31
    "dim_shift":      UNIVERSE_MART_DIR / "dim_shift.parquet",      # one row per shift code
    "fact_scan":      UNIVERSE_MART_DIR / "fact_scan.parquet",      # one row per board × step (MES WipScanData), deduped
    "model_terminal_step": UNIVERSE_MART_DIR / "model_terminal_step.parquet",  # one row per model: the step its boards finish at, learned
    "fact_unit_out":  UNIVERSE_MART_DIR / "fact_unit_out.parquet",  # one row per board that completed (its scan at the terminal step)
    "fact_paid_hours": UNIVERSE_MART_DIR / "fact_paid_hours.parquet",  # one row per (employee, date, shift, workcell, sub-workcell) — wave 2, pulled forward for the OLE proof
    "dim_smh":        UNIVERSE_MART_DIR / "dim_smh.parquet",        # one row per (workcell, model, scan_stage): standard man-hours per unit
    "ole_reconciliation": UNIVERSE_MART_DIR / "ole_reconciliation.parquet",  # one row per (workcell, ISO week): OLE from the universe beside the OLE module, delta explained
    # ── Phase 2 ──
    "dim_department":     UNIVERSE_MART_DIR / "dim_department.parquet",     # one row per department (what you do)
    "dim_employee":       UNIVERSE_MART_DIR / "dim_employee.parquet",       # one row per person; scope = workcell | site
    "dim_process":        UNIVERSE_MART_DIR / "dim_process.parquet",        # one row per process (the alias level); kind above, scan point below
    "process_alias":      UNIVERSE_MART_DIR / "process_alias.parquet",      # one row per (process, system, value)
    "dim_scan_point":     UNIVERSE_MART_DIR / "dim_scan_point.parquet",     # one row per (workcell, MES step): is it a scan point, what it maps to
    "fact_cycle_time_study": UNIVERSE_MART_DIR / "fact_cycle_time_study.parquet",  # one row per IEDB study row (model × revision × line × alias), ct_status
    "fact_cycle_time_measured": UNIVERSE_MART_DIR / "fact_cycle_time_measured.parquet",  # one row per observed (model, from_step → to_step) scan delta — ELAPSED, never a study
    "fact_route":         UNIVERSE_MART_DIR / "fact_route.parquet",         # one row per (model, line, step_order)
    "fact_demand":        UNIVERSE_MART_DIR / "fact_demand.parquet",        # one row per (workcell, model, period, source, as_of)
    "fact_production_share": UNIVERSE_MART_DIR / "fact_production_share.parquet",  # one row per (workcell, sub-workcell, assembly, date, shift) from the OLE share — a second opinion, never merged with boards
    # ── Phase 3 ──
    "completion_reconciliation": UNIVERSE_MART_DIR / "completion_reconciliation.parquet",  # one row per (workcell, model): completion from the universe beside the Cycle Time module, delta explained
    "auth_equipment_capacity": UNIVERSE_MART_DIR / "auth_equipment_capacity.parquet",  # AUTHORED: machines per (workcell, process) — seeded, to be corrected by people
    "auth_playbook":          UNIVERSE_MART_DIR / "auth_playbook.parquet",           # AUTHORED: operator → station per (workcell, model, route)
    "auth_process_group":     UNIVERSE_MART_DIR / "auth_process_group.parquet",      # AUTHORED: which steps form one buffer point (IPK)
    "auth_trolley_type":      UNIVERSE_MART_DIR / "auth_trolley_type.parquet",       # AUTHORED: trolley cavities per workcell
}

# ─── Plant vocabulary ─────────────────────────────────────────────────────────
# Registry spellings → the plant codes the plant itself uses.
PLANT_CODE = {"Plant 1": "P1", "JPE": "P2", "JBK": "BK", "P1": "P1", "P2": "P2", "BK": "BK"}
PLANT_REGION = {"P1": "Penang Island", "P2": "Penang Island", "BK": "Batu Kawan"}

# Physically in BK, supervised by Plant 1 (Faiz 2026-08-06; api/routers/ebuild.py
# _PLANT1_OVERRIDE encodes the same fact). "Which plant" is two questions.
PHYSICALLY_BK_GOVERNED_BY_P1 = {"MICRON SIG", "LAMGB", "LAMMEC"}

# Sheet spellings that canon() cannot resolve on its own. From the rename list in
# TO DO LIST.md ("still to confirm", 2026-08-06) — kept as PROPOSED aliases, never
# merged silently: every one lands in workcell_alias with system='workcell_group_sheet'.
SHEET_NAME_MAP = {
    "MICRON": "MICRON SIG",
    "LAM MECH / EFEM": "LAMMEC",
    "LAM GAS BOX": "LAMGB",
    "THERMOFISHER": "THERMO FISHER",
    "DANAHER / Beckman Coulter": "BECKMAN COULTER",
    "Becman Coulter / Danaher": "BECKMAN COULTER",
    "ASP / Fortive": "ASP (FORTIVE)",
    "COLLINS / Utas": "COLLINS",
    "Utas / Collins": "COLLINS",
    "Becton Dickingson": "BD",
    "AKAMAI TECHNOLOGIES": "AKAMAI",
    "GOPRO PCA": "GOPRO",
    "ENDURANCE PCA": "ENDURANCE",
    "HUMMINGBIRD": "HMB",
    "TERRA SANA FATP": "TERRA SANA",
    "Arista": "ARISTA NETWORKS",
    "LAM": "LAM RESEARCH",
}

# ─── Shifts ───────────────────────────────────────────────────────────────────
# Production runs on 2 and 3 only (case 49, confirmed from scan volume). Shift 1
# is office hours with zero direct output. Which of 2 / 3 is "morning" is open.
SHIFTS = [
    # shift, name,        start,   end,     carries_production
    (1, "Office hours",   "08:00", "17:00", False),
    (2, "Production A",   "07:00", "19:00", True),
    (3, "Production B",   "19:00", "07:00", True),
]

PROJECT_ROOT = PROJECT_ROOT

# ─── Completion vocabulary (case 48, §8.1 #9 refined) ─────────────────────────
# A scan AFTER completion: the board is already a unit; LINK is the logistics
# scan that follows PACKOUT (median 5.7 h later — a queue, not work). Boards
# whose history ends here are counted at the step before it.
POST_COMPLETION_STEPS = ("LINK",)
# An end that is NOT a unit: the board left the line as scrap.
NON_COMPLETION_STEPS = ("SCRAP",)
# Fallback when a model's history is too thin or too mixed to learn from.
DEFAULT_TERMINAL_STEP = "PACKOUT"
TERMINAL_MIN_BOARDS = 5       # fewer boards than this → default, learned = false
TERMINAL_MIN_SHARE = 0.5      # the modal end step must hold at least this share

# ─── The OLE proof ────────────────────────────────────────────────────────────
# The OLE module's own marts — read ONLY to compare against, never to compute from
# (nothing reads below its own layer; this is a reconciliation, not a dependency).
OLE_WEEKLY_PARQUET = DATA_MART_DIR / "ole" / "ole_weekly.parquet"
RECON_DELTA_PTS = 2.0          # a delta above this must carry a computed reason

# ─── Phase 2 sources ──────────────────────────────────────────────────────────
MES_WEBAPI_BASE = os.getenv("MES_WEBAPI_BASE", "https://mypenm0soap03.corp.jabil.org/meswebapi")
MES_WEBAPI_KEY  = os.getenv("MES_WEBAPI_KEY", "")
PLANNER_DEMAND_PARQUET = DATA_MART_DIR / "demand" / "planner_demand.parquet"   # the Cycle Time module's planner parse; the registry copy was a 29 Jun snapshot (case 68)
PAID_HOURS_SHARE = Path("//penhomev10/OLE/RawData")            # eTMS payroll export, rolling 16-day files (case 43)
PAID_HOURS_PREFIX = "PEN_PaidHours_Raw_"
RAW_PAID_HOURS_DIR = REGISTRY_DIR / "paid_hours_raw"            # local UTF-8 copies: the share rotates and mixes cp1252 (case 71)
OPERATIONAL_DB = PROJECT_ROOT / "data" / "operational.db"
# the metric glossary people edit in Obsidian; define() reads it on every call. Absent (prod 02) -> skill refs only
GLOSSARY_MD = Path(os.getenv("UNIVERSE_GLOSSARY_MD", "C:/Users/4033375/Obsidian/JABIL/IE CORE/Metric Glossary.md"))       # the SMH table people maintain (core/database.py)
RAW_WIPSCAN_DIR = REGISTRY_DIR / "wipscan"                      # the 30 raw hourly pulls (3.3 GB)
OLE_RAW_PRODUCTION = DATA_MART_DIR / "ole" / "raw_production.parquet"  # the share, W12–W31 — compared with, never merged

# ─── SMH policy (case 62) ─────────────────────────────────────────────────────
# What a unit with no SMH standard earns. 'zero' = nothing, and the gap is
# reported (the universe default — a gap looks like a gap). 'estimate' = the
# workcell's volume-weighted average SMH, which is what the OLE module does
# with OLE_SMH_FALLBACK=avg. A switch, never a silent choice.
SMH_MISSING_POLICY = "zero"          # 'zero' | 'estimate'
COMPLETION_DELTA = 0.10              # a coverage delta above this must carry a reason
