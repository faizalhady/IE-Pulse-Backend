"""
customer_status.py  (cycle_time)
────────────────────────────────
Snapshot the IEDB CustomerStatus coverage report into the mart.

Why
───
The /api/cycle-time/customer-status endpoint used to call IEDB live on every
request. Measured 2026-08-03: 3.6s cold on the server, on a call the Cycle Time
landing page makes on every load. A 5-minute TTL cache hid it for most users but
every service restart paid it again, and the endpoint failed outright whenever
IEDB was unreachable.

Snapshotting also makes it *more* correct: the coverage numbers now match the
daily marts rendered beside them, instead of being a live figure sitting next to
day-old data.

Best-effort: a failure here leaves the previous snapshot in place and does NOT
fail the pipeline — stale coverage is better than no coverage.
"""

import logging

import pandas as pd

from modules.cycle_time.config import CT_MART

log = logging.getLogger(__name__)


def run(site: str = "pen") -> bool:
    log.info("=" * 60)
    log.info("CYCLE TIME CUSTOMER-STATUS  starting")
    log.info("=" * 60)

    out = CT_MART["customer_status"]
    try:
        from modules.cycle_time.client import fetch_customer_status
        rows = fetch_customer_status(site=site)
    except Exception:
        log.exception("CustomerStatus fetch failed — keeping the previous snapshot")
        return False

    if not rows:
        log.warning("CustomerStatus returned no rows — keeping the previous snapshot")
        return False

    df = pd.DataFrame(rows)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    log.info(f"customer_status.parquet written ({len(df)} customers) → {out}")
    log.info("CYCLE TIME CUSTOMER-STATUS  complete")
    return True
