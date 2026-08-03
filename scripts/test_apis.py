"""Probe EVERY MES endpoint the completion-status pipeline uses, one at a time,
and write the verdicts to docs/MES/ENDPOINT_CONTRACT.md. Read-only.

Run this BEFORE any real pull. The v2 run on 2026-07-24 burned 3 hours because
nobody checked that #94's custId was a different id space -- 38/38 customers came
back 'Invalid custId'. One probe would have caught it in 5 seconds.

    python scripts/test_apis.py            # all endpoints
    python scripts/test_apis.py --quick    # skip the slow per-assembly route walk

Each probe answers three questions:
  1. Does it work at all?
  2. Which id does it take, and does a BOGUS id correctly return nothing?
     (an endpoint that ignores the id and returns everything is worse than one
      that errors -- it silently gives you the whole site as "one customer")
  3. What is the shape/limit -- window cap, required wildcards, missing fields?
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules.cycle_time.config import CT_MART
from modules.cycle_time.mes_webapi import post, MESWebApiError

sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 200)

DOC = Path(__file__).resolve().parents[1] / "docs" / "MES" / "ENDPOINT_CONTRACT.md"
BOGUS = "999999"
results: list[dict] = []          # one row per endpoint -> the contract table


def rec(ep, param, verdict, ok, note=""):
    results.append({"endpoint": ep, "id_param": param, "verdict": verdict,
                    "ok": ok, "note": note})
    print(f"  -> {'PASS' if ok else 'FAIL'}: {verdict}")
    if note:
        print(f"     note: {note}")


def head(t):
    print(f"\n### {t}\n" + "-" * 78)


def probe(ep, controller, method, body, param="-"):
    """Call once, return (rows, error). Never raises -- a dead endpoint is a result."""
    try:
        return post(controller, method, body), None
    except MESWebApiError as e:
        return None, str(e)[:90]


# ── the id source every probe below draws from ───────────────────────────────
def real_ids() -> list[tuple[str, str]]:
    """(customer, mes_customer_id) for a small and a large workcell, from the mart
    -- NOT hardcoded. Hardcoding 59/7 is how we got here."""
    amap = pd.read_parquet(CT_MART["mes_assembly_map"])
    size = amap.groupby("customer")["number"].nunique().sort_values()
    picks = [size.index[0], size.index[len(size) // 2], size.index[-1]]
    out = []
    for c in picks:
        cid = amap.loc[amap["customer"] == c, "customer_id"].iloc[0]
        out.append((c, str(int(float(cid)))))
    return out


# ═════════════════════════════════════════════════════════════════════════════
def t_customer():
    head("Customer/ListCustomer — the id SOURCE (mes_assembly_map, workcell_map)")
    rows, err = probe("Customer/ListCustomer", "Customer", "ListCustomer",
                      {"partialKey": "", "active": "1", "langId": "0"})
    if err:
        return rec("Customer/ListCustomer", "none", f"FAILED — {err}", False)
    d = pd.DataFrame(rows)
    print(f"  {len(d)} customers | fields: {list(d.columns)[:8]}")
    rec("Customer/ListCustomer", "none (partialKey)",
        f"{len(d)} active customers; Customer_ID is the id space for #21/#132", True,
        "blank partialKey is accepted here (unlike fmaRoute/step which need '%')")


def t_assembly(ids):
    head("Assembly/ListAssembly — name -> Assembly_ID (mes_assembly_map)")
    cust, cid = ids[1]
    rows, err = probe("Assembly/ListAssembly", "Assembly", "ListAssembly",
                      {"custId": cid, "partialKey": "", "langId": "0"})
    if err:
        return rec("Assembly/ListAssembly", "custId", f"FAILED — {err}", False)
    d = pd.DataFrame(rows)
    bog, _ = probe("x", "Assembly", "ListAssembly",
                   {"custId": BOGUS, "partialKey": "", "langId": "0"})
    nb = len(bog) if bog else 0
    print(f"  {cust} (id={cid}): {len(d)} assemblies | bogus id: {nb} rows")
    rec("Assembly/ListAssembly", "custId",
        f"{cust}={len(d)} rows, bogus={nb}", nb == 0,
        "SAME id space as Customer_ID" if nb == 0 else "IGNORES custId — do not trust")


def t21(ids):
    head("#21 Batch/ListBatchCountsByRouteStep — the BATCH step source")
    day = (datetime.now() - timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
    win = {"StartDate": day.strftime("%Y-%m-%d 00:00:00"),
           "EndDate": (day + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")}
    seen = []
    for cust, cid in ids:
        rows, err = probe("x", "Batch", "ListBatchCountsByRouteStep", {"CustomerID": cid, **win})
        n = len(rows) if rows else 0
        a = pd.DataFrame(rows).Assembly.str.split("/").str[0].str.strip().nunique() if n else 0
        seen.append(a)
        print(f"  {cust:<24} id={cid:<6} {n:6d} rows  {a:4d} assemblies  {err or ''}")
    bog, _ = probe("x", "Batch", "ListBatchCountsByRouteStep", {"CustomerID": BOGUS, **win})
    nb = len(bog) if bog else 0
    print(f"  {'bogus':<24} id={BOGUS:<6} {nb:6d} rows")
    ok = nb == 0 and max(seen) > 0
    rec("#21 Batch/ListBatchCountsByRouteStep", "CustomerID",
        f"per-customer, 1-day window; bogus={nb} rows", ok,
        "MUST be called per customer per day. A site-wide call is the 2026-07-24 bug "
        "(29 assemblies for the whole site over 120 days).")


def t126():
    head("#126 Test/ListTestDataWithinTime — the SERIAL source")
    s = (datetime.now() - timedelta(days=45)).replace(hour=10, minute=0, second=0, microsecond=0)
    rows, err = probe("x", "Test", "ListTestDataWithinTime",
                      {"startTime": s.strftime("%Y-%m-%d %H:%M:%S"),
                       "endTime": (s + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
                       "maxCount": "100000"})
    if err:
        return rec("#126 Test/ListTestDataWithinTime", "none (time)", f"FAILED — {err}", False)
    d = pd.DataFrame(rows)
    print(f"  {s:%Y-%m-%d %H:%M} +30m -> {len(d)} rows, {d.Assembly.nunique()} models, "
          f"{d.SerialNumber.nunique()} serials, {d.Customer.nunique()} customers")
    steps = d.StepText.value_counts()
    print(f"  StepText: {steps.head(10).to_dict()}")
    has_pack = d.StepText.str.upper().str.contains("PACK", na=False).any()
    print(f"  contains PACKOUT? {has_pack}   <-- if False, test-station models ONLY")
    # window cap
    _, e45 = probe("x", "Test", "ListTestDataWithinTime",
                   {"startTime": s.strftime("%Y-%m-%d %H:%M:%S"),
                    "endTime": (s + timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S"),
                    "maxCount": "100"})
    print(f"  45-min window: {'REJECTED' if e45 else 'ACCEPTED'}")
    rec("#126 Test/ListTestDataWithinTime", "none — time window only",
        f"{len(d)} rows/30min, {d.Customer.nunique()} customers; 45min={'rejected' if e45 else 'ok'}",
        len(d) > 0,
        f"TEST STEPS ONLY (PACKOUT present={has_pack}). Models that never hit a test "
        "station get NO serial and must fall back to #21. 30-min window is a hard cap.")
    return d


def t132(d, ids):
    head("#132 Wip/BoardHistoryReport — serial -> full journey (completion PROOF)")
    if d is None or not len(d):
        return rec("#132 Wip/BoardHistoryReport", "custId + serial", "SKIPPED — no serials", False)
    fin = ("PACKOUT", "PACK OUT", "OQA", "OBA", "SHIP")
    tried = done = 0
    for cust, cid in ids:
        sub = d[d.Customer.astype(str).str.upper().str.startswith(str(cust).upper()[:5])]
        for sn in sub.SerialNumber.drop_duplicates().head(2):
            rows, err = probe("x", "Wip", "BoardHistoryReport",
                              {"custId": cid, "serial": str(sn),
                               "useMultiPartBarCode": "", "lang": ""})
            tried += 1
            if err:
                print(f"  {cust:<20} {sn[:24]:<26} FAIL {err[:40]}")
                continue
            h = pd.DataFrame(rows)
            if not len(h):
                print(f"  {cust:<20} {sn[:24]:<26} 0 rows (no history)")
                continue
            tp = h["Test_Process"].dropna().astype(str)
            reach = tp.str.upper().str.contains("|".join(fin), na=False).any()
            done += reach
            print(f"  {cust:<20} {sn[:24]:<26} {len(h):3d} rows  finished={reach}")
    bog, ebog = probe("x", "Wip", "BoardHistoryReport",
                      {"custId": BOGUS, "serial": "NOTASERIAL", "useMultiPartBarCode": "", "lang": ""})
    nb = len(bog) if bog else 0
    print(f"  bogus custId+serial -> {nb} rows {('(' + ebog[:40] + ')') if ebog else ''}")
    rec("#132 Wip/BoardHistoryReport", "custId + serial",
        f"{done}/{tried} probe serials reached a final step; bogus={nb}", done > 0,
        "Completion is proven HERE, not by #126. finished = journey reaches "
        f"{'/'.join(fin)}. Retry loop needs a cap — an intermittent 404 hung the "
        "2026-07-24 run for 3 hours.")


def t_route(ids, quick):
    head("Assembly/ListAssemblyRouteByAssembly + Route/ListRouteStepByFactoryMARoute")
    if quick:
        return rec("Route/* (2-call chain)", "assemId / fmaRouteId", "SKIPPED (--quick)", True)
    amap = pd.read_parquet(CT_MART["mes_assembly_map"])
    cust = ids[1][0]
    row = amap[amap["customer"] == cust].iloc[0]
    aid = str(int(float(row["assembly_id"])))
    print(f"  using {cust} / {row['number']} assembly_id={aid}")

    # blank vs '%' — the documented gotcha
    _, e_blank = probe("x", "Assembly", "ListAssemblyRouteByAssembly",
                       {"assemId": aid, "fmaRoute": "", "langId": "0"})
    rows, err = probe("x", "Assembly", "ListAssemblyRouteByAssembly",
                      {"assemId": aid, "fmaRoute": "%", "langId": "0"})
    print(f"  fmaRoute='' -> {'REJECTED' if e_blank else 'accepted'} | "
          f"fmaRoute='%' -> {len(rows) if rows else 0} routes {err or ''}")
    if not rows:
        return rec("Assembly/ListAssemblyRouteByAssembly", "assemId",
                   f"no routes for {cust} ({err})", False)
    rid = str(rows[0].get("FactoryMARoute_ID"))
    steps, err2 = probe("x", "Route", "ListRouteStepByFactoryMARoute",
                        {"fmaRouteId": rid, "step": "%", "langId": "0"})
    n = len(steps) if steps else 0
    print(f"  route {rid} -> {n} steps {err2 or ''}")
    if n:
        s = pd.DataFrame(steps)
        for c in ("StepType", "WorkCenter_ID"):
            if c in s:
                print(f"  {c}: {s[c].unique()[:5]}  <-- 0 means unusable for filtering")
    rec("Route/* (2-call chain)", "assemId -> fmaRouteId",
        f"{len(rows)} routes -> {n} steps", n > 0,
        "BOTH calls need '%' where docs show \"\" — a blank raises 500 'Invalid "
        "fmaRoute'. StepType/WorkCenter_ID come back 0, so filter on StepName only.")


# NOT probed: #94 Reporting/AgingWipsReportByCustomer. No pipeline code calls it.
# It is LIVE WIP, so every serial it returns is by definition UNFINISHED and can
# never prove a route ran end to end — #126 replaced it outright. It would be the
# right source for a WIP-aging feature, which is not this pipeline. Its 2026-07-24
# 'Invalid custId' storm was a float id ("59.0"), not the endpoint; that is guarded
# in mes_webapi._clean_ids and asserted in its self-check.


def write_doc():
    DOC.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# MES Endpoint Contract — completion-status pipeline",
        "",
        f"Generated by `scripts/test_apis.py` on {datetime.now():%Y-%m-%d %H:%M}. Re-run "
        "this before any large pull; do not edit by hand.",
        "",
        "| Endpoint | Id param | Verdict | OK |",
        "|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| `{r['endpoint']}` | `{r['id_param']}` | {r['verdict']} | "
                     f"{'PASS' if r['ok'] else 'FAIL'} |")
    lines += ["", "## Notes", ""]
    for r in results:
        if r["note"]:
            lines.append(f"- **{r['endpoint']}** — {r['note']}")
    lines += ["", "## Pull order", "",
              "Every MES pull loops workcells **smallest first** "
              "(`workcell_map.parquet.pull_order`). Small workcells surface a broken "
              "param in seconds; the giants go last so an interruption never costs the "
              "whole run.", ""]
    DOC.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {DOC}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="skip the per-assembly route walk")
    a = ap.parse_args()

    ids = real_ids()
    print(f"probing with real ids from mes_assembly_map: {ids}")
    t_customer()
    t_assembly(ids)
    t21(ids)
    d = t126()
    t132(d, ids)
    t_route(ids, a.quick)

    print("\n" + "=" * 78)
    bad = [r for r in results if not r["ok"]]
    for r in results:
        print(f"  {'PASS' if r['ok'] else 'FAIL'}  {r['endpoint']:<48} {r['id_param']}")
    print(f"{len(results) - len(bad)}/{len(results)} endpoints usable")
    write_doc()


if __name__ == "__main__":
    main()
