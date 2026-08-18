"""
model_universe.py  (cycle_time)
───────────────────────────────
Every model we know of, from every source, deduplicated once — and the
per-workcell summary built on top of it.

WHY THIS EXISTS
  Until now "how many models are there?" had a different answer depending on
  which mart you asked and how you joined it. The completion check only ever ran
  on forward demand (4,398 models), so 99.5% of demand was graded and almost
  nothing else was — not by decision, just because nothing else was ever asked
  for. Widening that needs one agreed model list first, or a 12-hour grading run
  spends its time on duplicates.

THE IDENTITY RULE — a model is (canonical workcell, normalised assembly)
  WORKCELL = CUSTOMER. It is half the key, never a label hanging off the model.

  It is tempting to key on the assembly alone, and CLAUDE.md even says to join
  that way — but that rule is for summing UNITS, not for counting MODELS. These
  are separate workcells in CT_CUSTOMERS and each builds its own route:

      LAMGB · LAMMEC · LAMRESEARCH
      ARISTANETWORKS · ARISTA_NETWORKS_GLACIER
      KEYSIGHT · K_CTEC

  LAMMEC building 620-12345 and LAM RESEARCH building 620-12345 are two models
  with two routes and two sets of cycle times. Collapsing them on the assembly
  would have merged 57 models into false duplicates. Normalising case and
  punctuation is safe; merging different NAMES is not, and only the alias table
  may do it.

THE UNIVERSE IS NOT THE CATALOGUE
  `assembly_catalog` is IEDB's model list and the obvious place to start, and it
  is incomplete: 189 models carry cycle times in `raw.parquet` while being absent
  from the catalogue that is supposed to list them. Anchoring on the catalogue
  alone made those invisible in every group at once. The universe is the UNION of
  all five sources, so a model cannot fall between two of them.

WHAT IS EXCLUDED, AND WHY IT IS COUNTED RATHER THAN DROPPED
  MES `runners.parquet` carries 1,813 "models" with no workcell at all. They are
  not models — they are job-record noise: `Job Recovery`, `No schedule - No CTB`,
  `MES maintenance`, `RECOVER JOB C984 - ENDURANCE`. 31,277 job records holding
  7,562 units between them, 0.03% of MES history, and 1,796 of them are not in
  IEDB under any name.

  17 ARE real and resolvable — they carry 89% of those units and appear in the
  catalogue under exactly one workcell, so they are recovered rather than lost.
  Everything else is excluded EXPLICITLY and returned by `excluded()`, because a
  silent filter is how 1,813 rows disappear and nobody can say where they went.
"""

from __future__ import annotations

import csv
import logging
import re
from functools import lru_cache
from pathlib import Path

import pandas as pd

from modules.cycle_time.config import BASE_DIR, CT_CUSTOMERS, CT_MART

log = logging.getLogger(__name__)

ALIAS_CSV = BASE_DIR / "data" / "reference" / "workcell_alias.csv"

#: Assembly strings that are MES job bookkeeping, not products. Matched on the
#: raw string before normalisation strips the control characters that give them
#: away.
_JUNK = re.compile(r"[\t\n]|job\s|schedule|maintenance|recover|^0THERS", re.I)

norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


@lru_cache(maxsize=1)
def aliases() -> dict:
    """{normalised alias: normalised canonical}. Empty file = no aliases, which
    is a valid state and must not raise — the table only ever holds the handful
    of workcells that are genuinely one workcell under two names."""
    out: dict[str, str] = {}
    if not ALIAS_CSV.exists():
        log.warning("no workcell_alias.csv at %s - no aliases applied", ALIAS_CSV)
        return out
    with ALIAS_CSV.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            a, c = norm(row.get("alias")), norm(row.get("canonical"))
            if a and c and a != c:
                out[a] = c
    return out


def canon(workcell) -> str:
    """Workcell -> the one key it is counted under.

    Case and punctuation always (RESMED / ResMed), a different NAME only when the
    alias table says so (Cohu -> LTX). Anything unknown keeps its own key: an
    unrecognised workcell must show up as itself, never silently merge into a
    neighbour.
    """
    k = norm(workcell)
    return aliases().get(k, k)


@lru_cache(maxsize=1)
def _canonical_spelling() -> dict:
    """normalised key -> the display spelling CT_CUSTOMERS uses, so the summary
    reads 'LAM RESEARCH' rather than whichever mart was loaded first."""
    return {canon(c["customer"]): c["customer"] for c in CT_CUSTOMERS}


