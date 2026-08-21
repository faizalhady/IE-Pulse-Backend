"""
modules/ppqt/config.py
──────────────────────
PPQT module configuration.

Model = the official PPQT template EM-IE80-00003-B as used in the LAMRES 8.0
workbook (docs/PPQT_LAMRES_DIFF.md in the frontend repo). One workbook = one
workcell; one visible "PPQT ..." sheet = one Area x Period; "Exe Summaries" =
the DL report.
"""

from core.paths import DATA_MART_DIR, DATA_RAW_DIR

# ─── Paths ────────────────────────────────────────────────────────────────────
PPQT_RAW_DIR  = DATA_RAW_DIR  / "ppqt"      # drop workbooks here
PPQT_MART_DIR = DATA_MART_DIR / "ppqt"
for _d in (PPQT_RAW_DIR, PPQT_MART_DIR):
    _d.mkdir(parents=True, exist_ok=True)

PPQT_MART = {
    "workbooks":   PPQT_MART_DIR / "workbooks.parquet",    # one row per ingested file
    "periods":     PPQT_MART_DIR / "periods.parquet",      # workcell x period scalars (Exe Summaries)
    "stations":    PPQT_MART_DIR / "stations.parquet",     # workcell x area x period x station params
    "assemblies":  PPQT_MART_DIR / "assemblies.parquet",   # workcell x area x period x assembly (demand)
    "cycle_times": PPQT_MART_DIR / "cycle_times.parquet",  # long: assembly x station -> ct_sec (ct > 0 only)
    "bays":        PPQT_MART_DIR / "bays.parquet",         # Exe Summaries rows: bay x period (crew, NPI, available)
}

# ─── Workbook vocabulary ──────────────────────────────────────────────────────
# Area code is read from the PPQT sheet title ("PPQT Lam SMT- AUG'26" -> SMT).
# Fallback is the sheet's own C8 text.
AREA_LABELS = {"SMT": "SMT & Middle End", "BE": "Backend", "HLA": "HLA"}

# Exe Summaries 'Area' column -> area code of the PPQT sheet the bay lives in.
BAY_AREA = {"SMT FE": "SMT", "SMT ME": "SMT", "SMT BE": "BE", "HLA": "HLA"}

# Exe Summaries rows that are DL but not stations (overhead).
OVERHEAD_AREAS = {"NVA", "Non Mfg"}

# Exe Summaries bay name -> PPQT station header, where the workbook spells them
# differently. (area_code, bay) -> station.
# ponytail: one known mismatch today; the ingest logs any bay it cannot match.
BAY_STATION_ALIAS = {("HLA", "INSP"): "XFNI VIP"}
