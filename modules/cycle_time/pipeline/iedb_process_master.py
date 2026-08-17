"""
iedb_process_master.py  (cycle_time pipeline)
─────────────────────────────────────────────
RAW ENTITY LAYER — every process IEDB DEFINES, exactly as IEDB states it.

WHY THIS IS A RAW LAYER AND NOT A MART
  A domain is laid out entities-first: pull each source's own list, whole and
  unjoined, and only then aggregate. Everything cycle-time reads today is
  already an aggregate of something — `raw.parquet` is the CYCLE-TIME table, so
  it can only ever tell you about processes that have a TIME. Ask it "what
  processes does IEDB define?" and it answers a different question.

  This file asks the question directly. No joining, no merging, no
  normalisation — the registry does that downstream, and it can only do it
  honestly if the raw layer underneath is complete.

TWO SOURCES, TWO LEVELS
  Report/SubWorkcenterConfig   the SPECIFIC level: one row per
                               (workcell, workcenter, sub-workcenter, process,
                               alias, station). 4,834 rows, 28 workcells.
  Processes/BaseProcessNames   the GENERAL level: IEDB's base process
                               vocabulary + category. 98 rows, no params.

  IEDB names things at two levels and we had only ever pulled the specific one
  (`alias`, and only where timed). MES does the same. Matching them one level at
  a time is why `MA 1` looked like one process across three workcells.

WHAT IT ADDS OVER raw.parquet
  189 aliases plant-wide that IEDB defines and nobody has ever timed. Invisible
  in the cycle-time table by construction — a step with no time has no row.

Run:  python -m modules.cycle_time.pipeline.iedb_process_master
"""

import logging

import pandas as pd
import requests
import urllib3

from modules.cycle_time.client import _headers
from modules.cycle_time.config import API_TIMEOUT, BASE_URL, CT_MART, SITE_CODE

urllib3.disable_warnings()
log = logging.getLogger(__name__)

OUT = CT_MART["raw"].parent / "iedb_process_master.parquet"
OUT_BASE = CT_MART["raw"].parent / "iedb_base_process.parquet"


def _get(path: str, params: dict) -> list:
    r = requests.get(f"{BASE_URL}/api/{path}", headers=_headers(), params=params,
                     timeout=max(API_TIMEOUT, 60), verify=False)
    r.raise_for_status()
    d = r.json()
    return d if isinstance(d, list) else (d.get("data") or d.get("Data") or [])


def run(site: str = SITE_CODE) -> int:
    """Write both levels. Returns rows in the specific-level file."""
    rows = _get("Report/SubWorkcenterConfig", {"siteCode": site})
    df = pd.DataFrame(rows)
    if df.empty:
        log.error("SubWorkcenterConfig returned nothing - keeping the previous file")
        return 0

    # `Workcell` is NULL on many rows; `Customer` is the one that is always
    # populated, and is what every other cycle-time mart keys on.
    df["workcell_src"] = df["Workcell"].fillna(df["Customer"])
    df.columns = [c.lower() if c != "workcell_src" else c for c in df.columns]
    df["site"] = site

    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, index=False)
    log.info("iedb_process_master: %d rows | %d workcells, %d processes, %d aliases, "
             "%d sub-workcenters -> %s", len(df), df["workcell_src"].nunique(),
             df["process"].nunique(), df["alias"].nunique(),
             df["subworkcenter"].nunique(), OUT.name)

    # The general level. No params, tiny, and it is the vocabulary the specific
    # names are variants of — the container level the registry has been deriving
    # by string-splitting instead of reading.
    try:
        base = pd.DataFrame(_get("Processes/BaseProcessNames", {}))
        if len(base):
            base.columns = [c.lower() for c in base.columns]
            base.to_parquet(OUT_BASE, index=False)
            log.info("iedb_base_process: %d base process names -> %s",
                     len(base), OUT_BASE.name)
    except Exception as e:
        log.warning("BaseProcessNames failed, skipped: %s", e)

    return len(df)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s %(message)s")
    n = run()
    if n:
        d = pd.read_parquet(OUT)
        print(f"\n{n:,} rows")
        print(d.head(4).to_string()[:400])
        raw = pd.read_parquet(CT_MART["raw"], columns=["customer", "alias"]).dropna()
        import re
        norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())
        extra = set(d["alias"].dropna().map(norm)) - set(raw["alias"].map(norm))
        print(f"\naliases IEDB defines that have NEVER been timed: {len(extra)}")
