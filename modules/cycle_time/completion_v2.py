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
  2. STATUSES — four, worst-first, each with a `reason` that carries the detail:
       incomplete     in IEDB with cycle times, but gaps against what the floor runs
       no_cycle_time  EXISTS in the official IEDB, but not one cycle time entered
       not_in_iedb    does not exist in the official IEDB at all
       not_in_mes   no MES production found — not built yet, or workcell isn't on MES
       complete     every MES step it runs has a cycle time
     (7 statuses collapsed to 4 on 2026-08-05 — route_gap/no_data/unverified/
     unavailable were separate columns saying the same thing four ways. The
     distinction survives in `reason`; the breakdown report groups on it.)
  3. SOURCES — #21 (customer/day scan counts) is cached to disk per customer-day,
     so a re-run costs ~0 MES calls for days already pulled. #132 BoardHistory adds
     a per-serial view with TWO names per step (RouteStep + StepInstance) and the
     real order; a model classified from #132 gets source='serial' (strong), one
     from #21 gets source='batch' (weak — #21 is a customer aggregate and drags in
     rework/variant steps that this model may not actually run).
"""

import bisect
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

# `_cnorm` as SQL, for the queries that read the raw mart. The catalogue side
# has always keyed on _cnorm while the raw side matched the customer string
# EXACTLY, and the marts carry some workcells under two spellings — demand said
# "MASIMO", IEDB said "Masimo". The exact match found nothing, the normalised
# catalogue lookup found the model, and the two together reported a fully timed
# model (25959-AB, 25 steps, 25 cycle times) as "in IEDB, nobody has timed it".
# Same normalisation both sides or the bug comes straight back.
_SQL_CNORM = "regexp_replace(upper(customer), '[^A-Z0-9]', '', 'g')"

# An alias is a compound "CODE n - DISPLAY NAME", and the two sides disagree on
# how much of it they store. IEDB writes the whole thing ("MA 1 - BACK MECH ASSY 1",
# "TSTH 1 - TSTH TOP"); a workbook may write only the code ("MA 1", "PACKOUT 1").
# Comparing the strings whole can therefore NEVER match those — which is exactly
# what it did, and why R380-7868R9.0 read as a route gap on a step both sides
# spell identically.
#
# Only the CODE is an identifier. The display name is free text: it disagrees
# harmlessly ("TSTH1" vs "TSTH TOP") and agrees dangerously ("SCRB 1 - SCRT01"
# would fuse with "SCRT 1 - SCRT01" — bottom printer read as top). So we parse
# the code out and compare that.
#
# The trailing instance token goes too ("MA 2" ≡ "MA 1" ≡ "MA 2.1" ≡ "MA 1/2").
# IEDB numbers steps per-model, MES has no idea about that number, and the
# workbook can only carry one. ponytail: loses "MA 1 has CT but MA 2 doesn't" —
# worth ~nothing, only 0.4% of IEDB route rows lack a cycle time at all.
_code = lambda s: _anorm(re.sub(r"[\s\d./]+$", "", str(s).split("-")[0].strip().upper()))


# Suffixes that still resolve to the base model's route, confirmed by Faiz
# 2026-08-05: -SUB, -OP*, -S*, -FA, plus the repair/engineering variants.
#
# NOT here, on purpose:
#   -OPT, -SFT, -Z2   not confirmed as sharing the base route
#   bare letters      "EK050-66451N", "IN300-1074-203SDZ", "IN800-0673-201A1F"
#                     look like revisions but are NOT — the catalogue carries
#                     revision in its own column ('', 'D', 'A01' for those three)
#                     and EK050-66401 / EK050-66451N are separate live parts.
#                     Stripping them would fuse genuinely different assemblies.
#
# Boundaries are deliberately tight. A bare trailing "S" is only taken after a
# DIGIT ("SKY900-212127-000S"), never after a letter — "AK11-CAP-9D3C6-TS" would
# otherwise collapse into the real, separate part "AK11-CAP-9D3C6-T".
_SUFFIX = re.compile(
    "(?:"
    r"[-_ ]?(?:H?RMA|CRMA|BRMA|FRU|EV\d*)"   # repair / engineering variant
    r"|[-_ ]?SUB"                            # sub-assembly (often written with no dash)
    r"|[-_ ](?:OP\d*|S\d*|FA)"               # build-stage marker, separated
    r"|(?<=\d)(?:OP\d+|S\d*|FA)"             # ...or run straight onto a digit
    ")$", re.I)


def _desuffix(s: str) -> str:
    v = str(s).strip().upper()
    while True:
        n = _SUFFIX.sub("", v)
        if n == v:
            return v
        v = n


_MIN_STEM = 6      # below this a "front name" is too generic to trust


def _front_match(keys: list, lut: dict, k: str):
    """Match on the FRONT of the model name: either side may carry extra tail.
    `keys` is sorted anorm names, `lut` maps anorm -> the real name.

    Faiz 2026-08-05: match the front, whatever the suffix. Catches
    '8100-0774-R05' -> '8100-0774-R05-DEV' and every -OPT/-SFT/-Z2 style tail we
    could not enumerate. It cannot invent a match: 'PCA-01803-20' still fails
    against 'PCA-01803-10' because they diverge before the tail."""
    if not k or len(k) < _MIN_STEM:
        return None
    i = bisect.bisect_left(keys, k)                 # IEDB name extends ours
    if i < len(keys) and keys[i].startswith(k):
        return lut[keys[i]]
    for j in range(len(k) - 1, _MIN_STEM - 1, -1):  # ours extends an IEDB name
        if k[:j] in lut:
            return lut[k[:j]]
    return None


def _alias_name(s: str) -> str:
    """The display half of a compound alias — everything after the first dash,
    or the whole string when there is no dash. Free text: only ever consulted
    for steps the workbook never mapped, never to overrule a mapped code."""
    head, _, tail = str(s).partition("-")
    return (tail.strip() or head.strip())

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
                log.warning("  #126 %s FAILED - %s", s, str(ex)[:80])
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
            log.warning("  #132 %-24s BREAKER TRIPPED after %d consecutive failures - "
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
        # ONE bridge loader, not a workbook read plus a decision overlay bolted
        # on. `process_bridge` layers the workbook, the names real scans taught
        # us, and the engineers' answers — in that order — and its selfcheck
        # asserts it never resolves fewer names than the workbook alone.
        # Before this, the registry that was built to BE the bridge was read by
        # nobody and 105 answered names were stranded.
        from modules.cycle_time.process_bridge import load as _load_bridge
        self.pmap, self.pknown, _bstats = _load_bridge(CT_MART["raw"].parent.parent)
        self._load_iedb()

    def _load_iedb(self) -> None:
        """The IEDB side: catalogue, per-model cycle-time index, suffix keys.

        This used to be the tail of a method called `_apply_decisions`, which by
        then did two unrelated jobs — overlay the engineers' answers AND load
        every IEDB structure. The overlay moved to `process_bridge`; what is left
        is the loading, so it is named for that.
        """
        self._iedb, self._models, self._desuf, self._desufkeys = {}, {}, {}, {}
        # The FULL IEDB catalogue, including assemblies with no cycle time. `raw`
        # only holds rows that HAVE a cycle time, so a model that exists in IEDB
        # but was never timed is missing from it — reading absence from `raw`
        # alone reported 452 such models as "not in IEDB at all". Very different
        # jobs: create the model, versus go and time the one already there.
        self.catalog: dict = {}      # cnorm -> {anorm(name): original}
        cat_path = CT_MART["assembly_catalog"]
        if cat_path.exists():
            cat = pd.read_parquet(cat_path)
            cols = [c for c in ("assembly", "assembly_full") if c in cat.columns]
            for c, *names in zip(cat["customer"], *(cat[c] for c in cols)):
                d = self.catalog.setdefault(_cnorm(c), {})
                for n in names:
                    d.setdefault(_anorm(n), str(n).strip())
            # TWO COUNTING UNITS, never mix them:
            #   MODEL      = (customer, assembly name). What demand, MES and this
            #                report mean by "a model". Revisions collapse into one.
            #   ASSEMBLY_ID = (customer, assembly, revision). IEDB's own row unit,
            #                and what its CustomerStatus report counts.
            # Comparing our MODEL count to IEDB's ASSEMBLY_ID count made every
            # workcell look short (KEYSIGHT 22,208 vs 33,933) and wrongly marked
            # 706 models unverified. Same unit both sides -> 26/40 match exactly.
            self._cat_n = (cat.groupby(cat["customer"].map(_cnorm))["assembly_id"]
                              .nunique().to_dict())
        self._catkeys = {c: sorted(d) for c, d in self.catalog.items()}

        # Workcells where our catalogue snapshot is SMALLER than the assembly
        # count IEDB itself reports. For those we cannot honestly say a model is
        # "not in IEDB" — only that it is not in our copy. On 2026-08-05 the
        # snapshot was a month old and short by 11,953 assemblies on KEYSIGHT
        # alone, which made 898 "absent" verdicts unprovable.
        self.short: set = set()
        cs_path = CT_MART["customer_status"]
        if cs_path.exists() and self.catalog:
            cs = pd.read_parquet(cs_path)
            have = getattr(self, "_cat_n", {})
            for cd, n in zip(cs["CustomerDivision"], cs["NoOfAssemblies"]):
                c = _cnorm(str(cd).split("/")[0])
                if c in have and n and n > have[c]:
                    self.short.add(c)

    def iedb(self, customer: str) -> dict:
        """{assembly: {'ct_codes', 'all_codes', 'ct_names', 'all_names', 'detail'}}

        Built from the ALIAS column only. `process` ("Assembly 2", "Link 1",
        "Press 2", "THI 2") is a human display label, not an identifier — matching
        against it manufactured 1,377 false "present" verdicts, because MES's
        coarse route family ("LINK", "QC") collides with it by pure word overlap.
        It is kept for display in `detail` and never used to decide anything."""
        if customer in self._iedb:
            return self._iedb[customer]
        # Alias-less rows are KEPT. Filtering them out dropped the model from this
        # dict entirely, so a model with 18 cycle times and a blank alias column
        # (LIFE360 410-10152-00-Z1) fell through and was reported as "no cycle
        # time" — the exact opposite of the truth. They contribute no match key,
        # which is a real problem, but it is an alias-entry problem and classify()
        # names it as one instead of lying about the cycle times.
        df = self.con.execute(f"""
            SELECT DISTINCT assembly, process, alias, sub_workcenter,
                   "order" AS ord, cycle_time_per_process AS ct
            FROM read_parquet('{self.raw}')
            WHERE {_SQL_CNORM} = ? AND priority = 1
        """, [_cnorm(customer)]).fetchdf()
        d = {}
        # Keyed on the STRIPPED name: 85 models were reported "not in IEDB" purely
        # because the demand plan carried a trailing space or a literal tab
        # ('M068459C002-2\t'). Only whitespace is normalised away — case and
        # punctuation are load-bearing in these part numbers.
        df["assembly"] = df["assembly"].astype(str).str.strip()
        for a, p, al, sw, od, ct in zip(df["assembly"], df["process"], df["alias"],
                                        df["sub_workcenter"], df["ord"], df["ct"]):
            e = d.setdefault(a, {"ct_codes": set(), "all_codes": set(), "ct_names": set(),
                                 "all_names": set(), "any_ct": False, "detail": []})
            has_ct = not pd.isna(ct)
            e["any_ct"] |= has_ct
            if not pd.isna(al):                      # no alias -> no match key
                code, name = _code(al), _anorm(_alias_name(al))
                e["all_codes"].add(code); e["all_names"].add(name)
                if has_ct:
                    e["ct_codes"].add(code); e["ct_names"].add(name)
            e["detail"].append({"process": p, "alias": al, "sub_workcenter": sw,
                                "order": None if pd.isna(od) else int(od),
                                "cycle_time": None if pd.isna(ct) else float(ct)})
        for a in d:
            d[a]["detail"].sort(key=lambda x: (x["order"] is None, x["order"] or 0))
        self._iedb[customer] = d
        return d

    def models(self, customer: str) -> set:
        if customer not in self._models:
            self._models[customer] = {str(a).strip() for a in self.con.execute(
                f"SELECT DISTINCT assembly FROM read_parquet('{self.raw}') WHERE {_SQL_CNORM} = ?",
                [_cnorm(customer)]).fetchdf()["assembly"]}
        return self._models[customer]

    def resolve(self, customer: str, assembly: str):
        """Demand's model name -> the IEDB assembly that answers it, or None.

        EXACT WINS. The suffix rule is only a fallback, never a rewrite of the
        key: IEDB frequently holds both `X` and `X-RMA` as separate models, so
        normalising both sides up front fused 1,590 of them. As a fallback it
        cannot do that — an exact hit is taken before it is ever consulted."""
        a = str(assembly).strip()
        have = self.models(customer)
        if a in have:
            return a
        if customer not in self._desuf:
            # Keyed on anorm(desuffix(...)): demand writes 'AK-01-AKMCAC2-SUB'
            # where IEDB has 'AK01-AKMCAC2', so the dashes have to go too. Safe
            # only because this is reached after an exact miss.
            m: dict = {}
            for x in sorted(have):               # sorted -> deterministic winner
                m.setdefault(_anorm(_desuffix(x)), x)
            self._desuf[customer] = m
            self._desufkeys[customer] = sorted(m)
        d = self._desuf[customer]
        k = _anorm(_desuffix(a))
        return d.get(k) or _front_match(self._desufkeys[customer], d, k)

    def in_catalog(self, cn: str, assembly: str) -> bool:
        """Does the OFFICIAL IEDB list this model — exact, suffix, or front name."""
        d = self.catalog.get(cn)
        if not d:
            return False
        k = _anorm(str(assembly).strip())
        if k in d or _anorm(_desuffix(assembly)) in d:
            return True
        return _front_match(self._catkeys[cn], d, k) is not None

    def in_catalog_exact(self, cn: str, assembly: str) -> bool:
        """Does IEDB list this model under THIS EXACT name — no suffix or front
        matching. Used to stop a model borrowing a neighbour's route: if IEDB
        knows the name, that name answers for itself or not at all."""
        d = self.catalog.get(cn)
        return bool(d) and _anorm(str(assembly).strip()) in d

    def near(self, cn: str, assembly: str) -> str:
        """The catalogue entry this model most likely IS, when nothing matched.
        Surfaced rather than applied — `853-238767-003-OP2` and `-S1` and `-OP3`
        all point at `853-238767-003`, and silently collapsing them would report
        one route as covering four different build stages."""
        keys = self._catkeys.get(cn)
        if not keys:
            return ""
        k = _anorm(assembly)
        i = bisect.bisect_left(keys, k)         # IEDB name extends ours
        if i < len(keys) and keys[i].startswith(k) and keys[i] != k:
            return self.catalog[cn][keys[i]]
        for j in range(len(k) - 1, 5, -1):      # ours extends an IEDB name
            if k[:j] in self.catalog[cn]:
                return self.catalog[cn][k[:j]]
        return ""


def match_step(ctx: Ctx, cn: str, ie: dict, names: list[str]) -> tuple[str, str]:
    """Decide one MES step against one model's IEDB route.
    `names` = the MES name(s) for this step — [StepInstance] from #21, or
    [StepInstance, RouteStep] from #132.

    The IEDB side offers exactly ONE identifier: the alias code. The MES side may
    offer several, and they are NOT equal in weight:

      1. workbook says "not an IEDB step"           -> non_iedb
      2. workbook mapped this step to an alias      -> judge on THAT code, and stop
      3. no workbook entry: try the MES names       -> code, then alias display name
      4. nothing matched                            -> unmapped

    Rung 2 is deliberately a dead end. Once the workbook has told us which IEDB
    step this is, a miss means the route really lacks it — so we report the gap
    instead of going fishing with the step's free text. Fishing is what produced
    the false greens: `HLA 1 LINK` carries workbook alias `MA 1`, the model's IEDB
    route has no MA at all, but the MES family label "LINK" collided with IEDB's
    display name "Link 1" and the step was called complete. It proved nothing —
    it never checked MA. Like looking someone up by employee ID: if the ID is not
    found you report not found, you do not start matching on first names.

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

    if alias:
        c = _code(alias)
        if c in ie["ct_codes"]:
            return "present", alias
        if c in ie["all_codes"]:
            return "no_ct", alias                # in the route, cycle time is empty
        return "not_in_iedb", alias              # route genuinely lacks this step

    # Unmapped step — now the MES free text is all we have, so use every bit of it
    # against both halves of the IEDB alias.
    keys = {_code(n) for n in names} | {_anorm(n) for n in names}
    keys.discard("")
    if keys & ie["ct_codes"] or keys & ie["ct_names"]:
        return "present", ""
    if keys & ie["all_codes"] or keys & ie["all_names"]:
        return "no_ct", ""
    return "unmapped", ""