def _pairs(path: Path, cols: list[str] | None = None) -> pd.DataFrame:
    """(workcell, assembly) from one mart, canonicalised and deduped.

    DEDUPE FIRST, THEN NORMALISE. It used to run `canon` and `norm` — Python
    functions, one call per row — across all 4.8M rows of `raw.parquet` and then
    throw ~99% of the result away in drop_duplicates. Normalising is
    order-preserving on duplicates, so doing it after the dedupe gives the same
    answer for ~40k calls instead of 4.8M. That alone was most of build()'s 12s.

    The workcell map is tiny (50 entries), so `canon` is applied to the distinct
    customer strings and joined back rather than run per row.
    """
    if not path.exists():
        log.warning("missing source: %s", path)
        return pd.DataFrame(columns=["wc", "a", "assembly", "customer"])
    d = pd.read_parquet(path, columns=["customer", "assembly"] + (cols or []))
    # Raw dedupe first — cheap, vectorised, and it is what shrinks the frame.
    d = d.drop_duplicates(["customer", "assembly"])
    # `canon` per DISTINCT customer, not per row. There are 50 of them.
    cmap = {c: canon(c) for c in d["customer"].dropna().unique()}
    d["wc"] = d["customer"].map(cmap)
    # Vectorised: same regex, run in pandas' C path instead of once per row.
    d["a"] = d["assembly"].astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
    # A second dedupe: two spellings can collapse onto one canonical pair.
    return d.drop_duplicates(["wc", "a"])


#: The six answers, worst first. Same vocabulary the report and the FE use.
STATUSES = ["incomplete", "no_cycle_time", "not_in_iedb", "not_built",
            "cannot_check", "complete"]


def _read_status(path: Path) -> pd.DataFrame:
    """The graded verdicts, corrected on READ.

    Two corrections, both because a stored verdict can be older than the rule
    that produced it and no workcell is re-graded the moment a rule changes:

      * COMPLETE has to mean every step was named AND timed. That rule landed on
        16 Aug; rows graded before it can say `complete` while carrying a gap.
        `completion_v2._verdict` is the rule itself, so it is called rather than
        restated — a fourth copy of "what counts as complete" is exactly how the
        report and the website came to disagree in the first place.
      * `not_in_mes` is two answers in one word: "not built yet" means WAIT,
        `workcell_not_on_mes` means no scan will EVER arrive.
    """
    from modules.cycle_time.completion_v2 import _verdict

    cnt = ["no_ct", "not_in_iedb", "unmapped", "present"]
    d = _pairs(path, cols=["status", "reason"] + cnt)
    if d.empty:
        return d.assign(verdict=None)
    for c in cnt:
        d[c] = pd.to_numeric(d.get(c), errors="coerce").fillna(0).astype(int)

    def one(r):
        if r["status"] == "not_in_mes":
            return "cannot_check" if r["reason"] == "workcell_not_on_mes" else "not_built"
        if r["status"] in ("complete", "incomplete"):
            return _verdict({k: int(r[k]) for k in cnt})[0]
        # Legacy statuses (route_gap / unverified / unavailable) predate the
        # current vocabulary entirely. They are not translated into one of the
        # six — they are marked ungraded, because that is the truth: whatever
        # they say was decided by code that no longer exists.
        return r["status"] if r["status"] in STATUSES else None

    d["verdict"] = d.apply(one, axis=1)
    return d


def _rescue_blank(blank: pd.DataFrame, cat: pd.DataFrame) -> pd.DataFrame:
    """Give a workcell back to the blank-workcell rows IEDB can identify.

    Only an UNAMBIGUOUS hit counts. If two workcells both build the assembly
    there is no way to know which one ran the job, and guessing would attribute
    another workcell's build to a workcell that never made it.
    """
    owners = cat.groupby("a")["wc"].nunique()
    single = set(owners[owners == 1].index)
    one = cat[cat["a"].isin(single)].drop_duplicates("a").set_index("a")["wc"]
    out = blank[blank["a"].isin(single)].copy()
    out["wc"] = out["a"].map(one)
    return out


