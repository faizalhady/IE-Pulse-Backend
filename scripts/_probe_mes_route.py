"""Does MES expose the CONFIGURED route (every step it defines), not just scans?

production_scan only holds steps that RAN in a one-month window, so every
"completeness" number we produce is measured against what happened recently, not
against what MES says the model should do. mes_route.py pulls the route master
and has never been imported by anything.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sys, logging, pandas as pd
logging.basicConfig(level=logging.WARNING)
from modules.cycle_time.mes_route import get_assembly_routes, get_route_steps
from modules.cycle_time.config import CT_MART

amap = pd.read_parquet(CT_MART["mes_assembly_map"])
lam = amap[amap["customer"].str.upper().str.replace(" ", "") == "LAMRESEARCH"]
print(f"LAM RESEARCH models in the MES assembly map: {len(lam):,}")

for _, r in lam.head(3).iterrows():
    aid, num = r["assembly_id"], r["number"]
    try:
        routes = get_assembly_routes(aid)
    except Exception as e:
        print(f"  {num:<22} routes FAILED: {type(e).__name__} {str(e)[:60]}"); continue
    print(f"\n  {num}  (assembly_id {aid}) -> {len(routes)} configured route(s)")
    for rt in routes[:2]:
        try:
            steps = get_route_steps(rt["fma_route_id"])
        except Exception as e:
            print(f"     {rt['route_name']}: steps FAILED {str(e)[:50]}"); continue
        print(f"     {rt['route_name']!r} ({rt['factory']}/{rt['ma']}) -> {len(steps)} STEPS")
        print(f"        {' > '.join(s['step'] for s in steps[:12])}")
