"""
modules/ppqt/pipeline/refresh.py
────────────────────────────────
PPQT pipeline entry point: parse every workbook in data/raw/ppqt/ into the
PPQT marts. There is no incremental mode - a workbook is small and a full
re-parse is the simplest way to stay correct when an IE re-drops a file.

  python -m modules.ppqt.pipeline.refresh
  python -m modules.ppqt.pipeline.refresh --full     # same thing; kept for the scheduler convention
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import argparse
import logging

from modules.ppqt.pipeline.ingest import run as run_ingest

# Logging is configured in __main__ for standalone/scheduled runs, or by the API
# at startup. No basicConfig here - it fought core.logging_setup on import and
# sent scheduled-run output to a console that nothing captures.
log = logging.getLogger(__name__)


def run(mode: str = "full") -> bool:
    log.info(f"PPQT pipeline start (mode={mode})")
    ok = run_ingest()
    log.info("PPQT pipeline " + ("done" if ok else "FAILED"))
    return ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Full reload (the only mode)")
    args = parser.parse_args()

    from core.logging_setup import setup_logging, task_run
    setup_logging()
    log = logging.getLogger(__spec__.name if __spec__ else __name__)
    with task_run(log, mode="full", trigger="scheduled"):
        if not run():
            raise SystemExit(1)
