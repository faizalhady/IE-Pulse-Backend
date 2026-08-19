import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requests, urllib3
urllib3.disable_warnings()
from modules.cycle_time.client import _headers
from modules.cycle_time.config import BASE_URL, SITE_CODE

CANDIDATES = [
    ("Processes/BaseProcessNames",   {}),
    ("Report/WorkcenterConfig",      {"siteCode": SITE_CODE}),
    ("Report/SubWorkcenterConfig",   {"siteCode": SITE_CODE}),
    ("mpt/v2/SubWorkcenters",        {"siteCode": SITE_CODE}),
    ("mpt/v2/ProcessLinks",          {"siteCode": SITE_CODE}),
    ("Report/GetProcessReport",      {"site": SITE_CODE}),
    ("Report/GetProcessLinksReport", {"site": SITE_CODE, "customer": "LAMRESEARCH"}),
]
h = _headers()
for path, params in CANDIDATES:
    try:
        r = requests.get(f"{BASE_URL}/api/{path}", headers=h, params=params,
                         timeout=25, verify=False)
        if r.status_code != 200:
            print(f"{r.status_code:<5} {path:<32} {r.text[:60]}", flush=True); continue
        d = r.json()
        body = d if isinstance(d, list) else (d.get("data") or d.get("Data") or d)
        n = len(body) if isinstance(body, list) else "obj"
        keys = list(body[0].keys())[:8] if isinstance(body, list) and body and isinstance(body[0], dict) else ""
        print(f"200   {path:<32} rows={str(n):<7} {keys}", flush=True)
    except Exception as e:
        print(f"TIMEOUT/ERR  {path:<32} {type(e).__name__}", flush=True)
