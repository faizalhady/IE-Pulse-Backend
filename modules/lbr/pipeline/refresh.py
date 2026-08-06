"""
modules/lbr/pipeline/refresh.py
───────────────────────────────
LBR pipeline entry point. Placeholder — overwrite with real ingest/transform
when the module is built out.

  python -m modules.lbr.pipeline.refresh                 # incremental
  python -m modules.lbr.pipeline.refresh --full          # full reload
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import argparse
import logging

# Logging is configured in __main__ for standalone/scheduled runs, or by the API
# at startup. No basicConfig here - it fought core.logging_setup on import and
# sent scheduled-run output to a console that nothing captures.
log = logging.getLogger(__name__)


def run(mode: str = "incremental") -> bool:
    log.info(f"LBR pipeline placeholder invoked (mode={mode}) - nothing to do yet")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    mode = "full" if args.full else "incremental"

    from core.logging_setup import setup_logging, task_run
    setup_logging()
    # Under `python -m ...` __name__ becomes "__main__", which would tag every
    # line as "core" and miss the per-module log. __spec__.name keeps the real
    # dotted path. Rebinding the module-level `log` means run() gets it too.
    log = logging.getLogger(__spec__.name if __spec__ else __name__)
    with task_run(log, mode=mode, trigger="scheduled"):
        if not run(mode=mode):
            raise SystemExit(1)          # raise, so task_run records RUN FAILED
