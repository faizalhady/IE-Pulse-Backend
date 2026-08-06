"""
mes_crack.py — targeted assault on the endpoints still failing.

Earlier passes guessed. This one reads what each endpoint ACTUALLY complained
about and feeds it the right column from the right source, trying several real
candidate values each.

Mistakes this fixes:
  * batchId  — I sent BatchID ("M067G3126", a string). It wants BatchID_ID (int).
  * StepInstance — it is RouteStep.Description, NOT StepName (per the vault).
  * routeStepId — the first ListRouteStep rows carry RouteStep_ID = 0; must pick
    non-zero ones.
  * equipId  — Equipment_ID, sourced from a row that actually uses that equipment.
  * panelId  — Panel_ID from ListPanelSerialByWipId, not a serial.

Read-only.
"""
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).parent))

from mes_sweep import (base_seeds, call, endpoints, is_error, norm,  # noqa: E402
                       rowlist)

HERE = Path(__file__).parent
OUT = HERE / "mes_crack_results.json"

# param (normalised) -> list of (controller, method, column) to source it from
SOURCES = {
    "routestepid":     [("Route", "ListRouteStep", "RouteStep_ID"),
                        ("Wip", "ListWipRouteStepBySerial", "RouteStep_ID"),
                        ("EquipmentSetup", "GetActiveEquipmentSetupByAssemblyId", "RouteStep_ID")],
    "batchid":         [("Batch", "ListBatchByAssembly", "BatchID_ID"),
                        ("Batch", "ListBatchCountsByRouteStep", "BatchID")],
    "equipid":         [("Wip", "ListWipRouteStepBySerial", "Equipment_ID"),
                        ("EquipmentSetup", "GetActiveEquipmentSetupByAssemblyId", "Equipment_ID")],
    "panelid":         [("Panel", "ListPanelSerialByWipId", "Panel_ID")],
    "wipid":           [("Wip", "ListWipRouteStepBySerial", "WIP_ID"),
                        ("Panel", "ListPanelSerialByWipId", "WIP_ID")],
    "stepinstance":    [("Route", "ListRouteStep", "Description"),
                        ("Route", "ListRouteStep", "Descr")],
    "processstep":     [("Route", "ListRouteStep", "StepName"),
                        ("Route", "ListRouteStep", "Description"),
                        ("Test", "ListTestDataWithinTime", "StepText")],
    "factorymarouteid": [("Route", "ListRouteStep", "FactoryMARoute_ID")],
    "fmaroute":        [("Route", "ListRouteStep", "RouteName")],
    "custid":          [("Customer", "ListCustomer", "Customer_ID")],
    "customerid":      [("Customer", "ListCustomer", "Customer_ID")],
    "usrid":           [("Route", "ListRouteStep", "UserID_ID")],
    "assemid":         [("Assembly", "ListAssembly", "Assembly_ID"),
                        ("EquipmentSetup", "GetActiveEquipmentSetupByAssemblyId", "Assembly_ID")],
    "setupid":         [("EquipmentSetup", "GetNewSetups", "EquipmentSetup_ID"),
                        ("EquipmentSetup", "GetActiveEquipmentSetupByAssemblyId", "EquipmentSetup_ID")],
    "equipmentsetupid": [("EquipmentSetup", "GetNewSetups", "EquipmentSetup_ID")],
    "bomid":           [("Assembly", "ListAssembly", "BOM_ID")],
    "serialnumber":    [("Panel", "ListPanelSerialByWipId", "SerialNumber"),
                        ("Test", "ListTestDataWithinTime", "SerialNumber")],
    "panelserialnumber": [("Panel", "ListPanelSerialByWipId", "Panel")],
    "stepid":          [("Route", "ListRouteStep", "Step_ID")],
}
N_CAND = 4


def fetch(ctrl, meth, seeds, timeout=90):
    ep = next((e for e in endpoints() if e["method"] == meth and e["controller"] == ctrl), None)
    if not ep:
        return []
    body = {}
    for k, v in ep["body"].items():
        body[k] = seeds.get(norm(k), v) if isinstance(v, str) and v.strip() in ("", "0") else v
    try:
        _, rows = call(ctrl, meth, body, timeout=timeout)
        return rowlist(rows)
    except Exception:
        return []