_STEP_COLS = ["customer", "assembly", "side", "name", "name2", "alias",
              "sub_workcenter", "order", "value", "status", "source"]

# Model status -> the reasons it can carry. The status answers "how bad", the
# reason answers "why", and the breakdown report groups on (status, reason).
REASONS = {
    "incomplete":    ("missing_ct", "missing_step", "missing_ct+step", "unmapped", "no_alias"),
    "no_cycle_time": ("in_iedb_untimed",),
    "not_in_iedb":   ("absent", "absent_unverified"),
    "not_in_mes":  ("no_production", "workcell_not_on_mes"),
    "complete":    ("",),
}


def classify(ctx: Ctx, customer: str, assembly: str, steps: list[tuple], source: str) -> tuple[dict, list]:
    """`steps` = [(names:list[str], order:int, qty:int), ...] already deduped."""
    cn = _cnorm(customer)
    # `graded_on` is stamped HERE, not at _flush(). run() upserts, so a flush
    # rewrites every row including the thousands it never touched — stamping
    # there would date the whole mart to the last run and destroy the one fact
    # this column exists to record. Until 2026-08-17 there was no timestamp at
    # all: a row graded in June and a row graded this morning were
    # indistinguishable, which is how 1,666 rows carrying statuses the code can
    # no longer emit sat unnoticed behind numbers people were reporting upward.
    r = {"customer": customer, "assembly": assembly, "status": None, "reason": "",
         "source": source, "graded_on": pd.Timestamp.now().isoformat(timespec="seconds"),
         "expected": 0, "present": 0, "no_ct": 0, "not_in_iedb": 0, "unmapped": 0,
         "non_iedb": 0, "actual_steps": len(steps), "coverage": None,
         "missing_ct": "", "gap_steps": "", "near_match": ""}

    akey = ctx.resolve(customer, assembly)
    if akey is None:
        # Keep the MES route. Returning [] here meant the drawer showed an empty
        # "MES ROUTE (ACTUAL)" for exactly the models where knowing what the floor
        # runs matters most — IEDB has nothing, so the MES route is the only
        # record of the process that exists.
        # In IEDB's catalogue but untimed, or not in IEDB at all — same status,
        # different job for whoever picks it up.
        a = str(assembly).strip()
        known = ctx.in_catalog(cn, a)
        # Only claim "absent" when our catalogue is known-complete for this
        # workcell; otherwise say so rather than assert a fact we cannot back.
        if known:
            return {**r, "status": "no_cycle_time", "reason": "in_iedb_untimed"}, [
                {"customer": customer, "assembly": assembly, "side": "MES",
                 "name": names[0], "name2": names[1] if len(names) > 1 else "",
                 "alias": "", "sub_workcenter": None, "order": order, "value": qty,
                 "status": "unmapped", "source": source}
                for names, order, qty in sorted(steps, key=lambda x: (x[1], x[0][0]))]
        why = "absent_unverified" if cn in ctx.short else "absent"
        return {**r, "status": "not_in_iedb", "reason": why,
                "near_match": ctx.near(cn, a)}, [
            {"customer": customer, "assembly": assembly, "side": "MES",
             "name": names[0], "name2": names[1] if len(names) > 1 else "",
             "alias": "", "sub_workcenter": None, "order": order, "value": qty,
             "status": "unmapped", "source": source}
            for names, order, qty in sorted(steps, key=lambda x: (x[1], x[0][0]))]
    # resolve() falls back to a suffix / front-name match after an exact miss.
    # That is right for spelling differences ('AK-01-AKMCAC2-SUB' vs
    # 'AK01-AKMCAC2') and wrong for neighbouring part numbers: 810-495659-106C
    # has no cycle time of its own, matched 810-495659-106A, and was reported
    # COMPLETE off a route belonging to a different model. 15 LAM RESEARCH
    # models read that way, ~3,700 planner units behind them.
    #
    # If IEDB lists this EXACT name, it answers for itself or not at all.
    if akey != str(assembly).strip() and ctx.in_catalog_exact(cn, assembly):
        return {**r, "status": "no_cycle_time", "reason": "in_iedb_untimed"}, [
            {"customer": customer, "assembly": assembly, "side": "MES",
             "name": names[0], "name2": names[1] if len(names) > 1 else "",
             "alias": "", "sub_workcenter": None, "order": order, "value": qty,
             "status": "unmapped", "source": source}
            for names, order, qty in sorted(steps, key=lambda x: (x[1], x[0][0]))]

    if cn in _NON_MES:
        return {**r, "status": "not_in_mes", "reason": "workcell_not_on_mes",
                "actual_steps": 0, "source": "none"}, []

    ie = ctx.iedb(customer).get(akey)
    if ie is not None and ie["any_ct"] and not ie["ct_codes"]:
        # Cycle times ARE entered, but every row's alias is blank, so nothing can
        # be matched to the floor. An IEDB data-entry gap, not a missing cycle
        # time — saying "no cycle time" here would send someone to re-time a model
        # that is already fully timed.
        return {**r, "status": "incomplete", "reason": "no_alias"}, \
            _iedb_rows(customer, assembly, ie)
    if ie is None or not ie["ct_codes"]:
        # In IEDB's catalogue but not one cycle time entered — same practical hole
        # as not being there at all, so it lands in the same bucket.
        return {**r, "status": "not_in_iedb", "reason": "no_cycle_time"}, \
            _iedb_rows(customer, assembly, ie)
    if not steps:
        return {**r, "status": "not_in_mes", "reason": "no_production"}, \
            _iedb_rows(customer, assembly, ie)

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
    r["status"], r["reason"] = _verdict(counts)
    return r, rows + _iedb_rows(customer, assembly, ie)


