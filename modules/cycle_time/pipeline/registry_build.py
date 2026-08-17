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

THE THREE LAYERS, unchanged in meaning
    iedb_alias   what IEDB was told to price      raw.parquet
    mes_step     what MES defines as a step       mes_process_master.parquet
    bridge       the hand-curated map between     mes_process_map.parquet

  The original read SCANS for the mes_step layer — one month of what actually
  ran. This reads the ROUTE MASTER instead: what MES DEFINES, whether or not a
  board walked it lately. That is a superset (6,705 step instances against
  1,733 seen in a month) and it does not decay when a model pauses.

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


def _mes() -> pd.DataFrame:
    """What MES DEFINES as a step, from the route master.

    `description` is the step INSTANCE ('AOI TOP') and the level that joins to an
    IEDB alias; `step_name` is the general step ('AOI'). Both are kept — carrying
    only one of them is what made `MA 1` look like a single process plant-wide.

    The route master has no customer column, so the workcell comes from the
    assembly link. A step MES defines but no model of ours runs is not this
    workcell's step.
    """
    steps, routes = (CT_MART["raw"].parent / "mes_process_master.parquet",
                     CT_MART["raw"].parent / "mes_route_master.parquet")
    amap = CT_MART["mes_assembly_map"]
    if not (steps.exists() and routes.exists() and amap.exists()):
        log.warning("mes route/process master missing - mes_step layer will be empty")
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
    d["system"] = "mes_step"
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
    frames = [f for f in (_iedb(), _mes(), _bridge()) if len(f)]
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
