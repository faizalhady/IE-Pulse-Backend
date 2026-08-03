"""
completion_v2.py  (cycle_time)
──────────────────────────────
Rebuild of completion_status.py. Writes *_v2 marts — v1 is left untouched so the
FE keeps working and a bad v2 can just be deleted.

WHAT CHANGED vs v1
  1. MATCHING — v1 had ONE check: workbook alias == IEDB alias, exact. That made
     100% of its 7,752 "missing" verdicts false-ish (`PACKOUT` never equals
     `PACKOUT 1`). v2 uses a ladder: workbook first, then auto-match, and compares
     on a BASE key with the trailing instance number stripped.
  2. STATUSES — v1 fused three different problems into `incomplete`. v2 splits:
       unavailable  model not in IEDB at all
       no_data      in IEDB, zero cycle time entered
       route_gap    MES runs steps IEDB's route doesn't list  ← was "missing CT"
       incomplete   step IS in IEDB's route but its cycle time is empty
       complete     every MES step it runs has a cycle time
       unverified   has CT, but no MES production found to check against
       non_mes      workcell isn't on MES — can't be checked
  3. SOURCES — #21 (customer/day scan counts) is cached to disk per customer-day,
     so a re-run costs ~0 MES calls for days already pulled. #132 BoardHistory adds
     a per-serial view with TWO names per step (RouteStep + StepInstance) and the
     real order; a model classified from #132 gets source='serial' (strong), one
     from #21 gets source='batch' (weak — #21 is a customer aggregate and drags in
     rework/variant steps that this model may not actually run).
"""

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from modules.cycle_time.config import (CT_MART, CT_MES_BOARD_DIR, CT_MES_SCAN_DIR)
from modules.cycle_time.mes_webapi import post, MESWebApiError

log = logging.getLogger(__name__)

_cnorm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())       # customer key
_snorm = lambda s: re.sub(r"\s+", " ", str(s).strip().upper())     # step display key
_anorm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())        # exact match key

# BASE key — same as _anorm but with the trailing instance number dropped.
# IEDB numbers a step per-model ("MA 1", "MA 2.5"); MES has no idea about that
# number, and the workbook can only carry one. Matching on the base kills that
# whole class of false misses. ponytail: loses "MA 1 has CT but MA 2 doesn't" —
# worth ~nothing, only 0.4% of IEDB route rows lack a cycle time at all.
_base = lambda s: _anorm(re.sub(r"\s*\d+(\.\d+)?\s*$", "", str(s).strip().upper()))

_WINDOW_DAYS = 120
_NON_MES = {"LAMMEC", "ADVANTEST"}        # verified-zero MES production


# ═══════════════════════════════════════════════════════════════════════════
# cached MES pulls
# ═══════════════════════════════════════════════════════════════════════════

_iid = lambda x: str(int(float(x)))     # pandas hands ids back as 59.0 — MES rejects that

# Journey is "finished" if it reaches one of these. PACKOUT is the true end of the
# line; OBA/OQA is the last gate before it and is good enough to call the route run.
_FINAL = ("PACKOUT", "PACK OUT", "OQA", "OBA", "SHIP")

# #132 circuit breaker. Each failed serial burns ~7s (3 retries + backoff), so 20
# consecutive misses ≈ 2 min before we abandon #132 for this customer and let #21
# cover the rest. Without this a customer whose serials all 404 hangs the run —
# it burned 3 hours on 2026-07-24 before being killed by hand.
_MAX_MISSES = 20


