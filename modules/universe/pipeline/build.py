"""
modules/universe/pipeline/build.py
──────────────────────────────────
Promote the August registry into the universe's tested tables.

Phase 1, wave 1: dim_workcell + workcell_alias, dim_calendar, dim_shift.
Each builder is idempotent — it rewrites its parquet from the sources every time.
The acceptance tests live in tests/test_universe.py and were written first.

Run: python -m modules.universe.pipeline.build
"""

from __future__ import annotations

import logging
from datetime import date

import duckdb
import openpyxl
import pandas as pd

from core.naming import canon
from modules.universe import config as C
from modules.universe import registry

log = logging.getLogger(__name__)


# ─── dim_workcell + workcell_alias ───────────────────────────────────────────

def _read_sheet_blocks() -> tuple[dict[str, str], dict[str, str]]:
    """workcell group.xlsx → ({sheet name: region}, {sheet name: governing plant}).
    Left block A/B = region, right block D/E/F = plant (confirmed by Faiz 2026-08-06)."""
    region, plant = {}, {}
    if not C.WORKCELL_GROUP_XLSX.exists():
        log.warning("workcell group sheet missing: %s", C.WORKCELL_GROUP_XLSX)
        return region, plant
    ws = openpyxl.load_workbook(C.WORKCELL_GROUP_XLSX, data_only=True).worksheets[0]
    col_region = {"A": "Penang Island", "B": "Batu Kawan"}
    col_plant = {"D": "P1", "E": "P2", "F": "BK"}
    headers = {"PENANG ISLAND WC", "BK WC", "P1", "P2", "BK"}
    # Row 1 holds the plant headers (D/E/F); row 2 holds the REGION headers (A/B)
    # but already the first PLANT names (D/E/F) — Tellabs lives in F2. So read
    # from row 2 and skip header strings by value, not by row.
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            v = str(cell.value).strip() if cell.value is not None else ""
            if not v or v.upper() in headers:
                continue
            if cell.column_letter in col_region:
                region[v] = col_region[cell.column_letter]
            elif cell.column_letter in col_plant:
                plant[v] = col_plant[cell.column_letter]
    return region, plant


def build_dim_workcell() -> dict:
    src = C.REGISTRY_DIR / "workcell.csv"
    wc = pd.read_csv(src, encoding="utf-8-sig")
    al = pd.read_csv(C.REGISTRY_DIR / "workcell_alias.csv", encoding="utf-8-sig")

    # --- aliases first: the sheet's own spellings become alias rows, so the
    #     resolver (and anyone else) can find them later without re-parsing xlsx
    sheet_region, sheet_plant = _read_sheet_blocks()
    # Priority: a workcell's own match_key, then its name, then aliases — first
    # wins. The alias table carries two meanings at once (a spelling that belongs
    # to a workcell, and a customer whose cycle-time data FOLDS into another), so
    # eight spellings point at two ids. The canonical row wins; every conflict is
    # written to workcell_alias_conflict, not silently picked.
    known: dict[str, int] = {}
    for v, i in list(zip(wc["match_key"], wc["id"])) + list(zip(wc["name"], wc["id"])):
        known.setdefault(canon(str(v)), int(i))
    conflicts: dict[str, dict] = {}
    for v, i, sysname in zip(al["value"], al["workcell_id"], al["system"]):
        k = canon(str(v))
        if not k or k == "0":                       # sap:0 is a null, not a spelling
            continue
        if k in known and known[k] != int(i):
            c = conflicts.setdefault(k, {"spelling": k, "ids": {known[k]}, "claims": []})
            c["ids"].add(int(i)); c["claims"].append(f"{sysname}:{v}->{int(i)}")
        known.setdefault(k, int(i))
    for k, c in conflicts.items():
        c["canonical_id"] = known[k]
    conflict_df = pd.DataFrame([
        {"spelling": c["spelling"], "canonical_id": c["canonical_id"],
         "ids": sorted(c["ids"]), "claims": c["claims"]} for c in conflicts.values()])
    if len(conflict_df):
        log.warning("%d alias spellings point at 2+ workcells — recorded, not resolved: %s",
                    len(conflict_df), sorted(conflicts))

    def resolve_sheet(name: str) -> int | None:
        target = C.SHEET_NAME_MAP.get(name, name)
        return known.get(canon(target))

    sheet_names = set(sheet_region) | set(sheet_plant)
    unresolved, new_alias = [], []
    for n in sorted(sheet_names):
        wid = resolve_sheet(n)
        if wid is None:
            unresolved.append(n)
            continue
        if canon(n) not in known:
            new_alias.append({"workcell_id": wid, "system": "workcell_group_sheet", "value": n,
                              "valid_from": None, "valid_to": None})
            known[canon(n)] = wid
    if unresolved:
        log.warning("workcell group sheet: %d names resolve to no workcell — NOT guessed: %s",
                    len(unresolved), unresolved)

    alias = pd.concat([al[["workcell_id", "system", "value", "valid_from", "valid_to"]],
                       pd.DataFrame(new_alias)], ignore_index=True)
    alias = alias.drop_duplicates(subset=["system", "value"]).reset_index(drop=True)
    alias["workcell_id"] = alias["workcell_id"].astype("int64")

    # --- plant: two facts. Governing from the sheet's plant block, else the
    #     registry; physical = governing unless the override says BK.
    by_id_plant = {}
    by_id_region = {}
    for n, p in sheet_plant.items():
        wid = resolve_sheet(n)
        if wid is not None:
            by_id_plant.setdefault(wid, p)
    for n, r in sheet_region.items():
        wid = resolve_sheet(n)
        if wid is not None:
            by_id_region.setdefault(wid, r)

    def governing(row) -> str | None:
        return by_id_plant.get(int(row["id"])) or C.PLANT_CODE.get(str(row["plant"]))

    out = pd.DataFrame({
        "workcell_id": wc["id"].astype("int64"),
        "name": wc["name"],
        "match_key": wc["match_key"],
        "entity_type": wc["entity_type"],
        "serves_workcell_id": wc["serves_workcell_id"],
        "status": wc["status"],
        "division": wc["division"],
        "mes_customer_id_primary": wc["mes_customer_id_primary"],
        "parent_id": pd.Series([None] * len(wc), dtype="object"),      # families unverified — §8.1 #14
        "parent_id_proposed": wc["parent_id"],                          # the August proposal, kept
        "confidence": wc["confidence"],
        "source_systems": wc["source_systems"],
        "valid_from": wc["valid_from"],
        "valid_to": wc["valid_to"],
        "notes": wc["notes"],
    })
    out["plant_governing"] = wc.apply(governing, axis=1)
    out["plant_physical"] = [
        "BK" if n in C.PHYSICALLY_BK_GOVERNED_BY_P1 else g
        for n, g in zip(out["name"], out["plant_governing"])]
    out["region"] = [
        by_id_region.get(int(i)) or (C.PLANT_REGION.get(p) if p else None) or (r if isinstance(r, str) else None)
        for i, p, r in zip(out["workcell_id"], out["plant_physical"], wc["region"])]
    out["source"] = f"registry {src.name} + {C.WORKCELL_GROUP_XLSX.name} (built {date.today().isoformat()})"

    # The unknown member (case 6): a scan whose Customer_ID resolves to nothing
    # lands here, never on a NULL key. Zero rows may land on it today; it exists
    # so the join can never silently drop a board.
    unknown = {c: None for c in out.columns}
    unknown.update({"workcell_id": 0, "name": "UNKNOWN", "match_key": "UNKNOWN", "entity_type": "unknown",
                    "status": "n/a", "confidence": "n/a", "source": "universe: unknown member (case 6)"})
    out = pd.concat([out, pd.DataFrame([unknown])], ignore_index=True)
    out["workcell_id"] = out["workcell_id"].astype("int64")

    out.to_parquet(C.UNIVERSE_MART["dim_workcell"], index=False)
    alias.to_parquet(C.UNIVERSE_MART["workcell_alias"], index=False)
    conflict_df.to_parquet(C.UNIVERSE_MART["workcell_alias_conflict"], index=False)
    registry.reset()
    return {"dim_workcell": len(out), "workcell_alias": len(alias),
            "workcell_alias_conflict": len(conflict_df),
            "sheet_aliases_added": len(new_alias), "sheet_unresolved": unresolved}


