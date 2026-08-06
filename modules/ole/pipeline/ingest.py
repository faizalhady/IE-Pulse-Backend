r"""
ingest.py
─────────
Loads raw data from three sources, normalises column names and values,
and writes clean Parquet files to the mart.

Sources
  1. MES production CSVs      → \\penhomev10\OLE\PEN_TotalProduction_*.csv
  2. eTMS paid hours raw       → \\penhomev10\OLE\RawData\PEN_PaidHours_Raw_*.csv
     (row-level; one row per employee; TPHDirect is the canonical input-hours source)
  3. SMH XLS files             → data/raw/<WORKCELL>_SMH.xls  (HTML-disguised)

OLE denominator = SUM(TPHDirect) — single source of truth, raw file only.
The aggregated PEN_TotalPaidHours_* file is NOT used.

Outputs  (data/mart/)
  raw_production.parquet
  raw_paid_hours.parquet
  smh_lookup.parquet
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import re
import json
import logging
from datetime import datetime

import pandas as pd
from bs4 import BeautifulSoup

from modules.ole.config import (
    NETWORK_PATH,
    RAWDATA_PATH,
    DATA_MART_DIR,
    SMH_FILES,
    WORKCELL_CONFIG,
    INDIRECT_LABOR_CONFIG,
    ACTIVE_WORKCELLS,
    DATE_FROM,
    MART,
    PRODUCTION_PREFIX,
    PAID_HOURS_RAW_PREFIX,
)
from modules.ole import smh_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)

DATA_MART_DIR.mkdir(parents=True, exist_ok=True)


# --- Helpers ------------------------------------------------------------------

# Raw CSV name → canonical workcell name (typo / variant normalization only).
# The actual "is this workcell active?" check uses WORKCELL_CONFIG, not this map.
_WORKCELL_MAP: dict[str, str] = {
    "ASP":                  "ASP",
    "AOP1":                 "AOP1",
    "AOP 1":                "AOP1",
    "KEYSIGHT HLA":         "KEYSIGHT HLA",
    "KEYSIGHTHLA":          "KEYSIGHT HLA",
    "KEYSIGHT-HLA":         "KEYSIGHT HLA",
    "Keysight HLA":         "KEYSIGHT HLA",
    "WABTEC":               "WABTEC",
    "Wabtec":               "WABTEC",
    "UTAS":                 "UTAS",
    "Beckman Coulter":      "BECKMAN COULTER",
    "BECKMAN COULTER":      "BECKMAN COULTER",
    "IMED":                 "IMED PCA",
    "IMED PCA":             "IMED PCA",
    "Imed PCA":             "IMED PCA",
    # ARISTA NETWORKS PCA = the SMT line. Raw CSV uses several aliases.
    "Arista PCA":           "ARISTA NETWORKS PCA",
    "ARISTA PCA":           "ARISTA NETWORKS PCA",
    "ARISTA NETWORKS":      "ARISTA NETWORKS PCA",
    "ARISTA NETWORKS PCA":  "ARISTA NETWORKS PCA",
    # ARISTA NETWORKS HLA = the Backend line. Raw CSV uses just "ARISTA".
    "ARISTA":               "ARISTA NETWORKS HLA",
    "ARISTA NETWORKS HLA":  "ARISTA NETWORKS HLA",
    "LAM":                  "LAM RESEARCH",
    "LAM RESEARCH":         "LAM RESEARCH",
    "Lam":                  "LAM RESEARCH",
    "Lam Research":         "LAM RESEARCH",
    # Indirect labor (non-workcell entities — paid hours only, no production / SMH)
    "WAREHOUSE P1":         "WAREHOUSE P1",
    "Warehouse P1":         "WAREHOUSE P1",
    "WAREHOUSE P2":         "WAREHOUSE P2",
    "Warehouse P2":         "WAREHOUSE P2",
    "SUPPORT P1":           "SUPPORT P1",
    "Support P1":           "SUPPORT P1",
    "SUPPORT P2":           "SUPPORT P2",
    "Support P2":           "SUPPORT P2",
}


def _active_canonical_names() -> set[str]:
    """All configured canonical entity names — workcells AND indirect labor."""
    return set(WORKCELL_CONFIG.keys()) | set(INDIRECT_LABOR_CONFIG.keys())


def _active_raw_names() -> set[str]:
    """Raw CSV names that normalize to a configured (active) entity."""
    active = _active_canonical_names()
    return {raw for raw, canonical in _WORKCELL_MAP.items() if canonical in active}


def _normalise_workcell(name: str) -> str | None:
    """Normalise a raw CSV workcell/entity name to canonical. Returns None for unknowns."""
    if not isinstance(name, str):
        return None
    cleaned = name.strip()
    canonical = _WORKCELL_MAP.get(cleaned) or _WORKCELL_MAP.get(cleaned.upper())
    # Defense in depth: only return canonical names that are actually configured.
    if canonical and canonical in _active_canonical_names():
        return canonical
    return None


def _parse_date(value) -> pd.Timestamp | None:
    try:
        return pd.Timestamp(value)
    except Exception:
        return pd.NaT


def _cutoff_date() -> pd.Timestamp:
    return pd.Timestamp(DATE_FROM)


# --- State file (high-water marks per source) --------------------------------

STATE_FILE = DATA_MART_DIR / ".ingest_state.json"


def _load_state() -> dict:
    """Read incremental ingest state. Returns {} if missing or corrupt."""
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Could not read state file ({e}); starting from empty state.")
        return {}


def _save_state(state: dict) -> None:
    """Persist incremental ingest state."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.error(f"Could not write state file: {e}")


