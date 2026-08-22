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
    """One row per board × step — MES WipScanData as pulled in August (hourly
    windows, 9 Jul → 8 Aug 2026). The raw parquet holds ~1.1M duplicated keys
    from overlapping windows; one survives. Shift and shift_date follow the
    LOCAL clock (case 49): 07:00–19:00 → 2, else 3; a scan before 07:00 belongs
    to the previous date's night shift. Unknown workcells land on row 0."""
    src = (C.REGISTRY_DIR / "production_scan.parquet").as_posix()
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
              from read_parquet('{src}') s
              left join read_parquet('{wc}') w on w.workcell_id = try_cast(s.workcell_id as bigint)
              qualify row_number() over (
                partition by s.wip_id, s.step, s.step_instance, s.completed_at_utc
                order by s.process_loop, s.test_loop) = 1
              order by s.completed_at_utc
            ) to '{dst}' (format parquet, row_group_size 1000000)
        """)
        (n_src,) = con.execute(f"select count(*) from read_parquet('{src}')").fetchone()
        (n,) = con.execute(f"select count(*) from read_parquet('{dst}')").fetchone()
        (n_unknown,) = con.execute(f"select count(*) from read_parquet('{dst}') where workcell_id = 0").fetchone()
        lo, hi = con.execute(f"select min(date), max(date) from read_parquet('{dst}')").fetchone()
    finally:
        con.close()
    log.info("fact_scan: %d rows (%d duplicates removed), %s → %s, %d on UNKNOWN", n, n_src - n, lo, hi, n_unknown)
    return {"fact_scan": n, "fact_scan_duplicates_removed": n_src - n, "fact_scan_unknown_workcell": n_unknown,
            "fact_scan_range": f"{lo} → {hi}"}


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
    """Paid hours: one row per (employee, date, shift, workcell, sub-workcell).
    The share files are rolling 16-day windows (case 43), so the registry holds
    exact duplicates — dropped; same-key rows that differ are summed and flagged.
    SMH: one standard per (workcell, model, scan_stage), latest update wins."""
    src_p = (C.REGISTRY_DIR / "paid_hours.parquet").as_posix()
    src_s = (C.REGISTRY_DIR / "smh.parquet").as_posix()
    wc = C.UNIVERSE_MART["dim_workcell"].as_posix()
    dst_p = C.UNIVERSE_MART["fact_paid_hours"].as_posix()
    dst_s = C.UNIVERSE_MART["dim_smh"].as_posix()
    con = duckdb.connect()
    try:
        con.execute(f"""
            copy (
              with rows as (
                select distinct employee_no_raw, employee_name_raw, date, shift, workcell_id, workcell_raw,
                       sub_workcell_raw, cost_center, position, category, paid_hours
                from read_parquet('{src_p}')
              )
              select r.employee_no_raw as employee_no, any_value(r.employee_name_raw) as employee_name,
                     cast(r.date as date) as date, r.shift,
                     coalesce(w.workcell_id, 0) as workcell_id, any_value(r.workcell_raw) as workcell_raw,
                     r.sub_workcell_raw, any_value(r.cost_center) as cost_center,
                     any_value(r.position) as position, any_value(r.category) as category,
                     sum(r.paid_hours) as paid_hours, count(*) as n_source_rows
              from rows r
              left join read_parquet('{wc}') w on w.workcell_id = try_cast(r.workcell_id as bigint)
              group by r.employee_no_raw, r.date, r.shift, coalesce(w.workcell_id, 0), r.sub_workcell_raw
            ) to '{dst_p}' (format parquet)
        """)
        con.execute(f"""
            copy (
              select try_cast(s.workcell_id as bigint) as workcell_id, s.model_id, s.part_number_raw,
                     s.scan_stage, s.stage_label, s.smh_per_unit, s.source, s.updated_by, s.updated_at
              from read_parquet('{src_s}') s
              where s.smh_per_unit > 0
              qualify row_number() over (partition by s.workcell_id, s.model_id, s.scan_stage
                                         order by s.updated_at desc nulls last) = 1
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
               build_terminal_step_and_units, build_paid_hours_and_smh, build_ole_reconciliation):
        report.update(fn())
        log.info("built %s", fn.__name__)
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for k, v in build_all().items():
        print(f"{k}: {v}")