# ─── dim_model + dim_model_revision ──────────────────────────────────────────

def build_dim_model() -> dict:
    """A model is (workcell, assembly) together; a revision hangs off the model.
    Promoted from the registry's model.parquet / model_revision.parquet. Rows whose
    workcell is unknown keep workcell_id NULL — an orphan is a fact to show, not
    a row to drop (case 6 thinking)."""
    src_m = (C.REGISTRY_DIR / "model.parquet").as_posix()
    src_r = (C.REGISTRY_DIR / "model_revision.parquet").as_posix()
    wc = C.UNIVERSE_MART["dim_workcell"].as_posix()
    dst_m = C.UNIVERSE_MART["dim_model"].as_posix()
    dst_r = C.UNIVERSE_MART["dim_model_revision"].as_posix()
    con = duckdb.connect()
    try:
        con.execute(f"""
            copy (
              select m.id as model_id, m.match_key, m.part_number, m.name, m.family,
                     case when w.workcell_id is not null then try_cast(m.workcell_id as bigint) end as workcell_id,
                     m.workcell as workcell_raw, m.source_workcell_raw,
                     m.in_mes, m.mes_active, m.mes_assembly_id, m.in_iedb
              from read_parquet('{src_m}') m
              left join read_parquet('{wc}') w on w.workcell_id = try_cast(m.workcell_id as bigint)
              qualify row_number() over (partition by try_cast(m.workcell_id as bigint), m.match_key order by m.id) = 1
            ) to '{dst_m}' (format parquet)
        """)
        con.execute(f"""
            copy (
              select r.id as revision_id, r.model_id, r.match_key, r.revision, r.version,
                     r.mes_assembly_id, r.name, r.mes_active, r.mes_last_updated,
                     r.has_cycle_time, r.ct_rows, r.ct_lines
              from read_parquet('{src_r}') r
              join read_parquet('{dst_m}') m on m.model_id = r.model_id
              qualify row_number() over (partition by r.model_id, r.revision order by r.id) = 1
            ) to '{dst_r}' (format parquet)
        """)
        (n_m,) = con.execute(f"select count(*) from read_parquet('{dst_m}')").fetchone()
        (n_r,) = con.execute(f"select count(*) from read_parquet('{dst_r}')").fetchone()
        (n_src_m,) = con.execute(f"select count(*) from read_parquet('{src_m}')").fetchone()
        (n_src_r,) = con.execute(f"select count(*) from read_parquet('{src_r}')").fetchone()
        (n_orphan,) = con.execute(f"select count(*) from read_parquet('{dst_m}') where workcell_id is null").fetchone()
    finally:
        con.close()
    if n_src_m - n_m or n_src_r - n_r:
        log.warning("dim_model: %d duplicate (workcell, assembly) rows and %d duplicate revisions collapsed",
                    n_src_m - n_m, n_src_r - n_r)
    return {"dim_model": n_m, "dim_model_revision": n_r, "models_without_workcell": n_orphan}


# ─── fact_scan ───────────────────────────────────────────────────────────────

def build_fact_scan() -> dict:
    """One row per board × step — MES WipScanData from the raw hourly pulls in
    RAW_WIPSCAN_DIR (pipeline/refresh.py), or the August registry parquet when no
    pulls are on disk. Overlapping windows duplicate keys; one survives. Shift and shift_date follow the
    LOCAL clock (case 49): 07:00–19:00 → 2, else 3; a scan before 07:00 belongs
    to the previous date's night shift. Unknown workcells land on row 0."""
    from modules.universe.pipeline import refresh
    files = refresh.raw_files()
    src = (f"select * exclude (plant_raw, shift_name_raw), plant_raw as plant, shift_name_raw as shift_name from ({refresh._raw_sql(files)})"
           if files else f"select * from read_parquet('{(C.REGISTRY_DIR / 'production_scan.parquet').as_posix()}')")
    wc = C.UNIVERSE_MART["dim_workcell"].as_posix()
    dst = C.UNIVERSE_MART["fact_scan"].as_posix()
    con = duckdb.connect()
    try:
        con.execute(f"""
            copy (
              select
                s.wip_id, s.step, s.step_instance, s.completed_at_utc, s.completed_at_local,
                cast(s.completed_at_local as date) as date,
                case when hour(s.completed_at_local) between 7 and 18 then 2 else 3 end as shift,
                case when hour(s.completed_at_local) < 7
                     then cast(s.completed_at_local as date) - 1
                     else cast(s.completed_at_local as date) end as shift_date,
                coalesce(w.workcell_id, 0) as workcell_id,
                s.model_id, s.bay_id, s.process_type_id, s.equipment_id,
                s.process_loop, s.test_loop, s.test_status,
                s.workcell_raw, s.part_number_raw, s.revision_raw, s.route_raw,
                s.area_raw, s.equipment_raw, s.plant as plant_raw, s.shift_name as shift_name_raw
              from ({src}) s
              left join read_parquet('{wc}') w on w.workcell_id = try_cast(s.workcell_id as bigint)
              qualify row_number() over (
                partition by s.wip_id, s.step, s.step_instance, s.completed_at_utc
                order by s.process_loop, s.test_loop) = 1
              order by s.completed_at_utc
            ) to '{dst}' (format parquet, row_group_size 1000000)
        """)
        (n_src,) = con.execute(f"select count(*) from ({src})").fetchone()
        (n,) = con.execute(f"select count(*) from read_parquet('{dst}')").fetchone()
        (n_unknown,) = con.execute(f"select count(*) from read_parquet('{dst}') where workcell_id = 0").fetchone()
        lo, hi = con.execute(f"select min(date), max(date) from read_parquet('{dst}')").fetchone()
    finally:
        con.close()
    log.info("fact_scan: %d rows (%d duplicates removed), %s → %s, %d on UNKNOWN", n, n_src - n, lo, hi, n_unknown)
    return {"fact_scan": n, "fact_scan_duplicates_removed": n_src - n, "fact_scan_unknown_workcell": n_unknown,
            "fact_scan_range": f"{lo} -> {hi}"}   # ascii: the console is cp1252


# ─── model_terminal_step + fact_unit_out ─────────────────────────────────────

