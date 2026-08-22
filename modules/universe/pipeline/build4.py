"""
modules/universe/pipeline/build4.py
───────────────────────────────────
Wave 4 — the places and the things that were still boxes on the atlas (2026-08-23):

  dim_bay          every bay under BOTH naming schemes (layout BAY 1…19 A/B/C and MES BAY 101…),
                   plus every bay name the scans mention; the two schemes are not reconciled (case 9)
  bay_occupancy    who occupies which bay, with the evidence (observed production, MES config, layout)
  fact_bay_week    what the scans show: workcell × bay × ISO week → boards, scans — "where does WABTEC build"
  dim_line         the engineering unit inside a bay (IEDB sub_workcenter; 147 of 230 parse to a bay code)
  dim_asset        tools and machines from EST1C + SAP (13,943; lifecycle says installed / scrapped)
  dim_equipment    machines as the scans see them — a floor, not the fleet (case 55)

Sources: the August registry CSVs (docs/registry) and fact_scan. Tests first (tests/test_universe.py).
"""

from __future__ import annotations

import logging

import duckdb

from modules.universe import config as C

log = logging.getLogger(__name__)


def _m(name: str) -> str:
    return C.UNIVERSE_MART[name].as_posix()


def build_bays() -> dict:
    R = C.REGISTRY_DIR.as_posix()
    con = duckdb.connect()
    try:
        con.execute(f"""
            copy (
              with reg as (
                select try_cast(id as bigint) as bay_id, naming_scheme, plant, building, level, area, bay_number,
                       label, description, bay_type, status, bay_key, merge_candidate_id,
                       try_cast(merge_confidence as double) as merge_confidence, sources,
                       coalesce(nullif(label, ''), nullif(regexp_replace(bay_key, '^mes[|][|]', ''), ''), description) as name
                from read_csv('{R}/bay.csv', all_varchar = true)
              ),
              scans as (
                select bay_id as name, arg_max(plant_raw, n) as plant, min(first_seen) as first_seen, max(last_seen) as last_seen,
                       sum(n) as scans, count(distinct workcell_id) as workcells
                from (select bay_id, plant_raw, workcell_id, count(*) as n, min(date) as first_seen, max(date) as last_seen
                      from read_parquet('{_m("fact_scan")}')
                      where bay_id is not null and trim(bay_id) <> '' group by 1, 2, 3)
                group by 1
              ),
              extra as (
                -- bays the scans name that the registry's MES list does not have yet
                select 100000 + row_number() over (order by s.name) as bay_id, 'mes' as naming_scheme, s.plant,
                       null as building, null as level, null as area, null as bay_number, s.name as label, null as description,
                       'production' as bay_type, null as status, 'mes||' || s.name as bay_key, null as merge_candidate_id,
                       null as merge_confidence, 'fact_scan' as sources, s.name
                from scans s
                where upper(trim(s.name)) not in (select upper(trim(name)) from reg where naming_scheme = 'mes' and name is not null)
              ),
              allb as (select * from reg union all by name select * from extra)
              select b.bay_id, b.naming_scheme, b.name, coalesce(b.plant, s.plant) as plant, b.building, b.level, b.area,
                     b.bay_number, b.label, b.description, b.bay_type, b.status, b.bay_key, b.merge_candidate_id,
                     b.merge_confidence, b.sources,
                     s.first_seen as scan_first_seen, s.last_seen as scan_last_seen, s.scans, s.workcells as scan_workcells
              from allb b
              left join scans s on b.naming_scheme = 'mes' and upper(trim(s.name)) = upper(trim(b.name))
              order by b.naming_scheme, b.bay_id
            ) to '{_m("dim_bay")}' (format parquet)
        """)
        con.execute(f"""
            copy (
              select try_cast(o.bay_id as bigint) as bay_id, b.name as bay, b.naming_scheme,
                     try_cast(o.workcell_id as bigint) as workcell_id, o.workcell_name, o.workcell_raw,
                     o.evidence, o.source, try_cast(o.units_90d as bigint) as units_90d,
                     try_cast(o.assemblies_90d as bigint) as assemblies_90d, try_cast(o.last_build as date) as last_build,
                     try_cast(o.valid_from as date) as valid_from, try_cast(o.valid_to as date) as valid_to
              from read_csv('{R}/bay_occupancy.csv', all_varchar = true) o
              left join read_parquet('{_m("dim_bay")}') b on b.bay_id = try_cast(o.bay_id as bigint)
            ) to '{_m("bay_occupancy")}' (format parquet)
        """)
        con.execute(f"""
            copy (
              select workcell_id, bay_id as bay, plant_raw as plant,
                     cast(date_trunc('week', date) as date) as week_start,
                     count(distinct wip_id) as boards, count(*) as scans, min(date) as first_scan, max(date) as last_scan
              from read_parquet('{_m("fact_scan")}')
              where bay_id is not null and trim(bay_id) <> ''
              group by all
              order by workcell_id, bay, week_start
            ) to '{_m("fact_bay_week")}' (format parquet)
        """)
        (n_bay,) = con.execute(f"select count(*) from read_parquet('{_m('dim_bay')}')").fetchone()
        (n_extra,) = con.execute(f"select count(*) from read_parquet('{_m('dim_bay')}') where sources = 'fact_scan'").fetchone()
        (n_occ,) = con.execute(f"select count(*) from read_parquet('{_m('bay_occupancy')}')").fetchone()
        (n_wk,) = con.execute(f"select count(*) from read_parquet('{_m('fact_bay_week')}')").fetchone()
    finally:
        con.close()
    log.info("dim_bay: %d bays (%d named only by the scans), bay_occupancy %d, fact_bay_week %d", n_bay, n_extra, n_occ, n_wk)
    return {"dim_bay": n_bay, "dim_bay_from_scans_only": n_extra, "bay_occupancy": n_occ, "fact_bay_week": n_wk}


