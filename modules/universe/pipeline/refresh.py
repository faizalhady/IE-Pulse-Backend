"""
modules/universe/pipeline/refresh.py
────────────────────────────────────
Bring fact_scan forward from raw MES WipScanData pulls.

Two halves. The PARSE half (raw CSV -> fact_scan rows, keyed through the
registry, deduped on the scan key) works offline and is tested against the 30
hourly-pull CSVs already on disk: rebuilding from them must reproduce Phase 1's
fact_scan to the row. The PULL half (MES over HTTPS, hourly windows — case 42)
needs the plant network (ported from HUB/MES pull-wipscan.ts, 2026-08-23).

    python -m modules.universe.pipeline.refresh pull 2026-08-08 2026-08-22 [--force]   # UTC days [start, end) -> one CSV each
    python -m modules.universe.pipeline.refresh pull-paid-hours                        # copy new payroll files from the share, as UTF-8
    python -m modules.universe.pipeline.refresh count      # rows the raw CSVs hold, deduped
    python -m modules.universe.pipeline.refresh append     # fold every raw CSV into fact_scan (idempotent)

Case 70: the original puller ended every window at hh:59:00 and silently dropped
minute 59 of every hour (~1.2% of scans). Windows here end at hh:59:59 — the API
rejects hh:59:59.999 as 'more than 1 hour apart'.
"""

from __future__ import annotations

import csv
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import duckdb
import requests

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
            -- to the second: the API now returns milliseconds, the August pulls did not,
            -- and the scan key must match across both
            date_trunc('second', coalesce(try_strptime(completion_time, '%Y-%m-%dT%H:%M:%S'),
                     try_strptime(completion_time, '%Y-%m-%d %H:%M:%S'),
                     try_strptime(completion_time, '%m/%d/%Y %H:%M:%S'),
                     try_cast(completion_time as timestamp))) as completed_at_utc,
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


RAW_COLS = ["completion_time", "wip_id", "customer", "division", "assembly", "revision", "site", "building",
            "manufacturing_area", "manufacturing_area_id", "route", "route_id", "step", "step_id",
            "step_instance", "step_instance_id", "equipment", "equipment_id", "process_loop", "test_loop", "test_status"]
_API_COLS = ["CompletionTime", "WipId", "Customer", "Division", "Assembly", "Revision", "Site", "Building",
             "ManufacturingArea", "ManufacturingAreaId", "Route", "RouteId", "Step", "StepId",
             "StepInstance", "StepInstanceId", "Equipment", "EquipmentId", "ProcessLoop", "TestLoop", "TestStatus"]
_session = requests.Session()   # one TLS handshake per run, not per call


def _mes_hour(day: str, h: int, tries: int = 3) -> list[dict]:
    """One window, strictly under an hour (case 42): hh:00:00 -> hh:59:59 UTC.
    A dead hour raises — it must never look like an empty hour (silent gap)."""
    body = {"RouteStep": [], "StepInstance": [], "LangId": 0,
            "StartDateTime": f"{day}T{h:02d}:00:00.000Z", "EndDateTime": f"{day}T{h:02d}:59:59.000Z"}
    err = None
    for t in range(tries):
        try:
            r = _session.post(f"{C.MES_WEBAPI_BASE}/Wip/WipScanData", json=body,
                              headers={"APIKey": C.MES_WEBAPI_KEY}, timeout=180)
            r.raise_for_status()
            rows = r.json()
            if isinstance(rows, dict):                      # MES sometimes wraps the array
                rows = next((v for v in rows.values() if isinstance(v, list)), [])
            return [x for x in rows if x and x.get("WipId") is not None]
        except Exception as e:                              # 404/5xx/timeouts are intermittent on this SP
            err = e
            time.sleep(2 * (t + 1))
    raise RuntimeError(f"MES {day} hour {h:02d} failed after {tries} tries: {err}")


def pull_paid_hours() -> list[Path]:
    """Copy payroll files the share has and we do not into RAW_PAID_HOURS_DIR as UTF-8.
    The share keeps ~60 rolling files and rotates the oldest away, so the local copy
    is the only place the history accumulates. Six of 61 files were cp1252 (case 71)."""
    C.RAW_PAID_HOURS_DIR.mkdir(parents=True, exist_ok=True)
    new = []
    for src in sorted(C.PAID_HOURS_SHARE.glob(f"{C.PAID_HOURS_PREFIX}*.csv")):
        dst = C.RAW_PAID_HOURS_DIR / src.name
        if dst.exists():
            continue
        b = src.read_bytes()
        try:
            text = b.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = b.decode("cp1252")
        dst.write_text(text, encoding="utf-8", newline="")
        new.append(dst)
    log.info("paid hours: %d new files copied", len(new))
    return new


def pull(start: date, end: date, force: bool = False, workers: int = 4) -> list[Path]:
    """MES WipScanData for the UTC days [start, end) -> one CSV per day in
    RAW_WIPSCAN_DIR, RAW_COLS shape. A day's file appears only once all 24 hours
    came back, so a crash leaves nothing half-true; days already on disk are
    skipped unless force. Needs the plant network and MES_WEBAPI_KEY."""
    if not C.MES_WEBAPI_KEY:
        raise RuntimeError("MES_WEBAPI_KEY not set in .env")
    C.RAW_WIPSCAN_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range((end - start).days):
        day = (start + timedelta(days=i)).isoformat()
        dst = C.RAW_WIPSCAN_DIR / f"wipscan_{day}.csv"
        if dst.exists() and not force:
            out.append(dst)
            continue
        with ThreadPoolExecutor(workers) as ex:
            hours = list(ex.map(lambda h: _mes_hour(day, h), range(24)))
        tmp = dst.with_suffix(".part")
        n = 0
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(RAW_COLS)
            for rows in hours:
                for r in rows:
                    w.writerow(["" if r.get(k) is None else r.get(k) for k in _API_COLS])
                    n += 1
        tmp.replace(dst)
        out.append(dst)
        log.info("%s  %9d scans", day, n)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "count"
    if cmd == "count":
        print(count_from_raw())
    elif cmd == "append":
        print(append(raw_files()))
    elif cmd == "pull-paid-hours":
        print(len(pull_paid_hours()), "new files")
    elif cmd == "pull":
        files = pull(date.fromisoformat(sys.argv[2]), date.fromisoformat(sys.argv[3]), force="--force" in sys.argv)
        print(len(files), "files")
    else:
        raise SystemExit(f"unknown command {cmd}")