def build_terminal_step_and_units() -> dict:
    """Learn, per model, the step its boards finish at — from the boards
    themselves (§8.1 #9, refined by Faiz 2026-08-22). Then count every board
    once, at that step (case 48).

    Two corrections the raw "last scan" needs: LINK is a logistics scan AFTER
    completion, so a board ending at LINK finished at the step before it; a
    board ending at SCRAP is an end but not a unit. Both lists live in config."""
    f = C.UNIVERSE_MART["fact_scan"].as_posix()
    dst_t = C.UNIVERSE_MART["model_terminal_step"].as_posix()
    dst_u = C.UNIVERSE_MART["fact_unit_out"].as_posix()
    post = ", ".join(f"'{x}'" for x in C.POST_COMPLETION_STEPS)
    scrap = ", ".join(f"'{x}'" for x in C.NON_COMPLETION_STEPS)
    con = duckdb.connect()
    try:
        con.execute(f"""
            create temp table board_end as
            select wip_id, model_id,
                   arg_max(step, completed_at_utc) as last_step,
                   arg_max(case when step not in ({post}) then step end,
                           case when step not in ({post}) then completed_at_utc end) as last_work_step
            from read_parquet('{f}')
            where model_id is not null
            group by 1, 2
        """)
        con.execute(f"""
            copy (
              with ends as (
                select model_id, coalesce(last_work_step, last_step) as end_step
                from board_end where last_step not in ({scrap})
              ),
              per_model as (select model_id, end_step, count(*) as n from ends group by 1, 2),
              best as (
                select model_id, arg_max(end_step, n) as modal_step, max(n) as n_modal, sum(n) as boards
                from per_model group by 1
              )
              select model_id,
                     case when boards >= {C.TERMINAL_MIN_BOARDS} and n_modal * 1.0 / boards >= {C.TERMINAL_MIN_SHARE}
                          then modal_step else '{C.DEFAULT_TERMINAL_STEP}' end as terminal_step,
                     modal_step as learned_step,
                     round(n_modal * 1.0 / boards, 4) as share,
                     boards,
                     (boards >= {C.TERMINAL_MIN_BOARDS} and n_modal * 1.0 / boards >= {C.TERMINAL_MIN_SHARE}) as learned,
                     case when modal_step = '{C.DEFAULT_TERMINAL_STEP}' then 'packout'
                          when modal_step in ({post}) then 'link'
                          else 'other' end as terminal_kind,
                     'learned from fact_scan {date.today().isoformat()}' as source
              from best
            ) to '{dst_t}' (format parquet)
        """)
        con.execute(f"""
            copy (
              select s.wip_id, s.model_id, any_value(s.workcell_id) as workcell_id, t.terminal_step, t.learned,
                     max(s.completed_at_utc) as completed_at_utc,
                     max(s.completed_at_local) as completed_at_local,
                     arg_max(s.date, s.completed_at_utc) as date,
                     arg_max(s.shift, s.completed_at_utc) as shift,
                     arg_max(s.shift_date, s.completed_at_utc) as shift_date,
                     arg_max(s.bay_id, s.completed_at_utc) as bay_id,
                     max(s.process_loop) as process_loop
              from read_parquet('{f}') s
              join read_parquet('{dst_t}') t on t.model_id = s.model_id
              join board_end b on b.wip_id = s.wip_id and b.model_id = s.model_id
              where s.step = t.terminal_step and b.last_step not in ({scrap})
              group by s.wip_id, s.model_id, t.terminal_step, t.learned
            ) to '{dst_u}' (format parquet)
        """)
        (n_t,) = con.execute(f"select count(*) from read_parquet('{dst_t}')").fetchone()
        (n_learned,) = con.execute(f"select count(*) from read_parquet('{dst_t}') where learned").fetchone()
        (n_u,) = con.execute(f"select count(*) from read_parquet('{dst_u}')").fetchone()
        steps = con.execute(f"select terminal_step, count(*) from read_parquet('{dst_t}') group by 1 order by 2 desc limit 6").fetchall()
    finally:
        con.close()
    log.info("terminal step: %d models, %d learned; units out: %d; top steps %s", n_t, n_learned, n_u, steps)
    return {"model_terminal_step": n_t, "terminal_learned": n_learned, "fact_unit_out": n_u,
            "terminal_steps_top": steps}


# ─── fact_paid_hours + dim_smh (wave 2, pulled forward for the proof) ─────────

def build_paid_hours_and_smh() -> dict:
    """Paid hours: one row per (employee, date, shift, workcell, sub-workcell), every
    entity payroll knows (71 — the OLE module keeps 14). Source: the local UTF-8 copies
    of the share's rolling 16-day files (case 43), date-stitched — the newest file
    holding a date wins it; the August registry parquet fills dates the share has
    already rotated away. Same-key rows that differ are summed and flagged.
    SMH: the live SQLite table, one standard per (workcell, model, scan_stage),
    latest update wins."""
    from modules.universe import registry
    src_p = (C.REGISTRY_DIR / "paid_hours.parquet").as_posix()
    wc = C.UNIVERSE_MART["dim_workcell"].as_posix()
    dm = C.UNIVERSE_MART["dim_model"].as_posix()
    dst_p = C.UNIVERSE_MART["fact_paid_hours"].as_posix()
    dst_s = C.UNIVERSE_MART["dim_smh"].as_posix()
    con = duckdb.connect()
    try:
        files = sorted(C.RAW_PAID_HOURS_DIR.glob("*.csv"))
        registry_rows = f"""select employee_no_raw, employee_name_raw, date, shift, try_cast(workcell_id as bigint) as workcell_id,
                                    workcell_raw, sub_workcell_raw, cost_center, position, category, paid_hours
                             from read_parquet('{src_p}')"""
        if files:
            share = f"""select THCDirect as employee_no_raw, Name as employee_name_raw,
                              try_strptime(Startdate, '%m/%d/%Y %H:%M:%S')::date as date, try_cast(Shift as bigint) as shift,
                              WorkCell as workcell_raw, SubWorkCell as sub_workcell_raw, CostCenter as cost_center,
                              Position as position, Category as category, try_cast(TPHDirect as double) as paid_hours, filename
                       from read_csv('{C.RAW_PAID_HOURS_DIR.as_posix()}/*.csv', header = true, all_varchar = true, filename = true, union_by_name = true)
                       qualify dense_rank() over (partition by date order by filename desc) = 1"""
            names = [r[0] for r in con.execute(f"select distinct workcell_raw from ({share}) where workcell_raw is not null").fetchall()]
            wc_values = ", ".join(f"('{n.replace(chr(39), chr(39) * 2)}', {registry.resolve(n) or 0})" for n in names) or "('', 0)"
            (lo,) = con.execute(f"select min(date) from ({share})").fetchone()
            registry_rows = f"""{registry_rows} where date < '{lo}'
                                 union all by name
                                 select s.* exclude (filename), wcm.workcell_id
                                 from ({share}) s left join (values {wc_values}) wcm(workcell_raw, workcell_id) on wcm.workcell_raw = s.workcell_raw"""
        smh_names = [r[0] for r in con.execute(f"select distinct workcell from sqlite_scan('{C.OPERATIONAL_DB.as_posix()}', 'smh') where workcell is not null").fetchall()]
        smh_wc = ", ".join(f"('{n.replace(chr(39), chr(39) * 2)}', {registry.resolve(n) or 0})" for n in smh_names) or "('', 0)"
        con.execute(f"""
            copy (
              with rows as (
                select distinct employee_no_raw, employee_name_raw, date, shift, workcell_id, workcell_raw,
                       sub_workcell_raw, cost_center, position, category, paid_hours
                from ({registry_rows})
                where date is not null
              )
              select r.employee_no_raw as employee_no, any_value(r.employee_name_raw) as employee_name,
                     cast(r.date as date) as date, r.shift,
                     coalesce(w.workcell_id, 0) as workcell_id, any_value(r.workcell_raw) as workcell_raw,
                     r.sub_workcell_raw, any_value(r.cost_center) as cost_center,
                     any_value(r.position) as position, any_value(r.category) as category,
                     sum(r.paid_hours) as paid_hours, count(*) as n_source_rows
              from rows r
              left join read_parquet('{wc}') w on w.workcell_id = r.workcell_id
              group by r.employee_no_raw, r.date, r.shift, coalesce(w.workcell_id, 0), r.sub_workcell_raw
            ) to '{dst_p}' (format parquet)
        """)
        con.execute(f"""
            copy (
              with matched as (
                -- the model: same workcell first, any workcell second (case 66) — the SMH sheet
                -- names the payroll entity (KEYSIGHT HLA) while models hang off the customer (KEYSIGHT)
                select wcm.workcell_id, m.model_id, s.assembly as part_number_raw,
                       s.scan_stage, s.stage_label, s.smh_value as smh_per_unit, s.source, s.updated_by, s.updated_at
                from sqlite_scan('{C.OPERATIONAL_DB.as_posix()}', 'smh') s
                left join (values {smh_wc}) wcm(workcell_raw, workcell_id) on wcm.workcell_raw = s.workcell
                left join read_parquet('{dm}') m on m.match_key = regexp_replace(upper(s.assembly), '[^A-Z0-9]', '', 'g')
                where s.smh_value > 0
                qualify row_number() over (partition by s.workcell, s.assembly, s.scan_stage
                                           order by (m.workcell_id = wcm.workcell_id) desc nulls last, m.model_id) = 1
              )
              select * from matched
              qualify row_number() over (partition by workcell_id, model_id, scan_stage
                                         order by updated_at desc nulls last) = 1
            ) to '{dst_s}' (format parquet)
        """)
        (n_p,) = con.execute(f"select count(*) from read_parquet('{dst_p}')").fetchone()
        (n_multi,) = con.execute(f"select count(*) from read_parquet('{dst_p}') where n_source_rows > 1").fetchone()
        (n_s,) = con.execute(f"select count(*) from read_parquet('{dst_s}')").fetchone()
    finally:
        con.close()
    return {"fact_paid_hours": n_p, "paid_hours_summed_keys": n_multi, "dim_smh": n_s}