def build_lines() -> dict:
    R = C.REGISTRY_DIR.as_posix()
    con = duckdb.connect()
    try:
        con.execute(f"""
            copy (
              select try_cast(id as bigint) as line_id, name, try_cast(workcell_id as bigint) as workcell_id, workcell,
                     workcell_prefix, line_type, workcenter, workcenter_type, plant, building, sub_area, area, bay_code, cell,
                     descriptor, try_cast(ct_rows as bigint) as ct_rows, try_cast(models as bigint) as models,
                     try_cast(steps as bigint) as steps, try_cast(total_ct_min as double) as total_ct_min,
                     try_cast(updated_on as date) as updated_on, lower(parsed) = 'true' as parsed, review
              from read_csv('{R}/line.csv', all_varchar = true)
            ) to '{_m("dim_line")}' (format parquet)
        """)
        (n,) = con.execute(f"select count(*) from read_parquet('{_m('dim_line')}')").fetchone()
        (n_parsed,) = con.execute(f"select count(*) from read_parquet('{_m('dim_line')}') where parsed").fetchone()
    finally:
        con.close()
    return {"dim_line": n, "dim_line_parsed_to_bay": n_parsed}


def build_assets() -> dict:
    R = C.REGISTRY_DIR.as_posix()
    con = duckdb.connect()
    try:
        con.execute(f"""
            copy (
              select try_cast(id as bigint) as asset_id, sap_equipment, sap_asset, serial_no, description, model, manufacturer,
                     equip_category, lower(is_smart_torque) = 'true' as is_smart_torque, torque_range, lifecycle, est1c_status,
                     try_cast(workcell_id as bigint) as workcell_id, workcell, workcell_source, workcell_raw, main_workctr, cost_center,
                     bay_raw, bay_normalised, try_cast(bay_id as bigint) as bay_id, bay_area, room, location, functional_loc, pc_name,
                     software_version, try_cast(days_since_last_used as bigint) as days_since_last_used,
                     try_cast(construct_year as integer) as construct_year, try_cast(acquisition_value as double) as acquisition_value,
                     source, review
              from read_csv('{R}/asset.csv', all_varchar = true)
            ) to '{_m("dim_asset")}' (format parquet)
        """)
        (n,) = con.execute(f"select count(*) from read_parquet('{_m('dim_asset')}')").fetchone()
        (n_inst,) = con.execute(f"select count(*) from read_parquet('{_m('dim_asset')}') where lifecycle = 'installed'").fetchone()
        (n_st,) = con.execute(f"select count(*) from read_parquet('{_m('dim_asset')}') where is_smart_torque").fetchone()
    finally:
        con.close()
    return {"dim_asset": n, "dim_asset_installed": n_inst, "dim_asset_smart_torque": n_st}


def build_equipment() -> dict:
    """Machines as the scans see them: one row per MES equipment id, with where and at
    what step it was seen most. A floor, not the fleet (case 55)."""
    con = duckdb.connect()
    try:
        con.execute(f"""
            copy (
              select equipment_id, mode(equipment_raw) as name,
                     mode(workcell_id) as workcell_id, count(distinct workcell_id) as workcells,
                     mode(step) as step, count(distinct step) as steps,
                     mode(bay_id) as bay, mode(plant_raw) as plant,
                     min(date) as first_seen, max(date) as last_seen,
                     count(*) as scans, count(distinct wip_id) as boards
              from read_parquet('{_m("fact_scan")}')
              where equipment_id is not null and trim(equipment_id) <> ''
              group by equipment_id
              order by scans desc
            ) to '{_m("dim_equipment")}' (format parquet)
        """)
        (n,) = con.execute(f"select count(*) from read_parquet('{_m('dim_equipment')}')").fetchone()
    finally:
        con.close()
    return {"dim_equipment": n}


def build_wave4() -> dict:
    out = {}
    for fn in (build_bays, build_lines, build_assets, build_equipment):
        out.update(fn())
        log.info("built %s", fn.__name__)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for k, v in build_wave4().items():
        print(f"{k}: {v}")