def _verdict(counts: dict) -> tuple[str, str]:
    """(status, reason) from the per-step tally. Pure — see _selfcheck.

    COMPLETE means every step MES ran was checked and passed, so a step we could
    not even NAME has to count against it. Until 2026-08-16 this ignored
    `unmapped` and one matched step was enough to be called complete: 206 of
    1,139 complete rows in prod carried unmapped steps, worst being ELENION
    `3KC93830ACAA01Z1` at 2 matched / 39 unmapped — a model nobody had verified,
    showing green.

    `unmapped` keeps its OWN reason rather than folding into missing_ct /
    missing_step, because it is OUR gap (the naming bridge could not identify the
    step) not IEDB's (a cycle time is genuinely absent). The report and the FE
    both rely on that split — the FE renders it "steps unrecognised". Collapsing
    them would blame IEDB for our own mapping holes.
    """
    if counts["no_ct"] or counts["not_in_iedb"] or counts["unmapped"]:
        return "incomplete", (
            "missing_ct+step" if counts["no_ct"] and counts["not_in_iedb"]
            else "missing_ct" if counts["no_ct"]
            else "missing_step" if counts["not_in_iedb"]
            else "unmapped")
    if counts["present"]:
        return "complete", ""
    # Every step was excluded as non_iedb, or there were none. Nothing was
    # verified, so it cannot be complete. Unreachable in prod today (0 rows).
    return "incomplete", "unmapped"


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
            log.info("RESUME - %d customers done, %d models in v2 mart", len(done), len(summary))
        except Exception:
            log.warning("resume state unreadable - starting over")

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
                    log.warning("  #132 %s failed - %s", cust, ex)
        serial_models = set(bs["assembly"]) if len(bs) else set()

        # ── batch source (#21) — only needed for models with NO serial. Skip the
        #    (slow) per-customer pull entirely if every model already got #132.
        acc = {}
        if len(wanted - serial_models):
            try:
                acc = batch_steps(cust, cid, window)
            except MESWebApiError as ex:
                log.warning("  SKIP %s - #21 unreachable (%s); retry on re-run", cust, ex)
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
        log.info("  ok %-24s %4d models (%d serial / %d batch) | %d/%d customers",
                 cust, len(by_cust[cust]), n_serial, len(by_cust[cust]) - n_serial,
                 len(done), len(by_cust))

    if skipped:
        log.warning("finished with %d SKIPPED (retry on re-run): %s", len(skipped), ", ".join(skipped))
    else:
        state.unlink(missing_ok=True)
    df = pd.DataFrame(list(summary.values()))
    log.info("v2 COMPLETE: %d models in mart", len(df))
    return df


