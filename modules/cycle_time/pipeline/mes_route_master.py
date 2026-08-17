"""
mes_route_master.py  (cycle_time pipeline)
──────────────────────────────────────────
RAW ENTITY LAYER — which ROUTE each model is configured to run.

This is the last missing edge in the domain. We already hold every model
(44,099 from IEDB, 23,424 built in MES) and every route definition
(`mes_process_master`: 1,139 routes, 91,010 ordered steps). What we did not hold
is the link between them, so "what steps does this model go through?" could only
be answered for the 3,465 models that had actually been built and scanned.

    model  ──(this file)──>  route  ──(mes_process_master)──>  ordered steps

WHY IT IS ONE CALL PER ASSEMBLY, WHICH IS SLOW AND UNAVOIDABLE
  `Assembly/ListAssemblyRouteByAssembly` takes ONE assembly. Probed and rejected:
  a wildcard `assemId` (500), an empty one (500), and
  `Route/ListRouteAssemblyByCustomer` — whose param our Postman collection calls
  `custId` while the API wants something else again, and no spelling worked.

  The working body needs all three fields; two of them are undocumented:

      {"assemId": "<id>", "fmaRoute": "%", "langId": "0"}

  `fmaRoute` is REQUIRED and rejects '0', '-1' and 'null'. Only '%' means "any
  route", and without it every call returns "Object reference not set" — which
  reads like a broken endpoint rather than a missing parameter. That cost an hour.

WHY RESUMABLE IS NOT OPTIONAL
  44,099 assemblies at ~2.6 s is ~3.2 h even at 10 threads; every revision is
  ~14.6 h. Something WILL interrupt that — a token expiry, a VPN blip, a laptop
  asleep. A pull that cannot resume is a pull that never finishes, so progress is
  flushed continuously and a re-run only fetches what is missing.

  Re-running is therefore always safe and always cheap. That is the contract for
  every domain pull: interruptible, resumable, additive.

Run:
    python -m modules.cycle_time.pipeline.mes_route_master              # latest rev/model
    python -m modules.cycle_time.pipeline.mes_route_master --all-revisions
    python -m modules.cycle_time.pipeline.mes_route_master --workers 10
"""

from __future__ import annotations

import argparse
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from modules.cycle_time.config import CT_MART
from modules.cycle_time.mes_webapi import post

log = logging.getLogger(__name__)

OUT = CT_MART["raw"].parent / "mes_route_master.parquet"
#: Assemblies already attempted, including the ones MES has no route for. Kept
#: apart from OUT so a model with genuinely no route is not retried on every run
#: — otherwise the "resume" would re-ask the same dead ids for hours.
SEEN = CT_MART["raw"].parent / "mes_route_master_seen.parquet"

#: `fmaRoute` is required and only '%' means "any". See the docstring.
_PARAMS = {"fmaRoute": "%", "langId": "0"}

KEEP = ["Assembly_ID", "AssemblyName", "Number", "Revision", "Version", "Customer_ID",
        "FactoryMARoute_ID", "FactoryName", "ManufacturingAreaName", "RouteName",
        "StartDate", "FinishDate", "EstimatedUnits", "EstimatedTime", "FactoryMA_ID",
        "LastUpdated"]

#: The generic camel->snake rule mangles trailing acronyms: `Assembly_ID` becomes
#: `assembly_i_d`, which then silently fails every later lookup. It already broke
#: `mes_process_master`'s own shrink guard once. Name the ID columns explicitly.
_RENAME = {
    "Assembly_ID": "assembly_id", "Customer_ID": "customer_id",
    "FactoryMARoute_ID": "fma_route_id", "FactoryMA_ID": "fma_id",
}
_snake = lambda c: _RENAME.get(c) or re.sub(r"(?<!^)(?=[A-Z])", "_", c).lower().replace("__", "_")


def _targets(all_revisions: bool, everything: bool = False) -> pd.DataFrame:
    """The assembly_ids to ask about.

    SCOPED TO OUR DOMAIN, which is the whole point and was nearly missed.
    `mes_assembly_map` is MES's ENTIRE assembly list — 202,186 ids, 164,482
    distinct (customer, number). Our domain is 56,882 models. Pulling the other
    ~108,000 is 14 hours spent fetching routes for models we do not have and
    cannot join to anything.

    `--everything` lifts the scope for the day someone genuinely wants MES's
    whole route book; it is not the default because that day is not today.

    Default is also the LATEST revision per (customer, number): a route can
    differ between revisions, but the newest is the one being built. Filling in
    older revisions later with `--all-revisions` adds to the same file and
    redoes nothing.
    """
    am = pd.read_parquet(CT_MART["mes_assembly_map"])
    am = am.dropna(subset=["assembly_id"]).copy()
    am["assembly_id"] = am["assembly_id"].astype(int)

    if not everything:
        from modules.cycle_time.model_universe import build, canon, norm
        u = build(CT_MART["raw"].parent.parent)
        ours = set(zip(u["wc"], u["a"]))
        keep = [ (canon(c), norm(x)) in ours for c, x in zip(am["customer"], am["number"]) ]
        am = am[keep]
        log.info("scoped to the domain: %d of MES's assemblies match a model we hold", len(am))

    if all_revisions:
        return am.drop_duplicates("assembly_id")
    # Numeric-aware: revisions are '001'/'126'/'A0' depending on the customer.
    am["_r"] = am["revision"].astype(str)
    am = am.sort_values("_r", key=lambda s: s.map(lambda x: (len(x), x)), ascending=False)
    return am.drop_duplicates(["customer", "number"]).drop(columns=["_r"])


