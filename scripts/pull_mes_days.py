"""
pull_mes_days.py - fill the MES #21 day cache for EVERY configured customer.

    python scripts/pull_mes_days.py                     # list only
    python scripts/pull_mes_days.py --days 1095 --go    # the real pull

WHY THIS EXISTS (vs run_completion_scope.py)
    run_completion_scope pulls days only for customers that still have models to
    grade. A customer with nothing left to check is skipped, so its days are never
    fetched. That is right for grading and wrong for building a production history:
    the models we most want to find are the ones IEDB has never heard of, and they
    live in exactly the customers grading skips.

    So this script asks one question only - "what ran, on which day, at which
    step" - for every customer, with no reference to IEDB at all. Grading happens
    later, locally, off the cache.

WHAT IT COSTS
    One MES call per customer-day NOT already on disk. A day already cached is a
    disk read. 40 customers x 1095 days = 43,800 files; ~25k already exist.

TODAY IS NOT CACHED
    completion_v2.batch_steps starts its loop at day 0, so it writes a parquet for
    a day that is still in progress and then never refreshes it - scan_day returns
    early on any file that exists. Every past run froze a partial 'today' into the
    cache. This script starts at day 1, and deletes any day-file whose mtime falls
    on the day it claims to describe, so those partials are re-pulled whole.

ponytail: no resume state. The cache IS the state - an interrupted run costs
nothing but the day it was mid-flight on.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.cycle_time import completion_v2 as v2                     # noqa: E402
from modules.cycle_time.config import CT_CUSTOMERS, CT_MART, CT_MES_SCAN_DIR  # noqa: E402

log = logging.getLogger("pulldays")


def _customer_ids() -> list[tuple[str, int]]:
    """[(configured name, MES customer_id)] - the map is the only source of ids."""
    amap = pd.read_parquet(CT_MART["mes_assembly_map"])
    cid: dict = {}
    for c, i in zip(amap["customer"], amap["customer_id"]):
        cid.setdefault(v2._cnorm(c), i)
    out, lost = [], []
    for c in CT_CUSTOMERS:
        name = c["customer"]
        i = cid.get(v2._cnorm(name))
        (out.append((name, int(i))) if i is not None else lost.append(name))
    if lost:
        log.warning("%d customer(s) have no MES id, cannot be scanned: %s",
                    len(lost), ", ".join(sorted(lost)))
    return out


def _drop_partial_days() -> int:
    """Delete day-files captured on the day they describe - they are half a day."""
    n = 0
    for p in CT_MES_SCAN_DIR.rglob("*.parquet"):
        try:
            day = datetime.strptime(p.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if datetime.fromtimestamp(p.stat().st_mtime).date() == day:
            p.unlink()
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1095, help="how far back, in days")
    ap.add_argument("--go", action="store_true", help="make MES calls; without it, list only")
    ap.add_argument("--keep-partials", action="store_true",
                    help="do not re-pull day-files that were captured mid-day")
    a = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    root = logging.getLogger(); root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout); sh.setFormatter(fmt); root.addHandler(sh)
    (ROOT / "logs").mkdir(exist_ok=True)
    logfile = ROOT / "logs" / f"pull_mes_days_{datetime.now():%Y%m%d_%H%M}.log"
    fh = logging.FileHandler(logfile, encoding="utf-8"); fh.setFormatter(fmt); root.addHandler(fh)
    log.info("log file: %s", logfile)

    custs = _customer_ids()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    days = [today - timedelta(days=i) for i in range(1, a.days + 1)]   # day 1 = yesterday

    if not a.keep_partials:
        if a.go:
            log.info("dropped %d partial day-file(s) captured mid-day", _drop_partial_days())
        else:
            log.info("would drop partial day-files captured mid-day (run with --go)")

    todo = {}
    for name, cid in custs:
        d = CT_MES_SCAN_DIR / v2._cnorm(name)
        miss = [x for x in days if not (d / f"{x:%Y-%m-%d}.parquet").exists()]
        todo[(name, cid)] = miss

    total = sum(len(v) for v in todo.values())
    print(f"\ncustomers    : {len(custs)}")
    print(f"window       : {a.days} days  ({days[-1]:%Y-%m-%d} .. {days[0]:%Y-%m-%d})")
    print(f"day-files    : {len(custs) * a.days:,} wanted, {len(custs) * a.days - total:,} cached")
    print(f"TO PULL      : {total:,}\n")
    for (name, _), miss in sorted(todo.items(), key=lambda kv: -len(kv[1])):
        if miss:
            print(f"  {name:<28}{len(miss):>7,}")

    if not a.go:
        print("\nDRY RUN - nothing sent to MES. Add --go to run it.")
        return 0

    t0 = datetime.now()
    done = fail = 0
    # smallest first: the same reason completion_v2 does it - bank the quick wins.
    for (name, cid), miss in sorted(todo.items(), key=lambda kv: len(kv[1])):
        if not miss:
            continue
        c0, ok, bad = datetime.now(), 0, 0
        for day in miss:
            try:
                v2.scan_day(name, cid, day)
                ok += 1
            except v2.MESWebApiError:
                bad += 1
        done += ok; fail += bad
        secs = (datetime.now() - c0).total_seconds()
        log.info("  %-28s %5d pulled / %4d failed  in %5.1f min  (%.2f s/day)",
                 name, ok, bad, secs / 60, secs / max(ok + bad, 1))

    mins = (datetime.now() - t0).total_seconds() / 60
    log.info("PULL COMPLETE: %d days pulled, %d failed, in %.0f min", done, fail, mins)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
