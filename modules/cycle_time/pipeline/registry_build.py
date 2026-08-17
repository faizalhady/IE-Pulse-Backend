"""
registry_build.py  (cycle_time pipeline)
────────────────────────────────────────
Rebuild `workcell_process_raw.csv` — the naming registry — from marts the server
already holds, so it stops being a file somebody copies by hand.

WHY IT HAD TO BE PORTED
  The registry generators live in `Projects/docs/registry` on one laptop, and one
  of their inputs (`production_scan.parquet`) exists only there. So 02 could
  never rebuild the registry, and nothing kept it current: on 2026-08-18 the
  server had no registry at all beyond a 10-row decision file, which silently
  dropped `process_bridge` back to workbook-only and changed verdicts.

  A file that must be hand-copied to stay correct will eventually be wrong.

FOUR LAYERS, AND THE DIFFERENCE BETWEEN TWO OF THEM IS THE POINT
    iedb_alias      what IEDB was told to price     raw.parquet
    mes_step        what this workcell PROVABLY ran completion_steps_v2 (scans)
    mes_configured  what MES has configured on a    route master + process master
                    route its models can reach      — evidence only, never answered
    bridge          the hand-curated map between    mes_process_map.parquet

  Only `mes_step` carries an answer, because only a scan proves a workcell runs
  a step. The first version of this file used the route master for that, and a
  route is shared between customers — so joining route -> model -> customer gave
  every workcell every step on the bay. 7,251 names became 62,612 rows,
  `MI BTM 3` was filed under 34 workcells, and the unanswered count went from
  141 to 62,612. Splitting the two layers puts it back to 564.

  `mes_configured` is still worth keeping: the difference between it and
  `mes_step` is "configured on the route, never seen in a scan" — dead config,
  or a process that quietly stopped.

IDENTITY IS WORKCELL-SCOPED, ALWAYS
  `MA 1` is Mech Assy at ARISTA, Smart Torque at BD, Deposition OPT 10 at LAM
  GAS BOX. A plant-wide key fuses them and every number downstream is wrong.

NOTHING IS EVER DELETED
  Every original spelling survives as its own row, byte-exact, including the 20
  names that differ only by a trailing space. `answer` records the verdict;
  the raw name records the evidence.
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

import pandas as pd

from modules.cycle_time.config import CT_MART

log = logging.getLogger(__name__)

REG_DIR = CT_MART["raw"].parent / "registry"
OUT = REG_DIR / "workcell_process_raw.csv"

_anorm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s or "").upper())
_snorm = lambda s: re.sub(r"\s+", " ", str(s or "")).strip().upper()
_cnorm = _anorm


def pkey(name: str) -> str:
    """The identity, computed the same way on both sides.

    Mirrors `completion_v2._code` with one correction: a BARE code means
    instance 1, so `PACKOUT ` merges with `Packout 1`. `_code` drops the
    instance entirely, which would fuse `MA 1` with `MA 2`.
    """
    head = str(name or "").split("-")[0].strip().upper()
    m = re.search(r"([\d.]+)\s*$", head)
    inst = m.group(1) if m else "1"
    return _anorm(re.sub(r"[\s\d./]+$", "", head)) + "#" + inst


def _iedb() -> pd.DataFrame:
    """IEDB's own alias names, per workcell, with usage."""
    raw = CT_MART["raw"]
    if not raw.exists():
        return pd.DataFrame()
    import duckdb
    con = duckdb.connect()
    try:
        d = con.execute(f"""
            SELECT customer AS workcell, alias AS name_raw,
                   COUNT(*) AS rows, COUNT(DISTINCT assembly) AS models
            FROM read_parquet('{raw.as_posix()}')
            WHERE alias IS NOT NULL AND trim(alias) <> ''
            GROUP BY customer, alias
        """).df()
    finally:
        con.close()
    d["system"] = "iedb_alias"
    d["is_iedb"] = True
    return d


