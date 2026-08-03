"""
completion_status.py  (cycle_time)  — P3/P4
───────────────────────────────────────────
Decides a completion status per top-runner model:

  ④ unavailable   model not in IEDB at all
  ① no_data       in IEDB, zero cycle-time data
  ② incomplete    has CT, but a real production step it runs has no IEDB cycle time
  ③ complete      has CT for every mapped production step it actually runs
  · unverified     has CT but no MES production history in the window to compare

Step source = MES `Batch/ListBatchCountsByRouteStep` (#21): one call per customer
per day returns every (Assembly, RouteStep=step-instance, ActualQty) that actually
scanned — no serial, no per-model calls, historical. `RouteStep` is the step
instance, so it joins straight to the MNS mapping (mes_process_map). We loop the
last `window_days` (24h chunks — the SP caps a single window at 24h) and union.

  model's actual steps (#21) → step instance → IEDB alias (mes_process_map)
        → check raw.parquet has that alias WITH cycle time → missing = ② incomplete.

Unmapped steps don't count against the model (mapping gap or legit non-IEDB) — they
lower `coverage`, which flags low-confidence verdicts.
"""

import json
import logging
import re
from datetime import datetime, timedelta

import duckdb
import pandas as pd

from modules.cycle_time.config import CT_MART
from modules.cycle_time.mes_webapi import post, MESWebApiError

log = logging.getLogger(__name__)

_cnorm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())        # customer key
_snorm = lambda s: re.sub(r"\s+", " ", str(s).strip().upper())      # step-instance display key
_anorm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())         # alias MATCH key — spacing-agnostic ("OBA1" == "OBA 1")
_WINDOW_DAYS = 120     # how far back to gather production history per customer

# Workcells with VERIFIED-zero MES production (checked per-customer, not assumed) —
# their routes can't verify via #21. Reported as "non_mes" (N/A). KEYSIGHT was removed
# after it proved to have 6k+ MES step rows; only genuinely MES-absent workcells belong here.
_NON_MES = {"LAMMEC", "ADVANTEST"}   # _cnorm keys


