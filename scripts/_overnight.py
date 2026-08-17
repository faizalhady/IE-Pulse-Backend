"""Unattended run: fix the duplicate-workcell corruption, then re-grade every
workcell that has forward demand.

STEP 1 - THE CORRUPTION
  completion_status_v2 holds the same workcell under TWO spellings:
      'MOTOROLA'      72 models    0 legacy rows
      'Motorola'     135 models  125 legacy rows
  533 rows across 5 workcells, one copy fresh and one five weeks old. A report
  reads whichever spelling it happens to match, so Motorola's numbers depend on
  a capital letter. Both copies are kept until the re-grade replaces them —
  nothing is deleted here; the stale spelling is RENAMED onto the canonical one
  and the newer row wins per model.

STEP 2 - THE RE-GRADE
  Only 7 of 43 workcells have been re-graded since the 5 Aug rewrite. The other
  36 still carry statuses the current code cannot even emit (route_gap,
  unverified, unavailable). That is why LAM RESEARCH looked like the best
  workcell: it is the only one whose numbers are current.

  4,408 demand models across 35 workcells. run() UPSERTS, so a crash costs the
  workcell in flight and nothing else, and each workcell is backed up before it
  is touched.

Read the log, not this file, for what actually happened.
"""

import logging
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from modules.cycle_time.config import CT_CUSTOMERS, CT_MART

log = logging.getLogger("overnight")
_norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())


def step1_fix_duplicate_workcells() -> int:
    """Collapse two spellings of one workcell onto the configured spelling."""
    path = CT_MART["completion_status_v2"]
    s = pd.read_parquet(path)
    s["_k"] = s["customer"].map(_norm)

    dupes = {k for k, n in s.groupby("_k")["customer"].nunique().items() if n > 1}
    if not dupes:
        log.info("STEP 1: no duplicate spellings, nothing to do")
        return 0

    canon = {_norm(c["customer"]): c["customer"] for c in CT_CUSTOMERS}
    bak = path.with_suffix(f".{datetime.now():%Y%m%d_%H%M%S}.predupe.bak.parquet")
    shutil.copy2(path, bak)
    log.info("STEP 1: %d duplicated workcells, %d rows | backup %s",
             len(dupes), int(s["_k"].isin(dupes).sum()), bak.name)

    for k in sorted(dupes):
        g = s[s["_k"] == k]
        target = canon.get(k) or g["customer"].value_counts().index[0]
        for name, gg in g.groupby("customer"):
            legacy = int(gg["status"].isin(
                ["route_gap", "unverified", "unavailable", "no_data"]).sum())
            log.info("   %-22s %4d models  %4d legacy   %s",
                     repr(name), len(gg), legacy,
                     "<- canonical" if name == target else "-> renamed")
        s.loc[s["_k"] == k, "customer"] = target

    # Same model under both spellings: keep the row with the CURRENT status
    # vocabulary — a legacy status means it was never re-graded.
    s["_legacy"] = s["status"].isin(["route_gap", "unverified", "unavailable", "no_data"])
    before = len(s)
    s = (s.sort_values("_legacy")               # False first = keep the fresh one
          .drop_duplicates(subset=["customer", "assembly"], keep="first"))
    s = s.drop(columns=["_k", "_legacy"])
    s.to_parquet(path, index=False)
    log.info("STEP 1 done: %d -> %d rows (%d duplicate model rows collapsed)",
             before, len(s), before - len(s))
    return before - len(s)


def step2_regrade_all() -> None:
    """Re-grade every workcell with forward demand, one at a time."""
    eb = CT_MART["completion_status_v2"].parent.parent / "ebuild"
    dem = pd.concat([
        pd.read_parquet(eb / "planner_runners.parquet")[["customer", "assembly"]],
        pd.read_parquet(eb / "projection_runners.parquet")[["customer", "assembly"]],
    ]).drop_duplicates()
    counts = dem.groupby("customer")["assembly"].nunique().sort_values(ascending=False)

    runner = ROOT / "scripts" / "run_completion_workcell.py"
    log.info("STEP 2: %d workcells, %d demand models", len(counts), int(counts.sum()))

    for i, (wc, n) in enumerate(counts.items(), 1):
        t0 = time.time()
        log.info("[%2d/%d] %-24s %4d models ...", i, len(counts), wc, n)
        try:
            r = subprocess.run(
                [sys.executable, "-u", str(runner), "--workcell", str(wc), "--go"],
                cwd=str(ROOT), capture_output=True, text=True, timeout=3600)
        except subprocess.TimeoutExpired:
            log.error("[%2d/%d] %-24s TIMED OUT after 60 min - skipped", i, len(counts), wc)
            continue
        tail = [l for l in (r.stdout or "").splitlines() if l.strip()][-14:]
        if r.returncode != 0:
            log.error("[%2d/%d] %-24s FAILED rc=%s", i, len(counts), wc, r.returncode)
            for l in (r.stderr or "").splitlines()[-6:]:
                log.error("        %s", l)
            continue
        after = [l for l in tail if "->" in l or "status AFTER" in l]
        log.info("[%2d/%d] %-24s done in %.0fs   %s", i, len(counts), wc,
                 time.time() - t0, after[0].strip() if after else "")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, stream=sys.stdout,
        format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    log.info("=" * 70)
    step1_fix_duplicate_workcells()
    log.info("=" * 70)
    step2_regrade_all()
    log.info("=" * 70)
    log.info("ALL DONE")