def _latest_file_date(prefix: str, search_dirs: list[Path]) -> str | None:
    """Scan dirs and return the max YYYYMMDD date found in filenames, as 'YYYY-MM-DD'."""
    date_re = re.compile(r"(\d{8})\.csv$")
    latest = None
    for d in search_dirs:
        try:
            for f in d.glob(f"{prefix}*.csv"):
                m = date_re.search(f.name)
                if not m:
                    continue
                try:
                    fdate = datetime.strptime(m.group(1), "%Y%m%d")
                except ValueError:
                    continue
                if latest is None or fdate > latest:
                    latest = fdate
        except Exception:
            continue
    return latest.strftime("%Y-%m-%d") if latest else None


def _find_csv_files(prefix: str, search_dirs: list[Path], since: str | None = None) -> list[Path]:
    """
    Scan given directories for CSVs matching prefix.
    Always filtered by DATE_FROM cutoff.
    If `since` (YYYY-MM-DD) provided, also filters out files with filename-date <= since.
    """
    pattern = f"{prefix}*.csv"
    cutoff  = datetime.strptime(DATE_FROM, "%Y-%m-%d")
    since_dt = datetime.strptime(since, "%Y-%m-%d") if since else None
    files   = []
    seen    = set()
    date_re = re.compile(r"(\d{8})\.csv$")

    for search_dir in search_dirs:
        try:
            all_files = list(search_dir.glob(pattern))
        except Exception as e:
            log.warning(f"Cannot access {search_dir}: {e}")
            continue

        for f in sorted(all_files):
            if f.name in seen:
                continue
            seen.add(f.name)
            m = date_re.search(f.name)
            if not m:
                files.append(f)
                continue
            try:
                fdate = datetime.strptime(m.group(1), "%Y%m%d")
            except ValueError:
                files.append(f)
                continue
            if fdate < cutoff:
                continue
            if since_dt and fdate <= since_dt:
                continue
            files.append(f)

    if since:
        log.info(f"Found {len(files)} file(s) for prefix '{prefix}' newer than {since}")
    else:
        log.info(f"Found {len(files)} file(s) for prefix '{prefix}'")
    return sorted(files)


# --- 1. MES Production --------------------------------------------------------