class _Ctx:
    def __init__(self, window_days: int = _WINDOW_DAYS, window_start: int = 0):
        self.con = duckdb.connect()
        self.raw = CT_MART["raw"].as_posix()
        self.window_days = window_days
        self.window_start = window_start   # days-ago offset: scan [start, start+len) back from today

        # process map: pmap = (cust,inst)→IEDB alias for REAL steps; pknown = every
        # (cust,inst) the workbook has an opinion on (incl. known-non-IEDB).
        pm = pd.read_parquet(CT_MART["mes_process_map"])
        self.pmap, self.pknown = {}, set()
        for c, s, a, isie in zip(pm["customer"], pm["step_instance"], pm["iedb_alias"], pm["is_iedb"]):
            k = (_cnorm(c), s)
            self.pknown.add(k)
            if isie:
                self.pmap[k] = a

        self._steps: dict = {}   # cust_key → {assembly: set(step_instance)}  (from #21)
        self._iedb: dict = {}    # customer → {assembly: set(alias w/ CT)}
        self._models: dict = {}  # customer → set(assembly in IEDB)

    # ── #21: a customer's actual (assembly → step-instances) from scan history ──
    def batch_steps(self, customer: str, customer_id) -> dict:
        key = _cnorm(customer)
        if key in self._steps:
            return self._steps[key]
        acc: dict = {}
        ok = fail = 0
        end = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        for day in range(self.window_start, self.window_start + self.window_days):
            s, e = end - timedelta(days=day + 1), end - timedelta(days=day)
            try:
                rows = post("Batch", "ListBatchCountsByRouteStep", {
                    "CustomerID": str(customer_id),
                    "StartDate": s.strftime("%Y-%m-%d %H:%M:%S"),
                    "EndDate":   e.strftime("%Y-%m-%d %H:%M:%S"),
                })
                ok += 1
            except MESWebApiError:
                fail += 1
                continue
            for r in rows:
                asm = str(r.get("Assembly", "")).split("/")[0].strip()   # "Number / Rev / Ver"
                step = _snorm(r.get("RouteStep"))
                if not (asm and step):
                    continue
                order, qty = int(r.get("StepOrder") or 0), int(r.get("ActualQty") or 0)
                d = acc.setdefault(asm, {})
                if step in d:                                # keep min order + summed qty
                    d[step][0] = min(d[step][0], order); d[step][1] += qty
                else:
                    d[step] = [order, qty]
        # Every MES call failed → the pull is broken, not "no production". Don't cache
        # a bogus-empty result; raise so run() skips (doesn't mark this customer done)
        # and it gets retried on the next run instead of being saved as all-unverified.
        if ok == 0 and fail > 0:
            raise MESWebApiError(f"all {fail} #21 calls failed for {customer} — MES unreachable")
        self._steps[key] = acc
        log.info("  #21 %s: %d assemblies with production history", customer, len(acc))
        return acc

    def iedb_detail(self, customer: str) -> dict:
        """{assembly: [{process, alias, sub_workcenter, cycle_time}]} — the model's
        IEDB route steps that HAVE a cycle time (for the FE comparison)."""
        cachekey = "_D_" + customer
        if not hasattr(self, cachekey):
            # `order` = IEDB step sequence (a SQL keyword → must be quoted).
            df = self.con.execute(f"""
                SELECT DISTINCT assembly, process, alias, sub_workcenter,
                       "order" AS ord, cycle_time_per_process AS ct
                FROM read_parquet('{self.raw}')
                WHERE customer = ? AND cycle_time_per_process IS NOT NULL AND priority = 1
            """, [customer]).fetchdf()
            d: dict = {}
            for a, p, al, sw, od, ct in zip(df["assembly"], df["process"], df["alias"],
                                            df["sub_workcenter"], df["ord"], df["ct"]):
                d.setdefault(a, []).append({"process": p, "alias": al, "sub_workcenter": sw,
                                            "order": None if pd.isna(od) else int(od),
                                            "cycle_time": None if pd.isna(ct) else float(ct)})
            for a in d:                                        # sort each route by step sequence
                d[a].sort(key=lambda x: (x["order"] is None, x["order"] or 0))
            setattr(self, cachekey, d)
        return getattr(self, cachekey)

    def iedb_aliases(self, customer: str) -> dict:
        if customer not in self._iedb:
            df = self.con.execute(f"""
                SELECT assembly, alias FROM read_parquet('{self.raw}')
                WHERE customer = ? AND cycle_time_per_process IS NOT NULL AND alias IS NOT NULL
            """, [customer]).fetchdf()
            d: dict = {}
            for a, al in zip(df["assembly"], df["alias"]):
                d.setdefault(a, set()).add(_anorm(al))     # spacing-agnostic alias key
            self._iedb[customer] = d
        return self._iedb[customer]

    def iedb_models(self, customer: str) -> set:
        if customer not in self._models:
            self._models[customer] = set(self.con.execute(
                f"SELECT DISTINCT assembly FROM read_parquet('{self.raw}') WHERE customer = ?",
                [customer]).fetchdf()["assembly"])
        return self._models[customer]


