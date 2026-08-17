"""
build_lamres_report.py — the cycle-time completion report, one workcell.

    python scripts/build_lamres_report.py
    python scripts/build_lamres_report.py --mart //mypenm0iesvr02/.../data/mart --out C:/Users/.../LAM_RES_report.xlsx

WHY THIS FILE EXISTS
────────────────────
The 14 Aug report was ad-hoc and was not saved, so every rebuild re-derived the
same five decisions from scratch and got a different answer three times. This
is that logic, written down once.

THE SIX STATUSES — nothing else is ever emitted
    Not in IEDB            this EXACT model+revision is not in assembly_catalog
    No cycle time in IEDB  it IS in the catalogue, has_data = False
    Not built yet          MES has no record inside the window. WAIT.
    Cannot be checked      the workcell is not on MES. No scan will ever come,
                           so waiting is pointless - a different decision.
                           470 LAMMEC models read as "Not built yet" until this
                           status existed, which implied a wait that never ends.
    Complete               every MES step we can name has a cycle time
    Incomplete             at least one does not, OR we could not name it

TWO TRAPS THIS FILE REFUSES TO REPEAT
    1. `raw.parquet` is the CYCLE-TIME table, not the model list. It holds zero
       models with no cycle time, so "is it in IEDB?" asked of it can only ever
       answer "does it have a cycle time?". The model list is
       `assembly_catalog.parquet` and it carries has_data. (14 Aug: this one
       mislabelled 66 models.)
    2. The mart's `expected` column is NOT IEDB's route size. It counts MES
       steps that mapped to IEDB, so for a passing model it always equals
       Matched and tells you nothing. Real route size is counted from raw.

THE GAP IS SPLIT, ON PURPOSE
    Missing CT   mapped to IEDB, no time entered      -> IEDB's gap
    Not in route mapped to an alias this model lacks  -> IEDB's gap
    Unmapped     we cannot name the step at all       -> OUR gap (the bridge)

    Half of LAM RES's gap steps are `unmapped`. Folding them into one number
    blames IEDB for our own mapping holes. The report shows all three so the
    reader can see which is which.

    COMPLETE requires gap == 0 AND unmapped == 0. The backend enforces the same
    rule since 2026-08-16 (completion_v2._verdict); before that it called a model
    complete on one matched step however many were unmapped, and the website
    disagreed with this report on 41 models.

ponytail: read-only. Reads parquet, writes one xlsx. No MES calls, no writes to
any mart.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

_norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s or "").upper())

# The five statuses, in report order.
COMPLETE, INCOMPLETE, NO_CT, NOT_BUILT, NOT_IN_IEDB, CANT_CHECK = (
    "Complete", "Incomplete", "No cycle time in IEDB", "Not built yet",
    "Not in IEDB", "Cannot be checked")
ORDER = [COMPLETE, INCOMPLETE, NO_CT, NOT_BUILT, NOT_IN_IEDB, CANT_CHECK]


def load(mart: Path, workcell: str):
    """Every source, filtered to one workcell. Workcell names differ per file
    (`LAM RESEARCH` in cycle-time data, `LAMRESEARCH` in demand data), so every
    match is on the normalised name — never the raw string."""
    wc = _norm(workcell)
    ct, eb = mart / "cycle_time", mart / "ebuild"

    def only(df, col="customer"):
        return df[df[col].map(_norm) == wc].copy()

    cat = only(pd.read_parquet(ct / "assembly_catalog.parquet",
                               columns=["customer", "assembly", "revision", "has_data"]))
    status = only(pd.read_parquet(ct / "completion_status_v2.parquet"))
    planner = only(pd.read_parquet(eb / "planner_runners.parquet"))
    edash = only(pd.read_parquet(eb / "projection_runners.parquet"))
    history = only(pd.read_parquet(eb / "runners.parquet"))

    # Real IEDB route size: distinct aliases this model lists. NOT the mart's
    # `expected`. Read late and narrow — raw.parquet is 4.4M rows.
    raw = only(pd.read_parquet(ct / "raw.parquet", columns=["customer", "assembly", "alias"]))
    route = raw.groupby("assembly")["alias"].nunique().rename("iedb_route_steps")
    return cat, status, planner, edash, history, route


def build(cat, status, planner, edash, history, route):
    # ---- scope: forward demand only. planner (13wk) UNION eDash (~4wk). ------
    scope = pd.DataFrame({"assembly": sorted(
        set(planner["assembly"]) | set(edash["assembly"]))})
    scope["k"] = scope["assembly"].map(_norm)

    def pick(df, col, out):
        d = df.assign(k=df["assembly"].map(_norm)).groupby("k")[col].sum()
        return scope["k"].map(d).fillna(0).astype(int).rename(out)

    scope["Planner units"] = pick(planner, "units", "u")
    scope["eDash units"] = pick(edash, "units", "u")

    # ---- IEDB catalogue, EXACT revision. Trap 1. ----------------------------
    cat["k"] = cat["assembly"].map(_norm)
    in_cat = dict(zip(cat["k"], cat["has_data"].fillna(False)))

    # ---- completion verdict --------------------------------------------------
    status["k"] = status["assembly"].map(_norm)
    st = status.drop_duplicates("k").set_index("k")
    for c in ["present", "no_ct", "not_in_iedb", "unmapped", "non_iedb", "actual_steps"]:
        st[c] = pd.to_numeric(st.get(c), errors="coerce").fillna(0).astype(int)

    def verdict(k):
        """The five statuses, in precedence order. Catalogue first — a model
        IEDB never heard of cannot be graded on anything else."""
        if k not in in_cat:
            return NOT_IN_IEDB, ""
        if not in_cat[k]:
            return NO_CT, "in IEDB, nobody timed it"
        if k not in st.index:
            return INCOMPLETE, "IN DEMAND - never checked"      # must read 0
        r = st.loc[k]
        if r["status"] == "not_in_mes":
            # "Not built yet" implies WAIT. For a workcell that is not on MES at
            # all that is a lie - waiting will never produce a scan. 470 LAMMEC
            # models read as pending that way. They are not pending; they are
            # unverifiable by this method, and that is a different decision for
            # whoever picks the list up.
            if str(r.get("reason")) == "workcell_not_on_mes":
                return CANT_CHECK, "this workcell is not on MES - no scan will ever come"
            return NOT_BUILT, str(r.get("reason") or "")
        gap, unm = int(r["no_ct"] + r["not_in_iedb"]), int(r["unmapped"])
        if gap == 0 and unm == 0:
            return COMPLETE, ""
        why = []
        if r["no_ct"]:
            why.append(f"{r['no_ct']} step(s) with no cycle time")
        if r["not_in_iedb"]:
            why.append(f"{r['not_in_iedb']} step(s) not on the IEDB route")
        if unm:
            why.append(f"{unm} step(s) we could not name (our bridge)")
        return INCOMPLETE, "; ".join(why)

    v = scope["k"].map(verdict)
    scope["Status"] = [x[0] for x in v]
    scope["Why"] = [x[1] for x in v]

    # ---- step columns. Blank unless a real comparison ran. -------------------
    def col(name, src):
        s = scope["k"].map(st[src] if src in st else {})
        return s.where(scope["Status"].isin([COMPLETE, INCOMPLETE]))

    scope["MES steps scanned"] = col("mes", "actual_steps")
    scope["Matched"] = col("m", "present")
    scope["Missing CT"] = col("m", "no_ct")
    scope["Not in route"] = col("n", "not_in_iedb")
    scope["Unmapped"] = col("u", "unmapped")
    scope["Gap"] = scope["Missing CT"].fillna(0) + scope["Not in route"].fillna(0)
    scope["Gap"] = scope["Gap"].where(scope["Status"].isin([COMPLETE, INCOMPLETE]))
    # The REAL route size. Trap 2. MES scans coarser, so it is NOT comparable
    # to "MES steps scanned" — it is here to show how much IEDB describes.
    scope["IEDB route steps"] = scope["assembly"].map(route)

    # ---- dates ---------------------------------------------------------------
    ed = edash.assign(k=edash["assembly"].map(_norm)).groupby("k")["first_start"].min()
    pl = planner.assign(k=planner["assembly"].map(_norm)).groupby("k").agg(
        s=("first_start", "min"), e=("planned_finish", "max"))
    hist = history.assign(k=history["assembly"].map(_norm)).groupby("k")["last_completed"].max()

    def upcoming(k):
        """eDash carries a real scheduled date. Planner `first_start` is a MONTH
        BUCKET — 410 of 475 LAM RES rows sit in the past — so it is printed as a
        period, never as a date."""
        d = ed.get(k)
        if pd.notna(d):
            return pd.Timestamp(d).strftime("%d %b %Y")
        if k in pl.index:
            s, e = pl.loc[k, "s"], pl.loc[k, "e"]
            if pd.notna(s):
                return f"planner {pd.Timestamp(s):%b %Y} - {pd.Timestamp(e):%b %Y}" \
                    if pd.notna(e) else f"planner {pd.Timestamp(s):%b %Y}"
        return ""

    scope["Upcoming build"] = scope["k"].map(upcoming)
    scope["Last build"] = scope["k"].map(
        lambda k: pd.Timestamp(hist[k]).strftime("%d %b %Y")
        if k in hist.index and pd.notna(hist[k]) else "")

    scope["_o"] = scope["Status"].map({s: i for i, s in enumerate(ORDER)})
    scope = scope.sort_values(["_o", "Gap", "Unmapped", "Planner units"],
                              ascending=[True, False, False, False])
    return scope.drop(columns=["k", "_o"])


COLS = ["assembly", "Status", "MES steps scanned", "Matched", "Missing CT",
        "Not in route", "Unmapped", "Gap", "IEDB route steps", "Planner units",
        "eDash units", "Upcoming build", "Last build", "Why"]

HOWTO = [
    ("SCOPE", "Models with forward demand: 13-week planner UNION eDash. Not every model the workcell owns."),
    ("Complete", "Every MES step we could name has a cycle time. Gap = 0 AND Unmapped = 0."),
    ("Incomplete", "At least one named step has no cycle time, or we could not name a step at all."),
    ("No cycle time in IEDB", "The record EXISTS in IEDB's catalogue, has_data = False. Go time it - do not create it."),
    ("Not in IEDB", "This exact model+revision is not in our IEDB catalogue copy. Our copy is ~1% short, so this can be our gap, not IEDB's."),
    ("Not built yet", "MES has no production record inside the check window. Wait, or widen the window."),
    ("Cannot be checked", "The workcell is not on MES. No scan will ever arrive, so this model can never be verified by comparing to MES - however much cycle time gets entered. A different problem from 'Not built yet'."),
    ("Missing CT", "IEDB knows the step, no time entered. IEDB's gap."),
    ("Not in route", "MES ran a step this model's IEDB route does not list. IEDB's gap."),
    ("Unmapped", "We could not match the MES step name to any IEDB alias. OUR gap - the naming bridge, not a missing cycle time."),
    ("Gap", "Missing CT + Not in route. Excludes Unmapped on purpose."),
    ("MES steps scanned", "Distinct steps MES logged. NOT comparable to IEDB route steps - MES scans coarser."),
    ("IEDB route steps", "Distinct aliases IEDB lists for this model. One MES scan can cover several of these."),
    ("Upcoming build", "A real date only when the model is in eDash. Planner first_start is a MONTH BUCKET, so it prints as a period."),
    ("Last build", "From a rolling 24-month window. Blank means 'not built in 24 months', NOT 'never built'."),
    ("WEBSITE", "The site says 'Missing CT' where this says 'Incomplete'. The site may still show Complete for models with unmapped steps - completion_v2.py does not enforce Gap=0."),
]


def freshness(mart: Path) -> pd.DataFrame:
    """How old is every input this report rests on.

    Staleness is not the enemy — INVISIBLE staleness is. `assembly_catalog` was
    a 9 Jul snapshot while everything around it refreshed nightly, and nobody
    noticed for five and a half weeks; every model created in that window read
    as "Not in IEDB", including two Faiz flagged by hand and we argued about.
    `planner_runners` sat at 14 Jul the same way.

    Both were findable in one `ls`. Nobody ran it because nothing asked them to.
    So the report now carries the age of its own inputs, on its own sheet, and
    prints a warning above the numbers when one is old enough to distort them.
    """
    import datetime as _dt
    files = [
        ("assembly_catalog", "cycle_time/assembly_catalog.parquet", "is a model in IEDB, and is it timed"),
        ("raw", "cycle_time/raw.parquet", "the cycle times themselves"),
        ("completion_status_v2", "cycle_time/completion_status_v2.parquet", "the MES comparison verdicts"),
        ("planner_runners", "ebuild/planner_runners.parquet", "13-week forward demand - half the scope"),
        ("projection_runners", "ebuild/projection_runners.parquet", "eDash demand - the other half"),
        ("runners", "ebuild/runners.parquet", "24 months of MES build history"),
        ("mes_process_map", "cycle_time/mes_process_map.parquet", "the MES-to-IEDB name bridge"),
    ]
    now, rows = _dt.datetime.now(), []
    for name, rel, why in files:
        p = mart / rel
        if not p.exists():
            rows.append({"mart": name, "built": "MISSING", "days_old": None, "drives": why})
            continue
        t = _dt.datetime.fromtimestamp(p.stat().st_mtime)
        rows.append({"mart": name, "built": t.strftime("%d %b %Y %H:%M"),
                     "days_old": (now - t).days, "drives": why})
    return pd.DataFrame(rows).sort_values("days_old", ascending=False, na_position="first")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mart", default="data/mart", help="mart root (point at prod's copy to match the site)")
    ap.add_argument("--workcell", default="LAM RESEARCH")
    ap.add_argument("--out", default=str(Path.home() / "Downloads" / "LAM_RES_report.xlsx"))
    a = ap.parse_args()

    mart = Path(a.mart)
    df = build(*load(mart, a.workcell))

    summary = (df.groupby("Status")
                 .agg(Models=("assembly", "size"),
                      **{"Planner units": ("Planner units", "sum"),
                         "eDash units": ("eDash units", "sum"),
                         "Gap steps": ("Gap", "sum"),
                         "Unmapped steps": ("Unmapped", "sum")})
                 .reindex(ORDER).dropna(how="all").reset_index())
    summary["%"] = (summary["Models"] / len(df) * 100).round(1)

    fresh = freshness(mart)
    out = Path(a.out)
    with pd.ExcelWriter(out, engine="openpyxl") as x:
        pd.DataFrame([
            {"Level": "GRANDPARENT", "What": f"All {a.workcell} models in the IEDB catalogue",
             "Models": len(load(mart, a.workcell)[0])},
            {"Level": "PARENT", "What": "With forward demand - 13wk planner + eDash", "Models": len(df)},
        ]).to_excel(x, "Breakdown", index=False)
        summary.to_excel(x, "By status", index=False)
        df[COLS].rename(columns={"assembly": "Model"}).to_excel(x, "Models", index=False)
        pd.DataFrame(HOWTO, columns=["Term", "What it means"]).to_excel(x, "How to read this", index=False)
        fresh.to_excel(x, "Freshness", index=False)

    print(f"\n{a.workcell} - {len(df)} models with forward demand   ->  {out}\n")
    print(summary.to_string(index=False))
    inc = df[df["Status"] == INCOMPLETE]
    if len(inc):
        print(f"\nOf {len(inc)} Incomplete: {int(inc['Gap'].sum())} gap steps "
              f"(IEDB's) + {int(inc['Unmapped'].sum())} unmapped steps (ours).")
        print(f"  {int((inc['Gap'] == 0).sum())} are incomplete ONLY because of unmapped steps.")
    return df


def selftest(df):
    """The three models that were graded wrong on 14 Aug. If any regresses, the
    numbers in the report are not trustworthy and the run must stop."""
    g = lambda m: (df.loc[df["assembly"] == m, "Status"].iloc[0]
                   if (df["assembly"] == m).any() else "NOT IN SCOPE")
    checks = [
        # in the catalogue with has_data=False. Was reported Complete off
        # revision -106A's cycle times. Must never read Complete again.
        ("810-495659-106C", {NO_CT, "NOT IN SCOPE"}),
        # Faiz said on 14 Aug that this model IS in IEDB; we reported "Not in
        # IEDB" and blamed a short extract. He was right and we were wrong. Its
        # `updated_on` is 2026-07-18 and prod's assembly_catalog was a 9 JUL
        # snapshot — the catalogue is not in the nightly refresh, so a model
        # created after that date could never appear. Refreshing the catalogue
        # on 17 Aug moved it to `No cycle time in IEDB`, which is the truth:
        # the record exists, nobody has timed it.
        ("810-B48709-003A", {NO_CT, "NOT IN SCOPE"}),
        # 17 matched, 1 unmapped (FPROBE3(AUTO)). Complete requires unmapped=0.
        ("810-028298-005C", {INCOMPLETE, "NOT IN SCOPE"}),
    ]
    bad = [(m, g(m)) for m, ok in checks if g(m) not in ok]
    for m, ok in checks:
        print(f"  {'OK ' if g(m) in ok else 'BAD'}  {m:<20} -> {g(m)}")
    assert not bad, f"regressed: {bad}"
    print("  selftest passed")


if __name__ == "__main__":
    d = main()
    print("\nselftest - the models that were graded wrong on 14 Aug:")
    selftest(d)
    sys.exit(0)