def _load(path):
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def _fetch(aid: int) -> list[dict]:
    """Rows for one assembly, or [] when MES simply does not have it.

    A 404 here is an ANSWER, not a failure: `mes_assembly_map` carries ids MES no
    longer knows (43% of a 40-assembly probe), and the shared client retries any
    error three times with backoff. Retrying a definitive "does not exist" turns
    a 0.5 s answer into ~7 s of waiting, which on 44,000 assemblies is hours
    spent re-asking questions already answered. Swallow it and record the
    assembly as seen so it is never asked again.
    """
    try:
        return post("Assembly", "ListAssemblyRouteByAssembly",
                    {"assemId": str(aid), **_PARAMS}) or []
    except Exception as e:
        if "404" in str(e):
            return []
        raise


def run(all_revisions: bool = False, workers: int = 10, flush_every: int = 500,
        limit: int | None = None, everything: bool = False) -> int:
    have, seen = _load(OUT), _load(SEEN)
    done = set(seen["assembly_id"].astype(int)) if len(seen) else set()

    tgt = _targets(all_revisions, everything)
    todo = [int(a) for a in tgt["assembly_id"] if int(a) not in done]
    if limit:
        todo = todo[:limit]

    log.info("route master: %d target assemblies, %d already done, %d to fetch "
             "(%d workers, ~%.1f h at 2.6 s/call)",
             len(tgt), len(tgt) - len(todo), len(todo), workers,
             len(todo) * 2.6 / 3600 / max(workers, 1))
    if not todo:
        log.info("nothing to do - already complete for this scope")
        return len(have)

    got, attempted, fails, t0 = [], [], 0, time.time()

    def flush():
        """Write BOTH files together. Writing `seen` without `got` would mark an
        assembly done whose rows never landed, and it would never be retried."""
        nonlocal got, attempted
        if attempted:
            s = pd.concat([seen, pd.DataFrame({"assembly_id": attempted})], ignore_index=True)
            s.drop_duplicates("assembly_id").to_parquet(SEEN, index=False)
        if got:
            df = pd.DataFrame(got)
            df = df[[c for c in KEEP if c in df.columns]]
            df.columns = [_snake(c) for c in df.columns]
            out = pd.concat([have, df], ignore_index=True) if len(have) else df
            out.drop_duplicates(["assembly_id", "fma_route_id"]).to_parquet(OUT, index=False)
        got, attempted = [], []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch, a): a for a in todo}
        for i, f in enumerate(as_completed(futs), 1):
            aid = futs[f]
            try:
                got.extend(f.result())
            except Exception as e:
                fails += 1
                if fails <= 5:
                    log.warning("assembly %s failed: %s", aid, str(e)[:90])
            attempted.append(aid)          # attempted, not succeeded — see flush()
            if i % flush_every == 0:
                have = _load(OUT)
                seen = _load(SEEN)
                flush()
                have, seen = _load(OUT), _load(SEEN)
                rate = i / max(time.time() - t0, 1e-9)
                log.info("  %d/%d  (%.1f/s, %d failed, ~%.0f min left)",
                         i, len(todo), rate, fails, (len(todo) - i) / max(rate, 1e-9) / 60)
    flush()

    final = _load(OUT)
    nun = lambda c: int(final[c].nunique()) if c in final else 0
    log.info("route master: %d rows | %d assemblies, %d routes, %d factories | "
             "%d attempted, %d failed", len(final), nun("assembly_id"),
             nun("route_name"), nun("factory_name"), len(_load(SEEN)), fails)
    if not len(final):
        log.error("NO ROUTE ROWS. Every call either failed or returned nothing - "
                  "check the MES token and that fmaRoute='%%' is still accepted.")
    return len(final)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-revisions", action="store_true",
                    help="every revision (202k ids) instead of the latest per model")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--limit", type=int, help="stop after N assemblies (for a probe)")
    ap.add_argument("--everything", action="store_true",
                    help="all of MES, not just assemblies matching a model we hold")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    run(all_revisions=a.all_revisions, workers=a.workers, limit=a.limit,
        everything=a.everything)
