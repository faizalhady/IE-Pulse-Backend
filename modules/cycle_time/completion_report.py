"""
completion_report.py  (cycle_time)
──────────────────────────────────
The completion report, as a module — so the website and the Excel builder are
the same code.

WHY A MODULE AND NOT JUST THE SCRIPT
  On 14 Aug the report was ad-hoc and unsaved, and three rebuilds in one
  afternoon gave three different answers. Writing it down once fixed that. But a
  second copy of the logic behind an endpoint would recreate exactly the same
  problem — a model reading Complete on screen and Incomplete in the file. One
  implementation, two renderers.

THE SIX STATUSES — nothing else is ever emitted
    Not in IEDB            this EXACT model+revision is not in assembly_catalog
    No cycle time in IEDB  it IS in the catalogue, has_data = False
    Not built yet          MES has no record in the window. WAIT.
    Cannot be checked      the workcell is not on MES. No scan will ever come,
                           so waiting is pointless. 470 LAMMEC models read as
                           "Not built yet" until this existed.
    Complete               every MES step we can name has a cycle time
    Incomplete             at least one does not, OR we could not name it

TWO TRAPS IT REFUSES TO REPEAT
  1. `raw.parquet` is the CYCLE-TIME table, not the model list. Asking it "is
     this in IEDB?" can only answer "does it have a time?" — that mislabelled 66
     models. The model list is `assembly_catalog.parquet` + its has_data flag.
  2. The mart's `expected` column is NOT IEDB's route size. It counts MES steps
     that mapped, so for a passing model it always equals Matched.

THE GAP IS SPLIT ON PURPOSE
    Missing CT   / Not in route  -> IEDB's gap
    Unmapped                     -> OUR gap (the naming bridge)
  Folding them into one number blames IEDB for our own mapping holes.
"""

from __future__ import annotations

import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd

_norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s or "").upper())

COMPLETE, INCOMPLETE, NO_CT, NOT_BUILT, NOT_IN_IEDB, CANT_CHECK, NOT_CHECKED = (
    "Complete", "Incomplete", "No cycle time in IEDB", "Not built yet",
    "Not in IEDB", "Cannot be checked", "Not checked")
ORDER = [COMPLETE, INCOMPLETE, NO_CT, NOT_BUILT, NOT_IN_IEDB, CANT_CHECK, NOT_CHECKED]

FRESHNESS_FILES = [
    ("assembly_catalog", "cycle_time/assembly_catalog.parquet", "is a model in IEDB, and is it timed"),
    ("raw", "cycle_time/raw.parquet", "the cycle times themselves"),
    ("completion_status_v2", "cycle_time/completion_status_v2.parquet", "the MES comparison verdicts"),
    ("planner_runners", "ebuild/planner_runners.parquet", "13-week forward demand - half the scope"),
    ("projection_runners", "ebuild/projection_runners.parquet", "eDash demand - the other half"),
    ("mes_process_map", "cycle_time/mes_process_map.parquet", "the MES-to-IEDB name bridge"),
]


def freshness(mart: Path) -> list[dict]:
    """Age of every input. Staleness is not the enemy — INVISIBLE staleness is.
    `assembly_catalog` sat five weeks stale and turned real models into
    "Not in IEDB"; nobody noticed because nothing showed the date."""
    now, out = datetime.now(), []
    for name, rel, why in FRESHNESS_FILES:
        p = mart / rel
        if not p.exists():
            out.append({"mart": name, "built": None, "days_old": None, "drives": why})
            continue
        t = datetime.fromtimestamp(p.stat().st_mtime)
        out.append({"mart": name, "built": t.isoformat(timespec="minutes"),
                    "days_old": (now - t).days, "drives": why})
    return sorted(out, key=lambda r: -(r["days_old"] if r["days_old"] is not None else 999))


def _load(mart: Path, workcell: str):
    wc = _norm(workcell)
    ct, eb = mart / "cycle_time", mart / "ebuild"
    only = lambda df: df[df["customer"].map(_norm) == wc].copy()
    cat = only(pd.read_parquet(ct / "assembly_catalog.parquet",
                               columns=["customer", "assembly", "revision", "has_data"]))
    status = only(pd.read_parquet(ct / "completion_status_v2.parquet"))
    planner = only(pd.read_parquet(eb / "planner_runners.parquet"))
    edash = only(pd.read_parquet(eb / "projection_runners.parquet"))
    history = only(pd.read_parquet(eb / "runners.parquet"))
    raw = only(pd.read_parquet(ct / "raw.parquet", columns=["customer", "assembly", "alias"]))
    route = raw.groupby("assembly")["alias"].nunique().rename("iedb_route_steps")
    return cat, status, planner, edash, history, route


