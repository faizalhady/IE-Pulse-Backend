"""
mes_process_master.py  (cycle_time pipeline)
────────────────────────────────────────────
RAW ENTITY LAYER — every route step MES DEFINES, exactly as MES states it.
One call, ~91,000 rows, every factory / MA / route / step in the plant.

WHY THIS IS A RAW LAYER AND NOT A MART
  A domain is laid out entities-first: pull each source's own list, whole and
  unjoined, before aggregating anything. Every MES step name we had came from
  `production_scan` — one MONTH of what actually RAN. Ask it "what steps does
  MES define?" and it answers "what happened recently", which is a different
  question and a moving one.

  This is the route master: the configured route, in order, whether or not a
  board has walked it lately.

BOTH NAMING LEVELS, WHICH IS THE POINT
  MES names at two levels and we only ever had the specific one:
      StepName      the general step     'AOI'        'C.COATING'
      Description   the step INSTANCE    'AOI TOP'    'C.COATING BOT 1.1'
  `Description` is the MNS workbook's join key to the IEDB alias. Having both,
  from the source, is what lets a general↔general match happen at all — matching
  only the specific level is why `MA 1` looked like one process across three
  workcells.

THE PARAMETERS, WHICH COST AN HOUR TO FIND
      {"factory": "%", "usrId": "142", "langId": "0"}
  The stored proc demands `@FactoryName`, so `factoryName` looks obvious and is
  wrong — the API maps `factory`. It also needs `usrId`, undocumented. Every
  other spelling (factoryName / FactoryName / maName / routeName / stepName, in
  any combination) returns the same "@FactoryName was not supplied".

  ⚠️ `usrId=142` IS A REAL EMPLOYEE — MES user `khoom`, Khoo MN. Every call is
  attributed to them and this breaks the day their account does. The vault
  records this as exploration-only. **Ask the MES admin for a service account.**
  Until then this ingest borrows a person's identity, which is a thing to fix,
  not a thing to forget.

Run:  python -m modules.cycle_time.pipeline.mes_process_master
"""

import logging
import re

import pandas as pd

from modules.cycle_time.config import CT_MART
from modules.cycle_time.mes_webapi import post

log = logging.getLogger(__name__)

OUT = CT_MART["raw"].parent / "mes_process_master.parquet"

# See the docstring. `factory` (not factoryName) + usrId, both undocumented.
_PARAMS = {"factory": "%", "usrId": "142", "langId": "0"}

_norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())

KEEP = ["RouteStep_ID", "FactoryMARoute_ID", "FactoryName", "ManufacturingAreaName",
        "RouteName", "Step_ID", "StepName", "Descr", "Description", "Occurrence",
        "StepOrder", "StepType", "StepTypeName", "NextStep_ID", "BirthingStation",
        "WorkCenter_ID", "WorkCenterText", "LastUpdated"]


def run() -> int:
    rows = post("Route", "ListRouteStep", _PARAMS)
    if not rows:
        log.error("ListRouteStep returned nothing - keeping the previous file")
        return 0

    df = pd.DataFrame(rows)
    df = df[[c for c in KEEP if c in df.columns]].copy()
    df.columns = [re.sub(r"(?<!^)(?=[A-Z])", "_", c).lower().replace("__", "_")
                  for c in df.columns]

    # A previous pull that collapsed would quietly shrink the plant's route
    # master; the same guard the catalogue needed, for the same reason.
    # Row count from the FOOTER, not by reading a column. The guard used to ask
    # for `route_step_id`, but the mangler above turns `RouteStep_ID` into
    # `route_step_i_d` — so the guard raised ArrowInvalid on every run after the
    # first and the whole pull failed on its own safety check. Reading metadata
    # needs no column name at all, so it cannot drift with the mangling again.
    import pyarrow.parquet as pq
    before = pq.ParquetFile(OUT).metadata.num_rows if OUT.exists() else 0
    if before and len(df) < before * 0.9:
        log.error("mes_process_master SHRANK %d -> %d rows - keeping the previous file",
                  before, len(df))
        return before

    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, index=False)

    log.info("mes_process_master: %d rows | %d routes, %d factories, "
             "%d general step names, %d step instances -> %s",
             len(df), df["route_name"].nunique(), df["factory_name"].nunique(),
             df["step_name"].map(_norm).nunique(),
             df["description"].dropna().map(_norm).nunique(), OUT.name)
    return len(df)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s %(message)s")
    n = run()
    if n:
        d = pd.read_parquet(OUT)
        lam = d[d["route_name"].str.upper().str.contains("LAM", na=False)]
        print(f"\nLAM routes: {lam['route_name'].nunique()} | "
              f"{lam['step_name'].map(_norm).nunique()} general names | "
              f"{lam['description'].dropna().map(_norm).nunique()} instances")
        print(d.head(3)[["route_name", "step_name", "description", "step_order"]]
              .to_string(index=False))