def build(mart: Path | None = None, _use_mart: bool = True) -> pd.DataFrame:
    """One row per model, with a flag per source. Nothing is aggregated yet.

    Reads the stored frame when it is newer than every input, which is the
    normal case between nightly runs. `_use_mart=False` forces a recompute and
    is what `write()` uses.
    """
    root = mart or CT_MART["raw"].parent.parent
    if _use_mart and _mart_is_fresh(root):
        return pd.read_parquet(root / MART)
    ct = (mart / "cycle_time") if mart else CT_MART["raw"].parent
    eb = (mart / "ebuild") if mart else CT_MART["raw"].parent.parent / "ebuild"

    src = {
        "in_iedb_catalog": _pairs(ct / "assembly_catalog.parquet", cols=["has_data"]),
        "in_iedb_ct":      _pairs(ct / "raw.parquet"),
        "in_mes_history":  _pairs(eb / "runners.parquet"),
        "in_planner":      _pairs(eb / "planner_runners.parquet"),
        "in_edash":        _pairs(eb / "projection_runners.parquet"),
        "graded":          _read_status(ct / "completion_status_v2.parquet"),
    }

    cat = src["in_iedb_catalog"]
    for k, d in src.items():
        blank = d[d["wc"].isin(("", "-"))]
        if not len(blank):
            continue
        rescued = _rescue_blank(blank, cat)
        kept = d[~d["wc"].isin(("", "-"))]
        src[k] = pd.concat([kept, rescued], ignore_index=True)
        log.info("%s: %d blank-workcell rows, %d rescued from the catalogue, %d dropped",
                 k, len(blank), len(rescued), len(blank) - len(rescued))

    # Union first, flags second — a model missing from four sources still has to
    # appear once, which is the whole point of doing this as a union.
    u = (pd.concat([d[["wc", "a"]] for d in src.values()], ignore_index=True)
           .drop_duplicates(["wc", "a"]))
    for k, d in src.items():
        u[k] = u.set_index(["wc", "a"]).index.isin(d.set_index(["wc", "a"]).index)

    u["in_demand"] = u["in_planner"] | u["in_edash"]
    u["in_iedb"] = u["in_iedb_catalog"] | u["in_iedb_ct"]

    # Keep a readable name. Prefer the configured spelling; fall back to whatever
    # a source called it, so an unconfigured workcell is still legible.
    seen = (pd.concat([d[["wc", "customer"]] for d in src.values() if "customer" in d],
                      ignore_index=True).drop_duplicates("wc").set_index("wc")["customer"])
    u["workcell"] = u["wc"].map(lambda k: _canonical_spelling().get(k) or seen.get(k, k))
    name = (pd.concat([d[["wc", "a", "assembly"]] for d in src.values() if "assembly" in d],
                      ignore_index=True).drop_duplicates(["wc", "a"])
              .set_index(["wc", "a"])["assembly"])
    u["assembly"] = u.set_index(["wc", "a"]).index.map(name)

    # The verdict rides along on the model, so "how many models" and "how many
    # complete" are answered off ONE row and cannot drift apart.
    g = src["graded"]
    if "verdict" in g:
        v = g.dropna(subset=["verdict"]).set_index(["wc", "a"])["verdict"]
        u["verdict"] = u.set_index(["wc", "a"]).index.map(v)
        # A legacy row is graded in the mart but says nothing we can still read.
        u["graded"] = u["verdict"].notna()

        # ── the catalogue correction, applied ONCE, here ────────────────────
        # `raw.parquet` is the CYCLE-TIME table, so the grader can only ever ask
        # it "does this have a time?" — never "is this in IEDB?". That mislabels
        # models, and until now each renderer patched it separately: the report
        # applied this check, Coverage applied only the gap rule, and
        # /completion/demand applied neither. Same workcell, three answers
        # (208 / 236 / 279). The correction belongs to the verdict, not to
        # whoever happens to be drawing it.
        #
        # The catalogue is re-pulled nightly, so applying it on READ also picks
        # up models created since the last grading run.
        cat = src["in_iedb_catalog"]
        hd = (cat.assign(h=cat["has_data"].fillna(False)).set_index(["wc", "a"])["h"]
              if "has_data" in cat else pd.Series(dtype=bool))
        idx = u.set_index(["wc", "a"]).index
        in_cat = idx.isin(hd.index)
        timed = idx.map(hd).fillna(False) if len(hd) else pd.Series(False, index=u.index)
        judged = u["verdict"].notna()
        # Order matters: "does not exist" outranks "exists but untimed".
        u.loc[judged & ~in_cat, "verdict"] = "not_in_iedb"
        u.loc[judged & in_cat & ~pd.Series(timed, index=u.index), "verdict"] = "no_cycle_time"
    return u.reset_index(drop=True)