def classify_model(ctx: _Ctx, customer: str, assembly: str, customer_id) -> dict:
    base = {"customer": customer, "assembly": assembly, "expected": 0, "present": 0,
            "missing": [], "unmapped": 0, "coverage": None, "actual_steps": 0}

    if assembly not in ctx.iedb_models(customer):
        return {**base, "status": "unavailable"}
    if _cnorm(customer) in _NON_MES:
        return {**base, "status": "non_mes"}   # different shopfloor — MES can't verify (N/A)
    iedb = ctx.iedb_aliases(customer).get(assembly, set())   # aliases WITH cycle time (may be empty)

    steps = ctx.batch_steps(customer, customer_id).get(assembly, {})   # {instance: [order, qty]}
    insts = set(steps.keys())
    if not insts:
        # no MES history to compare: some CT → can't verify (unverified); zero CT →
        # definitely incomplete (nothing entered; no MES needed to know it's not done).
        return {**base, "status": "unverified" if iedb else "incomplete"}

    cn = _cnorm(customer)
    known = {i for i in insts if (cn, i) in ctx.pknown}
    mapped = {i: ctx.pmap[(cn, i)] for i in insts if (cn, i) in ctx.pmap}
    alias_by_key = {}                                    # match-key → original alias (for display)
    for a in mapped.values():
        alias_by_key.setdefault(_anorm(a), a)
    expected = set(alias_by_key)
    coverage = round(len(known) / len(insts), 2)
    unmapped = len(insts) - len(known)

    # MES-route detail (ordered) for the FE side-by-side: each step tagged
    # present / missing / non_iedb / unmapped.
    mes_detail = []
    for inst, (order, qty) in sorted(steps.items(), key=lambda x: (x[1][0], x[0])):
        alias = ctx.pmap.get((cn, inst))
        if alias:
            st = "present" if _anorm(alias) in iedb else "missing"
        elif (cn, inst) in ctx.pknown:
            st = "non_iedb"                       # workbook says not an IEDB step
        else:
            st = "unmapped"                       # mapping gap
        mes_detail.append({"step": inst, "order": order, "qty": qty, "alias": alias or "", "status": st})
    iedb_detail = ctx.iedb_detail(customer).get(assembly, [])

    if not expected:
        # MES steps run, but none map to an IEDB step: some CT → unverified; zero CT → incomplete.
        return {**base, "status": "unverified" if iedb else "incomplete", "unmapped": unmapped,
                "coverage": coverage, "actual_steps": len(insts),
                "_mes_detail": mes_detail, "_iedb_detail": iedb_detail}

    missing = sorted(alias_by_key[k] for k in (expected - iedb))   # original aliases, readable
    return {**base,
            "status": "incomplete" if missing else "complete",
            "expected": len(expected),
            "present": len(expected) - len(missing),
            "missing": missing,
            "unmapped": unmapped,
            "coverage": coverage,
            "actual_steps": len(insts),
            "_mes_detail": mes_detail,
            "_iedb_detail": iedb_detail}


def top_models(top_n: int = 100) -> pd.DataFrame:
    """Union of the top-`top_n` (customer, assembly) runners across every layer
    (overall + each plant + each workcell) × every runner mart. Returns
    [customer(config name), assembly, customer_id] — #21 is keyed by customer_id,
    no per-model assembly_id needed."""
    eb = CT_MART["completion_status"].parent.parent / "ebuild"
    amap = pd.read_parquet(CT_MART["mes_assembly_map"])
    amap["ck"] = amap["customer"].map(_cnorm)
    cust = amap.drop_duplicates("ck")[["ck", "customer", "customer_id"]]   # ck → config name + id

    picks: set = set()
    for fname in ("plant_runners.parquet", "projection_runners.parquet", "planner_runners.parquet"):
        p = eb / fname
        if not p.exists():
            continue
        df = pd.read_parquet(p)
        secs = [df]
        if "plant" in df.columns:
            secs += [sub for _, sub in df.groupby("plant")]
        secs += [sub for _, sub in df.groupby("customer")]
        for sub in secs:
            top = (sub.groupby(["customer", "assembly"])["units"].sum()
                      .reset_index().nlargest(top_n, "units"))
            picks.update((_cnorm(c), a) for c, a in zip(top["customer"], top["assembly"]))

    pk = pd.DataFrame(sorted(picks), columns=["ck", "assembly"])
    models = (pk.merge(cust, on="ck")[["customer", "assembly", "customer_id"]]
                .drop_duplicates(["customer", "assembly"]).reset_index(drop=True))
    log.info("top_models: %d displayed picks → %d in mapped customers", len(picks), len(models))
    return models


_STEP_COLS = ["customer", "assembly", "side", "name", "alias", "sub_workcenter", "order", "value", "status"]


def _step_rows(r: dict, mes_d: list, iedb_d: list) -> list:
    rows = []
    for s in mes_d:
        rows.append({"customer": r["customer"], "assembly": r["assembly"], "side": "MES",
                     "name": s["step"], "alias": s["alias"], "sub_workcenter": None,
                     "order": s["order"], "value": s["qty"], "status": s["status"]})
    for s in iedb_d:
        rows.append({"customer": r["customer"], "assembly": r["assembly"], "side": "IEDB",
                     "name": s["process"], "alias": s["alias"], "sub_workcenter": s["sub_workcenter"],
                     "order": s.get("order"), "value": s["cycle_time"], "status": "has_ct"})
    return rows