def ingest_production(since: str | None = None, exclude_dates: set | None = None) -> pd.DataFrame:
    """
    Load and normalise MES production CSVs using DATE-STITCHING.

    Files are processed NEWEST → OLDEST. For each file, only rows for dates
    not yet collected from any newer file are taken. This works because:
      - Each daily CSV is a rolling snapshot covering several recent days
      - For overlapping dates, the NEWER file is treated as authoritative
      - Older files only fill in the "back-edge" dates that have rolled off
        the newer files

    Benefits over the old all-column-dedup approach:
      - Faster: skip rows we'll never use
      - Memory-efficient: per-date kept exactly once
      - Revision-tolerant: newer file silently wins for overlapping dates
      - No leak path via "noise columns" (cv, category, etc.)

    Parameters:
      since         — YYYY-MM-DD: only files with filename-date > since are read
      exclude_dates — set of dates already in the mart (incremental mode);
                      stitching skips any date in this set, so we never re-read
                      data we already have. Pass None for a full ingest.
    """
    files = _find_csv_files(PRODUCTION_PREFIX, [NETWORK_PATH, RAWDATA_PATH], since=since)
    if not files:
        log.warning("No production CSV files found.")
        return pd.DataFrame()

    # Newest first for date-stitching
    files = sorted(files, reverse=True)

    cutoff = _cutoff_date()
    active = _active_raw_names()
    # Pre-populate seen_dates with already-ingested dates so stitching skips
    # them entirely. In --full mode exclude_dates is None → seen starts empty.
    seen_dates: set = set(exclude_dates) if exclude_dates else set()
    pre_seen = len(seen_dates)
    if pre_seen:
        log.info(f"  Production: pre-loaded {pre_seen} dates already in mart (incremental mode)")
    frames = []

    for f in files:
        try:
            df = pd.read_csv(f, dtype=str)
            df.columns = [c.strip() for c in df.columns]

            wc_col   = next((c for c in df.columns if c == 'Workcell'), None)
            date_col = next((c for c in df.columns if c == 'StartDate'), None)

            # Early filter: active workcells only
            if wc_col:
                df = df[df[wc_col].isin(active)]

            # Early filter: cutoff date
            if date_col:
                df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
                df = df[df[date_col] >= cutoff]

            if df.empty:
                log.info(f"  Skipped (no relevant rows): {f.name}")
                continue

            # ── Date stitching: take only rows for dates not yet seen ──
            if date_col is None:
                log.warning(f"  No StartDate column in {f.name} - taking all rows (fallback)")
                frames.append(df)
                log.info(f"  Loaded production: {f.name}  ({len(df)} rows)")
                continue

            file_dates = set(df[date_col].dt.date.dropna().unique())
            new_dates  = file_dates - seen_dates
            if not new_dates:
                log.info(f"  Skipped (all dates already taken from newer file): {f.name}")
                continue

            df = df[df[date_col].dt.date.isin(new_dates)]
            seen_dates.update(new_dates)
            d = sorted(new_dates)
            log.info(
                f"  Stitched production: {f.name}  "
                f"({len(df)} rows, {len(new_dates)} new date(s): {d[0]} ... {d[-1]})"
            )
            frames.append(df)
        except Exception as e:
            log.error(f"  Failed to read {f.name}: {e}")

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df.columns = [c.strip() for c in df.columns]

    df = df.rename(columns={
        "Site":           "site",
        "Workcell":       "workcell_raw",
        "SubWorkcell":    "sub_workcell",
        "AssemblyNumber": "assembly",
        "Qty":            "qty",
        "StartDate":      "date",
        "EndDate":        "end_date",
        "Shift":          "shift",
    })

    df["qty"]      = pd.to_numeric(df["qty"],   errors="coerce").fillna(0).astype(int)
    df["shift"]    = pd.to_numeric(df["shift"], errors="coerce").fillna(0).astype(int)
    df["date"]     = df["date"].apply(_parse_date)
    df["assembly"] = df["assembly"].str.strip().str.rstrip("/")
    df["workcell"] = df["workcell_raw"].apply(_normalise_workcell)

    unrecognised = df[df["workcell"].isna()]["workcell_raw"].dropna().unique()
    if len(unrecognised):
        log.info(f"  Production - unrecognised workcells (filtered out): {sorted(unrecognised)}")

    df = df[df["workcell"].notna()].copy()

    def _stage_match(row):
        cfg = WORKCELL_CONFIG.get(row["workcell"], {})
        return str(row["sub_workcell"]).strip().upper() == cfg.get("scan_stage", "").upper()

    df = df[df.apply(_stage_match, axis=1)].copy()
    df = df[df["date"] >= _cutoff_date()].copy()
    df = df[["site", "workcell", "sub_workcell", "assembly", "qty", "date", "shift"]].copy()

    new_dates_count = len(seen_dates) - pre_seen
    log.info(f"Production: {len(df)} rows after normalisation and filtering "
             f"({new_dates_count} new date(s) from this run; {len(seen_dates)} total tracked)")
    return df


