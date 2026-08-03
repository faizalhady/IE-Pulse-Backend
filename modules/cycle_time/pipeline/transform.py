"""
transform.py  (cycle_time)
──────────────────────────
Reads raw.parquet (one row per assembly/revision/sub_workcenter/process)
and pivots it into pivoted.parquet — one row per assembly/revision/sub_workcenter
with each process (BIRTH, SCRB, GLUEB, …) as its own column.

This matches the exact table layout shown in Image 2.
"""

import logging

import pandas as pd

from modules.cycle_time.config import CT_MART

log = logging.getLogger(__name__)

# ─── Pivot identity columns ───────────────────────────────────────────────────
# These stay as rows (not pivoted). Everything else is metadata or pivoted.
# IMPORTANT: do NOT include process-level fields here (grp, playbook, priority,
# order, alias, hc, etc.) — they vary per process and would prevent the pivot
# from collapsing rows. Only fields that are constant for an
# (assembly, revision, sub_workcenter) belong in the index.
_INDEX_COLS = [
    "customer",
    "division",
    "family",
    "assembly",
    "revision",
    "workcenter",
    "workcenter_type",
    "sub_workcenter",
]

# The friendly, line-specific process name (e.g. "MA 1", "HI-PORT", "BURN IN")
# lives in the API's `Alias` field — which is what the customer-facing Excel
# reports use. The raw `Process` field carries generic codes ("Assembly 1",
# "Hi-Pot 1") that aren't meaningful to engineers. We pivot on Alias so the
# pivoted.parquet column headers match the Excel report.
_PROCESS_COL   = "alias"
_VALUE_COL     = "cycle_time_per_process"


def run() -> bool:
    log.info("=" * 60)
    log.info("CYCLE TIME TRANSFORM  starting")
    log.info("=" * 60)

    if not CT_MART["raw"].exists():
        log.error(f"raw.parquet not found at {CT_MART['raw']}. Run ingest first.")
        return False

    # Read ONLY the columns this pivot touches — 12 of raw.parquet's 34.
    #
    # This is a memory fix, not a tidiness one. Measured 2026-08-03: reading all
    # 34 columns peaked at 11.6 GB RSS, which is fine on a 62 GB dev box (49s)
    # but thrashes a 16 GB server — the same run took 706s there, blocked on
    # paging rather than CPU. The columns we drop are the widest strings in the
    # file and are never referenced below.
    try:
        import pyarrow.parquet as pq
        available = set(pq.ParquetFile(CT_MART["raw"]).schema.names)
        wanted = list(dict.fromkeys(
            _INDEX_COLS + [_PROCESS_COL, _VALUE_COL, "process", "priority"]
        ))
        use_cols = [c for c in wanted if c in available] or None
    except Exception:
        use_cols = None                 # unreadable schema — fall back to all columns

    try:
        df = pd.read_parquet(CT_MART["raw"], columns=use_cols)
    except Exception as e:
        log.error(f"Could not read raw.parquet: {e}")
        return False

    log.info(f"Raw rows: {len(df)}"
             + (f" | {len(use_cols)} of {len(available)} columns read" if use_cols else ""))

    # ── Guard: required columns ───────────────────────────────────────────────
    required = [_PROCESS_COL, _VALUE_COL]
    missing = [c for c in required if c not in df.columns]
    if missing:
        log.error(f"Missing columns in raw.parquet: {missing}")
        log.error(f"Available columns: {list(df.columns)}")
        return False

    # ── Only keep index cols that actually exist in the data ──────────────────
    index_cols = [c for c in _INDEX_COLS if c in df.columns]

    # ── Ensure numeric value col ──────────────────────────────────────────────
    df[_VALUE_COL] = pd.to_numeric(df[_VALUE_COL], errors="coerce")

    # ── Pivot col fallback: alias → process when alias is null ────────────────
    # Some customers (e.g. DYSON, ADVA) don't populate the Alias field in IEDB.
    # Without this fallback their rows would have no column to land in and the
    # whole customer would be silently dropped from pivoted.parquet.
    if _PROCESS_COL == "alias" and "process" in df.columns:
        n_null_alias = df["alias"].isna().sum()
        if n_null_alias > 0:
            log.info(f"Filling {n_null_alias:,} null aliases with their process code "
                     f"(affects customers: "
                     f"{sorted(df.loc[df['alias'].isna(), 'customer'].dropna().unique().tolist())})")
            df["alias"] = df["alias"].fillna(df["process"])

    # ── Pivot: process rows → columns ─────────────────────────────────────────
    # aggfunc='first': if an assembly/sub_wc/process combination appears more than
    # once (e.g. duplicate entries), take the first occurrence.
    try:
        pivoted = df.pivot_table(
            index=index_cols,
            columns=_PROCESS_COL,
            values=_VALUE_COL,
            aggfunc="first",
        ).reset_index()
        pivoted.columns.name = None          # remove "process" label from column axis
    except Exception as e:
        log.error(f"Pivot failed: {e}")
        return False

    process_cols = [c for c in pivoted.columns if c not in index_cols]
    log.info(f"Pivoted: {len(pivoted)} rows | {len(process_cols)} process columns")
    log.info(f"Process columns found: {sorted(process_cols)}")

    # ── Build-level priority ──────────────────────────────────────────────────
    # priority is process-level (dropped by the pivot) but a build's routing rank
    # is well-defined as the MIN across its steps (1 = primary route). Merge it
    # back so the wide table / export can filter to primary builds.
    if "priority" in df.columns:
        prio = (
            df.groupby(index_cols, dropna=False)["priority"]
            .min()
            .reset_index()
            .rename(columns={"priority": "priority"})
        )
        pivoted = pivoted.merge(prio, on=index_cols, how="left")
        log.info("Merged build-level priority (min over steps) onto pivoted rows.")

    # ── Write ─────────────────────────────────────────────────────────────────
    pivoted.to_parquet(CT_MART["pivoted"], index=False)
    log.info(f"pivoted.parquet written → {CT_MART['pivoted']}")
    log.info("CYCLE TIME TRANSFORM  complete")
    return True
