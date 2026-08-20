"""
bom_material.py  (cycle_time)
─────────────────────────────
Builds bom_material.parquet — the materials behind every BOM we care about.

WHY IT IS KEYED ON BOM_ID AND NOT ON THE MODEL
  MES shares one BOM across an assembly's revisions: E5052-66516 revs 003, 004
  and 106 are all BOM 7433. Keying the mart on (customer, assembly) would store
  those 194 materials three times and make "how many materials does this BOM
  have" a question about how many revisions happen to exist. The bridge from a
  model to its BOM already exists — `mes_assembly_map.bom_id` — so this mart is
  the child table and nothing else.

  model_universe stays one-row-per-model. It gets a `has_bom` flag, never the
  materials; 57k models x 63 materials is 3.6M rows and would break every
  consumer that assumes one row per model.

SCOPE — "demand" MEANS THE SAME THING HERE AS ON THE SCREEN
  Default is `demand`: every model in config.DEMAND_MARTS, which is the planner
  sheet UNION the MES projection — the exact set the report's "Planned" chip
  counts. 4,401 models.

  It used to be a scope called "planner" that read planner_runners alone, 2,454
  models, while the UI chip said "Planned 4,401". Coverage was then reported as
  75.5% "of planned" when it was 42.1% of what the screen means by Planned, and
  1,945 models had never been fetched at all. The two sets overlap by only 472,
  so the narrow scope was missing roughly half the answer. The scope is named
  after the thing it selects, and both sides read one tuple in config.

  `--scope all` is every BOM in mes_assembly_map (118,973) — hours, not minutes,
  and nothing needs it yet.

  A bom_id of 0/NULL is a real answer, not a gap: MES has the assembly and no BOM
  was ever loaded. All of LAMGB is like this, and 26% of MES overall.

Run:  python -m modules.cycle_time.pipeline.bom_material            # demand
      python -m modules.cycle_time.pipeline.bom_material --scope all
      python -m modules.cycle_time.pipeline.bom_material --selftest # offline

Requires MES_WEBAPI_KEY in .env for the live build.
"""

import logging
import re
import sys
import time

import pandas as pd

from modules.cycle_time.config import CT_MART, DEMAND_MARTS, EB_MART_DIR
from modules.cycle_time.mes_webapi import post, MESWebApiError

log = logging.getLogger(__name__)

#: MES column -> mart column. Anything MES returns that is not here is dropped —
#: an unmapped column silently changing name is how a mart grows a duplicate.
COLS = {
    "BOM_ID":          "bom_id",
    "BOMMaterial_ID":  "bom_material_id",
    "BOMMaterial":     "bom_material",
    "Material_ID":     "material_id",
    "Material":        "material",
    "Description":     "description",
    "Qty":             "qty",
    "BOMLevel":        "bom_level",
    "BOMSortOrder":    "bom_sort_order",
    "EffectiveFrom":   "effective_from",
    "EffectiveTo":     "effective_to",
}


def _norm(s):
    """Join key: uppercase, alphanumeric only. Same rule the universe uses, so a
    model matches here exactly when it matches there."""
    return s.astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)


def bom_ids(scope: str = "demand") -> pd.Series:
    """The distinct BOM_IDs to fetch, as ints. Raises when the bridge has no
    bom_id column — that means mes_assembly_map predates this feature and has to
    be rebuilt first, which is a clearer failure than fetching nothing."""
    am = pd.read_parquet(CT_MART["mes_assembly_map"])
    if "bom_id" not in am.columns:
        raise RuntimeError(
            "mes_assembly_map.parquet has no bom_id column - rebuild it first: "
            "python -m modules.cycle_time.pipeline.mes_assembly_map")
    am["_b"] = pd.to_numeric(am["bom_id"], errors="coerce").fillna(0).astype("int64")
    am = am[am["_b"] > 0]

    if scope == "all":
        return am["_b"].drop_duplicates()

    # Both demand marts, never one: planner-only is 1,982 models and
    # projection-only is 1,945, overlapping by 472. Reading either alone silently
    # halves the scope.
    frames = []
    for name in DEMAND_MARTS:
        f = EB_MART_DIR / name
        if f.exists():
            frames.append(pd.read_parquet(f, columns=["customer", "assembly"]))
        else:
            log.warning("demand mart missing, scope will be narrower: %s", f)
    if not frames:
        raise RuntimeError(f"no demand marts found in {EB_MART_DIR}: {DEMAND_MARTS}")
    pl = pd.concat(frames, ignore_index=True)
    keys = set(_norm(pl["customer"]) + "|" + _norm(pl["assembly"]))
    am["_k"] = _norm(am["customer"]) + "|" + _norm(am["number"])
    return am[am["_k"].isin(keys)]["_b"].drop_duplicates()


def fetch(bid) -> list[dict]:
    """Materials for one BOM. `BOM_ID` is the only accepted param name — bomId /
    bomID / bom all return 404, which is why the PDF's blank sample body cost an
    afternoon. Verified 2026-08-19."""
    return post("Bom", "GetBOMMaterialsByBOM", {"BOM_ID": str(int(bid))})


