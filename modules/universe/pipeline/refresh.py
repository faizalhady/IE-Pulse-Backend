"""
modules/universe/pipeline/refresh.py
────────────────────────────────────
Bring fact_scan forward from raw MES WipScanData pulls.

Two halves. The PARSE half (raw CSV -> fact_scan rows, keyed through the
registry, deduped on the scan key) works offline and is tested against the 30
hourly-pull CSVs already on disk: rebuilding from them must reproduce Phase 1's
fact_scan to the row. The PULL half (MES over HTTPS, hourly windows — case 42)
needs the plant network and is wired but not exercised until the VPN is back.

    python -m modules.universe.pipeline.refresh count      # rows the raw CSVs hold, deduped
    python -m modules.universe.pipeline.refresh append     # fold every raw CSV into fact_scan (idempotent)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import duckdb

from modules.universe import config as C

log = logging.getLogger(__name__)

SCAN_KEY = "wip_id, step, step_instance, completed_at_utc"


def _raw_sql(files: list[Path]) -> str:
    """SELECT over the raw pull CSVs in fact_scan's column shape. MES returns two
    timestamp formats (case 52); both are parsed. Keys come through the registry:
    workcell via the mes_name alias, model via (workcell, normalised assembly)."""
    lst = "[" + ", ".join(f"'{f.as_posix()}'" for f in files) + "]"
    M = {k: v.as_posix() for k, v in C.UNIVERSE_MART.items()}
    return f"""
        with raw as (
          -- quote is explicit: division holds "BECTON, DICKINSON AND COMPANY" and auto-detect chose none
          select * from read_csv({lst}, header = true, union_by_name = true, all_varchar = true, quote = '"', escape = '"')
        ),
        parsed as (
          select
            coalesce(try_strptime(completion_time, '%Y-%m-%dT%H:%M:%S'),
                     try_strptime(completion_time, '%Y-%m-%d %H:%M:%S'),
                     try_strptime(completion_time, '%m/%d/%Y %H:%M:%S'),
                     try_cast(completion_time as timestamp)) as completed_at_utc,
            wip_id, customer, division, assembly, revision, site, building,
            manufacturing_area, route, step, step_instance, equipment, equipment_id,
            try_cast(process_loop as integer) as process_loop,
            try_cast(test_loop as integer) as test_loop,
            nullif(test_status, '') as test_status
          from raw
        ),
        keyed as (
          select p.*,
                 p.completed_at_utc + interval 8 hour as completed_at_local,
                 coalesce(a.workcell_id, 0) as workcell_id,
                 m.model_id
          from parsed p
          left join (select workcell_id, regexp_replace(upper(value), '[^A-Z0-9]', '', 'g') as k
                     from read_parquet('{M["workcell_alias"]}') where system = 'mes_name') a
            on a.k = regexp_replace(upper(p.customer), '[^A-Z0-9]', '', 'g')
          left join read_parquet('{M["dim_model"]}') m
            on m.workcell_id = a.workcell_id and m.match_key = regexp_replace(upper(p.assembly), '[^A-Z0-9]', '', 'g')
        )
        select wip_id, step, step_instance, completed_at_utc, completed_at_local,
               cast(completed_at_local as date) as date,
               case when hour(completed_at_local) between 7 and 18 then 2 else 3 end as shift,
               case when hour(completed_at_local) < 7 then cast(completed_at_local as date) - 1
                    else cast(completed_at_local as date) end as shift_date,
               workcell_id, model_id,
               manufacturing_area as bay_id, cast(null as varchar) as process_type_id, equipment_id,
               process_loop, test_loop, test_status,
               customer as workcell_raw, assembly as part_number_raw, revision as revision_raw, route as route_raw,
               manufacturing_area as area_raw, equipment as equipment_raw, building as plant_raw,
               cast(null as varchar) as shift_name_raw
        from keyed
        where completed_at_utc is not null
    """


def raw_files(raw_dir: Path | None = None) -> list[Path]:
    d = raw_dir or C.RAW_WIPSCAN_DIR
    return sorted(d.glob("*.csv"))


def count_from_raw(raw_dir: Path | None = None) -> int:
    """Distinct scan keys across every raw CSV — what fact_scan would hold if
    rebuilt from them. The offline acceptance test for the parse half."""
    files = raw_files(raw_dir)
    if not files:
        return 0
    con = duckdb.connect()
    try:
        (n,) = con.execute(f"select count(*) from (select distinct {SCAN_KEY} from ({_raw_sql(files)}))").fetchone()
    finally:
        con.close()
    return n


def append(files: list[Path]) -> dict:
    """Fold raw pulls into fact_scan. Existing rows win on the scan key, so
    re-appending a window changes nothing (idempotent). Rebuilds the derived
    tables afterwards."""
    from modules.universe.pipeline import build
    dst = C.UNIVERSE_MART["fact_scan"]
    tmp = dst.with_suffix(".appending.parquet")
    con = duckdb.connect()
    try:
        (before,) = con.execute(f"select count(*) from read_parquet('{dst.as_posix()}')").fetchone() if dst.exists() else (0,)
        existing = f"select *, 0 as _src from read_parquet('{dst.as_posix()}')" if dst.exists() else None
        incoming = f"select *, 1 as _src from ({_raw_sql(files)})"
        union = f"{existing} union all by name {incoming}" if existing else incoming
        con.execute(f"""
            copy (
              select * exclude (_src) from ({union})
              qualify row_number() over (partition by {SCAN_KEY} order by _src) = 1
              order by completed_at_utc
            ) to '{tmp.as_posix()}' (format parquet, row_group_size 1000000)
        """)
        (after,) = con.execute(f"select count(*) from read_parquet('{tmp.as_posix()}')").fetchone()
    finally:
        con.close()
    tmp.replace(dst)
    log.info("fact_scan: %d -> %d rows (+%d) from %d files", before, after, after - before, len(files))
    report = {"fact_scan_before": before, "fact_scan_after": after, "files": len(files)}
    if after != before:
        report.update(build.build_terminal_step_and_units())
        report.update(build.build_ole_reconciliation())
    return report


def pull(start, end) -> list[Path]:
    """MES WipScanData over HTTPS in hourly windows (case 42) -> raw CSVs in
    RAW_WIPSCAN_DIR. Needs the plant network. Ported from HUB/MES pull-wipscan.ts
    when the VPN is back; until then this raises so nothing pretends to refresh."""
    raise NotImplementedError("MES pull needs the plant network — port pull-wipscan.ts here when the VPN is back")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "count"
    if cmd == "count":
        print(count_from_raw())
    elif cmd == "append":
        print(append(raw_files()))
    else:
        raise SystemExit(f"unknown command {cmd}")
