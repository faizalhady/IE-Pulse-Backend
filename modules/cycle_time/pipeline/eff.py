"""
eff.py  (cycle_time)
────────────────────
Builds eff_by_line.parquet — (customer, sub_workcenter) → efficiency goal.

Efficiency (`effGoal`) is NOT in the detail raw data; it lives in the GRP-level
GetSummaryGroupProcessData endpoint. Efficiency is a PER-LINE standard (same
across the products on a line), so we collapse the Summary down to one eff per
(customer, sub_workcenter) — a tiny lookup the assembly-summary build and the
/assembly-builds endpoint join against to get real UPH.

Network step (small): one Summary request per customer (GRP-rolled, no
pagination). Resilient — a customer that fails just contributes no eff rows;
the pipeline continues with NULL eff for that line.
"""

import logging

import duckdb
import pandas as pd

from modules.cycle_time.client import fetch_summary
from modules.cycle_time.config import CT_CUSTOMERS, CT_MART
from modules.cycle_time.pipeline.ingest import _camel_to_snake

log = logging.getLogger(__name__)

# Max assemblies per Summary request — keeps the URL/response bounded. effGoal
# is per line, so we only need a handful (one per line) to cover every line.
_REP_BATCH = 60


def _representative_assemblies(customer: str) -> list[str]:
    """One assembly per sub_workcenter for this customer (from raw.parquet) —
    a small set that collectively touches every line, so the scoped Summary
    pull returns each line's effGoal without fetching the whole customer."""
    raw = CT_MART["raw"]
    if not raw.exists():
        return []
    con = duckdb.connect()
    try:
        df = con.execute(
            f"""
            SELECT ANY_VALUE(assembly) AS assembly
            FROM read_parquet('{raw.as_posix()}')
            WHERE customer = ? AND sub_workcenter IS NOT NULL AND assembly IS NOT NULL
            GROUP BY sub_workcenter
            """,
            [customer],
        ).df()
    finally:
        con.close()
    return df["assembly"].dropna().astype(str).unique().tolist()


def run(only: list[str] | None = None, exclude: list[str] | None = None) -> bool:
    log.info("=" * 60)
    log.info("CYCLE TIME EFF (efficiency by line)  starting")
    log.info("=" * 60)

    only_set    = {c.lower() for c in only}    if only    else None
    exclude_set = {c.lower() for c in exclude} if exclude else set()

    rows: list[dict] = []
    for c in CT_CUSTOMERS:
        name, division = c["customer"], c["division"]
        if only_set is not None and name.lower() not in only_set:
            continue
        if name.lower() in exclude_set:
            continue

        # Scope the pull to a few representative assemblies (one per line) so big
        # customers don't time out. Fall back to an unscoped pull if we can't
        # read the line list (e.g. raw.parquet missing).
        reps = _representative_assemblies(name)
        try:
            if reps:
                recs = []
                for i in range(0, len(reps), _REP_BATCH):
                    recs.extend(fetch_summary(name, division, assemblies=reps[i:i + _REP_BATCH]))
            else:
                recs = fetch_summary(name, division)
        except Exception as e:
            log.warning(f"  {name}: summary fetch failed ({type(e).__name__}: {e}) - skipping")
            continue
        if not recs:
            continue
        df = pd.DataFrame(recs)
        df.columns = [_camel_to_snake(col) for col in df.columns]
        if "sub_workcenter" not in df.columns or "eff_goal" not in df.columns:
            log.warning(f"  {name}: summary missing sub_workcenter/eff_goal cols - skipping")
            continue
        df["eff_goal"] = pd.to_numeric(df["eff_goal"], errors="coerce")
        # One eff per line: average across that line's GRP rows (they should be
        # equal; AVG is just a safe collapse), ignoring blank/zero goals.
        per_line = (
            df[df["eff_goal"] > 0]
            .groupby("sub_workcenter", dropna=True)["eff_goal"]
            .mean()
            .reset_index()
        )
        per_line.insert(0, "customer", name)
        rows.append(per_line)
        log.info(f"  {name}: {len(per_line)} lines with efficiency")

    if not rows:
        log.warning("No efficiency rows collected - eff_by_line.parquet not written.")
        return False

    out = pd.concat(rows, ignore_index=True).rename(columns={"eff_goal": "eff"})
    out.to_parquet(CT_MART["eff_by_line"], index=False)
    log.info(f"eff_by_line.parquet written ({len(out):,} lines across "
             f"{out['customer'].nunique()} customers) -> {CT_MART['eff_by_line']}")
    log.info("CYCLE TIME EFF  complete")
    return True


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
    p = argparse.ArgumentParser(description="Build eff_by_line.parquet from GetSummaryGroupProcessData")
    p.add_argument("--only", type=lambda s: [x.strip() for x in s.split(",") if x.strip()], default=None)
    p.add_argument("--exclude", type=lambda s: [x.strip() for x in s.split(",") if x.strip()], default=None)
    a = p.parse_args()
    run(only=a.only, exclude=a.exclude)
