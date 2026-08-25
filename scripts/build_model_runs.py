"""
build_model_runs.py - what actually ran, straight from the #21 day cache.

    python scripts/build_model_runs.py            # print, write nothing
    python scripts/build_model_runs.py --write    # write data/mart/cycle_time/model_runs.parquet

WHY THIS EXISTS
    "Which models are active" used to come from ebuild/runners.parquet - a
    SQL Server build-plan aggregate (SP_GET_SY_SMT_BUILDPLAN, keyed on
    SMT_Assembly). The verdicts come from the MES WebAPI day scans (#21, keyed
    on Assembly). Two systems, two keys, so the same model could read as "built"
    in one and "no production in 3 years" in the other - 1,083 of them did.

    This builds the same answer from the SAME files the verdicts use, so the two
    can never disagree again. Measured 2026-08-25: runners holds 11,884 rows with
    units=0 (planned, never built) which is most of the gap; of real production
    #21 carries 99.2% of all units.

GRAIN
    one row per (customer, assembly) seen in the day cache, over its whole span.

    NOTE the revision. #21's Assembly carries a revision suffix, so 4001303011LD
    and 4001303011LE are two rows here. Whether that is one model or two is a
    domain question nobody has ruled on yet. ❓

ponytail: steps are aggregated away on purpose - this answers WHEN, not WHICH
STEPS. The per-step view is a separate pass over the same files; build it when
something actually needs it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.cycle_time.config import CT_MART, CT_MES_SCAN_DIR      # noqa: E402

OUT = CT_MART["raw"].parent / "model_runs.parquet"

# A quiet day is still cached, and an EMPTY day-file stores its columns as the
# parquet NULL type. DuckDB cannot unify that with the VARCHAR/BIGINT of a busy
# day in the same folder - it silently skipped 9 whole customers before
# union_by_name + explicit casts were added here.
_SQL = ("select left(right(filename,18),10)::DATE as day, "
        "       cast(assembly as VARCHAR) as assembly, "
        "       coalesce(try_cast(qty as BIGINT), 0) as qty "
        "from read_parquet(?, filename=true, union_by_name=true) "
        "where assembly is not null and cast(assembly as VARCHAR) <> ''")


def build() -> pd.DataFrame:
    c = duckdb.connect()
    parts, empty = [], []
    for d in sorted(p for p in CT_MES_SCAN_DIR.iterdir() if p.is_dir()):
        try:
            df = c.execute(_SQL, [(d / "*.parquet").as_posix()]).df()
        except Exception as ex:                                    # noqa: BLE001
            print(f"  SKIP {d.name}: {str(ex)[:70]}")
            continue
        if len(df):
            df["customer"] = d.name
            parts.append(df)
        else:
            empty.append(d.name)
    if empty:
        # LAMMEC and ADVANTEST are verified-zero MES production, so empty is the
        # right answer for them - say so rather than leaving a silent hole.
        print(f"  no production in cache: {', '.join(empty)}")

    s = pd.concat(parts, ignore_index=True)
    s["day"] = pd.to_datetime(s["day"])
    return (s.groupby(["customer", "assembly"])
              .agg(first_seen=("day", "min"), last_seen=("day", "max"),
                   days_seen=("day", "nunique"), units=("qty", "sum"))
              .reset_index())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    runs = build()
    print(f"\nmodels that ran : {len(runs):,}")
    print(f"customers       : {runs['customer'].nunique()}")
    print(f"day range       : {runs['first_seen'].min():%Y-%m-%d} .. {runs['last_seen'].max():%Y-%m-%d}")
    print("\nby 'last ran on or after':")
    for cut in ("2024-01-01", "2024-09-01", "2025-01-01", "2025-09-01", "2026-01-01"):
        print(f"   {cut} : {int((runs['last_seen'] >= pd.Timestamp(cut)).sum()):>7,}")

    if a.write:
        runs.to_parquet(OUT, index=False)
        print(f"\nwrote {OUT}  ({len(runs):,} rows)")
    else:
        print("\nnot written - add --write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