# --- 2. eTMS Paid Hours (Raw, employee-level) ----------------------------------

def ingest_paid_hours(since: str | None = None, exclude_dates: set | None = None) -> pd.DataFrame:
    """
    Load row-level paid hours from PEN_PaidHours_Raw_*.csv using DATE-STITCHING.

    Files are processed NEWEST → OLDEST. For each file, only rows for dates
    not yet collected from any newer file are taken. This works because:
      - Each daily CSV is a rolling snapshot covering several recent days
      - For overlapping dates, the NEWER file is treated as authoritative
      - Older files only fill in the "back-edge" dates that have rolled off
        the newer files

    Source of truth for OLE input hours. Each row = one employee × one shift.

    Parameters:
      since         — YYYY-MM-DD: only files with filename-date > since are read
      exclude_dates — set of dates already in the mart (incremental mode);
                      stitching skips any date in this set, so we never re-read
                      data we already have. Pass None for a full ingest.

    Columns captured from raw file:
      Site, CostCenter, WorkCell, SubWorkCell,
      THCDirect, TPHDirect,
      Startdate, EndDate, Shift,
      Value1  (VA | NVA | blank -- labour category),
      Position, CV, Name, DateJoined, DayJoined, Category
    """
    files = _find_csv_files(PAID_HOURS_RAW_PREFIX, [RAWDATA_PATH], since=since)
    if not files:
        log.warning("No raw paid hours CSV files found in RawData -- mart will be empty.")
        return pd.DataFrame()

    # Newest first for date-stitching
    files = sorted(files, reverse=True)

    frames = []
    cutoff = _cutoff_date()
    active = _active_raw_names()   # only raw names that map to an ACTIVE (configured) workcell
    # Pre-populate seen_dates with already-ingested dates so stitching skips
    # them entirely. In --full mode exclude_dates is None → seen starts empty.
    seen_dates: set = set(exclude_dates) if exclude_dates else set()
    pre_seen = len(seen_dates)
    if pre_seen:
        log.info(f"  Paid hours: pre-loaded {pre_seen} dates already in mart (incremental mode)")

    for f in files:
        try:
            try:
                df = pd.read_csv(f, dtype=str, encoding='utf-8')
            except UnicodeDecodeError:
                df = pd.read_csv(f, dtype=str, encoding='windows-1252')

            df.columns = [c.strip() for c in df.columns]
            wc_col   = next((c for c in df.columns if c == 'WorkCell'), None)
            date_col = next((c for c in df.columns if c == 'Startdate'), None)

            # Early filter: active workcells only
            if wc_col:
                df = df[df[wc_col].isin(active)]

            # Early filter: cutoff date
            if date_col:
                df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
                df = df[df[date_col] >= cutoff]

            if df.empty:
                log.info(f"  Skipped (no relevant rows): {f.name}")
                continue

            # ── Date stitching: take only rows for dates not yet seen ──
            if date_col is None:
                log.warning(f"  No Startdate column in {f.name} - taking all rows (fallback)")
                frames.append(df)
                log.info(f"  Loaded raw paid hours: {f.name}  ({len(df)} rows)")
                continue

            file_dates = set(df[date_col].dt.date.dropna().unique())
            new_dates  = file_dates - seen_dates
            if not new_dates:
                log.info(f"  Skipped (all dates already taken from newer file): {f.name}")
                continue

            df = df[df[date_col].dt.date.isin(new_dates)]
            seen_dates.update(new_dates)
            d = sorted(new_dates)
            log.info(
                f"  Stitched raw paid hours: {f.name}  "
                f"({len(df)} rows, {len(new_dates)} new date(s): {d[0]} ... {d[-1]})"
            )
            frames.append(df)
        except Exception as e:
            log.error(f"  Failed to read {f.name}: {e}")

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df.columns = [c.strip() for c in df.columns]

    df = df.rename(columns={
        "Site":        "site",
        "CostCenter":  "cost_center",
        "WorkCell":    "workcell_raw",
        "SubWorkCell": "sub_workcell",
        "THCDirect":   "thc_direct",
        "TPHDirect":   "tph_direct",
        "Startdate":   "date",
        "EndDate":     "end_date",
        "Shift":       "shift",
        "Value1":      "value_type",
        "Position":    "position",
        "CV":          "cv",
        "Name":        "name",
        "DateJoined":  "date_joined",
        "DayJoined":   "day_joined",
        "Category":    "category",
    })

    df = df.drop(columns=["Custom01"], errors="ignore")

    df["thc_direct"]  = pd.to_numeric(df["thc_direct"], errors="coerce").fillna(0).astype(int)
    df["tph_direct"]  = pd.to_numeric(df["tph_direct"], errors="coerce").fillna(0.0)
    df["shift"]       = pd.to_numeric(df["shift"],      errors="coerce").fillna(0).astype(int)
    df["date"]        = df["date"].apply(_parse_date)
    df["value_type"]  = df["value_type"].fillna("").str.strip().str.upper()
    df["total_input_hours"] = df["tph_direct"]   # support hours added at compute stage

    df["workcell"] = df["workcell_raw"].apply(_normalise_workcell)

    unrecognised = df[df["workcell"].isna()]["workcell_raw"].dropna().unique()
    if len(unrecognised):
        log.info(f"  Raw paid hours - unrecognised workcells (filtered out): {sorted(unrecognised)}")

    df = df[df["workcell"].notna()].copy()
    df = df[df["date"] >= _cutoff_date()].copy()

    df = df[[
        "site", "cost_center", "workcell", "sub_workcell",
        "thc_direct", "tph_direct", "total_input_hours",
        "date", "shift",
        "value_type",
        "position", "cv", "name",
        "date_joined", "day_joined", "category",
    ]].copy()

    # No drop_duplicates here — date-stitching above guarantees each date
    # comes from exactly one file, so cross-file replicas are impossible.

    new_dates_count = len(seen_dates) - pre_seen
    log.info(f"Raw paid hours: {len(df)} rows after normalisation and filtering "
             f"({new_dates_count} new date(s) from this run; {len(seen_dates)} total tracked)")
    log.info(f"  value_type breakdown: {df['value_type'].value_counts().to_dict()}")
    return df