def _flush(summary: dict, steps: dict):
    df = pd.DataFrame(list(summary.values()))
    if "missing" in df.columns:
        df["missing"] = df["missing"].map(lambda x: "; ".join(x) if isinstance(x, list) else (x or ""))
    CT_MART["completion_status"].parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(CT_MART["completion_status"], index=False)
    rows = [r for group in steps.values() for r in group]
    pd.DataFrame(rows, columns=_STEP_COLS).to_parquet(CT_MART["completion_steps"], index=False)


def _load_existing() -> tuple[dict, dict]:
    """Seed upsert bases from the CURRENT mart so a run MERGES instead of rebuilding:
    summary keyed by (customer, assembly), steps grouped by the same key. Any model a
    run doesn't classify is carried through untouched — a partial/subset run can never
    nuke the mart (the old list-rebuild-from-scratch behaviour)."""
    summary, steps = {}, {}
    if CT_MART["completion_status"].exists():
        for r in pd.read_parquet(CT_MART["completion_status"]).to_dict("records"):
            summary[(r["customer"], r["assembly"])] = r
    if CT_MART["completion_steps"].exists():
        for r in pd.read_parquet(CT_MART["completion_steps"]).to_dict("records"):
            steps.setdefault((r["customer"], r["assembly"]), []).append(r)
    return summary, steps


def run(models: pd.DataFrame, resume: bool = True, window: int = _WINDOW_DAYS,
        planner_only: bool = False) -> pd.DataFrame:
    """Classify models → completion_status + completion_steps marts.

    STOP-PROOF: checkpoints after every customer (flushes both marts + a
    .completion_state.json of done customers). A killed/VPN-dropped run RESUMES
    from the last completed customer — only the interrupted customer is redone.
    On full completion the state file is cleared, so the next invocation starts
    fresh (with new data). Pass resume=False to force a clean rebuild.
    """
    state_path = CT_MART["completion_status"].parent / ".completion_state.json"
    ctx = _Ctx(window)
    # ALWAYS seed from the existing mart → upsert by (customer, assembly). A run updates
    # only the models it classifies and preserves every model it doesn't touch, so a
    # partial/subset run never wipes the mart. `resume` now only controls whether we
    # SKIP already-done customers this run (checkpoint), not whether the mart is seeded.
    summary, steps = _load_existing()
    done = set()

    if resume and state_path.exists():
        try:
            done = set(json.loads(state_path.read_text()).get("done_customers", []))
            log.info("RESUME — %d customers done, %d models already in mart", len(done), len(summary))
        except Exception:
            log.warning("resume state unreadable — reprocessing all customers")
            done = set()

    by_cust: dict = {}
    for m in models.itertuples(index=False):
        by_cust.setdefault(m.customer, []).append(m)

    if planner_only:                       # focus mode — only workcells with planner demand
        eb = CT_MART["completion_status"].parent.parent / "ebuild" / "planner_runners.parquet"
        planner = {_cnorm(c) for c in pd.read_parquet(eb)["customer"].unique()}
        by_cust = {c: v for c, v in by_cust.items() if _cnorm(c) in planner}

    # Process order = workcell SIZE ascending (assembly count ≈ #21 pull time). Smallest
    # first banks quick wins; the giants (KEYSIGHT, ARISTA) run last so an interruption
    # never costs the whole run.
    amap = pd.read_parquet(CT_MART["mes_assembly_map"], columns=["customer"])
    size = amap.groupby(amap["customer"].map(_cnorm)).size().to_dict()
    order = sorted(by_cust, key=lambda c: (size.get(_cnorm(c), 0), str(c)))

    log.info("completion run%s: %d customers, %d remaining (window=%dd) — smallest first",
             " [PLANNER-ONLY]" if planner_only else "",
             len(by_cust), len([c for c in by_cust if c not in done]), window)

    skipped = []
    for cust in order:
        if cust in done:
            continue
        # Prime the customer's MES pull first. If it totally fails, SKIP the customer
        # (don't mark done) so it's retried on the next run instead of being saved as
        # all-unverified garbage. (This is what silently corrupted the 120-day run.)
        try:
            ctx.batch_steps(cust, by_cust[cust][0].customer_id)
        except MESWebApiError as ex:
            log.warning("  ⚠ SKIP %s — MES pull failed (%s); will retry on re-run", cust, ex)
            skipped.append(cust)
            continue
        for m in by_cust[cust]:
            r = classify_model(ctx, m.customer, m.assembly, m.customer_id)
            mes_d = r.pop("_mes_detail", []); iedb_d = r.pop("_iedb_detail", [])
            key = (m.customer, m.assembly)
            summary[key] = r                                 # upsert — replaces any prior row
            steps[key] = _step_rows(r, mes_d, iedb_d)
        done.add(cust)
        _flush(summary, steps)                                   # ← checkpoint
        state_path.write_text(json.dumps({"done_customers": sorted(done)}))
        ctx._steps.pop(_cnorm(cust), None)                       # free this customer's #21 cache
        log.info("  ✓ %s (%d/%d customers · %d models)", cust, len(done), len(by_cust), len(summary))

    if skipped:
        # Keep the state file so a re-run resumes and retries ONLY the skipped customers.
        log.warning("run finished with %d customer(s) SKIPPED (MES failed) — re-run to retry: %s",
                    len(skipped), ", ".join(skipped))
    elif planner_only:
        # Partial run BY DESIGN (non-planner workcells untouched) — keep the state so a
        # later run resumes instead of starting fresh and overwriting the whole mart.
        log.info("planner-only run done — state kept (non-planner workcells not processed).")
    else:
        try:
            state_path.unlink()      # fully done → next run starts fresh
        except OSError:
            pass
    log.info("completion run COMPLETE: %d models in mart, %d step rows (%d skipped)",
             len(summary), sum(len(v) for v in steps.values()), len(skipped))
    return pd.DataFrame(list(summary.values()))


