"""How many step names does MES DEFINE for a workcell, vs how many we ever see?

Routes are shared: 'LAM RESEARCH Route 10' is the same 689 steps for all 269
models on it. So the route master is a per-ROUTE pull, not per-model - cheap.
"""
import sys, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logging, pandas as pd
logging.basicConfig(level=logging.WARNING)
from modules.cycle_time.mes_route import get_assembly_routes, get_route_steps
from modules.cycle_time.config import CT_MART

norm = lambda s: re.sub(r"\s+", " ", str(s).strip().upper())
amap = pd.read_parquet(CT_MART["mes_assembly_map"])
lam = amap[amap["customer"].str.upper().str.replace(" ", "") == "LAMRESEARCH"]

# walk a sample of models to discover the DISTINCT routes this workcell runs
routes, seen = {}, set()
for _, r in lam.head(40).iterrows():
    try:
        for rt in get_assembly_routes(r["assembly_id"]):
            routes.setdefault(rt["fma_route_id"], rt)
    except Exception:
        continue
print(f"distinct routes found from 40 models: {len(routes)}")

allsteps = {}
for fid, rt in routes.items():
    try:
        st = get_route_steps(fid)
    except Exception as e:
        print(f"  {rt['route_name']}: FAILED {str(e)[:40]}"); continue
    names = {norm(s["step"]) for s in st if s["step"]}
    allsteps[rt["route_name"]] = names
    print(f"  {rt['route_name']:<34}{len(st):>5} steps  {len(names):>4} distinct names")

defined = set().union(*allsteps.values()) if allsteps else set()
scanned = set(pd.read_csv(r"C:\Users\4033375\Projects\docs\registry\workcell_process_raw.csv")
              .query("workcell=='LAM RESEARCH' and system=='mes_step'")["name_raw"].map(norm))
print()
print(f"MES DEFINES        {len(defined):>5} distinct step names")
print(f"we have SEEN       {len(scanned):>5} (one month of scans)")
print(f"defined, never seen{len(defined - scanned):>5}")
print(f"seen, not in these routes{len(scanned - defined):>5}")
print()
print("sample defined-but-never-seen:", sorted(defined - scanned)[:12])
