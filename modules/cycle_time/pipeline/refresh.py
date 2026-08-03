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

    log.info("╔══════════════════════════════════════════════════════════╗")
    log.info(f"║        CYCLE TIME PIPELINE  —  {title:<28s}║")
    log.info("╚══════════════════════════════════════════════════════════╝")
    log.info(f"Started at {start.strftime('%Y-%m-%d %H:%M:%S')}")

    if mode == "backfill" and not only:
        log.error("Backfill requires --only <customer(s)> — it is a targeted, safe upsert.")
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
            log.error("Ingest failed — pipeline aborted.")
            return False

    if not run_transform():
        log.error("Transform failed — pivoted.parquet not written.")
        return False

    # Efficiency is a best-effort enrichment — if the Summary pull fails the
    # pipeline still completes; assembly_summary just carries NULL eff.
    with keep_system_awake():
        if not run_eff(only=only, exclude=exclude):
            log.warning("Efficiency build did not produce eff_by_line.parquet — continuing with NULL eff.")

    if not run_assembly_summary():
        log.error("Assembly-summary build failed — assembly_summary.parquet not written.")
        return False

    # Chain the eBuild runner mart rebuild so the Plant Runners dashboard's
    # has_data badges reflect the freshly-synced cycle-time data. Non-fatal —
    # the cycle-time pipeline itself already succeeded.
    try:
        from api.routers.ebuild import build_runners_mart, build_projection_runners_mart
        build_runners_mart(24)             # historical (units built, 24mo)
        build_projection_runners_mart()    # projection (planned demand, ~4wk)
        log.info("Chained eBuild runner mart rebuild complete (historical + projection).")
    except Exception:
        log.exception("Chained eBuild runner refresh failed (non-fatal) — run POST /api/ebuild/refresh manually.")

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