def reclassify_unverified(customers: list[str], window_start: int = 120,
                          window_days: int = 60) -> int:
    """Targeted cleanup — re-pull ONLY `customers` over a SHIFTED window
    [window_start, window_start+window_days) days ago, and re-classify their
    currently-UNVERIFIED models. A model found in the older window flips to
    complete/incomplete; everything else is left untouched. Cheap: scans only the
    unseen slice, never re-scans the recent window the main run already covered.
    Returns the number of models flipped out of 'unverified'."""
    cs = pd.read_parquet(CT_MART["completion_status"])
    stp = pd.read_parquet(CT_MART["completion_steps"])
    amap = pd.read_parquet(CT_MART["mes_assembly_map"])
    cidmap: dict = {}
    for c, i in zip(amap["customer"], amap["customer_id"]):
        cidmap.setdefault(_cnorm(c), i)

    ctx = _Ctx(window_days=window_days, window_start=window_start)
    rows = cs.to_dict("records")
    by_key = {(r["customer"], r["assembly"]): r for r in rows}
    new_step_rows, touched = [], set()
    flipped = 0

    for cust in customers:
        cn = _cnorm(cust)
        cid = cidmap.get(cn)
        if cid is None:
            log.warning("  reclassify: no customer_id for %s — skip", cust)
            continue
        targets = [r for r in rows if _cnorm(r["customer"]) == cn and r["status"] == "unverified"]
        if not targets:
            continue
        disp = targets[0]["customer"]                     # stored config name (raw/IEDB match)
        try:
            ctx.batch_steps(disp, cid)                    # one shifted-window pull for the customer
        except MESWebApiError as ex:
            log.warning("  reclassify: SKIP %s — MES failed (%s)", cust, ex)
            continue
        for r in targets:
            res = classify_model(ctx, r["customer"], r["assembly"], cid)
            mes_d = res.pop("_mes_detail", []); iedb_d = res.pop("_iedb_detail", [])
            if res["status"] != "unverified":             # found in older window → flip
                by_key[(r["customer"], r["assembly"])].update(res)
                touched.add((r["customer"], r["assembly"]))
                new_step_rows += _step_rows(res, mes_d, iedb_d)
                flipped += 1
        log.info("  reclassify %s: %d/%d unverified flipped", disp,
                 sum(1 for r in targets if (r["customer"], r["assembly"]) in touched), len(targets))

    if flipped:
        cs_new = pd.DataFrame(rows)
        if "missing" in cs_new.columns:
            cs_new["missing"] = cs_new["missing"].map(lambda x: "; ".join(x) if isinstance(x, list) else (x or ""))
        cs_new.to_parquet(CT_MART["completion_status"], index=False)
        keep = stp[~stp.apply(lambda x: (x["customer"], x["assembly"]) in touched, axis=1)]
        pd.concat([keep, pd.DataFrame(new_step_rows, columns=_STEP_COLS)], ignore_index=True) \
          .to_parquet(CT_MART["completion_steps"], index=False)
    log.info("reclassify_unverified COMPLETE: %d models flipped out of unverified", flipped)
    return flipped