def cached_steps(customer: str) -> dict:
    """{assembly: {step: [order, qty]}} from the on-disk #21 cache ONLY — no API
    call, no network. The earlier classify() threw the MES route away for models
    IEDB does not list, so those rows are gone from the steps mart and the drawer
    had nothing to draw. The scans that produced them are still on disk, so the
    route can be rebuilt for free."""
    d = CT_MES_SCAN_DIR / _cnorm(customer)
    acc: dict = {}
    if not d.exists():
        return acc
    for p in d.glob("*.parquet"):
        try:
            df = pd.read_parquet(p)
        except Exception:
            continue
        for a, st, o, q in zip(df["assembly"], df["step"], df["order"], df["qty"]):
            e = acc.setdefault(a, {})
            if st in e:
                e[st][0] = min(e[st][0], int(o)); e[st][1] += int(q)
            else:
                e[st] = [int(o), int(q)]
    return acc


def reclassify() -> pd.DataFrame:
    """Re-judge every model already in the v2 marts, from the CACHED steps mart.

    Zero MES calls. The MES route was already pulled and stored — only the verdict
    changes — so tuning the match rule costs a couple of minutes instead of a
    re-scrape. Use this after any change to match_step/classify; use run() only
    when you actually need fresh MES data."""
    ctx = Ctx()
    st = pd.read_parquet(CT_MART["completion_steps_v2"])
    old = pd.read_parquet(CT_MART["completion_status_v2"])
    mes = st[st["side"] == "MES"]

    by: dict = {}
    for c, a, n, n2, o, v, src in zip(mes["customer"], mes["assembly"], mes["name"],
                                      mes["name2"], mes["order"], mes["value"], mes["source"]):
        names = [str(n)] + ([str(n2)] if str(n2) not in ("", "nan", "None") else [])
        by.setdefault((c, a), (src, []))[1].append((names, int(o), int(v)))

    summary, steps = {}, {}
    cache: dict = {}
    for c, a, src in zip(old["customer"], old["assembly"], old["source"]):
        sl_src, sl = by.get((c, a), (src, []))
        if not sl:
            # Nothing in the mart — try the raw scan cache before giving up.
            if c not in cache:
                cache[c] = cached_steps(c)
            hit = cache[c].get(str(a).strip()) or cache[c].get(a)
            if hit:
                sl = [([st], v[0], v[1]) for st, v in hit.items()]
                sl_src = "batch"
        summary[(c, a)], steps[(c, a)] = classify(ctx, c, a, sl, sl_src)
    _flush(summary, steps)
    df = pd.DataFrame(list(summary.values()))
    log.info("reclassified %d models: %s", len(df), df["status"].value_counts().to_dict())
    return df