def _mes_scanned() -> pd.DataFrame:
    """Steps this workcell PROVABLY ran, from scan history.

    A scan row is direct evidence: this workcell, this model, this step, this
    date. It is the only honest basis for saying a workcell owns a step, and it
    is what `answer` is computed against.

    Coverage is limited to models the completion check has reached (3,465 of
    56,882), which is a real limit and the right one — an unproven claim is
    worse than a missing row.
    """
    import duckdb
    from modules.cycle_time.config import CT_MES_SCAN_DIR

    frames = []
    # The #21 scan cache: one parquet per customer-day, written by the completion
    # runs and already on the server (4,278 files). This is the real scan record
    # and it grows on its own, so the registry stays current with no extra pull.
    if CT_MES_SCAN_DIR.exists():
        glob = (CT_MES_SCAN_DIR / "*" / "*.parquet").as_posix()
        con = duckdb.connect()
        try:
            # The cache has no customer COLUMN — the customer is the folder
            # name (mes_scans/<customer>/<date>.parquet), so it comes from the
            # path. Columns are (assembly, step, order, qty).
            d = con.execute(f"""
                -- chr(92) is a backslash. Writing it literally inside this
                -- f-string is a fight between Python, DuckDB and Windows paths
                -- that nobody wins.
                SELECT regexp_extract(replace(filename, chr(92), '/'),
                                      'mes_scans/([^/]+)/', 1) AS workcell,
                       step AS name_raw,
                       COUNT(*) AS rows, COUNT(DISTINCT assembly) AS models
                FROM read_parquet('{glob}', union_by_name=true, filename=true)
                WHERE step IS NOT NULL AND trim(step) <> ''
                GROUP BY 1, 2
                HAVING workcell <> ''
            """).df()
            frames.append(d)
        except Exception as e:
            log.warning("scan cache unreadable (%s) - falling back to graded steps", str(e)[:90])
        finally:
            con.close()

    # Fallback / supplement: the MES side of the completion steps mart. Narrower
    # (only models the check has reached) but always present.
    p = CT_MART["completion_steps_v2"]
    if p.exists():
        con = duckdb.connect()
        try:
            frames.append(con.execute(f"""
                SELECT customer AS workcell, name AS name_raw,
                       COUNT(*) AS rows, COUNT(DISTINCT assembly) AS models
                FROM read_parquet('{p.as_posix()}')
                WHERE side = 'MES' AND name IS NOT NULL AND trim(name) <> ''
                GROUP BY customer, name
            """).df())
        finally:
            con.close()

    if not frames:
        log.warning("no scan source - mes_step layer will be empty")
        return pd.DataFrame()
    d = (pd.concat(frames, ignore_index=True)
           .groupby(["workcell", "name_raw"], as_index=False)[["rows", "models"]].sum())
    d["system"] = "mes_step"
    d["is_iedb"] = False
    return d


def _mes_configured() -> pd.DataFrame:
    """Steps MES has CONFIGURED on a route one of this workcell's models can run.

    NOT the same claim as `_mes_scanned`, and the distinction is the whole point.
    A route is shared between customers, so joining route -> model -> customer
    hands every workcell every step on the bay. Done that way on 2026-08-18 it
    turned 7,251 names into 62,612 rows: `MI BTM 3` was filed under 34 workcells,
    and `AOI BTM` appeared as unanswered in 33 workcells that never scan it.

    So these rows are kept as EVIDENCE and never answered. They are what makes
    "configured on the route, never seen in a scan" computable — which is the
    real prize from the route pull — but they never imply ownership.
    """
    steps, routes = (CT_MART["raw"].parent / "mes_process_master.parquet",
                     CT_MART["raw"].parent / "mes_route_master.parquet")
    amap = CT_MART["mes_assembly_map"]
    if not (steps.exists() and routes.exists() and amap.exists()):
        return pd.DataFrame()
    import duckdb
    con = duckdb.connect()
    try:
        d = con.execute(f"""
            SELECT a.customer AS workcell, s.description AS name_raw,
                   COUNT(*) AS rows, COUNT(DISTINCT r.number) AS models
            FROM read_parquet('{routes.as_posix()}') r
            JOIN read_parquet('{steps.as_posix()}') s
              ON r.fma_route_id = s.factory_m_a_route_i_d
            JOIN (SELECT DISTINCT customer, number FROM read_parquet('{amap.as_posix()}')) a
              ON regexp_replace(upper(a.number), '[^A-Z0-9]', '', 'g')
               = regexp_replace(upper(r.number), '[^A-Z0-9]', '', 'g')
            WHERE s.description IS NOT NULL AND trim(s.description) <> ''
            GROUP BY a.customer, s.description
        """).df()
    finally:
        con.close()
    d["system"] = "mes_configured"
    d["is_iedb"] = False
    return d


