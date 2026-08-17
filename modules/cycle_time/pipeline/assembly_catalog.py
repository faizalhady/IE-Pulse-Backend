"""
assembly_catalog.py  (cycle_time pipeline)
──────────────────────────────────────────
The FULL IEDB assembly catalogue — every assembly a customer has, with a
`has_data` flag saying whether a cycle time exists for it.

WHY THIS IS NOT `raw.parquet`
  `raw.parquet` is the CYCLE-TIME table: it holds only assemblies that already
  have a time. Asking it "is this model in IEDB?" can therefore only ever answer
  "does it have a cycle time?" — two different questions, one answer. That
  mistake mislabelled 66 LAM RESEARCH models on 2026-08-14 as "not in IEDB, go
  create the record" when the truth was "the record is there, go time it".
  THIS file is the model list. `has_data` is the flag.

WHY IT LIVES HERE AND NOT IN THE ROUTER
  It was defined in `api/routers/cycle_time.py`, so nothing in the pipeline
  could call it without a circular import — the router already imports the
  pipeline. That is why it was never chained into the nightly refresh, and why
  prod's catalogue sat at 9 Jul while `raw.parquet` refreshed every night. Five
  and a half weeks: any model created in that window read as "Not in IEDB".
  Routers stay thin; logic lives in modules/ (repo CLAUDE.md).

THE SHRINK GUARD IS NOT OPTIONAL
  A per-customer IEDB failure is caught and skipped, and whatever was collected
  gets written. So one network blip silently shrinks the catalogue, and a
  shrunk catalogue turns real models into "Not in IEDB" — the exact bug this
  file exists to prevent. Every write is checked against the previous row count
  and rolled back if it collapses.

Run:  python -m modules.cycle_time.pipeline.assembly_catalog
"""

import logging
import shutil
from datetime import datetime

import pandas as pd

from modules.cycle_time.config import CT_CUSTOMERS, CT_MART

log = logging.getLogger(__name__)

COLS = ["customer", "assembly_id", "assembly", "assembly_full", "revision",
        "description", "family", "updated_on", "has_data"]

# Below this share of the previous row count the write is treated as a partial
# pull and rolled back. IEDB grows slowly; a real refresh never loses a tenth.
_MIN_KEEP = 0.9


def run() -> int:
    """Rebuild assembly_catalog.parquet. Returns rows written, or the previous
    row count if the pull was partial and got rolled back."""
    from modules.cycle_time.client import fetch_assemblies

    path = CT_MART["assembly_catalog"]
    before = 0
    bak = None
    if path.exists():
        before = len(pd.read_parquet(path, columns=["customer"]))
        bak = path.with_suffix(f".{datetime.now():%Y%m%d_%H%M%S}.prerefresh.bak")
        shutil.copy2(path, bak)

    rows, skipped = [], []
    for c in CT_CUSTOMERS:
        cust, div = c["customer"], c.get("division", "")
        try:
            full = fetch_assemblies(cust, div, has_raw_data=None)
            with_data = fetch_assemblies(cust, div, has_raw_data=True)
        except Exception as e:
            log.warning("assembly catalog: skipping %s (%s)", cust, e)
            skipped.append(cust)
            continue
        with_ids = {a.get("AssemblyId") for a in with_data}
        for a in full:
            rows.append({
                "customer": cust,
                "assembly_id": a.get("AssemblyId"),
                "assembly": a.get("AssemblyName"),
                "assembly_full": a.get("Assembly"),
                "revision": a.get("AssemblyRevision"),
                "description": a.get("AssemblyDescription"),
                "family": a.get("CustomerFamily"),
                "updated_on": a.get("UpdatedOn"),
                "has_data": a.get("AssemblyId") in with_ids,
            })

    df = pd.DataFrame(rows, columns=COLS)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)

    if before and len(df) < before * _MIN_KEEP:
        if bak:
            shutil.copy2(bak, path)
        log.error("assembly catalog SHRANK %d -> %d rows (skipped: %s). "
                  "RESTORED the previous file - a partial catalogue reports real "
                  "models as 'Not in IEDB'.", before, len(df), ", ".join(skipped) or "none")
        return before

    log.info("assembly catalog: wrote %d rows across %d customers (%+d)%s",
             len(df), df["customer"].nunique() if len(df) else 0, len(df) - before,
             f" - SKIPPED {', '.join(skipped)}" if skipped else "")
    return len(df)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s %(message)s")
    print(f"rows: {run()}")
