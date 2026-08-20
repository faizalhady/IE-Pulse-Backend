"""
facts.py  (cycle_time.chat)
───────────────────────────
The two curated views the SQL lane may query — and the ONLY two.

WHY VIEWS, NOT THE MARTS
  Text-to-SQL over the raw marts hands an 8B model every trap this module has
  spent months fixing: workcell spellings that differ per source, demand vs
  universe scope, units-vs-models, U+00A0 in assembly names. The semantics are
  not in a raw schema, so the model would re-derive them wrong, silently.

  Here every concept is PRE-SOLVED into a plain column at build time, by the
  same code the screens trust:

    llm_model_facts      one row per model — verdict, demand, coverage, counts
    llm_workcell_facts   one row per workcell — totals and both completion %

  If answering needs a join or a formula, the answer is materialised as a
  column instead. The model only ever writes SELECT-WHERE-GROUP-ORDER.

WORKCELL_KEY IS THE JOIN SPINE
  `workcell` is the display name ("Lam Research"). `workcell_key` is canon() —
  uppercase alphanumerics — and it is what WHERE clauses match on, because it
  is the one spelling the user's text can be normalised to deterministically.
  sqllane.py rewrites every workcell_key literal through canon(), so even a
  model that copies "lam research" verbatim hits the row.

CACHED ON THE SAME KEY AS THE DEMAND PAYLOAD
  The frames rebuild when the marts do and never otherwise. A COPY is never
  handed out — DuckDB reads them, nothing mutates them.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import pandas as pd

from modules.cycle_time.model_universe import canon

log = logging.getLogger(__name__)

#: column -> (source column or None, SQL type, comment shown to the model).
#: ONE spec drives both the frame build and the DDL prompt, so the schema the
#: model reads cannot drift from the schema the query runs against.
MODEL_COLS: dict[str, tuple[str | None, str, str]] = {
    "workcell":      ("customer", "TEXT", "display name of the workcell (= CUSTOMER, never a station)"),
    "workcell_key":  (None, "TEXT", "UPPERCASE alphanumerics only, e.g. 'LAMRESEARCH' — ALWAYS filter workcells on this column"),
    "assembly":      ("assembly", "TEXT", "the model / part number"),
    "plant":         ("plant", "TEXT", "MES plant code, e.g. 'JBK', 'Plant 1'"),
    "region":        ("region", "TEXT", "'Batu Kawan' or 'Penang Island'"),
    "status":        ("status", "TEXT", "verdict: complete | incomplete | no_cycle_time | not_in_iedb | not_built | cannot_check"),
    "reason":        ("reason", "TEXT", "short reason behind the status, may be NULL"),
    "units":         ("units", "BIGINT", "demand units (13-week planner + MES projection); 0 = no demand"),
    "has_demand":    ("has_demand", "BOOLEAN", "TRUE = we are building or about to build it. 'Planned' scope = WHERE has_demand"),
    "expected_steps": ("expected", "BIGINT", "MES steps the floor actually runs for this model"),
    "present_steps": ("present", "BIGINT", "of those, steps named in IEDB with a cycle time"),
    "no_ct_steps":   ("no_ct", "BIGINT", "steps in IEDB but never timed"),
    "not_in_iedb_steps": ("not_in_iedb", "BIGINT", "steps missing from IEDB entirely"),
    "unmapped_steps": ("unmapped", "BIGINT", "MES steps nobody has mapped to an IEDB process yet"),
    "coverage_pct":  ("coverage", "DOUBLE", "present_steps / expected_steps * 100, NULL when nothing expected"),
    "lbr":           ("lbr", "DOUBLE", "labour ratio, may be NULL"),
    "bottleneck_ct_s": ("bottleneck_ct", "DOUBLE", "slowest station cycle time in seconds, may be NULL"),
    "station_count": ("station_count", "BIGINT", "stations on the IEDB route"),
    "bom_materials": (None, "BIGINT", "materials on the MES BOM; 0 = no BOM loaded for it"),
    "last_build":    ("last_build", "TEXT", "when MES last saw it built (ISO date), may be NULL"),
}

PROC_COLS: dict[str, tuple[None, str, str]] = {
    "workcell":     (None, "TEXT", "display name of the workcell"),
    "workcell_key": (None, "TEXT", "UPPERCASE alphanumerics — ALWAYS filter on this"),
    "assembly":     (None, "TEXT", "the model / part number"),
    "process":      (None, "TEXT", "one IEDB process step of this model's route"),
    "ct_seconds":   (None, "DOUBLE", "the LONGEST recorded cycle time for this process, seconds (max across lines and revisions)"),
    "records":      (None, "BIGINT", "how many CT records back this number"),
}

WC_COLS: dict[str, tuple[None, str, str]] = {
    "workcell":       (None, "TEXT", "display name of the workcell"),
    "workcell_key":   (None, "TEXT", "UPPERCASE alphanumerics — ALWAYS filter on this"),
    "models":         (None, "BIGINT", "models in demand (has_demand=TRUE)"),
    "complete_models": (None, "BIGINT", "of those, verdict complete"),
    "pct_by_models":  (None, "DOUBLE", "complete_models / models * 100 — the work-list number"),
    "units":          (None, "BIGINT", "total demand units"),
    "complete_units": (None, "BIGINT", "demand units on complete models"),
    "pct_by_units":   (None, "DOUBLE", "complete_units / units * 100 — the headline number"),
    "incomplete_models":    (None, "BIGINT", "models with verdict incomplete (Missing CT)"),
    "no_cycle_time_models": (None, "BIGINT", "models with verdict no_cycle_time"),
    "not_in_iedb_models":   (None, "BIGINT", "models with verdict not_in_iedb"),
    "not_built_models":     (None, "BIGINT", "models with verdict not_built"),
    "cannot_check_models":  (None, "BIGINT", "models with verdict cannot_check"),
    "unmapped_steps": (None, "BIGINT", "total unmapped MES steps across the workcell"),
}


def _num(s) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _bom_counts() -> pd.DataFrame:
    """(join key -> material count) for every model with a fetched BOM.

    "top 3 most materials model" was answered with UNITS because the views had
    no material column — the model grabbed the nearest number and the answer
    read perfectly. Counts ride bom.py's cached bridge; a model with several
    revisions takes the largest BOM. Degrades to empty (column = 0 everywhere)
    when the marts predate bom_id — same stance as the BOM tab."""
    try:
        from modules.cycle_time.bom import _bridge
        from modules.cycle_time.config import CT_MART
        bm = pd.read_parquet(CT_MART["bom_material"], columns=["bom_id"])
        per_bom = bm.groupby("bom_id").size().rename("bom_materials")
        b = _bridge()[["_c", "_a", "bom_id"]]
        b = b.join(per_bom, on="bom_id")
        out = (b.dropna(subset=["bom_materials"])
                .groupby(["_c", "_a"])["bom_materials"].max().reset_index())
        out["bom_materials"] = out["bom_materials"].astype("int64")
        return out
    except Exception as e:                       # noqa: BLE001
        log.warning("facts: bom counts unavailable (%s)", e)
        return pd.DataFrame(columns=["_c", "_a", "bom_materials"])


def _build_model_facts() -> pd.DataFrame:
    from modules.cycle_time.chat.tools import _demand
    d = _demand("all")
    out = pd.DataFrame()
    for col, (src, typ, _) in MODEL_COLS.items():
        if col == "workcell_key":
            out[col] = d["customer"].astype(str).map(canon)
            continue
        if col == "coverage_pct":
            # The mart stores coverage as a 0-1 FRACTION; the column promises
            # percent. Served raw, "average coverage of keysight" answered
            # "1.0" and the compose model dressed it up as 100%.
            out[col] = (_num(d["coverage"]) * 100).round(1)
            continue
        if col == "bom_materials":
            # Joined below on the SAME normalisation the BOM bridge uses —
            # raw upper-alnum, NOT canon(): canon folds aliases (COHU -> LTX)
            # and the bridge does not.
            continue
        v = d[src] if src in d.columns else None
        if v is None:
            out[col] = None
        elif typ == "BIGINT":
            out[col] = _num(v).fillna(0).astype("int64")
        elif typ == "DOUBLE":
            out[col] = _num(v).round(2)
        elif typ == "BOOLEAN":
            out[col] = v.fillna(False).astype(bool)
        else:
            out[col] = v.astype(str).where(v.notna(), None)
    # BOM material counts, joined on the bridge's own normalisation.
    key_c = d["customer"].astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
    key_a = d["assembly"].astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
    bc = _bom_counts()
    joined = pd.DataFrame({"_c": key_c, "_a": key_a}).merge(bc, on=["_c", "_a"], how="left")
    out["bom_materials"] = joined["bom_materials"].fillna(0).astype("int64").to_numpy()
    # Blank/placeholder workcells carry no answerable question. Column order
    # re-asserted to the spec — the DDL and the frame must agree.
    out = out[list(MODEL_COLS)]
    return out[out["workcell"].astype(str).str.strip("- ").ne("")]


def _build_wc_facts(m: pd.DataFrame) -> pd.DataFrame:
    dem = m[m["has_demand"]]
    rows = []
    for key, g in dem.groupby("workcell_key"):
        by = g["status"].value_counts()
        units = int(g["units"].sum())
        cu = int(g.loc[g["status"] == "complete", "units"].sum())
        n, c = len(g), int(by.get("complete", 0))
        rows.append({
            "workcell": g["workcell"].iloc[0], "workcell_key": key,
            "models": n, "complete_models": c,
            "pct_by_models": round(100 * c / n, 1) if n else None,
            "units": units, "complete_units": cu,
            "pct_by_units": round(100 * cu / units, 1) if units else None,
            "incomplete_models": int(by.get("incomplete", 0)),
            "no_cycle_time_models": int(by.get("no_cycle_time", 0)),
            "not_in_iedb_models": int(by.get("not_in_iedb", 0)),
            "not_built_models": int(by.get("not_built", 0)),
            "cannot_check_models": int(by.get("cannot_check", 0)),
            "unmapped_steps": int(g["unmapped_steps"].sum()),
        })
    return pd.DataFrame(rows)


def _build_process_facts(m: pd.DataFrame) -> pd.DataFrame:
    """Process grain, DEMAND models only — "which process has the longest CT"
    is unanswerable at model grain (the views only had bottleneck_ct_s, no
    process NAME, and the model hallucinated an iedb_process_facts table).

    The IEDB raw mart is 4.4M rows; grouped to (customer, assembly, process)
    in DuckDB, then cut to the demand set — the plant asks about what it
    builds. Joined on canon(customer) + normalised assembly, NOT raw workcell
    strings — the documented trap: planner and IEDB spell the same workcell
    differently (TMO vs THERMO FISHER)."""
    import duckdb
    from modules.cycle_time.config import CT_MART
    con = duckdb.connect()
    try:
        g = con.execute(
            f"""SELECT customer, assembly, process,
                       max(cycle_time_per_process) AS ct_seconds,
                       count(*) AS records
                FROM read_parquet('{CT_MART["raw"].as_posix()}')
                WHERE cycle_time_per_process IS NOT NULL
                  AND cycle_time_per_process > 0 AND process IS NOT NULL
                GROUP BY 1, 2, 3"""
        ).df()
    finally:
        con.close()
    g["workcell_key"] = g["customer"].astype(str).map(canon)
    g["_a"] = g["assembly"].astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)

    dem = m[m["has_demand"]][["workcell", "workcell_key", "assembly"]].copy()
    dem["_a"] = dem["assembly"].astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)

    out = dem.merge(g[["workcell_key", "_a", "process", "ct_seconds", "records"]],
                    on=["workcell_key", "_a"], how="inner")
    out["ct_seconds"] = _num(out["ct_seconds"]).round(2)
    out["records"] = out["records"].astype("int64")
    return out[list(PROC_COLS)]


@lru_cache(maxsize=2)
def _frames_cached(_key) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    m = _build_model_facts()
    return m, _build_wc_facts(m), _build_process_facts(m)


def frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(llm_model_facts, llm_workcell_facts, llm_process_facts), cached on the
    demand payload's key so a mart rebuild invalidates them without anyone
    remembering to."""
    from api.routers.cycle_time import _completion_demand_key
    return _frames_cached(_completion_demand_key())