def verify(df: pd.DataFrame = None) -> list:
    """Audit EVERY model's verdict against IEDB independently of how it was
    reached. Each rule is a claim the status makes; a hit is a verdict that
    contradicts the data. Run after any reclassify — a silent wrong verdict is
    worse than a crash, and `not_in_iedb` shipped three of these before it was
    caught by hand."""
    ctx = Ctx()
    if df is None:
        df = pd.read_parquet(CT_MART["completion_status_v2"])
    bad: list = []

    def flag(rule, r, detail=""):
        bad.append({"rule": rule, "customer": r["customer"], "assembly": r["assembly"],
                    "status": r["status"], "reason": r.get("reason", ""), "detail": detail})

    for r in df.to_dict("records"):
        c, a, cn = r["customer"], r["assembly"], _cnorm(r["customer"])
        st, why = r["status"], (r.get("reason") or "")
        resolved = ctx.resolve(c, a)
        in_cat = _anorm(str(a).strip()) in ctx.catalog.get(cn, {})

        # 1. "not in IEDB at all" must mean exactly that — in neither raw nor catalogue.
        if st == "not_in_iedb" and why == "absent" and (resolved or in_cat):
            flag("absent_but_present", r, f"resolves to {resolved or 'catalogue'}")
        # 2. The opposite: claimed found, but nothing in IEDB backs it.
        if st in ("complete", "incomplete") and not resolved:
            flag("judged_without_iedb", r)
        # 3. `complete` must have matched steps and no gaps of any kind.
        if st == "complete" and not (r.get("present", 0) > 0
                                     and not r.get("no_ct", 0) and not r.get("not_in_iedb", 0)):
            flag("complete_with_gaps", r, f"present={r.get('present')} no_ct={r.get('no_ct')}")
        # 4. `not_in_mes` claims zero production — steps would contradict it.
        if st == "not_in_mes" and r.get("actual_steps", 0):
            flag("no_mes_but_has_steps", r, f"actual_steps={r.get('actual_steps')}")
        # 5. `no_cycle_time` claims none entered; a timed route contradicts it.
        if why == "no_cycle_time" and resolved:
            ie = ctx.iedb(c).get(resolved)
            if ie and ie["ct_codes"]:
                flag("no_ct_but_timed", r, f"{len(ie['ct_codes'])} timed aliases")
        # 6. Every reason must belong to its status.
        if why and why not in REASONS.get(st, ()):
            flag("reason_not_valid_for_status", r, why)

    if bad:
        b = pd.DataFrame(bad)
        log.warning("VERIFY: %d contradictions\n%s", len(b),
                    b.groupby("rule").size().to_string())
    else:
        log.info("VERIFY: %d models, no contradictions", len(df))
    return bad


