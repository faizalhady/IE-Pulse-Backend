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
    "dim_calendar":   UNIVERSE_MART_DIR / "dim_calendar.parquet",   # one row per date, 2019-09-01 → 2031-12-31
    "dim_shift":      UNIVERSE_MART_DIR / "dim_shift.parquet",      # one row per shift code
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
