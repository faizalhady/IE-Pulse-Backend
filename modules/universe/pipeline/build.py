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
    for fn in (build_dim_workcell, build_dim_calendar, build_dim_shift, build_dim_model):
        report.update(fn())
        log.info("built %s", fn.__name__)
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for k, v in build_all().items():
        print(f"{k}: {v}")