# --- 3. SMH Lookup ------------------------------------------------------------

_SMH_COL_INDEX = {
    "SMH/ Unit - SMT":      3,
    "SMH/ Unit - Backend":  4,
    "SMH/ Unit - BoxBuild": 5,
}


def _parse_smh_xls(path: Path, workcell: str) -> pd.DataFrame:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            html = f.read()
    except Exception as e:
        log.error(f"Cannot read SMH file {path}: {e}")
        return pd.DataFrame()

    soup    = BeautifulSoup(html, "lxml")
    cfg     = WORKCELL_CONFIG[workcell]
    col_idx = _SMH_COL_INDEX[cfg["smh_col"]]

    data = []
    for row in soup.find_all("tr"):
        cols = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cols) < 6:
            continue
        try:
            smh_val = float(cols[col_idx]) if cols[col_idx] else 0.0
        except ValueError:
            smh_val = 0.0

        last_updated = pd.NaT
        if len(cols) >= 10:
            try:
                last_updated = pd.Timestamp(cols[9])
            except Exception:
                pass

        data.append({
            "assembly":     cols[1].strip().rstrip("/"),
            "smh_value":    smh_val,
            "last_updated": last_updated,
        })

    if not data:
        log.warning(f"No data rows found in {path.name}")
        return pd.DataFrame()

    df = pd.DataFrame(data)
    df["workcell"]    = workcell
    df["scan_stage"]  = cfg["scan_stage"]
    df["stage_label"] = cfg["label"]
    df["plant"]       = cfg["plant"]
    df = df[df["assembly"].str.len() > 0].copy()

    nonzero = (df["smh_value"] > 0).sum()
    log.info(
        f"  SMH {workcell} ({cfg['label']}): {len(df)} assemblies, "
        f"{nonzero} non-zero, {len(df) - nonzero} zero/missing"
    )
    return df[["workcell", "assembly", "smh_value", "scan_stage", "stage_label", "plant", "last_updated"]]