def ddl() -> str:
    """The schema exactly as the model should read it — generated from the same
    spec the frames are built from."""
    def table(name: str, cols: dict, head: str) -> str:
        # Comma BEFORE the comment — after it, the line comment swallows it
        # and the DDL the model reads is not the DDL a parser would accept.
        cs = list(cols.items())
        body = "\n".join(f"  {c} {t[1]}{',' if i < len(cs) - 1 else ''}  -- {t[2]}"
                         for i, (c, t) in enumerate(cs))
        return f"-- {head}\nCREATE TABLE {name} (\n{body}\n);"
    return (
        table("llm_model_facts", MODEL_COLS,
              "one row per model. A model is (workcell, assembly) TOGETHER.")
        + "\n\n"
        + table("llm_workcell_facts", WC_COLS,
                "one row per workcell, demand scope, both completion percentages precomputed.")
        + "\n\n"
        + table("llm_process_facts", PROC_COLS,
                "one row per (workcell, model, process) — IN-DEMAND models only. For per-process cycle-time questions.")
    )


if __name__ == "__main__":
    m, w, pr = frames()
    assert list(m.columns) == list(MODEL_COLS), "frame/spec drift"
    assert list(w.columns) == list(WC_COLS), "frame/spec drift"
    assert list(pr.columns) == list(PROC_COLS), "frame/spec drift"
    assert (m["workcell_key"].str.fullmatch(r"[A-Z0-9]+")).all()
    assert len(w) < len(m)
    assert len(pr) and (pr["ct_seconds"] > 0).all()
    d = ddl()
    assert "llm_model_facts" in d and "workcell_key" in d and "-- verdict" in d
    assert "llm_process_facts" in d
    print(f"facts self-check OK — {len(m):,} models, {len(w)} workcells, {len(pr):,} process rows")
    print(d)
