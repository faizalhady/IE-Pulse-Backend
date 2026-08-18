"""
run_completion_scope.py — run the completion check over a SCOPE of the model
universe, not just the forward-demand slice.

    python scripts/run_completion_scope.py                        # list only, default scope
    python scripts/run_completion_scope.py --scope has_ct --go    # the real run
    python scripts/run_completion_scope.py --scope has_ct --workcell KEYSIGHT --limit 200 --go

WHY THIS EXISTS
───────────────
`run_completion_workcell.py` grades one workcell's forward demand — the working
list, ~4.4k models, already done. `run_completion_v2.py` grades everything,
including 29k models nobody has built in two years and 12.7k IEDB has never
heard of. Neither answers the question actually being asked:

    "have we checked every model IEDB has a cycle time for?"

That is this script's default scope. After it finishes, the remaining models are
not a CHECKING gap, they are a DATA gap — nobody timed them, or they are not in
IEDB — and they belong on a list for IEDB, not in another run.

SCOPES
  has_ct   (default)  IEDB carries a cycle time for it. The comparison can
                      actually decide something.        ~36.6k to run
  demand              planner 13wk + eDash ~4wk — the working list.
  all                 the whole universe. Includes models with nothing to
                      compare against; the verdict for those is already derived
                      by `model_universe` without any MES call, so this scope
                      costs hours and changes no Status. Here for completeness.

ORDER
  Smallest workcell first — `completion_v2.run()` already sorts that way. Quick
  wins bank early and the giants (KEYSIGHT 21k, LAMRESEARCH 5.8k) run last, so
  an interruption never costs the small ones.

SAFETY
  * `completion_v2.run()` UPSERTS. Only the listed models change.
  * Both marts are snapshotted before the first write. Rollback is
    `python scripts/snapshot_marts.py restore <label> --go`.
  * Resumable, and now resumable INSIDE a workcell — see `_CKPT_EVERY` in
    completion_v2. A crash costs at most 500 models, not the workcell.
  * Dry by default. Nothing reaches MES without --go.

ponytail: no chunking loop here on purpose. Chunking at THIS level would re-run
`batch_steps()` — one slow pull per customer — once per block. The checkpoint
belongs inside run(), where the pulls are already hoisted out of the loop.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.cycle_time import completion_v2 as v2          # noqa: E402
from modules.cycle_time.config import CT_CUSTOMERS, CT_MART  # noqa: E402
from modules.cycle_time.model_universe import build, norm    # noqa: E402

log = logging.getLogger("scope")


def _targets(scope: str, workcell: str | None, include_graded: bool) -> pd.DataFrame:
    """[customer, assembly] for the chosen scope, minus what is already graded."""
    u = build(_use_mart=False)
    if scope == "has_ct":
        u = u[u["in_iedb_ct"].fillna(False)]
    elif scope == "demand":
        u = u[u["in_demand"].fillna(False)]
    elif scope != "all":
        raise SystemExit(f"unknown scope {scope!r}")
    if workcell:
        u = u[u["wc"] == norm(workcell)]
    if not include_graded:
        u = u[~u["graded"].fillna(False)]
    return u[["workcell", "assembly", "wc"]].rename(columns={"workcell": "customer"})


def _with_customer_id(tgt: pd.DataFrame) -> pd.DataFrame:
    """MES customer_id from the assembly map, and the CONFIGURED workcell spelling.

    The spelling matters: demand files say 'MOTOROLA', the cycle-time config says
    'Motorola', and writing whichever one was read is how the mart came to hold
    the same workcell twice with different verdicts.
    """
    canon = {norm(c["customer"]): c["customer"] for c in CT_CUSTOMERS}
    tgt = tgt.copy()
    tgt["customer"] = [canon.get(k) or c for k, c in zip(tgt["wc"], tgt["customer"])]

    amap = pd.read_parquet(CT_MART["mes_assembly_map"])
    cid: dict = {}
    for c, i in zip(amap["customer"], amap["customer_id"]):
        cid.setdefault(v2._cnorm(c), i)
    tgt["customer_id"] = tgt["customer"].map(lambda c: cid.get(v2._cnorm(c)))

    drop = tgt["customer_id"].isna()
    if drop.any():
        # Not an error. A workcell absent from the MES assembly map cannot be
        # checked at all — Cohu (MES calls it LTX), SHINKAWA, TED. Listing them
        # is the answer; crashing on them killed 4 of 38 overnight runs once.
        lost = sorted(tgt.loc[drop, "customer"].unique())
        log.warning("%d model(s) across %d workcell(s) have no MES customer_id "
                    "- cannot be checked: %s", int(drop.sum()), len(lost), ", ".join(lost))
        tgt = tgt[~drop].copy()
    tgt["customer_id"] = tgt["customer_id"].astype(int)
    return tgt[["customer", "assembly", "customer_id"]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default="has_ct", choices=["has_ct", "demand", "all"])
    ap.add_argument("--workcell", help="restrict to one workcell; any spelling")
    ap.add_argument("--limit", type=int, help="check only the first N models — for a timed sample")
    ap.add_argument("--smallest", type=int, metavar="N",
                    help="only the N SMALLEST workcells. The smoke-test switch: real "
                         "workcells, start to finish, for a few minutes rather than hours.")
    ap.add_argument("--include-graded", action="store_true",
                    help="re-check models that already have a verdict (a code fix needs this)")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--no-serial", action="store_true", help="skip the #132 path")
    ap.add_argument("--window", type=int, default=v2._WINDOW_DAYS)
    ap.add_argument("--go", action="store_true", help="make MES calls. Without it: list only")
    a = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    # Log to a FILE as well as stdout. Launched as a scheduled task on 02 there
    # is no console to watch, and the whole point of running it there is that
    # nobody is sitting in front of it. The file is what gets tailed over the
    # share to answer "how far along is it".
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout); sh.setFormatter(fmt); root.addHandler(sh)
    logdir = ROOT / "logs"; logdir.mkdir(exist_ok=True)
    logfile = logdir / f"completion_scope_{a.scope}_{datetime.now():%Y%m%d_%H%M}.log"
    fh = logging.FileHandler(logfile, encoding="utf-8"); fh.setFormatter(fmt); root.addHandler(fh)
    log.info("log file: %s", logfile)

    tgt = _targets(a.scope, a.workcell, a.include_graded)
    if tgt.empty:
        print(f"scope {a.scope!r}: nothing to check")
        return 0
    tgt = _with_customer_id(tgt)
    if a.smallest:
        keep = tgt.groupby("customer").size().sort_values().head(a.smallest).index
        tgt = tgt[tgt["customer"].isin(keep)].copy()
        log.info("SMOKE: the %d smallest workcells, %d models", a.smallest, len(tgt))
    if a.limit:
        tgt = tgt.head(a.limit).copy()
        log.info("SAMPLE: first %d models", len(tgt))

    by = tgt.groupby("customer").size().sort_values()
    print(f"\nscope        : {a.scope}"
          f"{'  workcell=' + a.workcell if a.workcell else ''}")
    print(f"models       : {len(tgt):,}")
    print(f"workcells    : {len(by)}   (smallest first)")
    print(f"checkpoint   : every {v2._CKPT_EVERY} models, inside the workcell\n")
    for c, n in by.items():
        print(f"  {c:<28}{n:>7,}")

    state = CT_MART["completion_status_v2"].parent / ".completion_v2_state.json"
    if state.exists() and not a.no_resume:
        try:
            st = json.loads(state.read_text())
            print(f"\nRESUME       : {len(st.get('done', []))} workcells done, "
                  f"{len(st.get('partial') or {})} part-done")
        except Exception:
            print("\nRESUME       : state file unreadable - the run will start over")

    if not a.go:
        print("\nDRY RUN - nothing sent to MES. Add --go to run it.")
        return 0

    # Snapshot before the first write, so a bad run is one command to undo.
    try:
        from scripts.snapshot_marts import take           # type: ignore
        label = take(f"pre_scope_{a.scope}_{datetime.now():%Y%m%d_%H%M}")
        print(f"\nsnapshot     : {label}")
    except Exception as ex:                                # noqa: BLE001
        log.warning("snapshot failed (%s) - continuing; .bak files are still written", ex)

    # Hold off idle-sleep for the whole run. A suspended network stack kills the
    # in-flight MES socket with ConnectionResetError(10054), which the runner
    # then records as a SKIPPED workcell. Closing the LID is a separate hardware
    # action this cannot override — keep it open, or run it on 02.
    from modules.cycle_time.keep_awake import keep_system_awake

    t0 = datetime.now()
    with keep_system_awake():
        df = v2.run(tgt, window=a.window, use_serial=not a.no_serial, resume=not a.no_resume)
    mins = (datetime.now() - t0).total_seconds() / 60
    print(f"\ndone in {mins:.0f} min | {len(df):,} models in the mart "
          f"| {len(tgt) / max(mins, 1e-9):.0f} models/min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
