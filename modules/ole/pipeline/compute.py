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

import logging
import duckdb
import pandas as pd

from modules.ole.config import MART, WORKCELL_CONFIG, INDIRECT_LABOR_CONFIG

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)


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

    # ── Step 1: Output SMH per workcell / date / shift ────────────────────────
    con.execute("""
    CREATE TEMP TABLE output_smh AS
    SELECT
        p.workcell,
        p.date,
        p.shift,
        COUNT(DISTINCT p.assembly)                              AS assembly_count,
        SUM(p.qty)                                             AS total_qty,
        SUM(CASE WHEN s.smh_value > 0
                 THEN p.qty * s.smh_value ELSE 0 END)          AS effective_output_smh,
        SUM(CASE WHEN s.smh_value IS NULL OR s.smh_value = 0
                 THEN p.qty ELSE 0 END)                        AS qty_missing_smh,
        COUNT(DISTINCT CASE WHEN s.smh_value IS NULL
                            OR s.smh_value = 0
                            THEN p.assembly END)               AS assemblies_missing_smh
    FROM production p
    LEFT JOIN smh_lookup s
           ON p.workcell = s.workcell
          AND p.assembly = s.assembly
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

    result.to_parquet(MART["ole"], index=False)

    smh_status = con.execute("SELECT * FROM smh_assembly_status").df()
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
