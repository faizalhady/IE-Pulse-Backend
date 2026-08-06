"""
mes_recheck.py — re-probe the MES WebAPI read surface and report what changed.

    python scripts/mes_recheck.py                 # full re-check, compare to last run
    python scripts/mes_recheck.py --quick         # skip the slow variant passes
    python scripts/mes_recheck.py --only Batch    # one controller folder

WHY THIS EXISTS
    MES fails in BURSTS: an endpoint can 404 five times in a row and return 389
    rows minutes later. So no single run sees the whole surface — the true set is
    the UNION of runs over time. This script re-runs the campaign, merges with
    everything seen before, and prints the delta.

SAFETY (non-negotiable)
    Read-only by construction. It refuses:
      * every endpoint the Postman collection marks with a warning emoji (46)
      * an explicit list of 6 named write actions
    It never creates, edits, prints, moves or scraps anything.

WHAT IT TRIES, in order, per endpoint
    1. the example body, with empty params filled from real seeds
    2. `%` instead of "" for filter-ish params      (empty filters are rejected)
    3. ISO-Z datetimes                              (yyyy-MM-ddTHH:mm:ss.fffZ)
    4. every param filled from ONE joined row       (ids must belong together)

Results accumulate in data/mes_surface.json — delete it to start fresh.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from modules.cycle_time.config import (MES_WEBAPI_BASE,  # noqa: E402
                                       MES_WEBAPI_KEY)

COLLECTION = ROOT / "docs" / "MES" / "MESWebApi.postman_collection.json"
STATE = ROOT / "data" / "mes_surface.json"
MART = ROOT / "data" / "mart" / "cycle_time"

NEVER_CALL = {
    "ScrapWip", "WipBatchPulling", "WipBatchPullingReversal",
    "OKToTestLinkMaterial", "OkToTest_Breakout", "RouteStepSetupValidation",
}
FILTERISH = ("partialkey", "filter", "name", "text", "descr", "key", "search", "mask", "fmaroute")
DATEISH = ("time", "date", "when", "start", "end", "after", "before")

SESSION = requests.Session()
norm = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())


# ─── plumbing ────────────────────────────────────────────────────────────────

def call(controller: str, method: str, body: dict, timeout: int = 45):
    r = SESSION.post(f"{MES_WEBAPI_BASE}/{controller}/{method}", json=body,
                     headers={"APIKey": MES_WEBAPI_KEY, "Accept": "application/json"},
                     timeout=timeout)
    try:
        d = r.json()
    except Exception:
        return r.status_code, None
    rows = d if isinstance(d, list) else ((d or {}).get("Data") or d)
    return r.status_code, rows


def rowlist(rows):
    if isinstance(rows, list):
        return [x for x in rows if isinstance(x, dict)]
    if isinstance(rows, dict) and "ExceptionMessage" not in rows:
        return [rows]
    return []


def endpoints():
    d = json.loads(COLLECTION.read_text(encoding="utf-8"))

    def walk(items, path=""):
        out = []
        for it in items:
            if "item" in it:
                out += walk(it["item"], f"{path}/{it.get('name','')}")
            else:
                out.append((path.strip("/"), it))
        return out

    eps, seen = [], set()
    for folder, it in walk(d.get("item", [])):
        raw = it.get("name", "")
        name = re.sub(r"^\s*\d+\.\s*", "", raw).strip()
        if "⚠" in raw or name in NEVER_CALL:
            continue
        req = it.get("request", {})
        url = req.get("url", {})
        raw_url = url.get("raw", "") if isinstance(url, dict) else str(url)
        body = {}
        b = req.get("body", {})
        if b.get("mode") == "raw" and b.get("raw"):
            try:
                body = json.loads(b["raw"])
            except Exception:
                body = {}
        parts = [p for p in raw_url.split("?")[0].split("/") if p and "{{" not in p]
        ctrl, meth = (parts[-2], parts[-1]) if len(parts) >= 2 else ("", name)
        key = (ctrl, meth)
        if key in seen:
            continue
        seen.add(key)
        eps.append({"folder": folder, "controller": ctrl, "method": meth, "body": body})
    return eps


# ─── seeds ───────────────────────────────────────────────────────────────────

def seed_pool():
    """Base seeds from our mart + a live serial, then whatever we can chain."""
    now = dt.datetime.now()
    s = {"langid": "0", "active": "1", "maxcount": "50", "partialkey": "%",
         "usrid": "142", "userid": "142",
         "starttime": (now - dt.timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
         "endtime": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
         "updatedafter": (now - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
         "expendtime": (now + dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    try:
        import duckdb
        am = duckdb.connect().execute(
            f"SELECT number, revision, assembly_id, customer_id "
            f"FROM read_parquet('{(MART / 'mes_assembly_map.parquet').as_posix()}') "
            f"WHERE assembly_id IS NOT NULL LIMIT 1").fetchone()
        if am:
            s.update({"number": am[0], "assemblynumber": am[0],
                      "revision": am[1], "assemblyrevision": am[1],
                      "assemid": str(am[2]), "assemblyid": str(am[2]),
                      "custid": str(am[3]), "customerid": str(am[3])})
    except Exception as e:
        print(f"  (mart seeds unavailable: {str(e)[:60]})")
    try:
        _, rows = call("Test", "ListTestDataWithinTime",
                       {"startTime": s["starttime"], "endTime": s["endtime"], "maxCount": "40"})
        rl = rowlist(rows)
        if rl:
            s.update({"serialnumber": rl[0].get("SerialNumber", ""),
                      "serial": rl[0].get("SerialNumber", ""),
                      "customername": rl[0].get("Customer", ""),
                      "division": rl[0].get("Division", "")})
            print(f"  live serial seeded from {rl[0].get('Customer')}")
    except Exception:
        print("  (no live serial this run)")
    return s


def joined_rows(seeds, limit=400):
    """Whole rows from join endpoints — ids on one row belong together."""
    pool = []
    for ctrl, meth in (("EquipmentSetup", "GetActiveEquipmentSetupByAssemblyId"),
                       ("Wip", "ListWipRouteStepBySerial"),
                       ("Route", "ListRouteStep"),
                       ("Wip", "BoardHistoryReport")):
        ep = next((e for e in endpoints() if e["method"] == meth), None)
        if not ep:
            continue
        body = {k: (seeds.get(norm(k), v) if isinstance(v, str) and not v.strip() else v)
                for k, v in ep["body"].items()}
        try:
            _, rows = call(ctrl, meth, body, timeout=90)
            for r in rowlist(rows)[:limit]:
                flat = {norm(k): str(v) for k, v in r.items()
                        if v not in (None, "", 0, "0") and not isinstance(v, (dict, list))}
                if flat:
                    pool.append(flat)
        except Exception:
            pass
    return pool


ALIASES = {"equipid": ("equipmentid", "equipid"), "assemid": ("assemblyid", "assemid"),
           "custid": ("customerid", "custid"), "setupid": ("equipmentsetupid", "setupid"),
           "batchid": ("batchid", "batchidid"), "wipid": ("wipid",)}


def bodies(body, seeds, rows):
    """Yield the body variants to try, cheapest first."""
    base = {}
    for k, v in body.items():
        base[k] = seeds.get(norm(k), v) if isinstance(v, str) and v.strip() in ("", "0", "string") else v
    yield base

    pct = dict(base)
    if any(isinstance(v, str) and not v.strip() and any(f in norm(k) for f in FILTERISH)
           for k, v in body.items()):
        for k, v in body.items():
            if isinstance(v, str) and not v.strip() and any(f in norm(k) for f in FILTERISH):
                pct[k] = "%"
        yield pct

    iso = dict(base)
    if any(any(d in norm(k) for d in DATEISH) for k in body):
        now = dt.datetime.utcnow()
        for k in body:
            if any(d in norm(k) for d in DATEISH):
                iso[k] = (now if ("end" in norm(k) or "to" in norm(k))
                          else now - dt.timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        yield iso

    scored = []
    for row in rows:
        n = sum(1 for k in body
                if any(row.get(a) for a in ALIASES.get(norm(k), (norm(k),))))
        if n:
            scored.append((n, row))
    scored.sort(key=lambda x: -x[0])
    for _, row in scored[:4]:
        out = dict(base)
        for k, v in body.items():
            if isinstance(v, str) and v.strip() in ("", "0", "string"):
                for a in ALIASES.get(norm(k), (norm(k),)):
                    if row.get(a):
                        out[k] = row[a]
                        break
        yield out


# ─── main ────────────────────────────────────────────────────────────────────

BOGUS = "999999999"


def control_test(ctrl, meth, body, real_rows):
    """Does this endpoint actually FILTER, or does it ignore the id we pass?

    Twice tonight an endpoint accepted an id and returned the same rows no matter
    what — `ListBatchByAssembly` gave an identical 400 rows for a real assembly,
    a nonsense one, and an empty one. That reads as success and silently produces
    a "filter" that filters nothing.

    Returns: 'filters' | 'decorative' | 'inconclusive'

    NOTE the asymmetry: two identical FAILURES prove nothing (both may be the
    burst 404), so a verdict is only issued when the real call actually returned
    rows.
    """
    idkeys = [k for k in body if norm(k).endswith("id") and norm(k) != "langid"]
    if not idkeys or not real_rows:
        return "inconclusive", None
    bogus = dict(body)
    for k in idkeys:
        bogus[k] = BOGUS
    try:
        code, rows = call(ctrl, meth, bogus)
    except Exception:
        return "inconclusive", None
    rl = rowlist(rows)
    if code != 200 or not rl:
        return "filters", idkeys              # bogus id rejected — good
    if len(rl) == len(real_rows):
        a = json.dumps(rl[0], sort_keys=True, default=str)[:400]
        b = json.dumps(real_rows[0], sort_keys=True, default=str)[:400]
        if a == b:
            return "decorative", idkeys       # same answer for a bogus id
    return "filters", idkeys


def main():
    ap = argparse.ArgumentParser(description="Re-probe the MES read surface")
    ap.add_argument("--quick", action="store_true", help="baseline body only")
    ap.add_argument("--only", help="limit to one controller folder, e.g. Batch")
    ap.add_argument("--no-control", action="store_true",
                    help="skip the does-it-actually-filter check")
    args = ap.parse_args()

    prev = {}
    if STATE.exists():
        prev = json.loads(STATE.read_text(encoding="utf-8")).get("endpoints", {})
    print(f"previous state: {sum(1 for v in prev.values() if v.get('ok'))} known working\n")

    print("building seeds...")
    seeds = seed_pool()
    rows = [] if args.quick else joined_rows(seeds)
    print(f"  seed keys={len(seeds)}  joined rows={len(rows)}\n")

    eps = endpoints()
    if args.only:
        eps = [e for e in eps if e["folder"].lower() == args.only.lower()]
    print(f"probing {len(eps)} endpoints...\n")

    now_state, newly, lost = {}, [], []
    for i, e in enumerate(eps, 1):
        key = f"{e['controller']}/{e['method']}"
        rec = {"folder": e["folder"], "ok": False, "rows": 0}
        for body in ([next(bodies(e["body"], seeds, rows))] if args.quick
                     else bodies(e["body"], seeds, rows)):
            try:
                code, resp = call(e["controller"], e["method"], body)
                rl = rowlist(resp)
                if code == 200 and rl:
                    rec = {"folder": e["folder"], "ok": True, "rows": len(rl),
                           "columns": list(rl[0].keys()), "http": 200}
                    if not args.no_control:
                        verdict, keys = control_test(e["controller"], e["method"], body, rl)
                        rec["filters"] = verdict
                        if keys:
                            rec["id_params"] = keys
                        time.sleep(0.3)
                    break
                rec["http"] = code
                if isinstance(resp, dict) and "ExceptionMessage" in resp:
                    rec["err"] = str(resp["ExceptionMessage"])[:150]
            except Exception as ex:
                rec["err"] = f"{type(ex).__name__}: {str(ex)[:90]}"
            time.sleep(0.3)
        now_state[key] = rec
        was = prev.get(key, {}).get("ok")
        if rec["ok"] and not was:
            newly.append(key)
        elif was and not rec["ok"]:
            lost.append(key)
        mark = "OK " if rec["ok"] else "   "
        print(f"[{i:3d}/{len(eps)}] {mark} {key[:52]:52s} rows={rec['rows']}", flush=True)

    # merge: once seen working, stay working (burst failures are not evidence of death)
    merged = dict(prev)
    for k, v in now_state.items():
        if v["ok"] or k not in merged:
            merged[k] = v
        else:
            merged[k]["last_failed"] = dt.datetime.now().isoformat(timespec="seconds")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(
        {"updated": dt.datetime.now().isoformat(timespec="seconds"),
         "endpoints": merged}, indent=1), encoding="utf-8")

    ever = sum(1 for v in merged.values() if v.get("ok"))
    print("\n" + "=" * 66)
    print(f"THIS RUN : {sum(1 for v in now_state.values() if v['ok'])} working")
    print(f"EVER SEEN: {ever} working  (union across all runs)")
    if newly:
        print(f"\nNEWLY WORKING ({len(newly)}):")
        for k in newly:
            print("  + " + k)
    if lost:
        print(f"\nFAILED THIS RUN but seen working before ({len(lost)}) "
              f"- burst failure, not death:")
        for k in lost[:12]:
            print("  ~ " + k)

    decorative = [k for k, v in now_state.items() if v.get("filters") == "decorative"]
    if decorative:
        print(f"\n!  IGNORE THEIR ID PARAM ({len(decorative)}) - same rows for a bogus id.")
        print("    Do NOT build a filter on these; they return everything regardless:")
        for k in decorative:
            print(f"  ! {k}  (params: {', '.join(now_state[k].get('id_params', []))})")
    print(f"\nstate -> {STATE}")


if __name__ == "__main__":
    main()