# ─── The OLE proof ────────────────────────────────────────────────────────────

def build_ole_reconciliation() -> dict:
    """OLE = Σ(units_out × SMH) ÷ Σ paid_hours per workcell per ISO week, from
    universe tables only, set beside the OLE module's weekly number. Every delta
    above RECON_DELTA_PTS carries a reason computed from the inputs — the point
    is not agreement, it is that every disagreement is explained."""
    M = {k: v.as_posix() for k, v in C.UNIVERSE_MART.items()}
    ole = C.OLE_WEEKLY_PARQUET.as_posix()
    dst = M["ole_reconciliation"]
    d = C.RECON_DELTA_PTS
    con = duckdb.connect()
    try:
        con.execute(f"""
            create temp table uni as
            with units as (
              select u.workcell_id, u.model_id, u.date, c.iso_year, c.iso_week
              from read_parquet('{M["fact_unit_out"]}') u
              join read_parquet('{M["dim_calendar"]}') c on c.date = u.date
            ),
            earned as (
              select un.workcell_id, un.iso_year, un.iso_week,
                     count(*) as units,
                     count(*) filter (where s.smh_per_unit is null) as units_missing_smh,
                     sum(coalesce(s.smh_per_unit, 0)) as earned_smh
              from units un
              left join (select workcell_id, model_id, max(smh_per_unit) as smh_per_unit
                         from read_parquet('{M["dim_smh"]}') group by 1, 2) s
                on s.workcell_id = un.workcell_id and s.model_id = un.model_id
              group by 1, 2, 3
            ),
            paid as (
              select p.workcell_id, c.iso_year, c.iso_week, sum(p.paid_hours) as paid_hours
              from read_parquet('{M["fact_paid_hours"]}') p
              join read_parquet('{M["dim_calendar"]}') c on c.date = p.date
              group by 1, 2, 3
            )
            ,
            -- days of each ISO week the scan pull actually covers (the pull, not the workcell)
            weekcov as (
              select c.iso_year, c.iso_week, count(*) as scan_days
              from read_parquet('{M["dim_calendar"]}') c
              where c.date between (select min(date) from read_parquet('{M["fact_scan"]}'))
                               and (select max(date) from read_parquet('{M["fact_scan"]}'))
              group by 1, 2
            )
            select coalesce(e.workcell_id, p.workcell_id) as workcell_id,
                   coalesce(e.iso_year, p.iso_year) as iso_year, coalesce(e.iso_week, p.iso_week) as iso_week,
                   e.units, e.units_missing_smh, e.earned_smh, wk.scan_days, p.paid_hours
            from earned e full outer join paid p
              on p.workcell_id = e.workcell_id and p.iso_year = e.iso_year and p.iso_week = e.iso_week
            left join weekcov wk on wk.iso_year = coalesce(e.iso_year, p.iso_year) and wk.iso_week = coalesce(e.iso_week, p.iso_week)
        """)
        con.execute(f"""
            create temp table mod as
            select a.workcell_id, o.workcell as ole_name, o.iso_year, o.iso_week,
                   o.total_qty as units_module, o.total_output_smh as earned_module,
                   o.total_input_hours as paid_module,
                   case when o.total_input_hours > 0 then o.total_output_smh / o.total_input_hours * 100 end as ole_module
            from read_parquet('{ole}') o
            join (select workcell_id, value from read_parquet('{M["workcell_alias"]}') where system = 'ole') a
              on upper(a.value) = upper(o.workcell)
        """)
        con.execute(f"""
            create temp table joined as
            select w.name as workcell, m.ole_name, u.workcell_id, u.iso_year, u.iso_week,
                   u.units, u.units_missing_smh, u.earned_smh, u.paid_hours, u.scan_days,
                   case when u.paid_hours > 0 then u.earned_smh / u.paid_hours * 100 end as ole_universe,
                   m.units_module, m.earned_module, m.paid_module, m.ole_module
            from uni u
            left join mod m on m.workcell_id = u.workcell_id and m.iso_year = u.iso_year and m.iso_week = u.iso_week
            join read_parquet('{M["dim_workcell"]}') w on w.workcell_id = u.workcell_id
            where u.units is not null
        """)
        con.execute(f"""
            copy (
              select workcell, ole_name, workcell_id, iso_year, iso_week,
                     units, units_missing_smh, round(earned_smh, 2) as earned_smh,
                     round(paid_hours, 2) as paid_hours, scan_days,
                     round(ole_universe, 2) as ole_universe,
                     units_module, round(earned_module, 2) as earned_module, round(paid_module, 2) as paid_module,
                     round(ole_module, 2) as ole_module,
                     round(ole_universe - ole_module, 2) as delta_pts,
                     concat_ws('; ',
                       case when ole_module is null then 'module has no row for this week' end,
                       case when scan_days < 7 then 'partial week in scans (' || scan_days || '/7 days)' end,
                       case when units_module > 0 and abs(units - units_module) / units_module > 0.05
                            then 'units ' || units || ' vs module ' || cast(units_module as bigint)
                                 || ' (' || round((units - units_module) / units_module * 100, 0)
                                 || '%): boards once at the learned terminal step vs share qty at scan_stage' end,
                       case when units > 0 and units_missing_smh * 1.0 / units > 0.02
                            then round(units_missing_smh * 100.0 / units, 0) || '% of units have no SMH' end,
                       case when paid_module > 0 and abs(paid_hours - paid_module) / paid_module > 0.05
                            then 'paid hours ' || round(paid_hours, 0) || ' vs module ' || round(paid_module, 0)
                                 || ' (' || round((paid_hours - paid_module) / paid_module * 100, 0) || '%)' end,
                       case when ole_module is not null and abs(ole_universe - ole_module) > {d}
                                 and coalesce(scan_days, 7) >= 7
                                 and not (units_module > 0 and abs(units - units_module) / units_module > 0.05)
                                 and not (units > 0 and units_missing_smh * 1.0 / units > 0.02)
                                 and not (paid_module > 0 and abs(paid_hours - paid_module) / paid_module > 0.05)
                            then 'compounded small differences in units, SMH coverage and hours (each under 5%)' end
                     ) as reason
              from joined
              order by workcell_id, iso_year, iso_week
            ) to '{dst}' (format parquet)
        """)
        (n,) = con.execute(f"select count(*) from read_parquet('{dst}')").fetchone()
        (n_both,) = con.execute(f"select count(*) from read_parquet('{dst}') where ole_module is not null").fetchone()
        (n_close,) = con.execute(f"select count(*) from read_parquet('{dst}') where abs(delta_pts) <= {d}").fetchone()
    finally:
        con.close()
    log.info("ole reconciliation: %d workcell-weeks, %d with a module number, %d within %.0f pts", n, n_both, n_close, d)
    return {"ole_reconciliation": n, "recon_with_module": n_both, "recon_within_2pts": n_close}


