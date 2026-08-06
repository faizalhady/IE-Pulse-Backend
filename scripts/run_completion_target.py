"""
run_completion_target.py — run the completion check against the DEMAND list.

    python scripts/run_completion_target.py --top 500
    python scripts/run_completion_target.py --top 500 --dry-run
    python scripts/run_completion_target.py --all

Why this exists (vs scripts/run_completion_v2.py)
─────────────────────────────────────────────────
run_completion_v2.py classifies the same models v1 did, so the two could be
compared like-for-like. That was right for validating v2, but it is the wrong
set to *operate* on: it is anchored to a historical list, not to what is
actually running.

This script scopes by DEMAND instead — what is being built now and what is
planned next:

    MES projection  (SP_GET_SY_SMT_BUILDPLAN, ~4wk forward)   39 workcells
  UNION
    planner demand  (planners' Excel, ~13wk)                  18 workcells

MES alone misses workcells the planners track; the planners' Excel covers only
18 workcells. The union covers both. Measured 2026-08-04: 3,907 models.

Then rank by units and take the top N, because volume is extremely concentrated:

    top   100 models = 66% of all planned volume
    top   500 models = 88%
    bottom 1,907     =  0.5%

So --top 500 buys ~88% of the volume for 13% of the models. Chasing the tail
costs MES calls and returns almost nothing.

Serial coverage is the real ceiling: the check needs a serial per model to read
its route from #132. Top 500 currently sits at 87%.
"""

import argparse
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.cycle_time import completion_v2 as v2
from modules.cycle_time.config import CT_MART

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
EBUILD = ROOT / "data" / "mart" / "ebuild"

_norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())


def setup_log(tag: str) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    # Windows hands a redirected stdout cp1252, which cannot encode the tick in
    # completion_v2's per-customer line — and a logging failure kills the run.
    # On 6 Aug that ended a 4,126-model rebuild after one customer. Force UTF-8
    # rather than policing every log string for non-cp1252 characters.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    p = LOG_DIR / f"completion_target_{datetime.now():%Y%m%d_%H%M%S}_{tag}.log"
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.FileHandler(p, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.info("log file: %s", p)
    return p


def target_list() -> pd.DataFrame:
    """MES projection UNION planner demand, one row per model, ranked by units."""
    frames = []
    for name, src in [("projection_runners.parquet", "mes"),
                      ("planner_runners.parquet", "planner")]:
        p = EBUILD / name
        if not p.exists():
            logging.warning("%s missing — that half of the demand list is absent", name)
            continue
        d = pd.read_parquet(p).groupby(["customer", "assembly"], as_index=False)["units"].sum()
        d["src"] = src
        frames.append(d)
    if not frames:
        raise SystemExit("no demand marts found — run IEPulse-eBuild-Refresh first")

    both = pd.concat(frames, ignore_index=True)
    both["k"] = [_norm(c) + "|" + _norm(a) for c, a in zip(both["customer"], both["assembly"])]
    tgt = (both.groupby("k")
                .agg(customer=("customer", "first"), assembly=("assembly", "first"),
                     units=("units", "sum"), sources=("src", lambda s: "+".join(sorted(set(s)))))
                .reset_index(drop=True)
                .sort_values("units", ascending=False)
                .reset_index(drop=True))
    return tgt


def attach_customer_id(tgt: pd.DataFrame) -> pd.DataFrame:
    """MES customer_id, from the MES assembly map — NOT the IEDB ids in config."""
    amap = pd.read_parquet(CT_MART["mes_assembly_map"])
    cid = {}
    for c, i in zip(amap["customer"], amap["customer_id"]):
        cid.setdefault(v2._cnorm(c), i)
    tgt = tgt.copy()
    tgt["customer_id"] = tgt["customer"].map(lambda c: cid.get(v2._cnorm(c)))
    miss = tgt["customer_id"].isna()
    if miss.any():
        logging.warning("no MES customer_id for %d models across: %s",
                        int(miss.sum()), ", ".join(sorted(tgt.loc[miss, "customer"].unique())[:12]))
    tgt = tgt[~miss].copy()
    tgt["customer_id"] = tgt["customer_id"].astype(int)     # MES rejects "59.0"
    return tgt.reset_index(drop=True)


def report_coverage(tgt: pd.DataFrame) -> None:
    if not CT_MART["mes_serial_index"].exists():
        logging.warning("no serial index — every model will fall back to the weak #21 source")
        return
    sidx = pd.read_parquet(CT_MART["mes_serial_index"])
    have = {_norm(a) for a in sidx["assembly"].dropna()}
    hit = tgt["assembly"].map(lambda a: _norm(a) in have)
    logging.info("serial coverage: %d/%d models (%.0f%%) — %.0f%% of the volume",
                 int(hit.sum()), len(tgt), 100 * hit.mean(),
                 100 * tgt.loc[hit, "units"].sum() / max(tgt["units"].sum(), 1))
    logging.info("models WITHOUT a serial fall back to #21 (customer-aggregate, "
                 "drags in rework and other variants' steps) and are tagged source='batch'")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--top", type=int, default=500, help="how many models, ranked by units (default 500)")
    g.add_argument("--all", action="store_true", help="every model in the demand list")
    ap.add_argument("--window", type=int, default=v2._WINDOW_DAYS, help="MES history window, days")
    ap.add_argument("--no-resume", action="store_true", help="ignore checkpoint, start over")
    ap.add_argument("--dry-run", action="store_true", help="show the target list, make no MES calls")
    a = ap.parse_args()

    tag = "all" if a.all else f"top{a.top}"
    p = setup_log("dry" if a.dry_run else tag)

    tgt = target_list()
    full_units = tgt["units"].sum()
    logging.info("demand list: %d models, %d workcells, %s units",
                 len(tgt), tgt["customer"].nunique(), f"{full_units:,.0f}")

    if not a.all:
        tgt = tgt.head(a.top)
        logging.info("taking top %d by units — %.0f%% of the demand volume",
                     a.top, 100 * tgt["units"].sum() / max(full_units, 1))

    tgt = attach_customer_id(tgt)
    report_coverage(tgt)

    if a.dry_run:
        logging.info("dry run — stopping before any MES call")
        print(tgt.head(25).to_string(index=False))
        return

    from modules.cycle_time.keep_awake import keep_system_awake
    with keep_system_awake():
        v2.run(tgt[["customer", "assembly", "customer_id"]],
               window=a.window, use_serial=True, resume=not a.no_resume)

    res = pd.read_parquet(CT_MART["completion_status_v2"])
    keys = {_norm(c) + "|" + _norm(x) for c, x in zip(tgt["customer"], tgt["assembly"])}
    mine = res[[( _norm(c) + "|" + _norm(x)) in keys
                for c, x in zip(res["customer"], res["assembly"])]]
    logging.info("RESULT for this target list (%d models):\n%s",
                 len(mine), mine["status"].value_counts().to_string())
    logging.info("by source:\n%s", mine["source"].value_counts().to_string())
    logging.info("DONE — log: %s", p)


if __name__ == "__main__":
    main()
