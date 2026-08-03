"""
refresh.py  (ebuild)
────────────────────
Standalone entry point for the eBuild runner marts, so they can run on their
OWN schedule instead of being chained onto the end of the cycle-time pipeline.

  python -m modules.ebuild.pipeline.refresh              # historical + projection
  python -m modules.ebuild.pipeline.refresh --months 12  # shorter history

Why this exists
───────────────
This rebuild used to run at the end of every cycle-time refresh, purely so the
Plant Runners `has_data` badges reflected fresh cycle-time data. But that flag
is a set-membership test against assembly_summary.parquet — milliseconds —
while the rebuild re-pulls 24 MONTHS of MES buildplan over SQL.

On 2026-08-03 a dropped MES connection made that pull hang for 51 minutes,
turning a 10-minute cycle-time run into 67. `has_data` is now computed at read
time (api/routers/ebuild.py::_read_runners), so the two pipelines are
independent and fail independently.

Steps:
  1. build_runners_mart        — units built per (customer, assembly), N months
  2. build_projection_runners  — MES planned demand, ~4 weeks forward
"""

import argparse
import logging
import sys
from pathlib import Path

# Allow running as a script from the project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

# Logging is configured in __main__ for standalone/scheduled runs, or by the API
# at startup. No basicConfig here - it fought core.logging_setup on import.
log = logging.getLogger(__name__)


def run(months: int = 24) -> bool:
    """Rebuild both runner marts. Returns False if either fails.

    The MES pull is bounded by MES_QUERY_TIMEOUT_S (default 300s) so a bad
    network day fails in minutes rather than hanging indefinitely.
    """
    from api.routers.ebuild import build_runners_mart, build_projection_runners_mart

    ok = True
    try:
        n = build_runners_mart(months)
        log.info("historical runners mart rebuilt (%s months, %s rows)", months, n)
    except Exception:
        log.exception("historical runners rebuild FAILED")
        ok = False

    # Projection is independent of the historical pull - attempt it either way,
    # so one bad query doesn't cost us both marts.
    try:
        n = build_projection_runners_mart()
        log.info("projection runners mart rebuilt (%s rows)", n)
    except Exception:
        log.exception("projection runners rebuild FAILED")
        ok = False

    return ok


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="eBuild runner mart refresh")
    p.add_argument("--months", type=int, default=24,
                   help="Months of buildplan history for the runner ranking (default 24)")
    args = p.parse_args()

    from core.logging_setup import setup_logging, task_run
    setup_logging()
    # Under `python -m ...` __name__ becomes "__main__", which would tag every
    # line as "core" and miss the per-module log. __spec__.name keeps the real
    # dotted path. Rebinding the module-level `log` means run() gets it too.
    log = logging.getLogger(__spec__.name if __spec__ else __name__)
    with task_run(log, mode=f"{args.months}mo", trigger="scheduled"):
        if not run(months=args.months):
            raise SystemExit(1)          # raise, so task_run records RUN FAILED