def parse_all_smh_files() -> pd.DataFrame:
    """Parse every configured .xls into one frame.

    No longer part of the refresh — SMH comes from SQLite now. This is the
    import path: used by scripts/migrate_smh_to_sqlite.py and by the
    upload endpoint, both of which feed smh_store.upsert_many().
    """
    frames = []
    for workcell, path in SMH_FILES.items():
        if workcell not in WORKCELL_CONFIG:
            continue
        if not path.exists():
            log.warning(f"SMH file NOT FOUND for {workcell}: {path}")
            continue
        df = _parse_smh_xls(path, workcell)
        if not df.empty:
            frames.append(df)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    return df.drop_duplicates(subset=["workcell", "assembly"])


def ingest_smh() -> pd.DataFrame:
    """SMH from the `smh` SQLite table — NOT from the .xls files.

    The files were the store until the UI needed to edit SMH; an edit written
    anywhere downstream was wiped by the next refresh. They are still accepted
    via the upload endpoint, which imports INTO this table.

    Empty table is a hard stop, not a warning. Returning an empty frame here
    would let compute.py join against nothing and publish a plant-wide OLE of
    0% — a silent, plausible-looking wrong answer.
    """
    df = smh_store.load_lookup()
    if df.empty:
        log.error(
            "SMH table is empty -- run scripts/migrate_smh_to_sqlite.py to import "
            "the .xls files, or add values via /api/smh."
        )
        return pd.DataFrame()

    log.info(f"SMH lookup: {len(df)} assembly entries across {df['workcell'].nunique()} "
             f"workcells (source: SQLite)")
    return df


# --- Entry point --------------------------------------------------------------

def _merge_with_existing(mart_path: Path, new_df: pd.DataFrame, label: str) -> pd.DataFrame:
    """
    Date-aware merge of new_df with existing mart parquet:
      - Existing rows whose DATE appears in new_df are dropped (newer wins).
      - Existing rows for older dates (not in new_df) are kept.
      - New_df rows are appended.

    This complements date-stitching ingest: the newest file is authoritative
    for the dates it covers, all the way through to the parquet history. No
    chance of duplicates or noise-column leakage for overlapping dates.

    Preserves historical rows that may have been deleted from the source
    folder due to retention. new_df may be empty (no new files this run).
    """
    if not mart_path.exists():
        log.info(f"  {label}: no existing mart, writing fresh ({len(new_df)} rows).")
        return new_df

    try:
        existing = pd.read_parquet(mart_path)
    except Exception as e:
        log.error(f"  {label}: could not read existing parquet ({e}); falling back to new only.")
        return new_df

    if new_df.empty:
        log.info(f"  {label}: no new rows this run; keeping {len(existing)} existing.")
        return existing

    # Determine which dates are covered by the new data (newer wins)
    new_dates = set(pd.to_datetime(new_df["date"]).dt.date.dropna().unique())
    existing_dates = pd.to_datetime(existing["date"]).dt.date

    existing_keep = existing[~existing_dates.isin(new_dates)]
    combined = pd.concat([existing_keep, new_df], ignore_index=True)
    dropped = len(existing) - len(existing_keep)
    log.info(
        f"  {label}: kept {len(existing_keep)} existing (older dates only) + "
        f"{len(new_df)} new = {len(combined)} total "
        f"({dropped} existing rows replaced by newer dates)"
    )
    return combined


def _share_unreachable() -> str | None:
    """Return why the share is unusable, or None if it looks fine.

    A run that cannot see the share reads nothing, merges nothing, and — before
    this check — still logged `Pipeline complete` and `RUN OK`. That happened on
    2026-08-04: an ingest with zero files reported success in 3.3 seconds. A
    scheduled task failing that way every night is invisible.

    Checked before any parsing so the failure names the cause, rather than
    surfacing later as the misleading "mart would be empty".
    """
    for label, dirs, prefix in (
        ("production", [NETWORK_PATH, RAWDATA_PATH], PRODUCTION_PREFIX),
        ("paid hours", [RAWDATA_PATH],               PAID_HOURS_RAW_PREFIX),
    ):
        reachable = False
        found     = 0
        for d in dirs:
            try:
                found += sum(1 for _ in d.glob(f"{prefix}*.csv"))
                reachable = True          # listed without raising
            except Exception as e:
                log.error("  Cannot access %s: %s", d, e)
        if not reachable:
            return f"{label}: none of {[str(d) for d in dirs]} could be listed"
        if found == 0:
            return f"{label}: no {prefix}*.csv files found in {[str(d) for d in dirs]}"
    return None