def breakdown(df: pd.DataFrame = None) -> None:
    """Print the summary report: models per status/reason, per workcell, and the
    ranked alias codes behind the gaps — that last list is the actual fix-list."""
    import collections
    if df is None:
        df = pd.read_parquet(CT_MART["completion_status_v2"])
    order = ["incomplete", "no_cycle_time", "not_in_iedb", "not_in_mes", "complete"]

    print(f"\n{len(df)} models\n")
    for s in order:
        g = df[df["status"] == s]
        if not len(g):
            continue
        print(f"{s:<14}{len(g):>6}  ({100*len(g)//len(df)}%)")
        for reason, n in g["reason"].fillna("").value_counts().items():
            print(f"    {str(reason) or '-':<20}{n:>6}")

    print(f"\n{'workcell':<26}" + "".join(f"{s[:9]:>11}" for s in order))
    piv = df.pivot_table(index="customer", columns="status", values="assembly",
                         aggfunc="count", fill_value=0)
    piv["_bad"] = sum(piv[s] for s in order[:3] if s in piv)
    for cust, row in piv.sort_values("_bad", ascending=False).head(20).iterrows():
        print(f"{str(cust):<26}" + "".join(f"{int(row.get(s, 0)):>11}" for s in order))

    inc = df[df["status"] == "incomplete"]
    codes = collections.Counter()
    for col in ("missing_ct", "gap_steps"):
        for v in inc[col].fillna(""):
            codes.update(x.strip() for x in str(v).split(";") if x.strip())
    if codes:
        print(f"\nTOP GAPS - alias codes missing across {len(inc)} incomplete models:")
        for a, n in codes.most_common(20):
            print(f"  {n:>6}  {a}")


