"""
build_mes_customers.py - the Jabil customer master, from MES itself.

    python scripts/build_mes_customers.py            # print, write nothing
    python scripts/build_mes_customers.py --write    # write data/reference/mes_customers.csv

WHY A FILE AND NOT config.py
    CT_CUSTOMERS is the CYCLE TIME scope - the 40 workcells that module works on.
    This is a different thing: every customer MES knows about, 132 of them. Pasting
    132 dicts into a module's config would say "cycle time owns this list", and it
    does not. Other modules will want the same list with a different subset ticked,
    so it lives in data/reference/ next to workcell_alias.csv and is refreshed by
    re-running this.

WHAT "PENANG" MEANS HERE
    MES ListCustomer has no site or plant column - it is the global master. So
    "does this customer build in Penang" cannot be read from it. It is DERIVED,
    from production we have actually seen:
      * in_mes_scans  - appears in our own #21 day cache (what ran, last 3 years)
      * in_runners    - appears in the ebuild runners mart
      * plant         - from ebuild/customer_plant.parquet, names canonicalised
                        through core.plants (JBK -> Batu Kawan, JPE -> Plant 2)
    A customer with neither has no production we can find, and is a name only.

SCOPE FLAGS
    in_cycle_time marks the 40 in CT_CUSTOMERS. Every other module gets its own
    column here when it needs one - that is the point of the file. Nothing in this
    script pulls production data for a customer; it only records who exists.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.cycle_time.completion_v2 import _cnorm, post          # noqa: E402
from modules.cycle_time.config import (CT_CUSTOMERS, CT_MART,      # noqa: E402
                                       CT_MES_SCAN_DIR)
from modules.cycle_time.model_universe import norm                 # noqa: E402
from core.plants import canon as plant_canon                        # noqa: E402

OUT = ROOT / "data" / "reference" / "mes_customers.csv"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    # active=0 is the SUPERSET (132 names) - active=1 returns only 97. Verified
    # 2026-08-25: union(active=0, active=1) == active=0.
    rows = post("Customer", "ListCustomer", {"partialKey": "", "active": "0", "langId": "0"})

    # one row per customer, not per customer/division - ACCELINK alone has three
    # divisions and they are the same workcell to us.
    seen: dict = {}
    for r in rows:
        name = (r.get("CustomerName") or "").strip()
        if not name:
            continue
        k = _cnorm(name)
        d = seen.setdefault(k, {"customer": name, "mes_customer_id": r.get("Customer_ID"),
                                "divisions": set(), "active": False})
        d["divisions"].add((r.get("DivisionName") or "").strip())
        d["active"] = d["active"] or bool(r.get("Active"))

    df = pd.DataFrame([{
        "customer": v["customer"],
        "key": k,
        "mes_customer_id": v["mes_customer_id"],
        "divisions": " | ".join(sorted(x for x in v["divisions"] if x)),
        "mes_active": v["active"],
    } for k, v in seen.items()])

    cfg = {_cnorm(c["customer"]) for c in CT_CUSTOMERS}
    df["in_cycle_time"] = df["key"].isin(cfg)

    scanned = {_cnorm(p.name) for p in CT_MES_SCAN_DIR.iterdir() if p.is_dir()}
    df["in_mes_scans"] = df["key"].isin(scanned)

    try:
        run = pd.read_parquet(ROOT / "data" / "mart" / "ebuild" / "runners.parquet")
        built = {norm(c) for c in run["customer"].dropna().unique()}
    except Exception:
        built = set()
    df["in_runners"] = df["key"].map(norm).isin(built) | df["key"].isin(built)

    # plant comes from the ebuild mart, not from MES - ListCustomer has no site
    # column at all. Names are normalised through core.plants because that mart
    # mixes site codes (JBK, JPE) with plant numbers (Plant 1, Plant 3).
    try:
        cp = pd.read_parquet(ROOT / "data" / "mart" / "ebuild" / "customer_plant.parquet")
        pl = {_cnorm(c): plant_canon(p) for c, p in zip(cp["customer"], cp["plant"])}
        raw = {_cnorm(c): p for c, p in zip(cp["customer"], cp["plant"])}
        unknown = sorted({v for k, v in raw.items() if pl.get(k) is None})
        if unknown:
            print(f"WARNING plant label(s) core.plants does not know: {unknown}")
    except Exception as ex:                                    # noqa: BLE001
        print(f"WARNING no customer_plant mart ({ex}) - plant left blank")
        pl = {}
    df["plant"] = df["key"].map(pl)

    df["has_production"] = df["in_mes_scans"] | df["in_runners"]
    df = df.sort_values(["in_cycle_time", "has_production", "customer"],
                        ascending=[False, False, True]).reset_index(drop=True)

    print(f"MES customers          : {len(df)}")
    print(f"  in cycle-time scope  : {int(df['in_cycle_time'].sum())}")
    print(f"  production seen      : {int(df['has_production'].sum())}")
    print(f"  name only, no build  : {int((~df['has_production']).sum())}")
    print("\nby plant:")
    byp = (df[df["has_production"]].groupby(df["plant"].fillna("(unknown)"))
             .agg(customers=("customer", "size"), in_cycle_time=("in_cycle_time", "sum")))
    print(byp.to_string())
    print("\n== builds, but NOT in cycle-time scope ==")
    gap = df[df["has_production"] & ~df["in_cycle_time"]]
    print(gap[["customer", "mes_customer_id", "in_mes_scans", "in_runners"]].to_string(index=False)
          if len(gap) else "  (none)")

    if a.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(OUT, index=False)
        print(f"\nwrote {OUT}  ({len(df)} rows)")
    else:
        print("\nnot written - add --write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