def classify_missing_planner(only=None, window_days: int = 180, window_start: int = 0) -> int:
    """Classify EVERY planner-demand model missing from completion_status, so the
    planner dashboard has no '-' rows. No-IEDB models resolve to 'unavailable' (no
    MES call); IEDB models are classified via one #21 pull per customer. APPENDS to
    both marts (never clobbers existing rows). `only` limits to given customer names."""
    cs = pd.read_parquet(CT_MART["completion_status"])
    stp = pd.read_parquet(CT_MART["completion_steps"])
    eb = CT_MART["completion_status"].parent.parent / "ebuild" / "planner_runners.parquet"
    pr = pd.read_parquet(eb).drop_duplicates(["customer", "assembly"])
    amap = pd.read_parquet(CT_MART["mes_assembly_map"])
    cidmap, dispmap = {}, {}
    for c, i in zip(amap["customer"], amap["customer_id"]):
        cidmap.setdefault(_cnorm(c), i)
    for c in cs["customer"].unique():                 # name that matches raw + the FE runner rows
        dispmap.setdefault(_cnorm(c), c)

    have = set(cs["customer"].map(_cnorm) + "|" + cs["assembly"].astype(str))
    pr["k"] = pr["customer"].map(_cnorm) + "|" + pr["assembly"].astype(str)
    missing = pr[~pr["k"].isin(have)]
    if only:
        keys = {_cnorm(o) for o in only}
        missing = missing[missing["customer"].map(_cnorm).isin(keys)]

    ctx = _Ctx(window_days=window_days, window_start=window_start)
    new_rows, new_steps = [], []
    for cn, grp in missing.groupby(missing["customer"].map(_cnorm)):
        disp = dispmap.get(cn) or grp["customer"].iloc[0]
        cid = cidmap.get(cn)
        asms = list(dict.fromkeys(grp["assembly"]))
        if cid is not None:
            try:
                ctx.batch_steps(disp, cid)            # one pull; classify_model reuses the cache
            except MESWebApiError as ex:
                log.warning("  classify-missing: MES pull failed for %s (%s)", disp, ex)
        classifiable = 0
        for a in asms:
            r = classify_model(ctx, disp, a, cid)
            md = r.pop("_mes_detail", []); idd = r.pop("_iedb_detail", [])
            new_rows.append(r); new_steps += _step_rows(r, md, idd)
            classifiable += r["status"] != "unavailable"
        log.info("  classify-missing %s: +%d models (%d with IEDB data)", disp, len(asms), classifiable)

    if new_rows:
        cs_new = pd.DataFrame(cs.to_dict("records") + new_rows)
        if "missing" in cs_new.columns:
            cs_new["missing"] = cs_new["missing"].map(lambda x: "; ".join(x) if isinstance(x, list) else (x or ""))
        cs_new.to_parquet(CT_MART["completion_status"], index=False)
        pd.concat([stp, pd.DataFrame(new_steps, columns=_STEP_COLS)], ignore_index=True) \
          .to_parquet(CT_MART["completion_steps"], index=False)
    log.info("classify_missing_planner: added %d models", len(new_rows))
    return len(new_rows)


