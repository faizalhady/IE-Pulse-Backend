"""
compute.py
──────────
Reads the clean Parquet files from the mart, performs DuckDB JOINs,
computes OLE per workcell per date per shift, and writes ole_computed.parquet.

OLE formula:
  Numerator   = SUM(qty x smh_value)   [per workcell, date, shift]
  Denominator = SUM(tph_direct)         [per workcell, date, shift, ALL rows]
  OLE %       = (Numerator / Denominator) x 100

Source of truth for input hours: raw_paid_hours.parquet (from PEN_PaidHours_Raw_*).
SUM all rows regardless of value_type (VA, NVA, blank). value_type is only
used for the VA/NVA split display — never for the OLE denominator.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import json
import logging
import os

import duckdb
import pandas as pd

from core.paths import DATA_MART_DIR
from modules.ole import smh_scope
from modules.ole.config import MART, WORKCELL_CONFIG, INDIRECT_LABOR_CONFIG

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# Written by ingest; `coverage.share_from` is the oldest date the source share
# still holds. Same file ingest and /api/health read.
STATE_FILE = DATA_MART_DIR / ".ingest_state.json"

# What a model with no SMH row earns.
#   zero — it earns nothing (the truthful default; a gap looks like a gap)
#   avg  — it earns its workcell's volume-weighted average SMH
#
# An env var, not a constant, so switching it back is a restart rather than an
# edit-and-redeploy. The switch is fully reversible: the estimate is computed
# here at run time and never written to the `smh` table, so flipping back to
# `zero` and re-running reproduces the old numbers exactly.
# Keep already-published OLE for dates the share can no longer supply. See
# _freeze_history. Default off: the first run with it on locks that history for
# good, so it is a decision, not a deploy side effect.
FREEZE_HISTORY = os.getenv("OLE_FREEZE_HISTORY", "").strip().lower() in ("1", "true", "yes")

SMH_FALLBACK = os.getenv("OLE_SMH_FALLBACK", "zero").strip().lower()
if SMH_FALLBACK not in ("zero", "avg"):
    log.warning(f"OLE_SMH_FALLBACK={SMH_FALLBACK!r} is not 'zero' or 'avg' -- using 'zero'.")
    SMH_FALLBACK = "zero"


def _share_cutoff() -> pd.Timestamp | None:
    """Oldest date the share still covers, or None if unknown.

    None means "recompute everything", which is the old behaviour — a missing
    or unreadable state file must not silently freeze the whole mart.
    """
    try:
        share_from = (json.loads(STATE_FILE.read_text(encoding="utf-8")).get("coverage")
                      or {}).get("share_from")
        return pd.Timestamp(share_from) if share_from else None
    except Exception as e:                       # missing, corrupt, unparseable date
        log.warning(f"Could not read share cutoff ({e}); recomputing all dates.")
        return None


def _planner_demand() -> pd.DataFrame | None:
    """Planner demand, or None if that mart hasn't been built.

    Written by a different pipeline (modules/cycle_time/planner_demand.py) that
    is not in OLE's refresh chain. OLE must still compute without it, so a
    missing file is a warning and a narrower report — never a failed run.
    """
    p = DATA_MART_DIR / "demand" / "planner_demand.parquet"
    if not p.exists():
        log.warning("No planner demand mart at %s -- SMH scope will be MES-only.", p)
        return None
    return pd.read_parquet(p)


def _freeze_history(new: pd.DataFrame, cutoff: pd.Timestamp) -> pd.DataFrame:
    """Keep published OLE for dates whose source files the share no longer has.

    compute rebuilds every date from the raw marts each run, so an SMH edit today
    moves numbers for every past week. That is right while the source data is
    still there to check against — and wrong once retention has deleted it, since
    those weeks were reported and can no longer be verified.

    So: dates from `cutoff` on are recomputed as always; older dates keep the rows
    already in the mart. The cutoff moves forward on its own as retention eats the
    share, which means each run freezes a little more history.

    The catch, and it is not small: a frozen row can never be regenerated. The
    mart is its only copy — a later fix to this file will not reach it either.
    """
    if not MART["ole"].exists():
        return new
    old = pd.read_parquet(MART["ole"])
    kept = old[old["date"] < cutoff]
    if kept.empty:
        return new
    merged = pd.concat([kept, new[new["date"] >= cutoff]], ignore_index=True)
    log.info(f"History frozen before {cutoff.date()}: {len(kept)} rows kept as published, "
             f"{len(merged) - len(kept)} recomputed")
    return merged.sort_values(["workcell", "date", "shift"]).reset_index(drop=True)


def run() -> bool:
    log.info("=" * 60)
    log.info("COMPUTE  starting")
    log.info("=" * 60)

    for key in ["production", "paid_hours", "smh"]:
        if not MART[key].exists():
            log.error(f"Mart file missing: {MART[key]} -- run ingest first.")
            return False

    con = duckdb.connect()

    con.execute(f"CREATE VIEW production AS SELECT * FROM read_parquet('{MART['production']}')")
    con.execute(f"CREATE VIEW paid_hours AS SELECT * FROM read_parquet('{MART['paid_hours']}')")
    con.execute(f"CREATE VIEW smh_lookup AS SELECT * FROM read_parquet('{MART['smh']}')")
    log.info("Parquet views created")

    # ── Step 0: per-workcell average SMH, for the fallback ────────────────────
    # Weighted by volume, not a plain mean of the values: a model built 10,000
    # times should shape the average more than one built twice.
    con.execute("""
    CREATE TEMP TABLE smh_avg AS
    SELECT p.workcell,
           SUM(p.qty * s.smh_value) / NULLIF(SUM(p.qty), 0)      AS avg_smh
    FROM production p
    JOIN smh_lookup s
      ON p.workcell = s.workcell
     AND p.assembly = s.assembly
    WHERE s.smh_value > 0
    GROUP BY p.workcell
    """)
    # Per WORKCELL, not plant-wide: SMH differs by an order of magnitude between
    # an SMT workcell and a box-build one, and each workcell is one scan stage.
    fb = "COALESCE(a.avg_smh, 0)" if SMH_FALLBACK == "avg" else "0"
    log.info(f"SMH fallback for models with no SMH: {SMH_FALLBACK}")

    # ── Step 1: Output SMH per workcell / date / shift ────────────────────────
    con.execute(f"""
    CREATE TEMP TABLE output_smh AS
    SELECT
        p.workcell,
        p.date,
        p.shift,
        COUNT(DISTINCT p.assembly)                              AS assembly_count,
        SUM(p.qty)                                             AS total_qty,
        SUM(CASE WHEN s.smh_value > 0
                 THEN p.qty * s.smh_value
                 ELSE p.qty * {fb} END)                        AS effective_output_smh,
        -- How much of the above is guessed. 0 when the fallback is off, so the
        -- number is always answerable: "how much of this OLE % is estimated?"
        SUM(CASE WHEN s.smh_value > 0
                 THEN 0
                 ELSE p.qty * {fb} END)                        AS estimated_output_smh,
        -- Still counted as missing even when the fallback fills it. An estimate
        -- is not a measurement, and this is what the coverage page chases.
        SUM(CASE WHEN s.smh_value IS NULL OR s.smh_value = 0
                 THEN p.qty ELSE 0 END)                        AS qty_missing_smh,
        COUNT(DISTINCT CASE WHEN s.smh_value IS NULL
                            OR s.smh_value = 0
                            THEN p.assembly END)               AS assemblies_missing_smh
    FROM production p
    LEFT JOIN smh_lookup s
           ON p.workcell = s.workcell
          AND p.assembly = s.assembly
    LEFT JOIN smh_avg a
           ON p.workcell = a.workcell
    GROUP BY p.workcell, p.date, p.shift
    """)
    log.info("Step 1 complete -- output SMH computed")

    # ── Step 2: Aggregate input hours per workcell / date / shift ─────────────
    # ONLY for production workcells — indirect labor (warehouses/support) is
    # handled in Step 5 and written to its own mart. Keeping it out of
    # input_hours prevents indirect entities from leaking into ole_computed.
    # SUM all tph_direct rows regardless of value_type (VA/NVA/blank).
    workcell_list = ", ".join(f"'{w}'" for w in WORKCELL_CONFIG.keys())
    con.execute(f"""
    CREATE TEMP TABLE input_hours AS
    SELECT
        workcell,
        date,
        shift,
        COUNT(*)                                                AS headcount,
        SUM(thc_direct)                                         AS total_hc_direct,
        SUM(tph_direct)                                         AS total_input_hours,
        SUM(CASE WHEN value_type = 'VA'  THEN tph_direct ELSE 0 END) AS va_hours,
        SUM(CASE WHEN value_type = 'NVA' OR value_type = '' THEN tph_direct ELSE 0 END) AS nva_hours,
        COUNT(CASE WHEN value_type = 'VA'  THEN 1 END)                        AS va_count,
        COUNT(CASE WHEN value_type = 'NVA' OR value_type = '' THEN 1 END)     AS nva_count
    FROM paid_hours
    WHERE workcell IN ({workcell_list})
    GROUP BY workcell, date, shift
    """)
    log.info("Step 2 complete -- input hours aggregated for workcells only")

    # ── Step 3: JOIN and compute OLE ──────────────────────────────────────────
    # FULL OUTER JOIN — a (workcell, date, shift) cell appears if EITHER side
    # has data. Critical for catching shifts where employees were paid but no
    # production was scanned (NO_OUTPUT_SMH) — those hours must hit the OLE
    # denominator, otherwise we silently inflate the OLE % by hiding bad shifts.
    con.execute("""
    CREATE TEMP TABLE ole_result AS
    SELECT
        COALESCE(o.workcell, h.workcell)                        AS workcell,
        COALESCE(o.date,     h.date)                            AS date,
        COALESCE(o.shift,    h.shift)                           AS shift,

        s_meta.stage_label,
        s_meta.scan_stage,

        -- output side (0 when paid-hours-only shift)
        COALESCE(o.assembly_count,        0)                    AS assembly_count,
        COALESCE(o.total_qty,             0)                    AS total_qty,
        ROUND(COALESCE(o.effective_output_smh, 0), 4)           AS effective_output_smh,
        ROUND(COALESCE(o.estimated_output_smh, 0), 4)           AS estimated_output_smh,
        COALESCE(o.qty_missing_smh,       0)                    AS qty_missing_smh,
        COALESCE(o.assemblies_missing_smh, 0)                   AS assemblies_missing_smh,

        -- input side (0 when production-only shift)
        COALESCE(h.total_hc_direct,   0)                        AS hc_direct,
        COALESCE(h.total_input_hours, 0)                        AS total_input_hours,

        -- VA / NVA breakdown (display only, not used in OLE calculation)
        COALESCE(h.va_hours,          0)                        AS va_hours,
        COALESCE(h.nva_hours,         0)                        AS nva_hours,
        COALESCE(h.va_count,          0)                        AS va_count,
        COALESCE(h.nva_count,         0)                        AS nva_count,

        -- OLE calculation
        CASE
            WHEN COALESCE(h.total_input_hours, 0) = 0 THEN NULL
            ELSE ROUND(
                (COALESCE(o.effective_output_smh, 0) / h.total_input_hours) * 100, 2
            )
        END                                                      AS ole_pct,

        -- Data quality flag
        CASE
            WHEN COALESCE(h.total_input_hours,    0) = 0 THEN 'NO_INPUT_HOURS'
            WHEN COALESCE(o.effective_output_smh, 0) = 0 THEN 'NO_OUTPUT_SMH'
            WHEN COALESCE(o.qty_missing_smh,      0) > 0 THEN 'PARTIAL_SMH'
            ELSE 'OK'
        END                                                      AS data_quality

    FROM output_smh o

    FULL OUTER JOIN input_hours h
           ON o.workcell = h.workcell
          AND o.date     = h.date
          AND o.shift    = h.shift

    LEFT JOIN (
        SELECT DISTINCT workcell, stage_label, scan_stage
        FROM smh_lookup
    ) s_meta ON COALESCE(o.workcell, h.workcell) = s_meta.workcell

    ORDER BY workcell, date, shift
    """)
    log.info("Step 3 complete -- OLE computed (FULL OUTER JOIN - paid-hours-only shifts now visible)")

    # ── Step 4: SMH coverage per assembly ─────────────────────────────────────
    con.execute(f"""
    CREATE TEMP TABLE smh_assembly_status AS
    SELECT
        p.workcell,
        p.assembly,
        COALESCE(s.smh_value, 0)                                AS smh_value,
        SUM(p.qty)                                              AS total_qty_produced,
        MIN(p.date)                                             AS first_seen_date,
        MAX(p.date)                                             AS last_seen_date,
        COUNT(DISTINCT p.date)                                  AS active_days,
        -- Two states only. There was a third, MISSING_SMH, for a row that
        -- existed with value 0 — what the old .xls import produced from a blank
        -- cell. smh_store._clean_value now refuses to store 0 ("if the value
        -- isn't known, the row shouldn't exist"), so those assemblies have no
        -- row at all and land in NOT_IN_SMH_DB. The branch was unreachable and
        -- left the UI with a filter that matched nothing.
        CASE
            WHEN s.assembly IS NULL THEN 'NOT_IN_SMH_DB'
            ELSE                         'OK'
        END                                                     AS smh_status
    FROM read_parquet('{MART["production"]}') p
    LEFT JOIN read_parquet('{MART["smh"]}') s
           ON p.workcell = s.workcell
          AND p.assembly = s.assembly
    GROUP BY p.workcell, p.assembly, s.assembly, s.smh_value
    ORDER BY p.workcell, smh_status, total_qty_produced DESC
    """)
    log.info("Step 4 complete -- SMH assembly coverage computed")

    # ── Step 5: Indirect labor (warehouses, support pools) ────────────────────
    # Non-workcell entities have paid hours but no production / no SMH.
    # Written to a separate mart so they never leak into OLE calculations.
    indirect_list = ", ".join(f"'{e}'" for e in INDIRECT_LABOR_CONFIG.keys())
    if INDIRECT_LABOR_CONFIG:
        con.execute(f"""
        CREATE TEMP TABLE indirect_labor AS
        SELECT
            workcell                                                AS entity,
            date,
            shift,
            COUNT(*)                                                AS headcount,
            SUM(thc_direct)                                         AS total_hc_direct,
            ROUND(SUM(tph_direct), 4)                               AS total_input_hours,
            ROUND(SUM(CASE WHEN value_type = 'VA'  THEN tph_direct ELSE 0 END), 4) AS va_hours,
            ROUND(SUM(CASE WHEN value_type = 'NVA' OR value_type = '' THEN tph_direct ELSE 0 END), 4) AS nva_hours
        FROM paid_hours
        WHERE workcell IN ({indirect_list})
        GROUP BY workcell, date, shift
        ORDER BY workcell, date, shift
        """)
        log.info("Step 5 complete -- indirect labor aggregated")
    else:
        con.execute("CREATE TEMP TABLE indirect_labor AS SELECT NULL AS entity LIMIT 0")
        log.info("Step 5 skipped -- INDIRECT_LABOR_CONFIG is empty")

    # ── Export ────────────────────────────────────────────────────────────────
    result = con.execute("SELECT * FROM ole_result").df()

    result["smh_coverage_pct"] = (
        (result["total_qty"] - result["qty_missing_smh"])
        / result["total_qty"].replace(0, float("nan"))
        * 100
    ).round(1)

    # Off by default. Freezing is effectively irreversible — a frozen row can
    # never be regenerated from source — so it has to be switched on
    # deliberately, not acquired by deploying a new build.
    cutoff = _share_cutoff() if FREEZE_HISTORY else None
    if cutoff is not None:
        result = _freeze_history(result, cutoff)
    result.to_parquet(MART["ole"], index=False)

    # NOT frozen — coverage is a to-do list ("which assemblies still need an
    # SMH"), not a reported number. It should always reflect the SMH table as it
    # is now, including for assemblies last built before the cutoff.
    smh_status = con.execute("SELECT * FROM smh_assembly_status").df()
    smh_status = smh_scope.apply_scope(smh_status, _planner_demand())
    smh_status.to_parquet(MART["smh_status"], index=False)

    # Indirect labor — attach plant + label from config, then write to its own mart
    indirect = con.execute("SELECT * FROM indirect_labor").df()
    if not indirect.empty:
        indirect["plant"] = indirect["entity"].map(
            lambda e: INDIRECT_LABOR_CONFIG.get(e, {}).get("plant", "")
        )
        indirect["label"] = indirect["entity"].map(
            lambda e: INDIRECT_LABOR_CONFIG.get(e, {}).get("label", e)
        )
    indirect.to_parquet(MART["indirect_labor"], index=False)

    # ── Summary log ───────────────────────────────────────────────────────────
    log.info(f"OLE result:      {len(result)} rows written to ole_computed.parquet")
    log.info(f"SMH status:      {len(smh_status)} assembly rows written to smh_assembly_status.parquet")
    log.info(f"Indirect labor:  {len(indirect)} rows written to indirect_labor.parquet")

    summary = con.execute("""
        SELECT
            workcell, scan_stage,
            COUNT(*)                    AS shifts,
            ROUND(AVG(ole_pct), 2)      AS avg_ole_pct,
            ROUND(MIN(ole_pct), 2)      AS min_ole_pct,
            ROUND(MAX(ole_pct), 2)      AS max_ole_pct,
            ROUND(SUM(effective_output_smh), 1) AS total_output_smh,
            ROUND(SUM(total_input_hours), 1)    AS total_input_hours,
            COUNT(CASE WHEN data_quality != 'OK' THEN 1 END) AS flagged_shifts
        FROM ole_result
        GROUP BY workcell, scan_stage
        ORDER BY workcell
    """).df()

    smh_summary = con.execute("""
        SELECT workcell, smh_status,
               COUNT(*) AS assemblies,
               SUM(total_qty_produced) AS qty_produced
        FROM smh_assembly_status
        GROUP BY workcell, smh_status
        ORDER BY workcell, smh_status
    """).df()

    log.info("\n" + summary.to_string(index=False))
    log.info("\nSMH coverage breakdown:\n" + smh_summary.to_string(index=False))
    log.info("COMPUTE  complete")

    con.close()
    return True


if __name__ == "__main__":
    run()
