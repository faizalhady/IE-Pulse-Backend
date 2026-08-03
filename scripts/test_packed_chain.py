"""SHORT proof of the completed-unit chain before the full run:
   #126 discover serials -> #32 confirm Packed -> #132 get route.
Runs on a handful of models only. Read-only."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
from modules.cycle_time.mes_webapi import post, MESWebApiError

# map customer NAME (as #126 returns) -> MES custId, from the assembly map
amap = pd.read_parquet("data/mart/cycle_time/mes_assembly_map.parquet")
import re
cn = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())
cid_by = {}
for c, i in zip(amap["customer"], amap["customer_id"]):
    cid_by.setdefault(cn(c), int(i))


def box_status(cid, serial):
    """#32 -> (ContainerStatusText, ContainerNumber) or (None, err)."""
    try:
        r = post("Container", "GetBoxDataByBoard", {"custId": str(cid), "serial": str(serial), "langId": "0"})
    except MESWebApiError as e:
        return None, str(e)[:40]
    d = r[0] if isinstance(r, list) and r else (r if isinstance(r, dict) else None)
    if not d:
        return None, "no box"
    return d.get("ContainerStatusText"), d.get("ContainerNumber")


def route(cid, serial):
    h = post("Wip", "BoardHistoryReport", {"custId": str(cid), "serial": str(serial),
                                           "useMultiPartBarCode": "", "lang": ""})
    steps = [str(x.get("Test_Process") or "") for x in h if "/" in str(x.get("Test_Process") or "")]
    reached = any(k in s.upper() for s in steps for k in ("PACKOUT", "OQA", "OBA"))
    return len(steps), reached, steps[-1] if steps else "?"


# one #126 window, ~50 days back
s = (datetime.now() - timedelta(days=50)).replace(hour=10, minute=0, second=0, microsecond=0)
r = post("Test", "ListTestDataWithinTime",
         {"startTime": s.strftime("%Y-%m-%d %H:%M:%S"),
          "endTime": (s + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"), "maxCount": "100000"})
d = pd.DataFrame(r)
print(f"#126 window {s:%Y-%m-%d %H:%M}: {len(d)} rows, {d.Assembly.nunique()} models\n")

# take 8 models across a few customers we have ids for
d["cid"] = d["Customer"].map(lambda c: cid_by.get(cn(c)))
d = d[d["cid"].notna()]
picks = d.drop_duplicates("Assembly").head(8)

print(f"{'customer':16} {'model':16} {'serial':22} {'#32 status':12} {'#132':>12}")
print("-" * 84)
packed_ok = 0
for row in picks.itertuples():
    # try up to 3 serials of this model until one confirms Packed
    serials = d[d["Assembly"] == row.Assembly]["SerialNumber"].head(3).tolist()
    hit = None
    for sn in serials:
        st, box = box_status(int(row.cid), sn)
        if st == "Packed":
            hit = (sn, st, box)
            break
        last = (sn, st or "—", box)
    sn, st, box = hit or last
    if st == "Packed":
        n, reached, lastst = route(int(row.cid), sn)
        r132 = f"{n} steps {'✓PACK' if reached else 'no-pack'}"
        packed_ok += 1
    else:
        r132 = "(skipped)"
    print(f"{str(row.Customer)[:16]:16} {str(row.Assembly)[:16]:16} {str(sn)[:22]:22} {str(st)[:12]:12} {r132:>12}")

print(f"\n{packed_ok}/{len(picks)} models got a #32-confirmed Packed serial + #132 route")