def scan_day(customer: str, customer_id, day: datetime) -> pd.DataFrame:
    """#21 for ONE customer-day, cached to disk.

    #21 FILTERS by CustomerID (proven 2026-07-24: Masimo id=59 -> 14 assemblies,
    KEYSIGHT id=7 -> 444, bogus id -> 0). So one call per customer per day; the
    day-cache means a re-run only fetches days it doesn't already have. An empty
    day is still cached — a quiet day is a fact, not a reason to re-pull."""
    p = CT_MES_SCAN_DIR / _cnorm(customer) / f"{day:%Y-%m-%d}.parquet"
    if p.exists():
        return pd.read_parquet(p)
    rows = post("Batch", "ListBatchCountsByRouteStep", {
        "CustomerID": _iid(customer_id),
        "StartDate": day.strftime("%Y-%m-%d 00:00:00"),
        "EndDate":  (day + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00"),
    })
    df = pd.DataFrame([{
        "assembly": str(r.get("Assembly", "")).split("/")[0].strip(),
        "step":     _snorm(r.get("RouteStep")),
        "order":    int(r.get("StepOrder") or 0),
        "qty":      int(r.get("ActualQty") or 0),
    } for r in rows], columns=["assembly", "step", "order", "qty"])
    df = df[(df["assembly"] != "") & (df["step"] != "")]
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p, index=False)
    return df


def batch_steps(customer: str, customer_id, window: int) -> dict:
    """{assembly: {step: [order, qty]}} for ONE customer over `window` days,
    day-cached. Raises if every uncached day failed so the customer is retried
    on the next run instead of being saved as false 'no production'."""
    acc, ok, fail, cached = {}, 0, 0, 0
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(window):
        day = today - timedelta(days=i)
        hit = (CT_MES_SCAN_DIR / _cnorm(customer) / f"{day:%Y-%m-%d}.parquet").exists()
        try:
            df = scan_day(customer, customer_id, day)
            ok += 1
            cached += hit
        except MESWebApiError:
            fail += 1
            continue
        for a, s, o, q in zip(df["assembly"], df["step"], df["order"], df["qty"]):
            d = acc.setdefault(a, {})
            if s in d:
                d[s][0] = min(d[s][0], o); d[s][1] += q
            else:
                d[s] = [o, q]
    if ok == 0 and fail > 0:
        raise MESWebApiError(f"all {fail} #21 days failed for {customer}")
    log.info("  #21 %-24s %4d assemblies (%d days ok / %d cached / %d failed)",
             customer, len(acc), ok, cached, fail)
    return acc


