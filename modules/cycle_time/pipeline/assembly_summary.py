"""
assembly_summary.py  (cycle_time)
─────────────────────────────────
Builds assembly_summary.parquet — ONE row per (customer, assembly) holding
exactly what the "Cycle Time by Assembly" list page renders, with the heavy
bits PRECOMPUTED so the /assembly-list endpoint is a cheap read instead of a
live aggregation over millions of raw rows.

Precomputed per assembly:
  • identity        — family
  • build counts    — builds, revisions, primary_builds, has_alternates
  • stage footprint — has_smt / has_th / has_be
  • SMH             — operator content per unit (the value that was being
                      recomputed on every page load and slowing KEYSIGHT down)
  • eff             — line efficiency, joined from eff_by_line.parquet when it
                      exists (built from GetSummaryGroupProcessData). NULL until
                      that pull has run.

This is a LOCAL transform over raw.parquet (already on disk) — no API calls.
Runs in seconds-to-minutes even for the largest customer.
"""

import logging

import duckdb

from modules.cycle_time.config import CT_MART

log = logging.getLogger(__name__)


def run() -> bool:
    log.info("=" * 60)
    log.info("CYCLE TIME ASSEMBLY-SUMMARY  starting")
    log.info("=" * 60)

    raw = CT_MART["raw"]
    if not raw.exists():
        log.error(f"raw.parquet not found at {raw}. Run ingest first.")
        return False

    out = CT_MART["assembly_summary"]
    eff = CT_MART["eff_by_line"]
    has_eff = eff.exists()
    log.info(f"eff_by_line {'found - joining efficiency' if has_eff else 'not found - eff will be NULL'}")

    # SMH per assembly mirrors the API helper: operator content per unit on the
    # PRIMARY routing (priority = 1) = Σ (IMT + Hand) × (S%/100) per build,
    # averaged across the assembly's priority-1 revisions.
    #
    # eff is per LINE (sub_workcenter). An assembly can span lines, so we take
    # the average eff across the lines it routes through (priority-1). When the
    # eff lookup is missing we still emit the column (NULL) so the schema is
    # stable.
    eff_join = ""
    eff_select = "CAST(NULL AS DOUBLE)        AS eff,"
    if has_eff:
        eff_join = f"""
            LEFT JOIN read_parquet('{eff.as_posix()}') el
              ON el.customer = base.customer
             AND el.sub_workcenter = base.sub_workcenter
        """
        eff_select = "AVG(el.eff)                AS eff,"

    sql = f"""
    COPY (
        WITH base AS (
            SELECT customer, assembly, family, revision, sub_workcenter,
                   workcenter, priority, imt, hand, sampling
            FROM read_parquet('{raw.as_posix()}')
        ),
        smh AS (
            SELECT customer, assembly, AVG(build_smh) AS smh
            FROM (
                SELECT customer, assembly, revision,
                       SUM((COALESCE(imt, 0) + COALESCE(hand, 0))
                           * (COALESCE(sampling, 100) / 100.0)) AS build_smh
                FROM base
                WHERE priority = 1
                GROUP BY customer, assembly, revision
            )
            GROUP BY customer, assembly
        )
        SELECT base.customer,
               base.assembly,
               ANY_VALUE(base.family)                                          AS family,
               COUNT(DISTINCT base.revision || '|' || base.sub_workcenter)     AS builds,
               COUNT(DISTINCT base.revision)                                   AS revisions,
               COUNT(DISTINCT base.revision) FILTER (WHERE base.priority = 1)  AS primary_builds,
               BOOL_OR(base.priority > 1)                                      AS has_alternates,
               BOOL_OR(base.workcenter = 'SMT')                                AS has_smt,
               BOOL_OR(base.workcenter = 'TH')                                 AS has_th,
               BOOL_OR(base.workcenter = 'BE')                                 AS has_be,
               ANY_VALUE(sm.smh)                                               AS smh,
               {eff_select}
        FROM base
        LEFT JOIN smh sm USING (customer, assembly)
        {eff_join}
        GROUP BY base.customer, base.assembly
        ORDER BY base.customer, base.assembly
    ) TO '{out.as_posix()}' (FORMAT PARQUET);
    """

    con = duckdb.connect()
    try:
        con.execute(sql)
        n = con.execute(f"SELECT count(*) FROM read_parquet('{out.as_posix()}')").fetchone()[0]
        log.info(f"assembly_summary.parquet written ({n:,} assemblies) -> {out}")
    except Exception as e:
        log.error(f"assembly_summary build failed: {e}")
        return False
    finally:
        con.close()

    log.info("CYCLE TIME ASSEMBLY-SUMMARY  complete")
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
    run()