# ═══ PHASE 2 ═════════════════════════════════════════════════════════════════

def _wc_int(expr: str) -> str:
    """SQL: a registry workcell id text -> int, validated against dim_workcell, else 0 (UNKNOWN)."""
    return f"coalesce((select w.workcell_id from read_parquet('{C.UNIVERSE_MART['dim_workcell'].as_posix()}') w where w.workcell_id = try_cast({expr} as bigint)), 0)"


def build_people() -> dict:
    """dim_department + dim_employee. Department = what you do; workcell = who you
    do it for. scope = workcell | site is a real fact (case 31). A person whose
    workcell does not resolve keeps NULL and link_status says so."""
    R = C.REGISTRY_DIR.as_posix()
    wc = C.UNIVERSE_MART["dim_workcell"].as_posix()
    con = duckdb.connect()
    try:
        con.execute(f"""
            copy (
              select id as department_id, code, name, kind, try_cast(parent_id as bigint) as parent_id,
                     people, dl, il, other, workcells_covered, cost_centers, job_family_groups, review
              from read_csv_auto('{R}/department.csv', header=true)
            ) to '{C.UNIVERSE_MART["dim_department"].as_posix()}' (format parquet)
        """)
        con.execute(f"""
            copy (
              select e.employee_id, e.etms_id as payroll_no, e.sap_id, e.name, e.ntid, e.email,
                     e.hire_date, e.worker_type, e.employee_type, e.job_category, e.job_family,
                     e.job_family_group, e.business_title,
                     e.department_id, e.department_code,
                     w.workcell_id, e.workcell as workcell_raw, e.scope, e.link_status,
                     e.cost_center, e.cost_center_name, e.profit_center, e.location,
                     e.manager_employee_id, e.org_level, e.direct_reports,
                     e.source_sheet, e.as_of, e.valid_from, e.valid_to
              from read_csv_auto('{R}/employee.csv', header=true) e
              left join read_parquet('{wc}') w on w.workcell_id = try_cast(e.workcell_id as bigint)
              qualify row_number() over (partition by e.employee_id order by e.as_of desc nulls last, e.id desc) = 1
            ) to '{C.UNIVERSE_MART["dim_employee"].as_posix()}' (format parquet)
        """)
        (n_d,) = con.execute(f"select count(*) from read_parquet('{C.UNIVERSE_MART['dim_department'].as_posix()}')").fetchone()
        (n_e,) = con.execute(f"select count(*) from read_parquet('{C.UNIVERSE_MART['dim_employee'].as_posix()}')").fetchone()
        (n_site,) = con.execute(f"select count(*) from read_parquet('{C.UNIVERSE_MART['dim_employee'].as_posix()}') where scope = 'site'").fetchone()
    finally:
        con.close()
    return {"dim_department": n_d, "dim_employee": n_e, "employees_site_scope": n_site}


def build_process() -> dict:
    """dim_process (the alias level, with the kind above and the MES steps below),
    process_alias, dim_scan_point. The alias is the identity (case 16); three
    levels exist, none invented (case 21)."""
    R = C.REGISTRY_DIR.as_posix()
    con = duckdb.connect()
    try:
        con.execute(f"""
            copy (
              select id as process_id, name, iedb_process as process_kind, iedb_alias as alias, mes_steps,
                     kind as work_kind, workcenter, workcenter_type,
                     try_cast(process_group as bigint) as process_group_proposed,
                     try_cast(scan_point_id as bigint) as scan_point_id,
                     iedb_rows, models, lines, customers, avg_sec, mach_sec, hand_sec, imt_sec,
                     mes_rows, mes_qty, match_key, source, review, valid_from, valid_to
              from read_csv_auto('{R}/process_type.csv', header=true)
            ) to '{C.UNIVERSE_MART["dim_process"].as_posix()}' (format parquet)
        """)
        con.execute(f"""
            copy (
              select a.process_type_id as process_id, a.system, a.value, a.valid_from, a.valid_to
              from read_csv_auto('{R}/process_type_alias.csv', header=true) a
              join read_parquet('{C.UNIVERSE_MART["dim_process"].as_posix()}') p on p.process_id = a.process_type_id
              qualify row_number() over (partition by a.system, a.value order by a.process_type_id) = 1
            ) to '{C.UNIVERSE_MART["process_alias"].as_posix()}' (format parquet)
        """)
        con.execute(f"""
            copy (
              select workcell_id, child_key as mes_step, parent_key, is_scan_point, method,
                     models, models_total, agreement, alternatives, review
              from read_csv_auto('{R}/scan_point.csv', header=true)
              qualify row_number() over (partition by workcell_id, child_key order by agreement desc) = 1
            ) to '{C.UNIVERSE_MART["dim_scan_point"].as_posix()}' (format parquet)
        """)
        out = {}
        for k in ("dim_process", "process_alias", "dim_scan_point"):
            (out[k],) = con.execute(f"select count(*) from read_parquet('{C.UNIVERSE_MART[k].as_posix()}')").fetchone()
    finally:
        con.close()
    return out


