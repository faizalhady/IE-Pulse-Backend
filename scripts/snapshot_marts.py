"""
snapshot_marts.py — take a rollback point before a run that rewrites marts, and
restore from one afterwards if it went wrong.

    python scripts/snapshot_marts.py take                 # before a big run
    python scripts/snapshot_marts.py list
    python scripts/snapshot_marts.py restore 20260818_0322 --go

WHY
  `completion_v2.run()` UPSERTS into completion_status_v2.parquet IN PLACE.
  There is no new file and no version — a run that goes wrong overwrites the
  verdicts everyone is reading, and the only way back was a scatter of
  per-workcell `.bak` files with different timestamps.

  A 4,408-model run touching 38 workcells is not something to start without one
  clean point to return to.

WHAT IS IN A SNAPSHOT
  The two verdict marts, the precomputed universe, and the decisions database.
  Together they are everything a completion run can change. Marts that a run
  only READS (raw, catalogue, demand) are not copied: they are rebuilt by the
  nightly from source and restoring them would be the wrong move anyway.

  ~13 MB each. Cheap enough to take one before every run.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CT = ROOT / "data" / "mart" / "cycle_time"
SNAPS = CT / "_snapshots"

#: (source, name-in-snapshot). Everything a completion run can write.
FILES = [
    (CT / "completion_status_v2.parquet", "completion_status_v2.parquet"),
    (CT / "completion_steps_v2.parquet", "completion_steps_v2.parquet"),
    (CT / "model_universe.parquet", "model_universe.parquet"),
    (ROOT / "data" / "operational.db", "operational.db"),
]

mb = lambda n: f"{n / 1_048_576:.1f}MB"


def take(label: str | None = None) -> Path:
    d = SNAPS / (label or datetime.now().strftime("%Y%m%d_%H%M%S"))
    d.mkdir(parents=True, exist_ok=True)
    for src, name in FILES:
        if src.exists():
            shutil.copy2(src, d / name)
            print(f"  saved {name:<34}{mb(src.stat().st_size)}")
        else:
            print(f"  skip  {name:<34}(does not exist)")
    print(f"\nsnapshot: {d}")
    return d


def ls() -> None:
    if not SNAPS.exists():
        print("no snapshots yet")
        return
    for d in sorted(SNAPS.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        n = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
        rows = ""
        p = d / "completion_status_v2.parquet"
        if p.exists():
            try:
                import pyarrow.parquet as pq
                rows = f"  {pq.ParquetFile(p).metadata.num_rows:,} verdicts"
            except Exception:
                pass
        print(f"  {d.name}   {mb(n):>8}{rows}")


def restore(label: str, go: bool) -> int:
    d = SNAPS / label
    if not d.is_dir():
        print(f"no snapshot {label!r}. Run `list`.")
        return 1
    print(f"restore {d}\n")
    for src, name in FILES:
        f = d / name
        if not f.exists():
            print(f"  skip    {name:<34}(not in this snapshot)")
            continue
        cur = f"{mb(src.stat().st_size)}" if src.exists() else "absent"
        print(f"  RESTORE {name:<34}{mb(f.stat().st_size)}   (current: {cur})")
    if not go:
        print("\nLIST ONLY - nothing restored. Re-run with --go.")
        return 0
    # Take a snapshot of the CURRENT state first: restoring is itself a
    # destructive act, and undoing it must not require the same conversation.
    print("\nsnapshotting current state before overwriting it:")
    take("pre_restore_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    print()
    for src, name in FILES:
        f = d / name
        if f.exists():
            shutil.copy2(f, src)
            print(f"  restored {name}")
    print("\ndone. Restart the backend so it drops its caches.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["take", "list", "restore"])
    ap.add_argument("label", nargs="?", help="snapshot name, for restore")
    ap.add_argument("--go", action="store_true", help="actually restore")
    a = ap.parse_args()
    if a.action == "take":
        take(a.label)
    elif a.action == "list":
        ls()
    else:
        if not a.label:
            print("restore needs a snapshot name. Run `list`.")
            return 1
        return restore(a.label, a.go)
    return 0


if __name__ == "__main__":
    sys.exit(main())
