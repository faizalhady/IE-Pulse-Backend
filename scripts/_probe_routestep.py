import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from modules.cycle_time.mes_webapi import post, MESWebApiError
W = "%"
TRIES = [
    ("fac+ma+route+step", {"factoryName": W, "manufacturingAreaName": W, "routeName": W, "stepName": W, "langId": "0"}),
    ("fac+ma+route+step2", {"factoryName": W, "maName": W, "routeName": W, "step": W, "langId": "0"}),
    ("+usrId 142",        {"factoryName": W, "maName": W, "routeName": W, "step": W, "usrId": "142", "langId": "0"}),
    ("factory only+usr",  {"factory": W, "usrId": "142", "langId": "0"}),
    ("fmaRoute style",    {"fmaRoute": W, "factoryName": W, "langId": "0"}),
    ("PascalCase all",    {"FactoryName": W, "ManufacturingAreaName": W, "RouteName": W, "StepName": W, "LangId": "0"}),
]
for label, p in TRIES:
    try:
        rows = post("Route", "ListRouteStep", p)
        print(f"OK  {label:<20} {len(rows):,} rows  {list(rows[0].keys())[:6] if rows else ''}")
        break
    except MESWebApiError as e:
        m = str(e); m = m[m.find("expects"):m.find("expects")+60] if "expects" in m else m[-70:]
        print(f"--  {label:<20} {m}")