#: Write the mart every N BOMs instead of only at the end. The first run of this
#: pipeline reached 1,750/1,861 BOMs and 127,321 rows over 2.6h, hit a VPN drop,
#: and lost ALL of it because nothing was on disk yet. Checkpointing also makes
#: the run resumable: BOMs already in the mart are skipped on restart.
_CHECKPOINT = 250


def _write(rows: list[dict], prior: pd.DataFrame | None) -> pd.DataFrame:
    """Normalise the fetched rows, merge with what is already on disk, write."""
    df = pd.DataFrame(rows)
    df = df[[c for c in COLS if c in df.columns]].rename(columns=COLS)
    for c in ("bom_id", "bom_material_id", "material_id", "bom_sort_order"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    if "qty" in df:
        df["qty"] = pd.to_numeric(df["qty"], errors="coerce")
    if prior is not None and len(prior):
        df = pd.concat([prior, df], ignore_index=True)
    # One row per (bom_id, material). The key is material_id, NOT bom_material_id:
    # BOMMaterial_ID identifies the BOM's own PARENT material and is constant
    # across every line of a BOM (all 194 rows of BOM 7433 carry 281045). Keying
    # on it collapsed each BOM to a single row — 250 BOMs wrote 249 rows before
    # this was caught. Material_ID is the per-line key (194 distinct for BOM 7433).
    df = df.drop_duplicates(["bom_id", "material_id"])
    CT_MART["bom_material"].parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(CT_MART["bom_material"], index=False)
    return df


def run(scope: str = "demand", resume: bool = True) -> bool:
    ids = bom_ids(scope)
    log.info("BOM material build starting - scope=%s, %d distinct BOMs", scope, len(ids))
    if ids.empty:
        log.error("No BOM ids in scope - mart not written.")
        return False

    prior = None
    if resume and CT_MART["bom_material"].exists():
        prior = pd.read_parquet(CT_MART["bom_material"])
        done = set(pd.to_numeric(prior["bom_id"], errors="coerce").dropna().astype("int64"))
        before = len(ids)
        ids = ids[~ids.isin(done)]
        log.info("  resuming: %d of %d BOMs already in the mart, %d to fetch",
                 before - len(ids), before, len(ids))
        if ids.empty:
            log.info("  nothing left to fetch - mart already complete.")
            return True

    rows, failed, t0 = [], [], time.time()
    for i, bid in enumerate(ids, 1):
        try:
            rows += fetch(bid)
        except MESWebApiError as e:
            failed.append(int(bid))
            log.warning("  bom %s failed: %s", bid, str(e)[:120])
        if i % _CHECKPOINT == 0 or i == len(ids):
            if rows:
                prior = _write(rows, prior)
                rows = []
            log.info("  %d/%d BOMs, %d rows on disk, %.0fs elapsed",
                     i, len(ids), len(prior) if prior is not None else 0, time.time() - t0)

    if prior is None or not len(prior):
        log.error("No material rows fetched - mart not written.")
        return False

    df = prior
    log.info("bom_material.parquet written (%d rows, %d BOMs, %d failed) -> %s",
             len(df), df["bom_id"].nunique(), len(failed), CT_MART["bom_material"])
    if failed:
        log.warning("%d BOMs failed and are absent from the mart: %s%s",
                    len(failed), failed[:10], " ..." if len(failed) > 10 else "")
    return True


def _selftest():
    """Offline: the column mapping and the dedupe, which are the only logic here."""
    # MES's REAL shape: BOMMaterial_ID is the parent and repeats on every line;
    # Material_ID is what differs. A fixture with a varying BOMMaterial_ID is what
    # let the original wrong key pass this check.
    raw = [
        {"BOM_ID": 7433, "BOMMaterial_ID": 281045, "Material_ID": 163219, "Material": "A0160-7511",
         "Qty": "6.0", "BOMLevel": "1", "Description": "CAP", "Junk": "dropped"},
        {"BOM_ID": 7433, "BOMMaterial_ID": 281045, "Material_ID": 163219, "Material": "A0160-7511",
         "Qty": "6.0", "BOMLevel": "1", "Description": "CAP", "Junk": "dropped"},   # dupe line
        {"BOM_ID": 7433, "BOMMaterial_ID": 281045, "Material_ID": 163227, "Material": "A0160-7749",
         "Qty": "3.0", "BOMLevel": "1", "Description": "CAP", "Junk": "dropped"},
    ]
    df = pd.DataFrame(raw)
    df = df[[c for c in COLS if c in df.columns]].rename(columns=COLS)
    df["bom_id"] = pd.to_numeric(df["bom_id"], errors="coerce").astype("Int64")
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce")
    df = df.drop_duplicates(["bom_id", "material_id"])
    # 2, not 1: two distinct materials survive, the repeated line does not.
    assert len(df) == 2, f"dedupe collapsed the BOM: {len(df)}"
    assert "Junk" not in df.columns, "unmapped MES column leaked into the mart"
    assert df["qty"].tolist() == [6.0, 3.0], df["qty"].tolist()
    assert df["bom_id"].tolist() == [7433, 7433]

    s = pd.Series(["W1312-63079", "w1312 63079"])
    assert _norm(s).tolist() == ["W131263079", "W131263079"]
    print("bom_material self-check OK")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
    if "--selftest" in sys.argv:
        _selftest()
    else:
        scope = "all" if "all" in sys.argv else "demand"
        sys.exit(0 if run(scope) else 1)
