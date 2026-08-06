"""
backfill_indirect_labor.py
──────────────────────────
ONE-OFF SCRIPT — run once after adding INDIRECT_LABOR_CONFIG entities.

What it does:
  1. Reads every PEN_PaidHours_Raw_*.csv in the network share
  2. Extracts ONLY rows for indirect labor entities (Warehouse / Support)
  3. Date-stitches them (newest file first, only-new-dates per file)
  4. Merges into the existing raw_paid_hours.parquet WITHOUT touching
     any existing workcell rows
  5. Re-runs compute.py + compute_weekly.py to refresh the marts

What it does NOT do:
  - Does NOT touch workcell paid_hours rows (they stay exactly as-is)
  - Does NOT touch production data
  - Does NOT call --full anywhere
  - Does NOT delete or wipe anything

Safe to run multiple times — idempotent thanks to date-stitching.

Usage:
  python backfill_indirect_labor.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logging
import pandas as pd

from modules.ole.config import (
    MART,
    RAWDATA_PATH,
    PAID_HOURS_RAW_PREFIX,
    INDIRECT_LABOR_CONFIG,
)
from modules.ole.pipeline.ingest import (
    _find_csv_files,
    _active_raw_names,
    _normalise_workcell,
    _parse_date,
    _cutoff_date,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)


def _read_indirect_rows_from_files(files: list[Path]) -> pd.DataFrame:
    """Stitch indirect-labor rows out of paid-hours CSVs, newest first."""
    indirect_canonical = set(INDIRECT_LABOR_CONFIG.keys())
    # Raw CSV names that normalize to one of the indirect entities:
    indirect_raw_names = {
        raw for raw in _active_raw_names()
        if _normalise_workcell(raw) in indirect_canonical
    }
    log.info(f"Indirect entities to backfill: {sorted(indirect_canonical)}")
    log.info(f"Raw CSV names recognized: {sorted(indirect_raw_names)}")

    files = sorted(files, reverse=True)  # newest first
    cutoff = _cutoff_date()
    seen_dates: set = set()
    frames = []

    for f in files:
        try:
            try:
                df = pd.read_csv(f, dtype=str, encoding='utf-8')
            except UnicodeDecodeError:
                df = pd.read_csv(f, dtype=str, encoding='windows-1252')

            df.columns = [c.strip() for c in df.columns]
            wc_col   = next((c for c in df.columns if c == 'WorkCell'), None)
            date_col = next((c for c in df.columns if c == 'Startdate'), None)
            if not (wc_col and date_col):
                log.info(f"  Skipped (missing WorkCell/Startdate cols): {f.name}")
                continue

            df = df[df[wc_col].isin(indirect_raw_names)]
            df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
            df = df[df[date_col] >= cutoff]

            if df.empty:
                log.info(f"  Skipped (no indirect rows in file): {f.name}")
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
                f"  Stitched indirect rows: {f.name}  "
                f"({len(df)} rows, {len(new_dates)} new date(s): {d[0]} ... {d[-1]})"
            )
            frames.append(df)
        except Exception as e:
            log.error(f"  Failed to read {f.name}: {e}")

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _normalize_indirect_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Apply the same column renaming/typing as ingest_paid_hours does."""
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
    df["total_input_hours"] = df["tph_direct"]

    df["workcell"] = df["workcell_raw"].apply(_normalise_workcell)
    df = df[df["workcell"].notna()].copy()
    df = df[df["date"] >= _cutoff_date()].copy()

    return df[[
        "site", "cost_center", "workcell", "sub_workcell",
        "thc_direct", "tph_direct", "total_input_hours",
        "date", "shift",
        "value_type",
        "position", "cv", "name",
        "date_joined", "day_joined", "category",
    ]].copy()