def serial_index(days_back: list[int], hours: list[int], per_model: int = 5) -> pd.DataFrame:
    """#126 ListTestDataWithinTime — site-wide test scans in ≤30-minute windows.

    WHY NOT #94: AgingWipsReport is LIVE WIP only. A unit leaves WIP the instant it
    packs out, so #94 literally never contains a finished unit (verified: 0 of 6,903
    Masimo rows sat at a final step). Its serials always give a half-run route.
    #126 looks BACKWARDS — a serial tested 45 days ago has long since finished, so
    #132 on it returns the complete journey. One window ≈ 300 models / 5.5k serials.

    Samples `hours` on each of `days_back`; keeps up to `per_model` serials per model."""
    rows = []
    for d in days_back:
        for h in hours:
            s = (datetime.now() - timedelta(days=d)).replace(hour=h, minute=0, second=0, microsecond=0)
            try:
                r = post("Test", "ListTestDataWithinTime", {
                    "startTime": s.strftime("%Y-%m-%d %H:%M:%S"),
                    "endTime": (s + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
                    "maxCount": "100000"})
            except MESWebApiError as ex:
                log.warning("  #126 %s FAILED — %s", s, str(ex)[:80])
                continue
            for x in r:
                a, sn = str(x.get("Assembly") or "").strip(), str(x.get("SerialNumber") or "").strip()
                if a and sn:
                    rows.append({"customer": x.get("Customer"), "assembly": a, "serial": sn,
                                 "scanned_at": x.get("StartDateTime"), "days_back": d})
            log.info("  #126 %s +30m: %d rows, %d models", s.strftime("%Y-%m-%d %H:%M"), len(r),
                     len({str(x.get("Assembly") or "") for x in r}))

    df = pd.DataFrame(rows, columns=["customer", "assembly", "serial", "scanned_at", "days_back"])
    if len(df):
        # oldest first — the more time has passed, the more certain the unit is finished
        df = (df.sort_values("days_back", ascending=False)
                .drop_duplicates(["assembly", "serial"])
                .groupby("assembly", group_keys=False).head(per_model).reset_index(drop=True))
    CT_MART["mes_serial_index"].parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(CT_MART["mes_serial_index"], index=False)
    log.info("serial index: %d serials over %d models", len(df), df["assembly"].nunique() if len(df) else 0)
    return df


def board_steps(customer: str, customer_id, picks: pd.DataFrame) -> pd.DataFrame:
    """#132 BoardHistoryReport, cached per customer. `picks` may hold several serials
    per model — we try them oldest-first and STOP at the first journey that reaches
    packout, so the stored route is a genuinely finished unit. If none finish we keep
    the longest attempt and flag it `finished=False` (partial route, weaker verdict)."""
    p = CT_MES_BOARD_DIR / f"{_cnorm(customer)}.parquet"
    cols = ["customer", "assembly", "serial", "route_step", "step_instance", "seq",
            "test_status", "finished"]
    have = pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=cols)
    todo = [a for a in dict.fromkeys(picks["assembly"]) if a not in set(have["assembly"])]
    if not todo:
        log.info("  #132 %-24s all %d models cached", customer, have["assembly"].nunique())
        return have

    cid = _iid(customer_id)
    rows, done, partial, dead = [], 0, 0, 0
    misses = 0                    # consecutive API failures — the circuit breaker
    for asm in todo:
        best = None
        for serial in picks.loc[picks["assembly"] == asm, "serial"]:
            try:
                h = post("Wip", "BoardHistoryReport",
                         {"custId": cid, "serial": str(serial), "useMultiPartBarCode": "", "lang": ""})
                misses = 0
            except MESWebApiError:
                # ponytail: circuit breaker, not a smarter retry. On 2026-07-24 #132
                # 404'd every serial of a customer and the 3-try-with-backoff loop
                # ground on for 3 HOURS before the run was killed. Each miss costs
                # ~7s, so _MAX_MISSES consecutive failures ≈ 2 min, then we give up
                # on this customer and let the caller fall back to #21. Upgrade path
                # if MES ever gets reliable: raise the threshold, don't remove it.
                misses += 1
                if misses >= _MAX_MISSES:
                    break
                continue
            j = []
            for i, r in enumerate(h):
                tp = str(r.get("Test_Process") or "")
                if "/" not in tp:
                    continue
                rs, si = tp.split("/", 1)
                j.append({"customer": customer, "assembly": asm, "serial": str(serial),
                          "route_step": _snorm(rs), "step_instance": _snorm(si), "seq": i,
                          "test_status": r.get("TestStatus"), "finished": False})
            if not j:
                continue
            fin = any(f in x["step_instance"] or f in x["route_step"] for x in j for f in _FINAL)
            if fin:
                for x in j:
                    x["finished"] = True
                best = j
                break                      # finished unit — no need to try more serials
            if best is None or len(j) > len(best):
                best = j                   # keep the longest partial as a fallback
        if misses >= _MAX_MISSES:
            log.warning("  #132 %-24s BREAKER TRIPPED after %d consecutive failures — "
                        "%d models done, %d left to #21", customer, misses,
                        len({r["assembly"] for r in rows}), len(todo) - todo.index(asm))
            break
        if best is None:
            dead += 1
        else:
            rows += best
            done += best[0]["finished"]
            partial += not best[0]["finished"]

    out = pd.concat([have, pd.DataFrame(rows, columns=cols)], ignore_index=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(p, index=False)
    log.info("  #132 %-24s +%d finished, +%d partial, %d no-history | %d models cached",
             customer, done, partial, dead, out["assembly"].nunique())
    return out


# ═══════════════════════════════════════════════════════════════════════════
# IEDB side + matching
# ═══════════════════════════════════════════════════════════════════════════

class Ctx:
    def __init__(self):
        self.con = duckdb.connect()
        self.raw = CT_MART["raw"].as_posix()
        pm = pd.read_parquet(CT_MART["mes_process_map"])
        self.pmap, self.pknown = {}, set()
        for c, s, a, isie in zip(pm["customer"], pm["step_instance"], pm["iedb_alias"], pm["is_iedb"]):
            k = (_cnorm(c), s)
            self.pknown.add(k)
            if isie:
                self.pmap[k] = a
        self._iedb, self._models = {}, {}

    def iedb(self, customer: str) -> dict:
        """{assembly: {'ct': set(exact), 'ct_b': set(base), 'all_b': set(base),
        'proc_b': set(base of process name), 'detail': [...] }}"""
        if customer in self._iedb:
            return self._iedb[customer]
        df = self.con.execute(f"""
            SELECT DISTINCT assembly, process, alias, sub_workcenter,
                   "order" AS ord, cycle_time_per_process AS ct
            FROM read_parquet('{self.raw}')
            WHERE customer = ? AND priority = 1 AND alias IS NOT NULL
        """, [customer]).fetchdf()
        d = {}
        for a, p, al, sw, od, ct in zip(df["assembly"], df["process"], df["alias"],
                                        df["sub_workcenter"], df["ord"], df["ct"]):
            e = d.setdefault(a, {"ct": set(), "ct_b": set(), "all_b": set(),
                                 "proc_b": set(), "detail": []})
            has = not pd.isna(ct)
            e["all_b"].add(_base(al))
            e["proc_b"].add(_base(p)); e["proc_b"].add(_anorm(p))
            if has:
                e["ct"].add(_anorm(al)); e["ct_b"].add(_base(al))
            e["detail"].append({"process": p, "alias": al, "sub_workcenter": sw,
                                "order": None if pd.isna(od) else int(od),
                                "cycle_time": None if pd.isna(ct) else float(ct)})
        for a in d:
            d[a]["detail"].sort(key=lambda x: (x["order"] is None, x["order"] or 0))
        self._iedb[customer] = d
        return d

    def models(self, customer: str) -> set:
        if customer not in self._models:
            self._models[customer] = set(self.con.execute(
                f"SELECT DISTINCT assembly FROM read_parquet('{self.raw}') WHERE customer = ?",
                [customer]).fetchdf()["assembly"])
        return self._models[customer]


def match_step(ctx: Ctx, cn: str, ie: dict, names: list[str]) -> tuple[str, str]:
    """Decide one MES step against one model's IEDB route.
    `names` = the MES name(s) for this step — [StepInstance] from #21, or
    [StepInstance, RouteStep] from #132.

    Ladder, first hit wins:
      1. workbook says "not an IEDB step"      -> non_iedb
      2. workbook alias, exact then base       -> present / (falls through)
      3. any MES name vs IEDB alias base       -> present
      4. any MES name vs IEDB process base     -> present
      5. anything above found the step in the route but with NO cycle time -> no_ct
      6. workbook had an alias but nothing matched -> not_in_iedb
      7. no workbook entry, no auto-match      -> unmapped
    Returns (status, alias_used).
    """
    alias = None
    for n in names:
        if (cn, n) in ctx.pmap:
            alias = ctx.pmap[(cn, n)]
            break
    known = any((cn, n) in ctx.pknown for n in names)
    if alias is None and known:
        return "non_iedb", ""                    # workbook explicitly says not IEDB

    keys = [_base(n) for n in names]
    if alias:
        if _anorm(alias) in ie["ct"] or _base(alias) in ie["ct_b"]:
            return "present", alias
        keys.insert(0, _base(alias))
    if any(k in ie["ct_b"] for k in keys):
        return "present", alias or ""
    if any(k in ie["proc_b"] for k in keys):
        return "present", alias or ""
    if any(k in ie["all_b"] for k in keys):
        return "no_ct", alias or ""              # in the route, cycle time is empty
    return ("not_in_iedb", alias) if alias else ("unmapped", "")


_STEP_COLS = ["customer", "assembly", "side", "name", "name2", "alias",
              "sub_workcenter", "order", "value", "status", "source"]


def classify(ctx: Ctx, customer: str, assembly: str, steps: list[tuple], source: str) -> tuple[dict, list]:
    """`steps` = [(names:list[str], order:int, qty:int), ...] already deduped."""
    cn = _cnorm(customer)
    r = {"customer": customer, "assembly": assembly, "status": None, "source": source,
         "expected": 0, "present": 0, "no_ct": 0, "not_in_iedb": 0, "unmapped": 0,
         "non_iedb": 0, "actual_steps": len(steps), "coverage": None,
         "missing_ct": "", "gap_steps": ""}

    if assembly not in ctx.models(customer):
        return {**r, "status": "unavailable", "actual_steps": 0, "source": "none"}, []
    if cn in _NON_MES:
        return {**r, "status": "non_mes", "actual_steps": 0, "source": "none"}, []

    ie = ctx.iedb(customer).get(assembly)
    if ie is None or not ie["ct"]:
        return {**r, "status": "no_data"}, _iedb_rows(customer, assembly, ie)
    if not steps:
        return {**r, "status": "unverified"}, _iedb_rows(customer, assembly, ie)

    rows, no_ct, gap = [], [], []
    counts = {"present": 0, "no_ct": 0, "not_in_iedb": 0, "unmapped": 0, "non_iedb": 0}
    for names, order, qty in sorted(steps, key=lambda x: (x[1], x[0][0])):
        st, alias = match_step(ctx, cn, ie, names)
        counts[st] += 1
        if st == "no_ct":
            no_ct.append(alias or names[0])
        elif st == "not_in_iedb":
            gap.append(alias or names[0])
        rows.append({"customer": customer, "assembly": assembly, "side": "MES",
                     "name": names[0], "name2": names[1] if len(names) > 1 else "",
                     "alias": alias, "sub_workcenter": None, "order": order,
                     "value": qty, "status": st, "source": source})

    real = counts["present"] + counts["no_ct"] + counts["not_in_iedb"]   # steps we have an opinion on
    r.update({"expected": real, "present": counts["present"], "no_ct": counts["no_ct"],
              "not_in_iedb": counts["not_in_iedb"], "unmapped": counts["unmapped"],
              "non_iedb": counts["non_iedb"],
              "coverage": round((len(steps) - counts["unmapped"]) / len(steps), 2),
              "missing_ct": "; ".join(sorted(set(no_ct))),
              "gap_steps": "; ".join(sorted(set(gap)))})
    if counts["no_ct"]:
        r["status"] = "incomplete"          # the real cycle-time gap — most actionable
    elif counts["not_in_iedb"]:
        r["status"] = "route_gap"           # IEDB's route doesn't list steps the floor runs
    elif counts["present"]:
        r["status"] = "complete"
    else:
        r["status"] = "unverified"          # only unmapped/non-IEDB steps — nothing to judge
    return r, rows + _iedb_rows(customer, assembly, ie)


def _iedb_rows(customer: str, assembly: str, ie) -> list:
    if not ie:
        return []
    return [{"customer": customer, "assembly": assembly, "side": "IEDB",
             "name": s["process"], "name2": "", "alias": s["alias"],
             "sub_workcenter": s["sub_workcenter"], "order": s.get("order"),
             "value": s["cycle_time"],
             "status": "has_ct" if s["cycle_time"] is not None else "no_ct",
             "source": "iedb"} for s in ie["detail"]]


# ═══════════════════════════════════════════════════════════════════════════
# run
# ═══════════════════════════════════════════════════════════════════════════

def _flush(summary: dict, steps: dict):
    CT_MART["completion_status_v2"].parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(list(summary.values())).to_parquet(CT_MART["completion_status_v2"], index=False)
    rows = [r for g in steps.values() for r in g]
    pd.DataFrame(rows, columns=_STEP_COLS).to_parquet(CT_MART["completion_steps_v2"], index=False)


def _load_existing() -> tuple[dict, dict]:
    summary, steps = {}, {}
    if CT_MART["completion_status_v2"].exists():
        for r in pd.read_parquet(CT_MART["completion_status_v2"]).to_dict("records"):
            summary[(r["customer"], r["assembly"])] = r
    if CT_MART["completion_steps_v2"].exists():
        for r in pd.read_parquet(CT_MART["completion_steps_v2"]).to_dict("records"):
            steps.setdefault((r["customer"], r["assembly"]), []).append(r)
    return summary, steps


def run(models: pd.DataFrame, window: int = _WINDOW_DAYS, use_serial: bool = True,
        resume: bool = True) -> pd.DataFrame:
    """models = [customer, assembly, customer_id]. Upserts into the v2 marts —
    a partial run updates only what it touched and never wipes the rest."""
    state = CT_MART["completion_status_v2"].parent / ".completion_v2_state.json"
    ctx = Ctx()
    summary, steps = _load_existing()
    done = set()
    if resume and state.exists():
        try:
            done = set(json.loads(state.read_text()).get("done", []))
            log.info("RESUME — %d customers done, %d models in v2 mart", len(done), len(summary))
        except Exception:
            log.warning("resume state unreadable — starting over")

    by_cust: dict = {}
    for m in models.itertuples(index=False):
        by_cust.setdefault(m.customer, []).append(m)

    sidx = pd.DataFrame()
    if use_serial and CT_MART["mes_serial_index"].exists():
        sidx = pd.read_parquet(CT_MART["mes_serial_index"])

    # smallest workcell first — banks quick wins, giants (KEYSIGHT/ARISTA) last so
    # an interruption never costs the whole run.
    order = sorted(by_cust, key=lambda c: (len(by_cust[c]), str(c)))
    log.info("v2 run: %d customers, %d models, %d remaining (window=%dd, serial=%s)",
             len(by_cust), len(models), len([c for c in by_cust if c not in done]), window, use_serial)

    skipped = []
    for cust in order:
        if cust in done:
            continue
        cid = by_cust[cust][0].customer_id
        wanted = {m.assembly for m in by_cust[cust]}

        # ── serial source (#132) — strong. Match on assembly alone; #126's Customer
        #    label is MES's own and doesn't always equal our workcell name.
        bs = pd.DataFrame()
        if use_serial and len(sidx):
            picks = sidx[sidx["assembly"].isin(wanted)]
            if len(picks):
                try:
                    bs = board_steps(cust, cid, picks)
                except MESWebApiError as ex:
                    log.warning("  #132 %s failed — %s", cust, ex)
        serial_models = set(bs["assembly"]) if len(bs) else set()

        # ── batch source (#21) — only needed for models with NO serial. Skip the
        #    (slow) per-customer pull entirely if every model already got #132.
        acc = {}
        if len(wanted - serial_models):
            try:
                acc = batch_steps(cust, cid, window)
            except MESWebApiError as ex:
                log.warning("  SKIP %s — #21 unreachable (%s); retry on re-run", cust, ex)
                skipped.append(cust)
                continue

        n_serial = 0
        for m in by_cust[cust]:
            if m.assembly in serial_models:
                sub = bs[bs["assembly"] == m.assembly]
                seen, sl = set(), []
                for rs, si, sq in zip(sub["route_step"], sub["step_instance"], sub["seq"]):
                    if si in seen:
                        continue                       # dedup retest/rework repeats
                    seen.add(si)
                    sl.append(([si, rs], int(sq), 1))
                src = "serial" if bool(sub["finished"].iloc[0]) else "serial_partial"
                n_serial += 1
            else:
                sl = [([s], v[0], v[1]) for s, v in acc.get(m.assembly, {}).items()]
                src = "batch"
            r, rows = classify(ctx, m.customer, m.assembly, sl, src)
            summary[(m.customer, m.assembly)] = r
            steps[(m.customer, m.assembly)] = rows

        done.add(cust)
        _flush(summary, steps)
        state.write_text(json.dumps({"done": sorted(done)}))
        log.info("  ✓ %-24s %4d models (%d serial / %d batch) | %d/%d customers",
                 cust, len(by_cust[cust]), n_serial, len(by_cust[cust]) - n_serial,
                 len(done), len(by_cust))

    if skipped:
        log.warning("finished with %d SKIPPED (retry on re-run): %s", len(skipped), ", ".join(skipped))
    else:
        state.unlink(missing_ok=True)
    df = pd.DataFrame(list(summary.values()))
    log.info("v2 COMPLETE: %d models in mart", len(df))
    return df


def _selfcheck():
    """The match ladder is the whole point of v2 — check every rung."""
    class C:  # stand-in Ctx: only pmap/pknown are read by match_step
        pmap = {("CUST", "PACKOUT LINK"): "PACKOUT", ("CUST", "AOI TOP"): "AOIT 1",
                ("CUST", "GHOST"): "NOSUCH 1", ("CUST", "DEAD"): "OLD 1"}
        pknown = set(pmap) | {("CUST", "UNLINK")}
    ie = {"ct": {"AOIT1"}, "ct_b": {"PACKOUT", "AOIT", "SCRT"}, "all_b": {"PACKOUT", "AOIT", "SCRT", "OLD"},
          "proc_b": {"XRAY", "XRAY1"}}
    m = lambda names: match_step(C(), "CUST", ie, names)
    assert m(["PACKOUT LINK"]) == ("present", "PACKOUT"), m(["PACKOUT LINK"])   # base: PACKOUT == PACKOUT 1
    assert m(["AOI TOP"])[0] == "present"                                       # exact workbook hit
    assert m(["SCRT01"])[0] == "present"                                        # auto: name -> alias base
    assert m(["XRAY"])[0] == "present"                                          # auto: name -> process
    assert m(["DEAD"])[0] == "no_ct"                                            # in route, cycle time empty
    assert m(["GHOST"])[0] == "not_in_iedb"                                     # mapped, route lacks it
    assert m(["UNLINK"])[0] == "non_iedb"                                       # workbook says not IEDB
    assert m(["WHO KNOWS"])[0] == "unmapped"                                    # nothing knows it
    assert m(["ZZZ", "SCRT01"])[0] == "present"                                 # #132 second name saves it
    print("match ladder OK")

    # ── #132 circuit breaker: an all-404 customer must bail, not grind ────────
    # Fake 500 models x 3 serials. Without the breaker this is 1,500 calls; with
    # it, _MAX_MISSES. Guards the 3-hour hang AND the reassign-doesn't-break-the-
    # outer-loop bug (todo=[] inside `for asm in todo` does nothing).
    calls = []

    def dead_post(*a, **k):
        calls.append(1)
        raise MESWebApiError("404 — simulated dead customer")

    real_post = globals()["post"]
    globals()["post"] = dead_post
    try:
        picks = pd.DataFrame({"assembly": [f"A{i}" for i in range(500) for _ in range(3)],
                              "serial": [f"S{i}" for i in range(1500)]})
        import tempfile
        from modules.cycle_time import config as _cfg
        orig = _cfg.CT_MES_BOARD_DIR
        globals()["CT_MES_BOARD_DIR"] = Path(tempfile.mkdtemp())
        out = board_steps("SELFCHECK", 1, picks)
    finally:
        globals()["post"] = real_post
        globals()["CT_MES_BOARD_DIR"] = orig
    assert len(calls) <= _MAX_MISSES + 3, f"breaker let {len(calls)} calls through, cap ~{_MAX_MISSES}"
    assert len(out) == 0, "a fully dead customer must return no rows"
    print(f"#132 breaker OK — stopped after {len(calls)} calls, not 1500")


if __name__ == "__main__":
    _selfcheck()
