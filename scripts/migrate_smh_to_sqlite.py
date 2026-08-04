"""
scripts/migrate_smh_to_sqlite.py
────────────────────────────────
One-time import of the ten SMH .xls files into the `smh` SQLite table.

After this runs, the pipeline reads SMH from SQLite (see ingest_smh) and the
.xls files become an optional bulk-import format rather than the store.

  python -m scripts.migrate_smh_to_sqlite --dry-run    # report only, write nothing
  python -m scripts.migrate_smh_to_sqlite              # import

Safe to re-run: upsert_many() updates changed values and skips identical ones.
Rows with a blank / zero SMH in the .xls are skipped rather than stored as 0 —
compute.py cannot tell a stored 0 from a missing row, so writing them would
create rows that look present but earn nothing.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.database import init_db                       # noqa: E402
from modules.ole import smh_store                       # noqa: E402
from modules.ole.pipeline.ingest import parse_all_smh_files  # noqa: E402

log = logging.getLogger("migrate_smh")


def main() -> int:
    p = argparse.ArgumentParser(description="Import SMH .xls files into SQLite")
    p.add_argument("--dry-run", action="store_true", help="Report what would happen, write nothing")
    p.add_argument("--by", default="migration", help="Recorded as updated_by / changed_by")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")

    init_db()

    df = parse_all_smh_files()
    if df.empty:
        log.error("No SMH rows parsed from the .xls files — nothing to import.")
        return 1

    usable = df[df["smh_value"] > 0]
    log.info("Parsed %d rows across %d workcells (%d usable, %d blank/zero)",
             len(df), df["workcell"].nunique(), len(usable), len(df) - len(usable))
    log.info("\n%s", df.groupby("workcell").agg(
        rows=("assembly", "size"),
        usable=("smh_value", lambda s: int((s > 0).sum())),
    ).to_string())

    existing = smh_store.count_smh()
    log.info("Rows already in the smh table: %d", existing)

    if args.dry_run:
        log.info("Dry run - nothing written.")   # ASCII: the console codepage mangles em dashes
        return 0

    result = smh_store.upsert_many(usable.to_dict("records"), by=args.by, source="xls")
    log.info("Import complete: %(created)d created, %(updated)d updated, %(skipped)d skipped", result)
    log.info("Total rows in smh table now: %d", smh_store.count_smh())
    log.info("Next pipeline refresh will use these values.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