def build_candidates(seeds):
    """Pull several REAL, non-zero values for every param type we know a source for."""
    cache, cands = {}, defaultdict(list)
    for param, srcs in SOURCES.items():
        for ctrl, meth, col in srcs:
            key = (ctrl, meth)
            if key not in cache:
                cache[key] = fetch(ctrl, meth, seeds)
                time.sleep(0.2)
            for r in cache[key]:
                v = r.get(col)
                if v in (None, "", 0, "0"):
                    continue                      # skip the zero rows that fooled us
                sv = str(v)
                if sv not in cands[param]:
                    cands[param].append(sv)
                if len(cands[param]) >= N_CAND:
                    break
            if len(cands[param]) >= N_CAND:
                break
    return cands


def main():
    seeds = base_seeds()
    for f in ("mes_sweep_results.json", "mes_sweep3_results.json", "mes_sweep4_results.json"):
        p = HERE / f
        if p.exists():
            for r in json.loads(p.read_text(encoding="utf-8"))["results"]:
                for k, v in (r.get("sample") or {}).items():
                    nk = norm(k)
                    if v not in (None, "", "0") and nk not in seeds:
                        seeds[nk] = str(v)

    print("sourcing real candidate values...")
    cands = build_candidates(seeds)
    for k in sorted(cands):
        print(f"  {k:20s} {cands[k][:3]}")
    print()

    ok = set()
    for f in ("mes_sweep_results.json", "mes_sweep2_results.json", "mes_sweep3_results.json",
              "mes_sweep4_results.json", "mes_probe_chained_results.json"):
        p = HERE / f
        if p.exists():
            for r in json.loads(p.read_text(encoding="utf-8"))["results"]:
                cols = r.get("columns") or []
                if r.get("ok") or (r.get("http") == 200 and (r.get("rows") or 0) > 0
                                   and "ExceptionMessage" not in cols):
                    ok.add((r.get("controller"), r.get("method")))

    todo = [e for e in endpoints() if (e["controller"], e["method"]) not in ok]
    print(f"cracking {len(todo)} endpoints - up to {N_CAND} real values per param\n")

    results, newly = [], 0
    for i, e in enumerate(todo, 1):
        rec = {"folder": e["folder"], "controller": e["controller"], "method": e["method"]}
        won = None
        # which params can we vary?
        varying = [k for k in e["body"]
                   if isinstance(e["body"][k], str)
                   and e["body"][k].strip() in ("", "0", "string")
                   and cands.get(norm(k))]
        rounds = max((len(cands[norm(k)]) for k in varying), default=1)
        for idx in range(min(rounds, N_CAND)):
            body = {}
            for k, v in e["body"].items():
                nk = norm(k)
                if isinstance(v, str) and v.strip() in ("", "0", "string"):
                    c = cands.get(nk)
                    if c:
                        body[k] = c[min(idx, len(c) - 1)]
                    elif seeds.get(nk):
                        body[k] = seeds[nk]
                    elif any(f in nk for f in ("partialkey", "filter", "mask", "name")):
                        body[k] = "%"
                    else:
                        body[k] = v
                else:
                    body[k] = v
            try:
                code, rows = call(e["controller"], e["method"], body)
                rl = rowlist(rows)
                if code == 200 and rl:
                    won = {"rows": len(rl), "columns": list(rl[0].keys()),
                           "sample": {k: str(v)[:40] for k, v in list(rl[0].items())[:12]},
                           "body": body, "candidate_index": idx}
                    break
                if is_error(rows):
                    rec["last_err"] = str(rows.get("ExceptionMessage")
                                          or rows.get("Message"))[:140]
                else:
                    rec["last_http"] = code
            except Exception as ex:
                rec["last_err"] = str(ex)[:110]
            time.sleep(0.3)
        if won:
            rec.update(won)
            rec["ok"] = True
            newly += 1
        else:
            rec["ok"] = False
        results.append(rec)
        mark = "CRACKED" if won else "       "
        print(f"[{i:3d}/{len(todo)}] {mark} {e['controller']}/{e['method'][:36]:36s} "
              f"rows={rec.get('rows','-')}", flush=True)

    OUT.write_text(json.dumps({"results": results, "newly_ok": newly,
                               "candidates": {k: v for k, v in cands.items()}},
                              indent=1), encoding="utf-8")
    print("\n" + "=" * 66)
    print(f"CRACK PASS DONE   newly cracked: {newly}")
    print(f"TOTAL WORKING: {len(ok) + newly}")
    print("\nstill resisting:")
    for r in results:
        if not r.get("ok"):
            why = r.get("last_err") or f"http {r.get('last_http','?')}"
            print(f"  {r['controller']}/{r['method'][:36]:36s} {str(why)[:80]}")


if __name__ == "__main__":
    main()