def verdicts(mart: Path | None = None) -> pd.DataFrame:
    """[wc, a, verdict] — THE answer for every model we have judged.

    Everything that shows a status joins this instead of re-reading the mart and
    re-deriving its own. Three renderers each deriving it privately is precisely
    how one workcell came to report 208, 236 and 279 complete models at once.
    """
    u = build(mart)
    if "verdict" not in u:
        return pd.DataFrame(columns=["wc", "a", "verdict"])
    return u.loc[u["verdict"].notna(), ["wc", "a", "verdict"]].reset_index(drop=True)


#: Where build()'s answer is cached between runs. Every user gets the same
#: frame until the marts change at 02:00, so computing it per request is work we
#: already know the answer to — the same rule the rest of this codebase follows
#: ("computed data -> parquet mart"). Written by the nightly refresh.
MART = "cycle_time/model_universe.parquet"


def write(mart: Path | None = None) -> int:
    """Compute the universe once and store it. Called by the pipeline."""
    root = mart or CT_MART["raw"].parent.parent
    u = build(root, _use_mart=False)
    if u.empty:
        log.error("model_universe: build produced nothing - keeping the previous file")
        return 0
    out = root / MART
    before = 0
    if out.exists():
        import pyarrow.parquet as pq
        before = pq.ParquetFile(out).metadata.num_rows
    # Same shrink guard as every other pull: a collapsed rebuild would quietly
    # drop models out of every denominator on the site.
    if before and len(u) < before * 0.9:
        log.error("model_universe SHRANK %d -> %d - keeping the previous file", before, len(u))
        return before
    out.parent.mkdir(parents=True, exist_ok=True)
    u.to_parquet(out, index=False)
    log.info("model_universe: %d models -> %s", len(u), out.name)
    return len(u)


#: The pipeline calls every step as `.run()`. Same function, expected name.
run = write


def _mart_is_fresh(root: Path) -> bool:
    """The stored frame is usable only if it is NEWER than every input. Serving a
    stale universe is worse than recomputing: it looks identical on screen."""
    out = root / MART
    if not out.exists():
        return False
    ct, eb = root / "cycle_time", root / "ebuild"
    srcs = [ct / "assembly_catalog.parquet", ct / "raw.parquet",
            ct / "completion_status_v2.parquet", eb / "runners.parquet",
            eb / "planner_runners.parquet", eb / "projection_runners.parquet"]
    newest = max((p.stat().st_mtime for p in srcs if p.exists()), default=0)
    return out.stat().st_mtime >= newest


def excluded(mart: Path | None = None) -> pd.DataFrame:
    """What `build()` refused to call a model, and why. Never let this be silent —
    1,813 rows vanishing with no record is how a number becomes unexplainable."""
    eb = (mart / "ebuild") if mart else CT_MART["raw"].parent.parent / "ebuild"
    ct = (mart / "cycle_time") if mart else CT_MART["raw"].parent
    rows = []
    for f in ("runners.parquet", "projection_runners.parquet"):
        p = eb / f
        if not p.exists():
            continue
        d = pd.read_parquet(p, columns=["customer", "assembly"])
        d = d[d["customer"].map(canon).isin(("", "-"))].copy()
        d["a"] = d["assembly"].map(norm)
        rows.append(d.assign(source=f))
    if not rows:
        return pd.DataFrame(columns=["assembly", "source", "why"])
    b = pd.concat(rows, ignore_index=True).drop_duplicates(["a", "source"])
    cat = _pairs(ct / "assembly_catalog.parquet")
    rescued = set(_rescue_blank(b, cat)["a"])
    b["why"] = [
        "rescued - IEDB names exactly one workcell for it" if a in rescued
        else "MES job record, not a product" if _JUNK.search(str(x))
        else "no workcell, and not in IEDB under any name"
        for a, x in zip(b["a"], b["assembly"])]
    return b[["assembly", "source", "why"]]