def _verify_coverage(prod: pd.DataFrame, hours: pd.DataFrame) -> dict:
    """Every date the share still holds must be in the mart. Report what isn't.

    The 2026-08-04 drift went unnoticed for weeks because a run that silently
    skipped a date still logged success. Reading the whole share (above) stops
    new gaps forming; this is the alarm for when something else does — a file
    that fails to parse, a share that vanishes mid-run, a workcell filtered out
    by a mapping change.

    Cheap on purpose: production files are one day each, so their filenames are
    the expected date set. No extra I/O beyond a directory listing.
    """
    result: dict = {"checked_at": datetime.now().isoformat(timespec="seconds")}
    try:
        date_re = re.compile(r"(\d{8})\.csv$")
        share_dates = sorted({
            datetime.strptime(m.group(1), "%Y%m%d").date()
            for f in _find_csv_files(PRODUCTION_PREFIX, [NETWORK_PATH, RAWDATA_PATH])
            if (m := date_re.search(f.name))
        })
        if not share_dates:
            result["error"] = "no production files visible in the share"
            log.warning("  Coverage check skipped: %s", result["error"])
            return result

        mart_dates = set(pd.to_datetime(prod["date"]).dt.date.dropna().unique())
        missing = [str(d) for d in share_dates if d not in mart_dates]

        result["share_from"]  = str(share_dates[0])
        result["share_to"]    = str(share_dates[-1])
        result["share_days"]  = len(share_dates)
        result["missing_days"] = missing
        result["paid_hours_from"] = str(pd.to_datetime(hours["date"]).min().date())
        result["paid_hours_to"]   = str(pd.to_datetime(hours["date"]).max().date())

        if missing:
            # ERROR, not warning: a hole here is wrong OLE on a dashboard people
            # make decisions from, and it will not fix itself if the share file
            # is deleted before the next run.
            log.error("  COVERAGE GAP -- %d share date(s) missing from the mart: %s",
                      len(missing), ", ".join(missing[:10]) + (" ..." if len(missing) > 10 else ""))
        else:
            log.info("  Coverage OK -- all %d share dates present in the mart (%s .. %s)",
                     len(share_dates), share_dates[0], share_dates[-1])
    except Exception as e:
        # Never fail the run over the self-check; a broken alarm must not stop
        # a good ingest from being written.
        result["error"] = str(e)
        log.warning("  Coverage check failed: %s", e)
    return result


