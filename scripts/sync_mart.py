"""
sync_mart.py — move mart files between this machine and the server (02).

    python scripts/sync_mart.py pull                 # 02 -> local   (the normal way)
    python scripts/sync_mart.py push                 # local -> 02   (after a heavy local run)
    python scripts/sync_mart.py pull --go            # actually copy; without --go it only lists
    python scripts/sync_mart.py push --only mes_route_master

WHY THIS EXISTS
  Local and 02 drift in BOTH directions and neither is simply "right":

      CODE   local runs ahead — it is where the work happens
      DATA   02 runs ahead — it is where the nightly job runs

  On 2026-08-17 that produced a real, reported-out-loud error: an analysis read
  the LOCAL completion mart (5 Aug) and compared it against a report built from
  PROD (17 Aug), then announced that two screens disagreed by 25 models. They did
  not. The marts did.

  So: 02 is the source of truth for data by default (`pull`). `push` exists
  because this machine is much faster than the server — a 5-hour route pull runs
  here, then the result goes up rather than being recomputed there.

SAFETY
  * Dry by default. Nothing is copied without --go.
  * NEWER FILES ARE NEVER SILENTLY OVERWRITTEN. If the destination is newer than
    the source the file is skipped and reported, unless --force. Overwriting a
    fresh nightly mart with a stale local copy is the one mistake that would be
    invisible afterwards.
  * Copies to a temp name then replaces, so an interrupted copy cannot leave a
    half-written parquet that every reader then chokes on.
  * Size and mtime are printed for every file, both sides, before anything moves.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

LOCAL = Path(__file__).resolve().parents[1] / "data" / "mart"
PROD = Path(r"\\mypenm0iesvr02\d$\Application\IE-Pulse\BACKEND\data\mart")

#: Only these move. A whitelist, not a mirror: `data/mart` also holds per-customer
#: MES scan caches (tens of thousands of small files) that are expensive to walk
#: and pointless to move — they are a cache, rebuilt on demand.
PATTERNS = ["cycle_time/*.parquet", "ebuild/*.parquet", "demand/*.parquet",
            "cycle_time/registry/*.csv"]

#: Never sync. Backups are per-machine history, and the scan cache is huge.
SKIP = ("mes_scans", "mes_board_steps", ".bak.", ".predupe.")

fmt = lambda t: datetime.fromtimestamp(t).strftime("%d %b %H:%M")
mb = lambda n: f"{n / 1_048_576:.1f}MB"


def files(root: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for pat in PATTERNS:
        for p in root.glob(pat):
            rel = p.relative_to(root).as_posix()
            if any(s in rel for s in SKIP):
                continue
            out[rel] = p
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("direction", choices=["pull", "push"],
                    help="pull = 02 to local (normal). push = local to 02.")
    ap.add_argument("--go", action="store_true", help="actually copy. Without it: list only.")
    ap.add_argument("--force", action="store_true",
                    help="overwrite even when the destination is NEWER. Think first.")
    ap.add_argument("--only", help="substring filter on the file name")
    a = ap.parse_args()

    if not PROD.exists():
        print(f"cannot reach {PROD}\nOn the Jabil network / VPN?")
        return 1

    src_root, dst_root = (PROD, LOCAL) if a.direction == "pull" else (LOCAL, PROD)
    print(f"{a.direction.upper()}   {src_root}\n  ->   {dst_root}\n")

    src, dst = files(src_root), files(dst_root)
    rels = sorted(r for r in src if not a.only or a.only in r)

    copy, newer, same = [], [], 0
    for rel in rels:
        s = src[rel]
        d = dst.get(rel)
        if d is None:
            copy.append((rel, "new", s, None))
        elif int(s.stat().st_mtime) > int(d.stat().st_mtime) + 2:
            copy.append((rel, "newer", s, d))
        elif int(d.stat().st_mtime) > int(s.stat().st_mtime) + 2:
            newer.append((rel, s, d))
        else:
            same += 1

    for rel, why, s, d in copy:
        was = f"  (dest {fmt(d.stat().st_mtime)} {mb(d.stat().st_size)})" if d else ""
        print(f"  {'COPY':<6} {rel:<52} {fmt(s.stat().st_mtime)} {mb(s.stat().st_size)}{was}")
    for rel, s, d in newer:
        print(f"  {'SKIP':<6} {rel:<52} destination is NEWER "
              f"({fmt(d.stat().st_mtime)} vs {fmt(s.stat().st_mtime)})")
    print(f"\n  {len(copy)} to copy, {len(newer)} skipped (destination newer), {same} identical")

    if newer and a.force:
        print("  --force: the skipped files WILL be overwritten")
        copy += [(r, "forced", s, d) for r, s, d in newer]

    if not a.go:
        print("\nLIST ONLY - nothing copied. Re-run with --go.")
        return 0
    if not copy:
        print("\nnothing to do")
        return 0

    ok = 0
    for rel, _why, s, _d in copy:
        target = dst_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            # Copy to .part then replace: an interrupted copy must never leave a
            # truncated parquet in place of a good one.
            shutil.copy2(s, tmp)
            os.replace(tmp, target)
            ok += 1
            print(f"  ok  {rel}")
        except Exception as e:
            tmp.unlink(missing_ok=True)
            print(f"  FAIL {rel}: {e}")
    print(f"\n{ok}/{len(copy)} copied")
    return 0 if ok == len(copy) else 1


if __name__ == "__main__":
    sys.exit(main())