def main():
    log.info("=" * 70)
    log.info("INDIRECT LABOR BACKFILL  starting")
    log.info("=" * 70)

    files = _find_csv_files(PAID_HOURS_RAW_PREFIX, [RAWDATA_PATH])
    if not files:
        log.error("No paid hours CSVs found in the share. Check VPN / path.")
        sys.exit(1)
    log.info(f"Found {len(files)} paid-hours file(s) to scan")

    indirect_raw = _read_indirect_rows_from_files(files)
    if indirect_raw.empty:
        log.warning("No indirect-labor rows found in any file. Nothing to merge.")
        sys.exit(0)

    log.info(f"Total indirect-labor rows collected: {len(indirect_raw)}")

    indirect = _normalize_indirect_frame(indirect_raw)
    log.info(f"After normalisation: {len(indirect)} rows")
    log.info(f"  per-entity row count: {indirect['workcell'].value_counts().to_dict()}")

    # ── Merge with existing raw_paid_hours.parquet ───────────────────────────
    paid_hours_path = MART["paid_hours"]
    if not paid_hours_path.exists():
        log.error(
            f"raw_paid_hours.parquet not found at {paid_hours_path}. "
            "Backfill expects existing workcell data - run pipeline/refresh.py first."
        )
        sys.exit(1)

    existing = pd.read_parquet(paid_hours_path)
    log.info(f"Existing raw_paid_hours.parquet: {len(existing)} rows")
    existing_indirect_rows = existing[existing["workcell"].isin(INDIRECT_LABOR_CONFIG.keys())]
    if len(existing_indirect_rows):
        log.info(
            f"  Note: existing mart already contains {len(existing_indirect_rows)} "
            f"indirect rows - they will be replaced for matching dates."
        )

    # Date-aware merge: drop existing indirect-labor rows whose date is in the
    # new data (newer wins). Don't touch workcell rows at all.
    new_dates = set(pd.to_datetime(indirect["date"]).dt.date.dropna().unique())
    existing_dates = pd.to_datetime(existing["date"]).dt.date
    is_indirect = existing["workcell"].isin(INDIRECT_LABOR_CONFIG.keys())
    to_drop = is_indirect & existing_dates.isin(new_dates)
    existing_keep = existing[~to_drop]

    combined = pd.concat([existing_keep, indirect], ignore_index=True)
    log.info(
        f"Merged: {len(existing_keep)} kept + {len(indirect)} new = "
        f"{len(combined)} total ({to_drop.sum()} existing indirect rows replaced)"
    )

    # Sanity check: workcell row count must be unchanged
    workcell_before = (~is_indirect).sum()
    workcell_after  = (~combined["workcell"].isin(INDIRECT_LABOR_CONFIG.keys())).sum()
    if workcell_before != workcell_after:
        log.error(
            f"SAFETY CHECK FAILED: workcell row count changed "
            f"({workcell_before} -> {workcell_after}). Aborting without writing."
        )
        sys.exit(1)
    log.info(f"Safety check OK: workcell rows unchanged ({workcell_before} preserved)")

    combined.to_parquet(paid_hours_path, index=False)
    log.info(f"Wrote {paid_hours_path}")

    # ── Refresh computed marts so /api/indirect-labor sees the new data ──────
    log.info("\n" + "=" * 70)
    log.info("Running compute.py to refresh ole_computed + indirect_labor mart")
    log.info("=" * 70)
    from pipeline.compute import run as run_compute
    if not run_compute():
        log.error("compute.py failed. Backfill data is in raw_paid_hours.parquet, "
                  "but indirect_labor.parquet was not refreshed.")
        sys.exit(1)

    log.info("\n" + "=" * 70)
    log.info("Running compute_weekly.py")
    log.info("=" * 70)
    from pipeline.compute_weekly import run as run_weekly
    run_weekly()

    log.info("\n" + "=" * 70)
    log.info("INDIRECT LABOR BACKFILL  complete")
    log.info("=" * 70)
    log.info("Test with:")
    log.info("  curl http://127.0.0.1:9007/api/indirect-labor")


if __name__ == "__main__":
    main()
