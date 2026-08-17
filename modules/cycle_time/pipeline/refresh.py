"""
refresh.py  (cycle_time)
────────────────────────
Single entry point for the Cycle Time pipeline.

  python -m modules.cycle_time.pipeline.refresh                                    # incremental
  python -m modules.cycle_time.pipeline.refresh --full                             # full re-fetch
  python -m modules.cycle_time.pipeline.refresh --full --exclude KEYSIGHT          # 40 customers
  python -m modules.cycle_time.pipeline.refresh --full --only   KEYSIGHT           # just KEYSIGHT
  python -m modules.cycle_time.pipeline.refresh --full --exclude KEYSIGHT,ARISTANETWORKS,Tellabs

Steps:
  1. ingest           — fetch detail from IEDB3.0 API → raw.parquet
  2. transform        — pivot raw → pivoted.parquet (Image 2 layout)
  3. eff              — fetch GRP Summary → eff_by_line.parquet (efficiency/line)
  4. assembly_summary — precompute the per-assembly list mart (SMH + eff + flags)
  5. planner_demand   — parse the OneDrive-synced planner Excels → planner_demand.parquet
"""

import sys
import argparse
import logging
from datetime import datetime
from pathlib import Path

# Allow running as a script from the project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from modules.cycle_time.pipeline.ingest           import run as run_ingest, run_backfill
from modules.cycle_time.pipeline.transform        import run as run_transform
from modules.cycle_time.pipeline.eff              import run as run_eff
from modules.cycle_time.pipeline.assembly_summary import run as run_assembly_summary
from modules.cycle_time.pipeline.customer_status  import run as run_customer_status
from modules.cycle_time.pipeline.assembly_catalog import run as run_assembly_catalog
from modules.cycle_time.planner_demand            import (build_planner_demand,
                                                          build_planner_runners_mart)
from modules.cycle_time.keep_awake                 import keep_system_awake

# Logging is configured in __main__ for standalone/scheduled runs, or by the API
# at startup. No basicConfig here - it fought core.logging_setup on import and
# sent scheduled-run output to a console that nothing captures.
log = logging.getLogger(__name__)


