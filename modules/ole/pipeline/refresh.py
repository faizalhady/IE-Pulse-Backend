"""
refresh.py
──────────
Single entry point. Run this to refresh all mart data end-to-end.

  python pipeline/refresh.py                  # incremental (default — safe)
  python pipeline/refresh.py --incremental    # same as above (explicit)
  python pipeline/refresh.py --full           # nuke & re-read everything

Modes
  incremental — reads every file in the share and MERGES over the mart. Dates
                the share covers are rebuilt from source; dates it no longer
                holds are PRESERVED. Self-healing: a date missed or partially
                read today is repaired on the next run. Use for all routine and
                scheduled runs.

  full        — re-reads everything currently visible in the network share
                and overwrites marts. Anything no longer in the share
                is LOST. Use only for disaster recovery / schema migration.

Incremental used to skip any date already in the mart, which made a missed
date permanent and silently drifted two machines apart. It no longer does —
see ingest.run() for what that cost us. There is deliberately no third
"repair" mode: repair is what the normal run does.

Steps
  1. ingest  — load raw sources, normalise, write Parquet
  2. compute — DuckDB JOIN + OLE calculation, write ole_computed.parquet
  3. weekly  — ISO-week aggregation, write ole_weekly.parquet
  4. mh      — per-shift man-hours distribution, write mh_distribution.parquet
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import argparse
import logging
from datetime import datetime

from modules.ole.pipeline.ingest         import run as run_ingest
from modules.ole.pipeline.compute        import run as run_compute
from modules.ole.pipeline.compute_weekly import run as run_compute_weekly
from modules.ole.pipeline.compute_mh     import run as run_compute_mh

# Logging is configured in __main__ for standalone/scheduled runs, or by the API
# at startup. No basicConfig here - it fought core.logging_setup on import and
# sent scheduled-run output to a console that nothing captures.
log = logging.getLogger(__name__)


def run(mode: str = "incremental"):
    start = datetime.now()
    title = "INCREMENTAL REFRESH" if mode == "incremental" else "FULL REFRESH"
    log.info("╔══════════════════════════════════════════════════════════╗")
    log.info(f"║              OLE PIPELINE  —  {title:<28s}║")
    log.info("╚══════════════════════════════════════════════════════════╝")
    log.info(f"Started at {start.strftime('%Y-%m-%d %H:%M:%S')}")

    ok = run_ingest(mode=mode)
    if not ok:
        log.error("Ingest failed — pipeline aborted.")
        sys.exit(1)

    ok = run_compute()
    if not ok:
        log.error("Compute failed — mart may be incomplete.")
        sys.exit(1)

    ok = run_compute_weekly()
    if not ok:
        log.error("Weekly compute failed — ole_weekly.parquet not written.")
        sys.exit(1)

    ok = run_compute_mh()
    if not ok:
        log.error("MH-distribution compute failed — mh_distribution.parquet not written.")
        sys.exit(1)

    elapsed = (datetime.now() - start).total_seconds()
    log.info(f"Pipeline complete in {elapsed:.1f}s")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="OLE pipeline refresh")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--incremental", action="store_const", const="incremental", dest="mode",
                   help="Append new files to existing mart (default — preserves historical data)")
    g.add_argument("--full",        action="store_const", const="full",        dest="mode",
                   help="Re-read everything currently in the share, overwriting marts")
    p.set_defaults(mode="incremental")
    args = p.parse_args()

    from core.logging_setup import setup_logging, task_run
    setup_logging()
    # Under `python -m ...` __name__ becomes "__main__", which would tag every
    # line as "core" and miss the per-module log. __spec__.name keeps the real
    # dotted path. Rebinding the module-level `log` means run() gets it too.
    log = logging.getLogger(__spec__.name if __spec__ else __name__)
    with task_run(log, mode=args.mode, trigger="scheduled"):
        run(mode=args.mode)
