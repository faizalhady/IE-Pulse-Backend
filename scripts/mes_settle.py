"""
mes_settle.py — hand-targeted fixes for the 11 'solvable' endpoints.

Generic passes are exhausted; each of these needs a specific insight:

  * matched assembly+customer  -> ListAssembly gives Assembly_ID AND Customer_ID
                                  on the SAME row, so the pair is consistent.
  * @Lane                      -> undocumented param; source Lane from equipment
                                  setup rows, else try common values.
  * equiSetupId                -> the API's own typo (equi, not equip).
  * numeric BatchID_ID         -> re-source batches; the string BatchID is wrong.
  * panel serial               -> Panel column, NOT a board SerialNumber.
  * CustomerName/DivisionName  -> never the encoded int Customer/Division.

Read-only.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).parent))

import datetime as dt  # noqa: E402

from mes_sweep import call, rowlist  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "mes_settle_results.json"
results = []


def show(label, ctrl, meth, body, timeout=45):
    try:
        code, rows = call(ctrl, meth, body, timeout=timeout)
        rl = rowlist(rows)
        err = ""
        if isinstance(rows, dict) and "ExceptionMessage" in rows:
            err = str(rows["ExceptionMessage"])[:90]
        ok = bool(code == 200 and rl)
        print(f"  {'CRACKED' if ok else '       '} {meth[:34]:34s} {code} rows={len(rl):<4} "
              f"{('cols=' + ','.join(list(rl[0].keys())[:5])) if rl else err}")
        results.append({"method": f"{ctrl}/{meth}", "ok": ok, "http": code,
                        "rows": len(rl), "body": body,
                        "columns": list(rl[0].keys()) if rl else [], "err": err})
        return rl
    except Exception as e:
        print(f"          {meth[:34]:34s} EXC {str(e)[:70]}")
        results.append({"method": f"{ctrl}/{meth}", "ok": False, "err": str(e)[:120]})
        return []


def main():
    now = dt.datetime.now()
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # ── 1. matched assembly + customer, straight from ONE ListAssembly row ────
    print("\n[1] matched assembly+customer (ListAssembly gives both on one row)")
    asm_rows = []
    for cust in ("141", "143", "155", "7"):
        rl = rowlist(call("Assembly", "ListAssembly",
                          {"custId": cust, "active": "1", "partialKey": "%",
                           "langId": "0"}, timeout=90)[1])
        if rl:
            asm_rows = rl
            print(f"  sourced {len(rl)} assemblies for custId={cust}")
            break
    if asm_rows:
        r = asm_rows[0]
        aid, cid = str(r.get("Assembly_ID")), str(r.get("Customer_ID"))
        print(f"  using Assembly_ID={aid} with its OWN Customer_ID={cid}")
        show("", "Assembly", "GetAssemblyProgressionRecursive",
             {"assemId": aid, "custId": cid})
        show("", "Assembly", "GetParentAssemblyRecursive",
             {"assemId": aid, "custId": cid})
        show("", "Container", "GetBoxContents", {"custId": cid, "langId": "0"})
        show("", "Container", "GetBoxDataByBoard", {"custId": cid, "langId": "0"})

    # ── 2. @Lane — undocumented param ────────────────────────────────────────
    print("\n[2] Equipment/GetEquipmentById + the undocumented @Lane")
    setup = rowlist(call("EquipmentSetup", "GetActiveEquipmentSetupByAssemblyId",
                         {"assemId": "", "langId": "0"})[1])
    lanes, equips = [], []
    for r in setup[:80]:
        for k, v in r.items():
            if k.lower() == "lane" and v not in (None, "", 0):
                lanes.append(str(v))
            if k == "Equipment_ID" and v not in (None, "", 0, "0"):
                equips.append(str(v))
    lanes = list(dict.fromkeys(lanes)) or ["1", "0", "A"]
    equips = list(dict.fromkeys(equips))[:3]
    print(f"  lanes found: {lanes[:4]}   equipment ids: {equips}")
    for eq in equips[:2]:
        for lane in lanes[:3]:
            rl = show("", "Equipment", "GetEquipmentById",
                      {"equipId": eq, "Lane": lane, "lane": lane, "langId": "0"})
            if rl:
                break
        else:
            continue
        break

    # ── 3. equiSetupId — the API's own typo ──────────────────────────────────
    print("\n[3] GetEquipmentSetupGRNQty - the API misspells it 'equiSetupId'")
    sids = [str(r.get("EquipmentSetup_ID")) for r in setup[:20]
            if r.get("EquipmentSetup_ID") not in (None, "", 0, "0")]
    for sid in list(dict.fromkeys(sids))[:3]:
        if show("", "Equipment", "GetEquipmentSetupGRNQty",
                {"equiSetupId": sid, "equipSetupId": sid, "usrId": "142", "langId": "0"}):
            break

    # ── 4. numeric BatchID_ID, re-sourced ────────────────────────────────────
    print("\n[4] Batch endpoints need the NUMERIC BatchID_ID, not the string BatchID")
    batches = []
    if asm_rows:
        for r in asm_rows[:6]:
            rl = rowlist(call("Batch", "ListBatchByAssembly",
                              {"custId": str(r.get("Customer_ID")),
                               "assemId": str(r.get("Assembly_ID")),
                               "langId": "0"})[1])
            if rl:
                batches = rl
                print(f"  {len(rl)} batches for assembly {r.get('Number')}")
                break
            time.sleep(0.2)
    bids = [str(b.get("BatchID_ID")) for b in batches[:8]
            if b.get("BatchID_ID") not in (None, "", 0, "0")]
    print(f"  numeric BatchID_ID candidates: {bids[:4]}")
    for bid in bids[:3]:
        if show("", "Batch", "GetBatchById", {"batchId": bid, "langId": "0"}):
            break
    for bid in bids[:3]:
        if show("", "Batch", "ListBatchAssemblyQty", {"batchId": bid, "langId": "0"}):
            break

    # ── 5. PANEL serial, not a board serial ──────────────────────────────────
    print("\n[5] GetPanelSerializeResult wants a PANEL serial (Panel column)")
    wip = rowlist(call("Test", "ListTestDataWithinTime",
                       {"startTime": iso(now - dt.timedelta(minutes=25)),
                        "endTime": iso(now), "maxCount": "30"})[1])
    panels = []
    if wip:
        w = rowlist(call("Wip", "GetWipBySerial",
                         {"serialNumber": wip[0]["SerialNumber"],
                          "custId": "", "langId": "0"})[1])
        if w and w[0].get("Wip_ID"):
            panels = rowlist(call("Panel", "ListPanelSerialByWipId",
                                  {"wipId": str(w[0]["Wip_ID"]), "langId": "0"})[1])
    pser = list(dict.fromkeys([str(p.get("Panel")) for p in panels
                               if p.get("Panel") not in (None, "", "0")]))[:3]
    print(f"  panel serials: {pser}")
    for ps in pser:
        if show("", "Test", "GetPanelSerializeResult",
                {"panelSerialNumber": ps, "serialNumber": ps, "langId": "0"}):
            break

    # ── 6. CustomerName / DivisionName, never the encoded ints ───────────────
    print("\n[6] Test endpoints want CustomerName/DivisionName (not encoded ints)")
    for r in wip[:4]:
        rl = show("", "Test", "GetLastTestResult",
                  {"serialNumber": r["SerialNumber"], "customerName": r["Customer"],
                   "division": r["Division"], "processStep": r["StepText"], "langId": "0"})
        if rl:
            break
        time.sleep(0.3)

    # ── 7. FactoryMARoute_ID from a route that really exists ─────────────────
    print("\n[7] ListRouteStepByFactoryMARoute - real FactoryMARoute_ID")
    rs = rowlist(call("Route", "ListRouteStep", {"factory": "", "langId": "0"}, timeout=120))
    fids = list(dict.fromkeys([str(r.get("FactoryMARoute_ID")) for r in rs[:2000]
                               if r.get("FactoryMARoute_ID") not in (None, "", 0, "0")]))[:4]
    print(f"  FactoryMARoute_ID candidates: {fids}")
    for fid in fids:
        if show("", "Route", "ListRouteStepByFactoryMARoute",
                {"fmaRouteId": fid, "FactoryMARoute_ID": fid, "langId": "0"}):
            break

    OUT.write_text(json.dumps(results, indent=1), encoding="utf-8")
    got = sum(1 for r in results if r.get("ok"))
    print("\n" + "=" * 66)
    print(f"SETTLE PASS: {got} newly cracked out of {len(results)} attempts")
    for r in results:
        if r.get("ok"):
            print(f"  + {r['method']}  rows={r['rows']}")


if __name__ == "__main__":
    main()