if __name__ == "__main__":
    # Modes:
    #   -m ...completion_status --top [N]        full top-N-runner union (all layers) → mart
    #   -m ...completion_status <customer> [n]   sample one customer's has-data models
    #   -m ...completion_status --select-only [N]  just print the selection (no MES)
    #   -m ...completion_status --reclassify-planner   shifted-window retry of unverified (bucket B)
    #   -m ...completion_status --classify-missing-planner [CUST]  classify planner '-' models
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")

    window = _WINDOW_DAYS
    if "--window" in sys.argv:
        window = int(sys.argv[sys.argv.index("--window") + 1])

    # Bucket B — shifted-window retry of unverified PLANNER customers (minus non-MES).
    if "--reclassify-planner" in sys.argv:
        ws = int(sys.argv[sys.argv.index("--window-start") + 1]) if "--window-start" in sys.argv else 120
        cs = pd.read_parquet(CT_MART["completion_status"])
        eb = CT_MART["completion_status"].parent.parent / "ebuild" / "planner_runners.parquet"
        planner = {_cnorm(c) for c in pd.read_parquet(eb)["customer"].unique()}
        custs = [c for c in cs[cs["status"] == "unverified"]["customer"].unique()
                 if _cnorm(c) not in _NON_MES and _cnorm(c) in planner]
        print(f"reclassify (window {ws}-{ws + window}d) for {len(custs)} planner customers: {', '.join(sorted(custs))}")
        reclassify_unverified(custs, window_start=ws, window_days=window)
        sys.exit(0)

    # Classify planner '-' models (missing from the mart). Optional customer arg.
    if "--classify-missing-planner" in sys.argv:
        i = sys.argv.index("--classify-missing-planner")
        only = [sys.argv[i + 1]] if len(sys.argv) > i + 1 and not sys.argv[i + 1].startswith("-") else None
        n = classify_missing_planner(only=only, window_days=window)
        print(f"added {n} planner models to completion mart (only={only or 'ALL'})")
        sys.exit(0)

    # Drop non_mes rows for customers no longer carved (e.g. un-carved KEYSIGHT) so the
    # next --classify-missing-planner re-pulls + reclassifies them for real.
    if "--sync-non-mes" in sys.argv:
        cs = pd.read_parquet(CT_MART["completion_status"])
        orphan = (cs["status"] == "non_mes") & (~cs["customer"].map(lambda c: _cnorm(c) in _NON_MES))
        n = int(orphan.sum())
        cs[~orphan].to_parquet(CT_MART["completion_status"], index=False)
        print(f"dropped {n} orphaned non_mes rows ({', '.join(sorted(cs.loc[orphan, 'customer'].unique()))}) -> now re-classifiable")
        sys.exit(0)

    # Bucket A — carve out non-MES workcells already in the mart (pure code, no MES).
    if "--carve-non-mes" in sys.argv:
        cs = pd.read_parquet(CT_MART["completion_status"])
        mask = cs["customer"].map(lambda c: _cnorm(c) in _NON_MES) & (cs["status"] != "unavailable")
        n = int(mask.sum())
        cs.loc[mask, "status"] = "non_mes"
        cs.to_parquet(CT_MART["completion_status"], index=False)
        print(f"carved {n} models -> non_mes ({', '.join(sorted(cs.loc[mask, 'customer'].unique()))})")
        sys.exit(0)

    if "--select-only" in sys.argv:
        n = int(sys.argv[-1]) if sys.argv[-1].isdigit() else 100
        m = top_models(n)
        print(f"{len(m)} models selected; per customer:\n", m["customer"].value_counts().to_string())
        sys.exit(0)

    if "--top" in sys.argv:
        n = int(sys.argv[sys.argv.index("--top") + 1]) if sys.argv[-1].isdigit() else 100
        models = top_models(n)
    else:
        cust = sys.argv[1] if len(sys.argv) > 1 else "Nokia Optics"
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        asum = pd.read_parquet(CT_MART["assembly_summary"])
        amap = pd.read_parquet(CT_MART["mes_assembly_map"]).drop_duplicates("customer")[["customer", "customer_id"]]
        models = (asum[asum["customer"] == cust].drop_duplicates("assembly").head(n)
                  .merge(amap, on="customer", how="left")[["customer", "assembly", "customer_id"]])

    planner_only = "--planner-only" in sys.argv
    print(f"classifying {len(models)} models… (window={window}d{', PLANNER-ONLY' if planner_only else ''})")
    from modules.cycle_time.keep_awake import keep_system_awake
    with keep_system_awake():                      # block idle-sleep for the whole run
        df = run(models, window=window, planner_only=planner_only)
    print(df[["customer", "assembly", "status", "expected", "present", "coverage", "actual_steps", "missing"]].head(40).to_string())
    print("\nstatus counts:\n", df["status"].value_counts().to_string())