def build_cycle_time() -> dict:
    """fact_cycle_time_study — one row per IEDB study row, ct_status as a value
    (case 41); the dead `quote` column is never read (case 17). fact_cycle_time_measured —
    MES scan deltas, elapsed time, provenance on every row, a separate table (case 51)."""
    R = C.REGISTRY_DIR.as_posix()
    P = C.UNIVERSE_MART["dim_process"].as_posix()
    con = duckdb.connect()
    try:
        con.execute(f"""
            copy (
              select s.id as study_id, s.model_id, s.model_revision_id, s.part_number_raw, s.revision_raw,
                     {_wc_int("s.workcell_id")} as workcell_id, s.workcell_raw,
                     s.line_id, s.line_raw,
                     p.process_id, s.process_alias_raw, s.process_raw,
                     s.workcenter, s.workcenter_type, s.step_order, s.step_group, s.priority, s.playbook,
                     s.cycle_time_sec, s.line_cycle_time_sec, s.mach_sec, s.imt_sec, s.hand_sec,
                     s.process_balance, s.parallel_cap, s.headcount, s.observations, s.sampling, s.fpy,
                     s.study_method, s.is_operator_step, s.comment, s.updated_on,
                     case when s.cycle_time_sec > 0 then 'measured' else 'missing' end as ct_status,
                     'iedb_study' as provenance
              from read_parquet('{R}/cycle_time.parquet') s
              left join read_parquet('{P}') p on p.process_id = try_cast(s.process_type_id as bigint)
            ) to '{C.UNIVERSE_MART["fact_cycle_time_study"].as_posix()}' (format parquet, row_group_size 1000000)
        """)
        con.execute(f"""
            copy (
              select m.from_step, m.to_step, try_cast(m.process_type_id as bigint) as process_id,
                     {_wc_int("m.workcell_id")} as workcell_id, m.model_id, m.part_number_raw,
                     m.bay_id, m.area_raw, m.equipment_raw, m.observations,
                     m.median_sec, m.p25_sec, m.p75_sec,
                     'mes_scan_delta' as provenance
              from read_parquet('{R}/cycle_time_measured.parquet') m
            ) to '{C.UNIVERSE_MART["fact_cycle_time_measured"].as_posix()}' (format parquet)
        """)
        (n_s,) = con.execute(f"select count(*) from read_parquet('{C.UNIVERSE_MART['fact_cycle_time_study'].as_posix()}')").fetchone()
        (n_miss,) = con.execute(f"select count(*) from read_parquet('{C.UNIVERSE_MART['fact_cycle_time_study'].as_posix()}') where ct_status = 'missing'").fetchone()
        (n_m,) = con.execute(f"select count(*) from read_parquet('{C.UNIVERSE_MART['fact_cycle_time_measured'].as_posix()}')").fetchone()
    finally:
        con.close()
    return {"fact_cycle_time_study": n_s, "studies_missing_ct": n_miss, "fact_cycle_time_measured": n_m}


def build_route() -> dict:
    """fact_route — one row per (model, line, step_order). 1,202 duplicate keys in
    the registry collapse to one; a step with no process keeps process_id NULL
    (cases 23–25: unmapped is a status, not an error)."""
    R = C.REGISTRY_DIR.as_posix()
    P = C.UNIVERSE_MART["dim_process"].as_posix()
    con = duckdb.connect()
    try:
        con.execute(f"""
            copy (
              select r.model_id, r.line_id, r.step_order, r.step_group, r.workcenter, r.workcenter_type, r.station,
                     p.process_id, r.process_alias, r.process,
                     r.cycle_time_sec, r.mach_sec, r.imt_sec, r.hand_sec, r.headcount, r.parallel_cap, r.fpy,
                     r.observations, r.is_operator_step, r.study_method, r.rows_behind
              from read_parquet('{R}/model_route.parquet') r
              left join read_parquet('{P}') p on p.process_id = try_cast(r.process_type_id as bigint)
              qualify row_number() over (partition by r.model_id, r.line_id, r.step_order order by r.process_alias) = 1
            ) to '{C.UNIVERSE_MART["fact_route"].as_posix()}' (format parquet, row_group_size 1000000)
        """)
        (n,) = con.execute(f"select count(*) from read_parquet('{C.UNIVERSE_MART['fact_route'].as_posix()}')").fetchone()
        (n_un,) = con.execute(f"select count(*) from read_parquet('{C.UNIVERSE_MART['fact_route'].as_posix()}') where process_id is null").fetchone()
    finally:
        con.close()
    return {"fact_route": n, "route_steps_unmapped": n_un}


def build_demand() -> dict:
    """fact_demand — the planner snapshot the Cycle Time module parses from the
    planners' OneDrive folder (PLANNER_DEMAND_PARQUET; the registry copy was a 29 Jun
    snapshot, case 68). Keyed on the model (part number normalised by the registry),
    workcell through the registry, never a raw name (case 18). as_of is per source."""
    from modules.universe import registry
    src = C.PLANNER_DEMAND_PARQUET.as_posix()
    con = duckdb.connect()
    try:
        names = [r[0] for r in con.execute(f"select distinct workcell from read_parquet('{src}') where workcell is not null").fetchall()]
        wc_values = ", ".join(f"('{n.replace(chr(39), chr(39) * 2)}', {registry.resolve(n) or 0})" for n in names) or "('', 0)"
        con.execute(f"""
            copy (
              with wc(workcell, workcell_id) as (values {wc_values})
              select row_number() over (order by d.as_of, d.workcell, d.model, d.period_start) as demand_id,
                     d.period_start, d.period_type,
                     coalesce(wc.workcell_id, 0) as workcell_id, d.workcell as workcell_raw,
                     m.model_id, d.model as part_number_raw, d.qty, d.source, d.as_of
              from (select workcell, model, period_start, period_type, source, as_of, sum(qty) as qty
                    from read_parquet('{src}') group by all) d
              left join wc on wc.workcell = d.workcell
              left join read_parquet('{C.UNIVERSE_MART["dim_model"].as_posix()}') m
                on m.workcell_id = wc.workcell_id and m.match_key = regexp_replace(upper(d.model), '[^A-Z0-9]', '', 'g')
              qualify row_number() over (partition by wc.workcell_id, m.model_id, d.period_start, d.period_type, d.source, d.as_of) = 1
            ) to '{C.UNIVERSE_MART["fact_demand"].as_posix()}' (format parquet)
        """)
        (n,) = con.execute(f"select count(*) from read_parquet('{C.UNIVERSE_MART['fact_demand'].as_posix()}')").fetchone()
        (q,) = con.execute(f"select sum(qty) from read_parquet('{C.UNIVERSE_MART['fact_demand'].as_posix()}')").fetchone()
    finally:
        con.close()
    return {"fact_demand": n, "demand_units": q}


