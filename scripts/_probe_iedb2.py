import sys, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requests, urllib3, pandas as pd
urllib3.disable_warnings()
from modules.cycle_time.client import _headers
from modules.cycle_time.config import BASE_URL, SITE_CODE, CT_MART

norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())
r = requests.get(f"{BASE_URL}/api/Report/SubWorkcenterConfig", headers=_headers(),
                 params={"siteCode": SITE_CODE}, timeout=60, verify=False)
d = pd.DataFrame(r.json())
print("SubWorkcenterConfig:", d.shape, "\ncolumns:", list(d.columns))
print(d.head(3).to_string()[:400])
print()
print("distinct:  workcells", d["Workcell"].nunique(),
      " process", d["Process"].nunique(), " alias", d["Alias"].nunique(),
      " subworkcenter", d["SubWorkCenter"].nunique())
print()
raw = pd.read_parquet(CT_MART["raw"], columns=["customer", "alias"]).dropna()
print("--- vs raw.parquet (TIMED steps only) ---")
for wc in ["LAMRESEARCH", "KEYSIGHT", "MICRONSIG"]:
    cfg = d[d["Workcell"].map(norm) == wc]
    rw = raw[raw["customer"].map(norm) == wc]
    extra = set(cfg["Alias"].map(norm)) - set(rw["alias"].map(norm))
    print(f"  {wc:<14} config aliases {cfg['Alias'].nunique():>4} | raw aliases {rw['alias'].nunique():>4} "
          f"| in config but NEVER timed: {len(extra)}")
