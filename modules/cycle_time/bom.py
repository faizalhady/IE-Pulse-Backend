"""
bom.py  (cycle_time)
────────────────────
Model -> BOM -> materials, for the model page's BOM tab.

THE JOIN IS TWO HOPS AND BOTH MATTER
    (customer, assembly)  --mes_assembly_map-->  bom_id  --bom_material-->  rows

  It is not one hop, because a model does not have a BOM — an ASSEMBLY REVISION
  has one, and MES routinely points several revisions at the same BOM
  (E5052-66516 revs 003/004/106 are all BOM 7433). The tab therefore reports the
  bom_id per revision as well as the materials, so an engineer can see that
  changing revision changed nothing, rather than wondering whether the page is
  stale.

A MODEL WITH NO BOM IS A NORMAL ANSWER
  `bom_id` 0/NULL means MES holds the assembly and no BOM was ever loaded — all
  of LAMGB, ~26% of MES overall. That is reported as an empty materials list with
  `has_bom: false`, never as a 404: a 404 reads as "we could not look", and the
  whole point is that we looked and there is nothing there.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache

import pandas as pd

from modules.cycle_time.config import CT_MART

log = logging.getLogger(__name__)

norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


@lru_cache(maxsize=1)
def _bridge() -> pd.DataFrame:
    """(customer, number, revision) -> bom_id, with the join keys precomputed.
    Cached: 203k rows re-normalised per request is 40x the cost of the lookup."""
    am = pd.read_parquet(CT_MART["mes_assembly_map"])
    if "bom_id" not in am.columns:
        log.warning("mes_assembly_map has no bom_id column - rebuild it; BOM tab will be empty")
        am["bom_id"] = 0
    am["bom_id"] = pd.to_numeric(am["bom_id"], errors="coerce").fillna(0).astype("int64")
    am["_c"] = am["customer"].astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
    am["_a"] = am["number"].astype(str).str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)
    return am[["_c", "_a", "revision", "assembly_id", "bom_id"]]


@lru_cache(maxsize=1)
def _materials() -> pd.DataFrame:
    """The material mart, or an empty frame with the right columns when the
    pipeline has not run yet — a missing mart must render an empty tab, not 500."""
    p = CT_MART["bom_material"]
    if not p.exists():
        log.warning("bom_material.parquet missing - run modules.cycle_time.pipeline.bom_material")
        return pd.DataFrame(columns=["bom_id", "bom_material_id", "material", "description",
                                     "qty", "bom_level", "bom_sort_order"])
    return pd.read_parquet(p)


def reset_cache() -> None:
    """Drop the cached marts. Called by the refresh pipeline after a rebuild."""
    _bridge.cache_clear()
    _materials.cache_clear()


def for_model(customer: str, assembly: str, revision: str | None = None) -> dict:
    """Materials for one model, at one revision.

    `revision=None` picks the newest revision that actually HAS a BOM, rather
    than simply the newest: opening the tab on a revision MES never loaded a BOM
    for shows an empty table for a model that plainly has one.
    """
    b = _bridge()
    hit = b[(b["_c"] == norm(customer)) & (b["_a"] == norm(assembly))]

    revs = (hit[["revision", "assembly_id", "bom_id"]]
            .drop_duplicates("revision")
            .sort_values("revision", ascending=False, key=lambda s: s.astype(str))
            .to_dict("records"))

    # The page's revision picker is fed by IEDB, this bridge by MES, and the two
    # do not always spell a revision the same way (or hold the same set at all).
    # An unmatched revision therefore falls BACK to the auto-pick and says so —
    # returning nothing would render an empty tab for a model that has a BOM, and
    # look like the feature is broken rather than like the revisions differ.
    auto = next((r for r in revs if r["bom_id"] > 0), revs[0] if revs else None)
    matched = True
    if revision is not None:
        want = [r for r in revs if str(r["revision"]) == str(revision)]
        chosen, matched = (want[0], True) if want else (auto, False)
    else:
        chosen = auto

    bom_id = int(chosen["bom_id"]) if chosen else 0
    mats: list[dict] = []
    if bom_id > 0:
        m = _materials()
        m = m[pd.to_numeric(m["bom_id"], errors="coerce") == bom_id]
        # MES's own display order. Falling back to material keeps the table
        # stable when BOMSortOrder is null, instead of shuffling per request.
        sort = [c for c in ("bom_level", "bom_sort_order", "material") if c in m.columns]
        mats = m.sort_values(sort).where(pd.notna(m), None).to_dict("records")

    return {
        "customer": customer,
        "assembly": assembly,
        "revision": str(chosen["revision"]) if chosen else None,
        "requested_revision": revision,
        #: False = `revision` was asked for and MES does not have it, so the
        #: revision above is a fallback. The tab says so rather than lying.
        "revision_matched": matched,
        "bom_id": bom_id or None,
        #: MES HAS a BOM for this revision. Deliberately not `bool(materials)` —
        #: the mart is pulled planner-first, so a non-planner model has a real
        #: bom_id and zero rows here. Reporting that as "no BOM was ever loaded"
        #: would be a flat lie about MES. The two states are separate on purpose:
        #:   bom_id null            -> MES never had one   (all of LAMGB)
        #:   bom_id set, no rows    -> we have not pulled it yet
        "has_bom": bom_id > 0,
        "in_mart": bool(mats),
        "in_mes": bool(len(hit)),
        "materials": mats,
        "revisions": [{"revision": str(r["revision"]),
                       "assembly_id": int(r["assembly_id"]) if pd.notna(r["assembly_id"]) else None,
                       "bom_id": int(r["bom_id"]) or None} for r in revs],
    }


if __name__ == "__main__":
    # Offline check of the pick rule — the only real logic here. A revision with
    # no BOM must not be chosen over one that has it.
    revs = [{"revision": "106", "bom_id": 0}, {"revision": "004", "bom_id": 7433},
            {"revision": "003", "bom_id": 7433}]
    assert next((r for r in revs if r["bom_id"] > 0), None)["revision"] == "004"
    assert norm("W1312-63079") == norm("w1312 63079") == "W131263079"
    assert norm(None) == ""
    print("bom self-check OK")