def run(mode: str = "incremental") -> bool:
    """
    mode='incremental' (default) — only reads files newer than the last successful
                                    run (per state file), then appends to existing
                                    parquet marts. Preserves historical data even
                                    when source files are deleted from the share.
    mode='full'                   — re-reads everything currently in the share and
                                    overwrites marts. Loses anything no longer in
                                    the share. Use for disaster recovery only.

    Incremental re-reads the WHOLE share every run — it does not skip dates
    ─────────────────────────────────────────────────────────────────────────
    It used to. Every date already in the mart was pre-loaded into
    `exclude_dates` and stitching skipped it, which made a date's first ingest
    permanent: if the machine was down when a file landed, or read a file that
    was later re-exported with fuller data, the gap froze. No later run could
    correct it, and every run still reported success.

    That cost us a day of debugging on 2026-08-04. The server was missing all
    of 2026-07-29 production while holding that day's paid hours, so W31 read
    49.1% against local's 57.2%; its paid-hours mart was also ~68k hours short
    across June. Two machines, same code, same state file, both "OK".

    Re-reading everything and merging costs ~5 seconds, which is what the
    skipping was buying. Incremental is now self-healing: a date missed today
    is repaired on the next run, with no operator involvement.

    `since` is still tracked and logged as a high-water mark, but it no longer
    gates which files are read. It is diagnostic, not control flow.
    """
    if mode not in ("incremental", "full"):
        log.error(f"Unknown ingest mode: {mode!r}")
        return False

    log.info("=" * 60)
    log.info(f"INGEST  starting  (mode={mode})")
    log.info("=" * 60)

    # Fail before doing anything, not after silently producing nothing.
    problem = _share_unreachable()
    if problem:
        log.error("SHARE UNREACHABLE -- %s", problem)
        log.error("Aborting: the mart is UNCHANGED and still serving its previous data, "
                  "which is now going stale. Fix share access and re-run.")
        return False

    state = _load_state() if mode == "incremental" else {}
    if mode == "incremental":
        log.info(f"  Production high-water mark:  {state.get('production')    or '(none - first run)'}")
        log.info(f"  Paid hours high-water mark:  {state.get('paid_hours_raw') or '(none - first run)'}")
        log.info("  Reading the full share and merging (high-water marks are diagnostic only).")

    # since=None / exclude_dates=None in BOTH modes, deliberately.
    #
    # Passing a `since` or an exclusion set is what made a missed or partial
    # date permanent — see the note in this function's docstring. The mart
    # merge (below) is what protects history, not the read filter.
    prod_new  = ingest_production(since=None, exclude_dates=None)
    hours_new = ingest_paid_hours(since=None, exclude_dates=None)
    smh       = ingest_smh()  # SMH lives locally, no retention concern → always full

    if smh.empty:
        log.error("SMH data is empty -- aborting ingest.")
        return False

    if mode == "incremental":
        # MERGE NEW WITH EXISTING — preserves rows already in mart even if files
        # have been deleted from the share. new_df covers the whole share window,
        # so that window is rebuilt from source while everything older survives.
        # This is the ONLY thing protecting pre-retention history; the read
        # filter used to do it too, and that redundancy is what hid the bug.
        prod  = _merge_with_existing(MART["production"], prod_new,  "raw_production")
        hours = _merge_with_existing(MART["paid_hours"], hours_new, "raw_paid_hours")
    else:
        # FULL MODE — fresh state. Empty new data means the share is truly empty.
        if prod_new.empty:
            log.error("Production data is empty in full mode -- aborting ingest.")
            return False
        if hours_new.empty:
            log.error("Paid hours data is empty in full mode -- aborting ingest.")
            return False
        prod  = prod_new
        hours = hours_new

    if prod.empty:
        log.error("Production mart would be empty -- aborting (no historical data + no new files).")
        return False
    if hours.empty:
        log.error("Paid hours mart would be empty -- aborting (no historical data + no new files).")
        return False

    prod.to_parquet(MART["production"],  index=False)
    hours.to_parquet(MART["paid_hours"], index=False)
    smh.to_parquet(MART["smh"],          index=False)

    # Update state to reflect the latest file date NOW present in the share.
    # We track "latest file date we are aware of" — next incremental run will
    # only read files with a strictly newer date.
    new_state = dict(state)
    latest_prod  = _latest_file_date(PRODUCTION_PREFIX,     [NETWORK_PATH, RAWDATA_PATH])
    latest_hours = _latest_file_date(PAID_HOURS_RAW_PREFIX, [RAWDATA_PATH])
    if latest_prod:  new_state["production"]      = latest_prod
    if latest_hours: new_state["paid_hours_raw"]  = latest_hours
    new_state["last_run"]      = datetime.now().isoformat(timespec="seconds")
    new_state["last_run_mode"] = mode
    # Persisted so /api/health can report it without re-reading the share —
    # that is what makes a gap visible without anyone running a script.
    new_state["coverage"]      = _verify_coverage(prod, hours)
    _save_state(new_state)

    log.info("Mart written:")
    log.info(f"  raw_production.parquet  -> {len(prod)} rows")
    log.info(f"  raw_paid_hours.parquet  -> {len(hours)} rows")
    log.info(f"  smh_lookup.parquet      -> {len(smh)} rows")
    log.info(f"State updated -> {STATE_FILE}")
    log.info(f"  production high-water:  {new_state.get('production')}")
    log.info(f"  paid_hours_raw high-water: {new_state.get('paid_hours_raw')}")
    log.info("INGEST  complete")
    return True


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="OLE ingest pipeline")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--incremental", action="store_const", const="incremental", dest="mode",
                   help="Append new files to existing mart (default — preserves historical data)")
    g.add_argument("--full",        action="store_const", const="full",        dest="mode",
                   help="Re-read everything currently in the share, overwriting marts")
    p.set_defaults(mode="incremental")
    args = p.parse_args()
    run(mode=args.mode)
