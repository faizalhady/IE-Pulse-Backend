"""
run_completion_workcell.py — re-run the completion check for ONE workcell's
forward-demand models.

    python scripts/run_completion_workcell.py --workcell LAMRESEARCH          # list only
    python scripts/run_completion_workcell.py --workcell LAMRESEARCH --go     # make MES calls

WHY, vs the two scripts that already exist
──────────────────────────────────────────
  run_lamres_missing.py   only models with NO row in the mart. Cannot re-grade a
                          model that was already checked — which is exactly what
                          a code fix needs.
  run_lamres_all.py       the full union (IEDB + 24mo history + eDash + planner).
                          6,726 models for LAM RES; killed after 40 min on 14 Aug.

This one is the middle: the SAME scope the report uses — planner (13wk) UNION
eDash (~4wk) — and it re-grades every one of them whether or not it has a row.

SAFETY
  * `completion_v2.run()` UPSERTS: _load_existing -> merge -> flush. Only the
    listed models change; every other workcell in the mart is untouched.
  * Both marts are copied to a dated .bak before the first write. Rollback is a
    file copy.
  * resume=False on purpose — the checkpoint records whole CUSTOMERS as done, so
    with resume on this would skip the very workcell it is meant to fix.
  * No mart is written until the run finishes (_flush fires at the end), so a
    crash mid-run loses the run, not the data.
  * Dry by default. Nothing reaches MES without --go.

ponytail: one runner, two existing scripts it replaces. Kept the backup and the
verification tail from run_lamres_missing.py rather than reinventing them.
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from modules.cycle_time import completion_v2 as v2
from modules.cycle_time.config import CT_MART

norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workcell", required=True, help="any spelling; matched normalised")
    ap.add_argument("--go", action="store_true", help="make MES calls. Without it: list only")
    a = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass                                  # a logging failure must not kill the run
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    wc = norm(a.workcell)
    eb = CT_MART["completion_status_v2"].parent.parent / "ebuild"

    # ── scope: the SAME union the report uses ────────────────────────────────
    frames = []
    for name in ("planner_runners.parquet", "projection_runners.parquet"):
        p = eb / name
        if p.exists():
            d = pd.read_parquet(p)
            d = d[d["customer"].map(norm) == wc]
            frames.append(d.groupby(["customer", "assembly"], as_index=False)["units"].sum())
            print(f"{name:<28}{d['assembly'].nunique():>6} models")
    if not frames or not len(pd.concat(frames)):
        print(f"no forward demand for {a.workcell}")
        return 1
    tgt = (pd.concat(frames, ignore_index=True)
             .groupby(["customer", "assembly"], as_index=False)["units"].sum())

    # Write the CONFIGURED spelling, never the demand file's.
    #
    # Demand uses MES's spelling ('MOTOROLA'), cycle-time data uses the config's
    # ('Motorola'), and this script used to pass whichever it read. So the mart
    # ended up holding the same workcell twice — 'MOTOROLA' 72 models freshly
    # graded, 'Motorola' 135 models five weeks stale — and a report showed
    # whichever spelling it happened to match. Collapsing the mart does not fix
    # it: the very next run re-splits it. It has to be fixed HERE, at the write.
    from modules.cycle_time.config import CT_CUSTOMERS
    canon = {norm(c["customer"]): c["customer"] for c in CT_CUSTOMERS}
    written = canon.get(wc)
    if written and len(tgt):
        from_demand = tgt["customer"].iloc[0]
        if from_demand != written:
            print(f"workcell name   : demand says {from_demand!r}, "
                  f"writing {written!r} (the configured spelling)")
        tgt["customer"] = written
    tgt["k"] = [norm(c) + "|" + norm(x) for c, x in zip(tgt["customer"], tgt["assembly"])]

    # ── MES customer_id from the assembly map, NOT the IEDB ids in config ────
    amap = pd.read_parquet(CT_MART["mes_assembly_map"])
    cid: dict = {}
    for c, i in zip(amap["customer"], amap["customer_id"]):
        cid.setdefault(v2._cnorm(c), i)
    tgt["customer_id"] = tgt["customer"].map(lambda c: cid.get(v2._cnorm(c)))
    drop = tgt["customer_id"].isna()
    if drop.any():
        print(f"!! {int(drop.sum())} model(s) have no MES customer_id - cannot be checked")
        tgt = tgt[~drop].copy()
    if tgt.empty:
        # NOT an error. The whole workcell is absent from the MES assembly map,
        # so no scan will ever arrive for it - Cohu (MES calls it LTX), SHINKAWA
        # and TED are all this. Crashing on `.iloc[0]` here made a known,
        # answerable state look like a bug and killed 4 of 38 overnight runs.
        print(f"\n{a.workcell}: not on MES at all - nothing to check.\n"
              f"Either the workcell genuinely does not run on MES, or it is "
              f"there under another name (Cohu -> LTX is the known case).\n"
              f"Add the alias to workcell_alias.csv if it is a naming problem.")
        return 0
    tgt["customer_id"] = tgt["customer_id"].astype(int)

    st = pd.read_parquet(CT_MART["completion_status_v2"])
    have = {norm(c) + "|" + norm(x) for c, x in zip(st["customer"], st["assembly"])}
    mine = st[st["customer"].map(norm) == wc]

    sidx = (pd.read_parquet(CT_MART["mes_serial_index"])
            if CT_MART["mes_serial_index"].exists() else pd.DataFrame())
    serials = {norm(x) for x in sidx["assembly"].dropna()} if len(sidx) else set()
    tgt["serial"] = tgt["assembly"].map(lambda x: norm(x) in serials)

    print(f"\nworkcell        : {a.workcell}  (customer_id {tgt['customer_id'].iloc[0]})")
    print(f"scope           : {len(tgt)} models, {tgt['units'].sum():,.0f} units")
    print(f"  already graded: {int(tgt['k'].isin(have).sum())}   <- these get RE-graded")
    print(f"  never checked : {int((~tgt['k'].isin(have)).sum())}")
    print(f"mart            : {len(st):,} rows total, {len(mine):,} for this workcell")
    print(f"with a serial   : {int(tgt['serial'].sum())}/{len(tgt)}  "
          f"(~{int(tgt['serial'].sum()) * 5:,} #132 calls; the rest fall back to #21)")
    if len(mine):
        print("\nstatus BEFORE:")
        print(mine["status"].value_counts().to_string())

    if not a.go:
        print("\nTop 15 by units:")
        print(tgt.nlargest(15, "units")[["assembly", "units", "serial"]].to_string(index=False))
        print("\nLIST ONLY — nothing sent to MES. Re-run with --go.")
        return 0

    # ── backups, then the run ────────────────────────────────────────────────
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for key in ("completion_status_v2", "completion_steps_v2"):
        src = CT_MART[key]
        if src.exists():
            dst = src.with_suffix(f".{stamp}.bak.parquet")
            shutil.copy2(src, dst)
            print(f"backup: {dst.name}")
    sys.stdout.flush()

    v2.run(tgt[["customer", "assembly", "customer_id"]], use_serial=True, resume=False)

    after = pd.read_parquet(CT_MART["completion_status_v2"])
    now = after[after["customer"].map(norm) == wc]
    print(f"\nmart: {len(st):,} -> {len(after):,} rows   |   {a.workcell}: {len(mine):,} -> {len(now):,}")
    print("\nstatus AFTER:")
    print(now["status"].value_counts().to_string())
    print("\nreason:")
    print(now["reason"].value_counts(dropna=False).to_string())
    print("\nsource:")
    print(now["source"].value_counts().to_string())

    # Dead statuses must not survive a re-grade of a model we actually touched.
    dead = now[now["status"].isin(["route_gap", "unverified", "unavailable", "no_data"])]
    if len(dead):
        print(f"\n!! {len(dead)} row(s) still carry a legacy status - not re-graded by this run:")
        print(dead["status"].value_counts().to_string())
    print("\nDONE", datetime.now().strftime("%H:%M:%S"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