def build_share_production() -> dict:
    """fact_production_share — the OLE module's share quantities (W12–W31), kept as
    a SEPARATE fact with source = 'share'. Boards (fact_unit_out) and share
    quantities count differently (case 48); this is a second opinion and a longer
    history, never merged."""
    M = {k: v.as_posix() for k, v in C.UNIVERSE_MART.items()}
    con = duckdb.connect()
    try:
        con.execute(f"""
            copy (
              with ole_alias as (
                select workcell_id, upper(value) as value from read_parquet('{M["workcell_alias"]}') where system = 'ole'
              )
              select coalesce(a.workcell_id, 0) as workcell_id, p.workcell as workcell_raw, p.sub_workcell,
                     coalesce(m.model_id, u.model_id) as model_id, p.assembly as assembly_raw,
                     case when m.model_id is not null then 'workcell+part' when u.model_id is not null then 'part only' end as model_match,
                     cast(p.date as date) as date, p.shift, p.qty, p.site,
                     'share' as source
              from read_parquet('{C.OLE_RAW_PRODUCTION.as_posix()}') p
              left join ole_alias a on a.value = upper(p.workcell)
              left join read_parquet('{M["dim_model"]}') m
                on m.workcell_id = a.workcell_id and m.match_key = regexp_replace(upper(p.assembly), '[^A-Z0-9]', '', 'g')
              -- the share files a model under a sub-workcell the registry keys differently;
              -- when the part number itself is unambiguous across the plant, use it
              left join (select match_key, any_value(model_id) as model_id
                         from read_parquet('{M["dim_model"]}') group by 1 having count(distinct workcell_id) = 1) u
                on u.match_key = regexp_replace(upper(p.assembly), '[^A-Z0-9]', '', 'g')
            ) to '{M["fact_production_share"]}' (format parquet)
        """)
        (n,) = con.execute(f"select count(*) from read_parquet('{M['fact_production_share']}')").fetchone()
        (n_wc,) = con.execute(f"select count(*) from read_parquet('{M['fact_production_share']}') where workcell_id = 0").fetchone()
        (n_m,) = con.execute(f"select count(*) from read_parquet('{M['fact_production_share']}') where model_id is null").fetchone()
    finally:
        con.close()
    return {"fact_production_share": n, "share_rows_unknown_workcell": n_wc, "share_rows_unknown_model": n_m}


# ═══ PHASE 3 — the first modules as queries ═══════════════════════════════════

def _ole_weekly_sql(policy: str) -> str:
    """Weekly OLE from universe tables under an SMH policy (case 62). 'zero' earns
    nothing for units without a standard; 'estimate' earns the workcell's
    volume-weighted average SMH — the OLE module's OLE_SMH_FALLBACK=avg."""
    M = {k: v.as_posix() for k, v in C.UNIVERSE_MART.items()}
    return f"""
        with units as (
          select u.workcell_id, u.model_id, c.iso_year, c.iso_week
          from read_parquet('{M["fact_unit_out"]}') u
          join read_parquet('{M["dim_calendar"]}') c on c.date = u.date
        ),
        smh as (select workcell_id, model_id, max(smh_per_unit) as smh_per_unit
                from read_parquet('{M["dim_smh"]}') group by 1, 2),
        joined as (
          select un.*, s.smh_per_unit from units un
          left join smh s on s.workcell_id = un.workcell_id and s.model_id = un.model_id
        ),
        wc_avg as (
          select workcell_id, sum(smh_per_unit) / count(*) as avg_smh
          from joined where smh_per_unit is not null group by 1
        ),
        earned as (
          select j.workcell_id, j.iso_year, j.iso_week, count(*) as units,
                 count(*) filter (where j.smh_per_unit is null) as units_missing_smh,
                 sum(case when j.smh_per_unit is not null then j.smh_per_unit
                          when '{policy}' = 'estimate' then coalesce(a.avg_smh, 0) else 0 end) as earned_smh
          from joined j left join wc_avg a on a.workcell_id = j.workcell_id
          group by 1, 2, 3
        ),
        paid as (
          select p.workcell_id, c.iso_year, c.iso_week, sum(p.paid_hours) as paid_hours
          from read_parquet('{M["fact_paid_hours"]}') p
          join read_parquet('{M["dim_calendar"]}') c on c.date = p.date
          group by 1, 2, 3
        )
        select e.workcell_id, e.iso_year, e.iso_week, e.units, e.units_missing_smh, e.earned_smh, p.paid_hours,
               case when p.paid_hours > 0 then e.earned_smh / p.paid_hours * 100 end as ole
        from earned e join paid p using (workcell_id, iso_year, iso_week)
    """


def ole_policy_comparison(workcell: str, weeks: tuple[int, ...], iso_year: int = 2026) -> list[dict]:
    """For one workcell and ISO weeks: the universe's OLE under both SMH policies
    beside the module's number. The test for case 62 — does 'estimate' close the gap?"""
    M = {k: v.as_posix() for k, v in C.UNIVERSE_MART.items()}
    wk = ", ".join(str(w) for w in weeks)
    con = duckdb.connect()
    try:
        (wid,) = con.execute(f"select workcell_id from read_parquet('{M['dim_workcell']}') where name = ?", [workcell]).fetchone()
        zero = {r[0]: r[1] for r in con.execute(
            f"select iso_week, ole from ({_ole_weekly_sql('zero')}) where workcell_id = {wid} and iso_year = {iso_year} and iso_week in ({wk})").fetchall()}
        est = {r[0]: r[1] for r in con.execute(
            f"select iso_week, ole from ({_ole_weekly_sql('estimate')}) where workcell_id = {wid} and iso_year = {iso_year} and iso_week in ({wk})").fetchall()}
        mod = {r[0]: r[1] for r in con.execute(
            f"select iso_week, ole_module from read_parquet('{M['ole_reconciliation']}') where workcell_id = {wid} and iso_year = {iso_year} and iso_week in ({wk})").fetchall()}
    finally:
        con.close()
    return [{"iso_week": w, "ole_zero": zero.get(w), "ole_estimate": est.get(w), "ole_module": mod.get(w),
             "delta_zero": (zero.get(w) or 0) - (mod.get(w) or 0),
             "delta_estimate": (est.get(w) or 0) - (mod.get(w) or 0)} for w in weeks if w in mod]