def _bridge() -> pd.DataFrame:
    p = CT_MART["mes_process_map"]
    if not p.exists():
        return pd.DataFrame()
    d = pd.read_parquet(p)
    return pd.DataFrame({
        "workcell": d["customer"], "name_raw": d["step_instance"],
        "rows": 1, "models": 0, "system": "bridge", "is_iedb": d["is_iedb"],
    })


def run() -> int:
    frames = [f for f in (_iedb(), _mes_scanned(), _mes_configured(), _bridge()) if len(f)]
    if not frames:
        log.error("registry_build: no inputs - keeping the previous file")
        return 0
    d = pd.concat(frames, ignore_index=True)

    d["workcell_id"] = d["workcell"].map(_cnorm)
    d["name_key"] = d["name_raw"].map(_snorm)
    d["process_key"] = d["name_raw"].map(pkey)

    # A MES step is answered when SOME row in the same workcell carries the same
    # identity from the IEDB side. That is the whole mechanism: identity, never
    # string similarity.
    iedb_keys = set(zip(d.loc[d["system"] == "iedb_alias", "workcell_id"],
                        d.loc[d["system"] == "iedb_alias", "process_key"]))
    known = set(zip(d.loc[d["system"] == "bridge", "workcell_id"],
                    d.loc[d["system"] == "bridge", "name_key"]))
    non_iedb = set(zip(d.loc[(d["system"] == "bridge") & (~d["is_iedb"]), "workcell_id"],
                       d.loc[(d["system"] == "bridge") & (~d["is_iedb"]), "name_key"]))

    def answer(r):
        if r["system"] == "iedb_alias":
            return "mapped"
        if r["system"] == "mes_configured":
            # Evidence, not a claim. Answering these would put 62,612 questions
            # in front of an engineer, most about steps their workcell never runs.
            return "configured"
        k = (r["workcell_id"], r["name_key"])
        if k in non_iedb:
            return "non_iedb"                       # declared rework/handling
        if (r["workcell_id"], r["process_key"]) in iedb_keys or k in known:
            return "mapped"
        return "unmapped"                           # nobody has answered it

    d["answer"] = d.apply(answer, axis=1)
    d["scans"] = 0
    d["source_customer"] = d["workcell"]

    cols = ["workcell_id", "workcell", "system", "name_raw", "name_key",
            "process_key", "answer", "rows", "models", "scans", "is_iedb",
            "source_customer"]
    d = d[cols].drop_duplicates(["workcell_id", "system", "name_raw"])

    # A collapsed rebuild would quietly shrink the bridge and change verdicts,
    # the same guard the catalogue and route pulls needed.
    before = sum(1 for _ in open(OUT, encoding="utf-8")) - 1 if OUT.exists() else 0
    if before and len(d) < before * 0.8:
        log.error("registry SHRANK %d -> %d rows - keeping the previous file", before, len(d))
        return before

    REG_DIR.mkdir(parents=True, exist_ok=True)
    # QUOTE_ALL: trailing and double spaces ARE the evidence. 20 names differ
    # only by that, and an unquoted round-trip erases them.
    d.to_csv(OUT, index=False, quoting=csv.QUOTE_ALL, encoding="utf-8")

    log.info("registry: %d rows | %d workcells | %s",
             len(d), d["workcell_id"].nunique(),
             " ".join(f"{k}={v}" for k, v in d["answer"].value_counts().items()))
    return len(d)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s %(message)s")
    run()