def run(mode: str = "incremental",
        only: list[str] | None = None,
        exclude: list[str] | None = None,
        overlap_days: int = 7) -> bool:
    start = datetime.now()
    title = {"incremental": "INCREMENTAL", "full": "FULL", "backfill": "BACKFILL (add-only upsert)"}.get(mode, mode.upper())

    log.info("+==========================================================+")
    log.info(f"|        CYCLE TIME PIPELINE  -  {title:<28s}|")
    log.info("+==========================================================+")
    log.info(f"Started at {start.strftime('%Y-%m-%d %H:%M:%S')}")

    if mode == "backfill" and not only:
        log.error("Backfill requires --only <customer(s)> - it is a targeted, safe upsert.")
        return False

    # Keep the machine awake for the whole ingest so a long unattended pull
    # isn't killed by idle-sleep tearing down the network connection.
    with keep_system_awake():
        if mode == "backfill":
            # Add-only upsert for the named customer(s): fetch ALL rows (no date
            # filter), upsert into raw.parquet. Never rebuilds from shards, never
            # prunes, never touches other customers. Heals watermark-gap misses.
            ingest_ok = run_backfill(only=only)
        else:
            ingest_ok = run_ingest(mode=mode, only=only, exclude=exclude, overlap_days=overlap_days)
        if not ingest_ok:
            log.error("Ingest failed - pipeline aborted.")
            return False

    if not run_transform():
        log.error("Transform failed - pivoted.parquet not written.")
        return False

    # Efficiency is a best-effort enrichment — if the Summary pull fails the
    # pipeline still completes; assembly_summary just carries NULL eff.
    with keep_system_awake():
        if not run_eff(only=only, exclude=exclude):
            log.warning("Efficiency build did not produce eff_by_line.parquet - continuing with NULL eff.")

    if not run_assembly_summary():
        log.error("Assembly-summary build failed - assembly_summary.parquet not written.")
        return False

    # Snapshot the IEDB coverage report so the endpoint reads a mart instead of
    # calling IEDB live on every request. Best-effort — a failure keeps the
    # previous snapshot and does not fail the pipeline.
    run_customer_status()

    # ── raw process entities: what each system DEFINES, not what it ran ───────
    # Every process name we had came from production_scan — one month of what
    # actually ran. That answers "what happened lately", not "what exists".
    # Best-effort: each keeps its previous file on failure and never fails the
    # pipeline, same contract as customer_status above.
    #
    # ⚠️ mes_process_master calls MES as usrId=142 — a REAL EMPLOYEE (`khoom`,
    # Khoo MN), not a service account. Every nightly call is attributed to them
    # and this breaks the day their account changes. Chained on 2026-08-17 on
    # the explicit instruction to run it anyway and log the username loudly if
    # it stops working. REPLACE WITH A SERVICE ACCOUNT.
    # registry_build LAST of the three: it reads the two masters above, so it has
    # to see this run's versions. It was a file copied by hand from a laptop —
    # on 2026-08-18 the server had none, which silently dropped process_bridge to
    # workbook-only and changed verdicts with nothing on screen to say why.
    for _name, _fn in (("mes_process_master", "modules.cycle_time.pipeline.mes_process_master"),
                       ("iedb_process_master", "modules.cycle_time.pipeline.iedb_process_master"),
                       ("registry", "modules.cycle_time.pipeline.registry_build"),
                       # LAST: it reads the catalogue, raw, the status mart and
                       # demand, so it must see this run's versions. Precomputed
                       # because every user gets the same frame until tomorrow —
                       # computing it per request cost 12s on six endpoints.
                       ("model_universe", "modules.cycle_time.model_universe")):
        try:
            import importlib
            n = importlib.import_module(_fn).run()
            log.info("%s: %s rows", _name, f"{n:,}")
        except Exception as e:
            log.error("%s FAILED - keeping the previous file: %s", _name, e)
            if _name == "mes_process_master":
                log.error("  if this is an auth/permission error, usrId=142 (MES user "
                          "'khoom', Khoo MN) is no longer usable - get a service account")

    # The IEDB model list + has_data flag. NOT chained until 2026-08-17, because
    # it lived in api/routers/cycle_time.py and the pipeline cannot import a
    # router. Consequence: prod's catalogue was a 9 JUL snapshot while
    # raw.parquet refreshed nightly, so every model created after that date read
    # as "Not in IEDB" - including two Faiz flagged by hand on 14 Aug. Two IEDB
    # calls per customer, ~5 min. Best-effort: a failure keeps the previous
    # catalogue (the builder rolls back a partial pull itself) and must not stop
    # the run.
    try:
        n = run_assembly_catalog()
        log.info(f"Assembly catalogue refreshed - {n} rows")
    except Exception as e:
        log.error(f"Assembly-catalogue refresh FAILED, mart left stale: {e}")

    # Planner demand — reads Choi Hui's SharePoint workbooks through the OneDrive sync
    # and rebuilds planner_demand.parquet + planner_runners.parquet. Unlike the eBuild
    # rebuild below, this is local file parsing only: no MES, no SQL, seconds not
    # minutes, so it is safe to chain. Best-effort, but LOUD on failure — this mart sat
    # 6 weeks stale (as_of 29 Jun, read in Aug) precisely because nothing rebuilt it and
    # nothing complained.
    try:
        n = build_planner_demand()
        r = build_planner_runners_mart()
        log.info(f"Planner demand refreshed - {n} rows, {r} planner-runner rows")
    except Exception as e:
        log.error(f"Planner-demand refresh FAILED, mart left stale: {e}")

    # NOTE: the eBuild runner rebuild used to be chained here, so the Plant
    # Runners `has_data` badges would reflect freshly-synced cycle-time data.
    # Removed 2026-08-03. That flag is a set-membership test against
    # assembly_summary.parquet — milliseconds — but refreshing it meant
    # re-pulling 24 MONTHS of MES buildplan over SQL. On 2026-08-03 a dropped
    # MES connection turned a 10-minute cycle-time run into 67 minutes.
    #
    # `has_data` is now computed at READ time in api/routers/ebuild.py
    # (_read_runners), cached on both marts' mtimes. That makes it MORE accurate
    # than the baked column ever was, and lets eBuild run on its own schedule
    # (IEPulse-eBuild-Refresh) so the two fail independently.

    elapsed = (datetime.now() - start).total_seconds()
    log.info(f"Cycle Time pipeline complete in {elapsed:.1f}s")
    return True


def _csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Cycle Time pipeline refresh")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--incremental", action="store_const", const="incremental", dest="mode",
                   help="Fetch only records updated since last run (default)")
    g.add_argument("--full",        action="store_const", const="full",        dest="mode",
                   help="Full re-fetch — overwrites raw.parquet")
    g.add_argument("--backfill",    action="store_const", const="backfill",    dest="mode",
                   help="Add-only upsert for --only customer(s): fetch ALL rows, upsert into "
                        "raw.parquet. Safe — never prunes, never touches other customers. "
                        "Heals watermark-gap misses. Requires --only.")
    p.set_defaults(mode="incremental")

    customer_group = p.add_mutually_exclusive_group()
    customer_group.add_argument("--only",    type=_csv, default=None,
                                help="Only ingest these customers (comma-separated, case-insensitive)")
    customer_group.add_argument("--exclude", type=_csv, default=None,
                                help="Skip these customers (comma-separated, case-insensitive)")

    p.add_argument("-v", "--verbose", action="store_true",
                   help="DEBUG logging — shows page-by-page fetch progress (useful to watch a long pull)")
    p.add_argument("--overlap-days", type=int, default=7,
                   help="Incremental look-back overlap in days (default 7). Larger = re-fetch more "
                        "(harmless — upsert dedups). Use a big value (e.g. 30) for a one-off catch-up run.")

    args = p.parse_args()

    from core.logging_setup import setup_logging, task_run
    setup_logging()
    # Under `python -m ...` __name__ becomes "__main__", which would tag every
    # line as "core" and miss the per-module log. __spec__.name keeps the real
    # dotted path. Rebinding the module-level `log` means run() gets it too.
    log = logging.getLogger(__spec__.name if __spec__ else __name__)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    with task_run(log, mode=args.mode, trigger="scheduled"):
        success = run(mode=args.mode, only=args.only, exclude=args.exclude,
                      overlap_days=args.overlap_days)
        if not success:
            raise SystemExit(1)          # raise, so task_run records RUN FAILED
