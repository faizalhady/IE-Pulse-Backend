"""
mes_process_map.py  (cycle_time)
────────────────────────────────
Loads the curated MNS process-mapping workbook into mes_process_map.parquet —
the naming bridge the completion-status feature (P3) needs:

    (customer, MES StepInstance)  →  IEDB Alias

MES route/scan data identifies a step by its **instance** (the route step's
`Description`, e.g. "COMBO LINK"); IEDB identifies it by **alias** (raw.parquet
`alias`, e.g. "LINK (COMBO) 1"). This file is the human-curated join between them.

Uses the consolidated `MES` sheet (all customers). IEDB-Alias values starting
with '#' are status flags (#REWORK/#OBSOLETE/#UNKNOWN…), not real aliases — dropped.

Run:  python -m modules.cycle_time.pipeline.mes_process_map
"""

import logging
import re

import pandas as pd

from modules.cycle_time.config import CT_CUSTOMERS, CT_MART, MES_PROCESS_MAP_XLSX

log = logging.getLogger(__name__)

_cnorm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())          # customer key
_snorm = lambda s: re.sub(r"\s+", " ", str(s).strip().upper())        # step-instance key


def run() -> bool:
    log.info("MES process-map load starting — %s", MES_PROCESS_MAP_XLSX)
    try:
        df = pd.read_excel(MES_PROCESS_MAP_XLSX, sheet_name="MES", header=1)
    except Exception as e:
        log.error("Cannot read MNS workbook: %s", e)
        return False

    df = df[["Customer", "StepInstance", "IEDB Alias"]].dropna(subset=["Customer", "StepInstance", "IEDB Alias"])

    # map MNS customer names → our config workcell names (near-identical; normalise + match)
    cfg_by_key = {_cnorm(c["customer"]): c["customer"] for c in CT_CUSTOMERS}
    df["customer"] = df["Customer"].map(lambda c: cfg_by_key.get(_cnorm(c)))
    unmatched = sorted(set(df.loc[df["customer"].isna(), "Customer"].astype(str)))
    if unmatched:
        log.info("MNS customers not in config (ignored): %s", ", ".join(unmatched))
    df = df.dropna(subset=["customer"])

    # is_iedb=True  → a real IEDB step (expected to have cycle time).
    # is_iedb=False → the workbook says this MES step is NOT an IEDB step: the "0"
    #   sentinel, or "#REWORK/#OBSOLETE/#UNKNOWN" flags. We KEEP these so the comparator
    #   can tell a "known non-IEDB step" apart from a genuinely-unmapped one (coverage).
    alias = df["IEDB Alias"].astype(str).str.strip()
    is_iedb = ~alias.str.startswith("#") & ~alias.isin(["0", "", "nan"])

    out = pd.DataFrame({
        "customer": df["customer"],
        "step_instance": df["StepInstance"].map(_snorm),
        "iedb_alias": alias.where(is_iedb, ""),
        "is_iedb": is_iedb.values,
    }).drop_duplicates(subset=["customer", "step_instance"])   # one classification per (customer, instance)

    CT_MART["mes_process_map"].parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(CT_MART["mes_process_map"], index=False)
    log.info("mes_process_map.parquet written (%d rows, %d workcells) → %s",
             len(out), out["customer"].nunique(), CT_MART["mes_process_map"])
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
    import sys
    sys.exit(0 if run() else 1)