def build_completion_reconciliation() -> dict:
    """Cycle Time completion per (workcell, model) from fact_route + the studies,
    beside the module's completion_status_v2. The universe walks the IEDB route;
    the module walks the MES route and asks whether IEDB has each step — two
    different denominators, so every gap names which one moved."""
    M = {k: v.as_posix() for k, v in C.UNIVERSE_MART.items()}
    mod = (C.REGISTRY_DIR.parent.parent / "IE-Pulse-Backend" / "data" / "mart" / "cycle_time" / "completion_status_v2.parquet")
    from core.paths import DATA_MART_DIR
    mod = DATA_MART_DIR / "cycle_time" / "completion_status_v2.parquet"
    d = C.COMPLETION_DELTA
    con = duckdb.connect()
    try:
        con.execute(f"""
            copy (
              with route as (
                -- one row per (model, route step) across lines: a step is measured if ANY line has a time
                select r.model_id, r.step_order, r.process_alias,
                       max(case when r.cycle_time_sec > 0 then 1 else 0 end) as measured,
                       max(case when r.process_id is null then 1 else 0 end) as unmapped
                from read_parquet('{M["fact_route"]}') r
                group by 1, 2, 3
              ),
              uni as (
                select model_id, count(*) as steps_total, sum(measured) as steps_measured,
                       count(*) - sum(measured) as steps_missing_ct, sum(unmapped) as steps_unmapped,
                       round(sum(measured) * 1.0 / count(*), 4) as coverage_universe
                from route group by 1
              ),
              modl as (
                select m.model_id, s.customer, s.assembly, s.status, s.expected, s.present, s.no_ct,
                       s.not_in_iedb, s.unmapped, s.non_iedb, s.coverage as coverage_module
                from read_parquet('{mod.as_posix()}') s
                join read_parquet('{M["workcell_alias"]}') a
                  on regexp_replace(upper(a.value), '[^A-Z0-9]', '', 'g') = regexp_replace(upper(s.customer), '[^A-Z0-9]', '', 'g')
                join read_parquet('{M["dim_model"]}') m
                  on m.workcell_id = a.workcell_id and m.match_key = regexp_replace(upper(s.assembly), '[^A-Z0-9]', '', 'g')
                qualify row_number() over (partition by m.model_id order by s.graded_on desc nulls last) = 1
              )
              select w.name as workcell, dm.part_number as assembly, dm.model_id,
                     u.steps_total, u.steps_measured, u.steps_missing_ct, u.steps_unmapped, u.coverage_universe,
                     l.status as status_module, l.expected as steps_module, l.present as present_module,
                     l.no_ct as no_ct_module, l.not_in_iedb, l.unmapped as unmapped_module, l.coverage_module,
                     round(u.coverage_universe - l.coverage_module, 4) as delta,
                     concat_ws('; ',
                       case when l.status in ('not_in_mes', 'unavailable') then 'module: ' || l.status || ' — the universe still walks the IEDB route' end,
                       case when l.not_in_iedb > 0 then l.not_in_iedb || ' MES steps absent from IEDB (module counts them as missing; the universe cannot see them)' end,
                       case when l.expected is not null and l.expected <> u.steps_total
                            then 'route length: module ' || l.expected || ' MES steps vs universe ' || u.steps_total || ' IEDB steps' end,
                       case when u.steps_unmapped > 0 then u.steps_unmapped || ' IEDB steps map to no process' end,
                       case when l.unmapped > 0 then l.unmapped || ' MES steps unmapped in the module' end,
                       case when l.coverage_module is not null and abs(u.coverage_universe - l.coverage_module) > {d}
                                 and l.status not in ('not_in_mes', 'unavailable') and coalesce(l.not_in_iedb, 0) = 0
                                 and (l.expected is null or l.expected = u.steps_total) and u.steps_unmapped = 0 and coalesce(l.unmapped, 0) = 0
                            then 'same route length, different step-level verdicts — compare the studies' end
                     ) as reason
              from uni u
              join read_parquet('{M["dim_model"]}') dm on dm.model_id = u.model_id
              left join read_parquet('{M["dim_workcell"]}') w on w.workcell_id = dm.workcell_id
              left join modl l on l.model_id = u.model_id
            ) to '{M["completion_reconciliation"]}' (format parquet)
        """)
        (n,) = con.execute(f"select count(*) from read_parquet('{M['completion_reconciliation']}')").fetchone()
        (n_both,) = con.execute(f"select count(*) from read_parquet('{M['completion_reconciliation']}') where coverage_module is not null").fetchone()
        (n_agree,) = con.execute(f"select count(*) from read_parquet('{M['completion_reconciliation']}') where abs(delta) <= 0.05").fetchone()
    finally:
        con.close()
    return {"completion_reconciliation": n, "completion_with_module": n_both, "completion_within_5pts": n_agree}


def build_authored_seeds() -> dict:
    """Case 54: entities that must be CREATED. Loaded from the August seeds with
    provenance on every row and authored = true, so nobody mistakes them for
    extracted facts. People correct them; the universe keeps the history."""
    R = C.REGISTRY_DIR.as_posix()
    wc = C.UNIVERSE_MART["dim_workcell"].as_posix()
    seeds = {
        "auth_equipment_capacity": ("equipment_capacity.csv", "registry seed 2026-08: machines observed in 30 days of scans (a FLOOR, not the fleet — case 55)"),
        "auth_playbook":           ("playbook.csv",           "registry seed 2026-08: stations and boards from the MES route; operator_count unknown until an IE fills it"),
        "auth_process_group":      ("process_group.csv",      "registry seed 2026-08: steps grouped by scan-gap (case 56); a candidate, not a decision"),
        "auth_trolley_type":       ("trolley_type.csv",       "registry seed 2026-08"),
    }
    out = {}
    con = duckdb.connect()
    try:
        for table, (csv, prov) in seeds.items():
            cols = [c[0] for c in con.execute(f"describe select * from read_csv_auto('{R}/{csv}', header=true, all_varchar=true)").fetchall()]
            has_prov = "provenance" in cols
            con.execute(f"""
                copy (
                  select s.*,
                         {"coalesce(nullif(s.provenance, ''), '" + prov + "')" if has_prov else "'" + prov + "'"} as provenance_final,
                         true as authored,
                         cast(null as varchar) as confirmed_by, cast(null as date) as confirmed_on
                  from read_csv_auto('{R}/{csv}', header=true, all_varchar=true) s
                ) to '{C.UNIVERSE_MART[table].as_posix()}' (format parquet)
            """)
            # rename provenance_final -> provenance (drop the seed's own column if present)
            con.execute(f"""
                copy (select * exclude ({"provenance, " if has_prov else ""}provenance_final), provenance_final as provenance
                      from read_parquet('{C.UNIVERSE_MART[table].as_posix()}'))
                to '{C.UNIVERSE_MART[table].as_posix()}.tmp' (format parquet)
            """)
            import os
            os.replace(f"{C.UNIVERSE_MART[table]}.tmp", C.UNIVERSE_MART[table])
            (out[table],) = con.execute(f"select count(*) from read_parquet('{C.UNIVERSE_MART[table].as_posix()}')").fetchone()
    finally:
        con.close()
    return out


# ─── dim_calendar + dim_shift ────────────────────────────────────────────────

def build_dim_calendar() -> dict:
    src = (C.REGISTRY_DIR / "calendar.csv").as_posix()
    dst = C.UNIVERSE_MART["dim_calendar"].as_posix()
    con = duckdb.connect()
    try:
        con.execute(f"""
            copy (select * replace (cast(date as date) as date)
                  from read_csv_auto('{src}', header=true)
                  order by date)
            to '{dst}' (format parquet)
        """)
        (n,) = con.execute(f"select count(*) from read_parquet('{dst}')").fetchone()
    finally:
        con.close()
    return {"dim_calendar": n}


def build_dim_shift() -> dict:
    df = pd.DataFrame(C.SHIFTS, columns=["shift", "name", "start_time", "end_time", "carries_production"])
    df["start_time"] = pd.to_datetime(df["start_time"], format="%H:%M").dt.time
    df["end_time"] = pd.to_datetime(df["end_time"], format="%H:%M").dt.time
    df["note"] = ["no direct output (case 49)",
                  "07:00–19:00 · whether this is 'morning' is an open question",
                  "19:00–07:00 · crosses midnight; date = the shift's start date"]
    df.to_parquet(C.UNIVERSE_MART["dim_shift"], index=False)
    return {"dim_shift": len(df)}


def build_all() -> dict:
    report = {}
    for fn in (build_dim_workcell, build_dim_calendar, build_dim_shift, build_dim_model, build_fact_scan,
               build_terminal_step_and_units, build_paid_hours_and_smh, build_ole_reconciliation,
               build_people, build_process, build_cycle_time, build_route, build_demand, build_share_production,
               build_completion_reconciliation, build_authored_seeds):
        report.update(fn())
        log.info("built %s", fn.__name__)
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for k, v in build_all().items():
        print(f"{k}: {v}")
