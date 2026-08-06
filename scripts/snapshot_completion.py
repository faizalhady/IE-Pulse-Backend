"""Append this week's completion rollup to the history mart.

    python scripts/snapshot_completion.py

Run it after a completion refresh - run_completion_target.py calls it for you.
Safe to run again: it replaces the current week rather than adding to it.

It deliberately reads the SAME joined frame the Incompletion Report serves,
rather than re-deriving demand from the ebuild marts. Two computations of the
same number is how the drawer and the badge ended up disagreeing; the trend and
the table must never be able to tell different stories.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.cycle_time import completion_history as hist


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    # Imported here, not at module scope: this pulls in FastAPI and the whole
    # router, which is a slow import to pay for `--help`.
    from api.routers.cycle_time import _completion_demand, _completion_demand_key

    data = _completion_demand(_completion_demand_key())
    models = data.get("models") or []
    if not models:
        raise SystemExit("no completion data - run scripts/run_completion_target.py first")

    out = hist.append(models, datetime.now())
    latest = out[out["iso_week"] == out["iso_week"].max()]
    done = int(latest.loc[latest["status"] == "complete", "units"].sum())
    total = int(latest["units"].sum())
    logging.info("week %s: %.1f%% of demand units complete (%s of %s)",
                 latest["iso_week"].iloc[0], 100 * done / max(total, 1),
                 f"{done:,}", f"{total:,}")


if __name__ == "__main__":
    main()