def summary(mart: Path | None = None) -> pd.DataFrame:
    """The per-workcell headline: how many models, how many answered, what is left.

    `graded` is not the same question as `complete` and the two must never be
    added together — a workcell can be 100% graded and 0% complete. `ungraded` is
    the honest "we have not looked yet" column, and it is the one that was
    invisible while the report scoped itself to forward demand.
    """
    u = build(mart)
    if u.empty:
        return u
    # THE THREE BUCKETS. Mutually exclusive, exhaustive, and known for 100% of
    # models because they come from IEDB alone — no MES call, no completion run.
    #
    #   has_ct    IEDB has priced it
    #   no_ct     IEDB lists it, nobody timed it   -> go time it
    #   not_iedb  IEDB has never heard of it       -> create it first
    #
    # This is the breakdown to lead with. `complete`/`incomplete` need the MES
    # comparison and only cover 10% of models, so a percentage built on them is
    # a share of what we happened to check. These cover everything.
    #
    # The last two are deliberately NOT merged into "missing cycle time": one is
    # an IE task and the other is a data-creation task, for different people.
    u = u.copy()
    u["has_ct"] = u["in_iedb_ct"]
    u["no_ct"] = u["in_iedb_catalog"] & ~u["in_iedb_ct"]
    u["not_iedb"] = ~u["in_iedb_catalog"] & ~u["in_iedb_ct"]

    g = u.groupby("workcell").agg(
        models=("a", "size"),
        in_iedb=("in_iedb", "sum"),
        has_ct=("has_ct", "sum"),
        no_ct=("no_ct", "sum"),
        not_iedb=("not_iedb", "sum"),
        built_24mo=("in_mes_history", "sum"),
        in_demand=("in_demand", "sum"),
        graded=("graded", "sum"),
    ).reset_index()
    # The partition must hold per workcell, not just plant-wide. If it ever does
    # not, one of the three is silently wrong and the page would still render.
    assert (g["has_ct"] + g["no_ct"] + g["not_iedb"] == g["models"]).all(),         "the three buckets do not sum to models - they are not a partition"
    g["pct_has_ct"] = (g["has_ct"] / g["models"] * 100).round(1)

    # One column per answer. reindex(): a workcell with nobody in a bucket must
    # print 0, not go missing from the frame.
    if "verdict" in u:
        v = (u.pivot_table(index="workcell", columns="verdict", values="a",
                           aggfunc="size", fill_value=0)
               .reindex(columns=STATUSES, fill_value=0).reset_index())
        g = g.merge(v, on="workcell", how="left")
    for c in STATUSES:
        g[c] = g.get(c, 0)
        g[c] = pd.to_numeric(g[c], errors="coerce").fillna(0).astype(int)

    # HOME PLANT — the one where most of the workcell's demand sits. A few
    # genuinely run in two (INFINERA is in JBK and Plant 1), so this is the
    # dominant one, not the only one. Same rule eBuild uses for customer_plant.
    cp = CT_MART["raw"].parent.parent / "ebuild" / "customer_plant.parquet"
    if cp.exists():
        d = pd.read_parquet(cp)
        by = {}
        for c, pl, un in zip(d["customer"], d["plant"], d.get("units", 0)):
            k = canon(c)
            if k not in by or (un or 0) > by[k][1]:
                by[k] = (pl, un or 0)
        g["plant"] = g["workcell"].map(lambda w: (by.get(canon(w)) or (None,))[0])

    g["ungraded"] = g["models"] - g["graded"]
    g["pct_graded"] = (g["graded"] / g["models"] * 100).round(1)
    # Denominator is what we have LOOKED at. Dividing by every model would read
    # as "3% complete" for a workcell that is simply 97% unchecked, which is a
    # different problem with a different owner.
    g["pct_complete_of_graded"] = (g["complete"] / g["graded"].where(g["graded"] > 0) * 100).round(1)
    return g.sort_values("models", ascending=False).reset_index(drop=True)


def _selfcheck() -> None:
    """Runnable proof of the two rules that cost the most to get wrong."""
    assert canon("Cohu") == canon("LTX") == "LTX", "the one true alias must collapse"
    # The near-miss: these normalise to different keys and MUST stay apart.
    for a, b in [("LAMMEC", "LAMRESEARCH"), ("KEYSIGHT", "K_CTEC"),
                 ("ARISTANETWORKS", "ARISTA_NETWORKS_GLACIER")]:
        assert canon(a) != canon(b), f"{a} and {b} are separate workcells"
    assert canon("ResMed") == canon("RESMED"), "case must not split a workcell"
    print("selfcheck OK - alias collapses, separate workcells stay separate")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s")
    _selfcheck()
    s = summary()
    print(f"\n{len(s)} workcells | {s['models'].sum():,} models | "
          f"{s['graded'].sum():,} graded | {s['ungraded'].sum():,} ungraded\n")
    print(s.head(25).to_string(index=False))
    x = excluded()
    print(f"\nexcluded: {len(x)} rows")
    print(x["why"].value_counts().to_string())