def build(mart: Path, workcell: str) -> pd.DataFrame:
    """One row per model with forward demand (13-week planner UNION eDash)."""
    cat, status, planner, edash, history, route = _load(mart, workcell)

    scope = pd.DataFrame({"assembly": sorted(set(planner["assembly"]) | set(edash["assembly"]))})
    if scope.empty:
        return scope
    scope["k"] = scope["assembly"].map(_norm)

    def units(df, out):
        d = df.assign(k=df["assembly"].map(_norm)).groupby("k")["units"].sum()
        return scope["k"].map(d).fillna(0).astype(int).rename(out)

    scope["planner_units"] = units(planner, "u")
    scope["edash_units"] = units(edash, "u")

    cat["k"] = cat["assembly"].map(_norm)
    in_cat = dict(zip(cat["k"], cat["has_data"].fillna(False)))

    status["k"] = status["assembly"].map(_norm)
    st = status.drop_duplicates("k").set_index("k")
    for c in ["present", "no_ct", "not_in_iedb", "unmapped", "non_iedb", "actual_steps"]:
        st[c] = pd.to_numeric(st.get(c), errors="coerce").fillna(0).astype(int)

    # The verdict comes from model_universe, which owns both corrections (the
    # gap rule and this catalogue check). This function used to derive it here,
    # Coverage derived it there, and the demand endpoint served the mart raw —
    # three answers for one workcell. What stays here is the WHY string and the
    # demand scoping, which are this report's own job.
    from modules.cycle_time.model_universe import canon, verdicts
    vk = verdicts(mart)
    wck = canon(workcell)
    ANSWER = {"complete": COMPLETE, "incomplete": INCOMPLETE, "no_cycle_time": NO_CT,
              "not_in_iedb": NOT_IN_IEDB, "not_built": NOT_BUILT, "cannot_check": CANT_CHECK}
    vmap = dict(zip(vk[vk["wc"] == wck]["a"], vk[vk["wc"] == wck]["verdict"]))

    def verdict(k):
        v = vmap.get(k)
        if v is None:
            # In demand, never judged. Previously called Incomplete, which put a
            # model nobody has looked at in the same bucket as one we checked and
            # found wanting — two different jobs, and only one of them is IEDB's.
            return NOT_CHECKED, "in demand - the completion run has not reached it"
        answer = ANSWER.get(v, INCOMPLETE)
        if answer is not INCOMPLETE or k not in st.index:
            return answer, ("in IEDB, nobody timed it" if answer is NO_CT else "")
        r = st.loc[k]
        why = []
        if r["no_ct"]:
            why.append(f"{r['no_ct']} step(s) with no cycle time")
        if r["not_in_iedb"]:
            why.append(f"{r['not_in_iedb']} step(s) not on the IEDB route")
        if r["unmapped"]:
            why.append(f"{r['unmapped']} step(s) we could not name (our bridge)")
        return INCOMPLETE, "; ".join(why)

    v = scope["k"].map(verdict)
    scope["status"] = [x[0] for x in v]
    scope["why"] = [x[1] for x in v]

    graded = scope["status"].isin([COMPLETE, INCOMPLETE])
    col = lambda src: scope["k"].map(st[src] if src in st else {}).where(graded)
    scope["mes_steps"] = col("actual_steps")
    scope["matched"] = col("present")
    scope["missing_ct"] = col("no_ct")
    scope["not_in_route"] = col("not_in_iedb")
    scope["unmapped"] = col("unmapped")
    scope["gap"] = (scope["missing_ct"].fillna(0) + scope["not_in_route"].fillna(0)).where(graded)
    scope["iedb_route_steps"] = scope["assembly"].map(route)

    ed = edash.assign(k=edash["assembly"].map(_norm)).groupby("k")["first_start"].min()
    pl = planner.assign(k=planner["assembly"].map(_norm)).groupby("k").agg(
        s=("first_start", "min"), e=("planned_finish", "max"))
    hist = history.assign(k=history["assembly"].map(_norm)).groupby("k")["last_completed"].max()

    def upcoming(k):
        """eDash carries a real date. Planner `first_start` is a MONTH BUCKET —
        410 of 475 LAM RES rows sat in the past — so it prints as a period."""
        d = ed.get(k)
        if pd.notna(d):
            return pd.Timestamp(d).strftime("%d %b %Y")
        if k in pl.index and pd.notna(pl.loc[k, "s"]):
            s, e = pl.loc[k, "s"], pl.loc[k, "e"]
            return (f"planner {pd.Timestamp(s):%b %Y} - {pd.Timestamp(e):%b %Y}"
                    if pd.notna(e) else f"planner {pd.Timestamp(s):%b %Y}")
        return ""

    scope["upcoming_build"] = scope["k"].map(upcoming)
    scope["last_build"] = scope["k"].map(
        lambda k: pd.Timestamp(hist[k]).strftime("%d %b %Y")
        if k in hist.index and pd.notna(hist[k]) else "")

    scope["_o"] = scope["status"].map({s: i for i, s in enumerate(ORDER)})
    return (scope.sort_values(["_o", "gap", "unmapped", "planner_units"],
                              ascending=[True, False, False, False])
                 .drop(columns=["k", "_o"]))


def summary(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    g = (df.groupby("status")
           .agg(models=("assembly", "size"), planner_units=("planner_units", "sum"),
                edash_units=("edash_units", "sum"), gap_steps=("gap", "sum"),
                unmapped_steps=("unmapped", "sum"))
           .reindex(ORDER).dropna(how="all").reset_index())
    g["pct"] = (g["models"] / len(df) * 100).round(1)
    return g.astype(object).where(pd.notna(g), 0).to_dict("records")


@lru_cache(maxsize=64)
def _cached(mart_str: str, workcell: str, _key: float):
    mart = Path(mart_str)
    df = build(mart, workcell)
    return {
        "workcell": workcell,
        "models": int(len(df)),
        "summary": summary(df),
        "freshness": freshness(mart),
        # astype(object) FIRST: on a float column `where` substitutes NaN back in
        # rather than None, and json.dumps refuses NaN outright. The step columns
        # are deliberately blank for ungraded models, so this path is normal, not
        # an edge case.
        "rows": df.astype(object).where(pd.notna(df), None).to_dict("records"),
    }


def report(mart: Path, workcell: str) -> dict:
    """Cached on the verdict mart's mtime — same trick the rest of the module
    uses, so a re-grade invalidates it and nothing else has to remember to."""
    p = mart / "cycle_time" / "completion_status_v2.parquet"
    return _cached(str(mart), workcell, p.stat().st_mtime if p.exists() else 0.0)