def _selfcheck():
    """The match ladder is the whole point of v2 — check every rung."""
    # ── the code parser ──────────────────────────────────────────────────────
    assert _code("MA 2 - BACK MECH ASSY 1") == _code("MA 1 - BACK MECH ASSY 1") == "MA"
    assert _code("TSTH 1 - TSTH1") == _code("TSTH 1  - TSTH TOP") == "TSTH"   # 27819-M
    assert _code("PACKOUT 1") == _code("PACKOUT - PACKOUT") == "PACKOUT"      # bare vs compound
    assert _code("MA 2.1") == _code("MA 1/2") == "MA"                         # instance forms
    assert _code("LINK (AOP) 1") == _code("LINK AOP 1") == "LINKAOP"          # punctuation
    assert _code("SCRB 1 - SCRT01") != _code("SCRT 1 - SCRT01")               # must NOT fuse

    # ── assembly suffix rule: narrow on purpose ─────────────────────────────
    for src, want in [("26432-N-RMA", "26432-N"), ("26432-N", "26432-N"),
                      ("M011872C001-F-RMA", "M011872C001-F"),
                      ("20049553-A.00-FRU", "20049553-A.00"),
                      ("AK-01-AKMCAC2-SUB", "AK-01-AKMCAC2"),
                      ("AK06-CAJ9D3C6HTSUB", "AK06-CAJ9D3C6HT"),   # SUB, no separator
                      ("853-800575-301-OP1", "853-800575-301"),
                      ("853-238767-003-S1", "853-238767-003"),
                      ("853-238767-003-FA", "853-238767-003"),
                      ("SKY900-212127-000S", "SKY900-212127-000")]:  # bare S after a digit
        assert _desuffix(src) == want.upper(), (src, _desuffix(src))
    # Must survive untouched. The trailing letters LOOK like revisions and are
    # not — IEDB carries revision separately ('', 'D', 'A01' for these three) and
    # EK050-66401 is its own live part. "AK11-CAP-9D3C6-TS" is the trap: a bare
    # trailing S after a LETTER, where the stripped form is a different real part.
    for keep in ("AK11-CAP-9D3C6-TS", "EK050-66401N", "IN300-1074-203SDZ",
                 "IN800-0673-201A1F", "853-064887-010-OPT", "SKYASB1097381-SFT",
                 "EK041-66401Q"):
        assert _desuffix(keep) == keep.upper(), (keep, _desuffix(keep))

    class C:  # stand-in Ctx: only pmap/pknown are read by match_step
        pmap = {("CUST", "PACKOUT LINK"): "PACKOUT 1 - PACKOUT",
                ("CUST", "AOI TOP"): "AOIT 1",
                ("CUST", "BACK MECH ASSY 1"): "MA 2 - BACK MECH ASSY 1",
                ("CUST", "HLA 1 LINK"): "PMI 1",
                ("CUST", "GHOST"): "NOSUCH 1", ("CUST", "DEAD"): "OLD 1"}
        pknown = set(pmap) | {("CUST", "UNLINK")}
    ie = {"ct_codes": {"PACKOUT", "AOIT", "SCRT", "MA"},
          "all_codes": {"PACKOUT", "AOIT", "SCRT", "MA", "OLD"},
          "ct_names": {"XRAY"}, "all_names": {"XRAY", "DEPANEL"}}
    m = lambda names: match_step(C(), "CUST", ie, names)

    assert m(["PACKOUT LINK"])[0] == "present"        # compound workbook vs code
    assert m(["AOI TOP"])[0] == "present"             # bare workbook code
    assert m(["BACK MECH ASSY 1"])[0] == "present"    # R380: MA 2 vs MA 1, names differ
    assert m(["DEAD"])[0] == "no_ct"                  # in route, cycle time empty
    assert m(["GHOST"])[0] == "not_in_iedb"           # mapped, route lacks it
    assert m(["UNLINK"])[0] == "non_iedb"             # workbook says not IEDB
    assert m(["SCRT01"])[0] == "present"              # unmapped -> code auto-match
    assert m(["XRAY"])[0] == "present"                # unmapped -> alias display name
    assert m(["WHO KNOWS"])[0] == "unmapped"          # nothing knows it
    assert m(["ZZZ", "SCRT01"])[0] == "present"       # #132 second name saves it

    # ── THE GUARD ───────────────────────────────────────────────────────────
    # `PMI 1` is not in the route, so this is a gap. The step's own words contain
    # "XRAY", which IS in the route. A mapped step must NOT be rescued by them —
    # this exact fishing is what produced 1,377 false greens.
    assert m(["HLA 1 LINK", "XRAY"]) == ("not_in_iedb", "PMI 1"), m(["HLA 1 LINK", "XRAY"])
    # And the IEDB `process` label is never a key at all: a step named exactly
    # after one can only match if the ALIAS backs it up.
    assert m(["ASSEMBLY 2"])[0] == "unmapped"
    print("match ladder OK")

    # ── COMPLETE <=> nothing left unchecked ─────────────────────────────────
    # The 16 Aug fix. A model is complete only when every step MES ran was both
    # named AND timed. Delete `or counts["unmapped"]` in _verdict and the third
    # assert fails — that one line was worth 206 false greens in prod.
    v = lambda **k: _verdict({"present": 0, "no_ct": 0, "not_in_iedb": 0,
                              "unmapped": 0, "non_iedb": 0, **k})
    assert v(present=17) == ("complete", "")
    assert v(present=17, non_iedb=3) == ("complete", "")      # excluded by design, harmless
    assert v(present=17, unmapped=1) == ("incomplete", "unmapped")   # 810-028298-005C
    assert v(present=2, unmapped=39) == ("incomplete", "unmapped")   # ELENION
    assert v(present=10, no_ct=2) == ("incomplete", "missing_ct")
    assert v(present=10, not_in_iedb=2) == ("incomplete", "missing_step")
    assert v(present=10, no_ct=1, not_in_iedb=2) == ("incomplete", "missing_ct+step")
    # IEDB's gap outranks ours: a real missing time is the headline, not our
    # inability to name some other step.
    assert v(present=10, no_ct=1, unmapped=5) == ("incomplete", "missing_ct")
    assert v(non_iedb=4) == ("incomplete", "unmapped")        # nothing verified at all
    for st, rs in [v(present=1), v(present=1, unmapped=1), v(present=1, no_ct=1)]:
        assert rs in REASONS[st], (st, rs)                    # reason stays in vocabulary
    print("verdict rule OK")

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
    print(f"#132 breaker OK - stopped after {len(calls)} calls, not 1500")


if __name__ == "__main__":
    _selfcheck()
