"""workcell_map — the authoritative workcell dictionary for the ecosystem.

One row per workcell (customer) with its MES customer id + name/division, plant,
region, and whether it carries planner demand. A tiny (~48-row), slow-changing
reference mart — every module joins this instead of re-deriving customer<->MES-id
<->plant ad hoc. Regenerate with `python -m modules.cycle_time.workcell_map`.

Sources (all already in the repo):
  - mes_assembly_map.parquet   -> customer + MES customer_id
  - customer_plant.parquet     -> dominant plant (Plant-1 override already baked in)
  - planner_runners.parquet    -> has_planner_demand
  - Customer/ListCustomer (MES)-> CustomerName, DivisionName, Active (optional; skipped if MES down)
"""
import logging
import pandas as pd

from modules.cycle_time.config import CT_MART

log = logging.getLogger(__name__)

OUT = CT_MART["mes_assembly_map"].parent / "workcell_map.parquet"

# Plant -> region (mirrors _REGIONS in api/routers/ebuild.py). Plants not listed
# (Plant 3/8) have no region and show only in the "Overall" rollup.
_REGION = {"JBK": "Batu Kawan", "Plant 1": "Penang Island", "JPE": "Penang Island"}


def build() -> pd.DataFrame:
    base = CT_MART["mes_assembly_map"].parent.parent / "ebuild"
    amap = pd.read_parquet(CT_MART["mes_assembly_map"])
    cp = pd.read_parquet(base / "customer_plant.parquet")
    plan = pd.read_parquet(base / "planner_runners.parquet")

    # one MES id per workcell
    df = amap.drop_duplicates("customer")[["customer", "customer_id"]].rename(
        columns={"customer": "workcell", "customer_id": "mes_customer_id"})

    # dominant plant (Plant-1 override already applied when customer_plant was built).
    # Join case-insensitively — mes_assembly_map keeps config casing ("Masimo") while
    # customer_plant is upper ("MASIMO").
    dom = cp.sort_values("units", ascending=False).drop_duplicates("customer")
    plant_by = {str(c).strip().lower(): p for c, p in zip(dom["customer"], dom["plant"])}
    df["plant"] = df["workcell"].str.strip().str.lower().map(plant_by)
    df["region"] = df["plant"].map(_REGION)

    planner_set = {str(c).strip().lower() for c in plan["customer"].unique()}
    df["has_planner_demand"] = df["workcell"].str.strip().str.lower().isin(planner_set)

    # MES name/division/active — authoritative, but optional (MES is flaky). String params.
    df["mes_customer_name"] = None
    df["division"] = None
    df["active"] = None
    try:
        from modules.cycle_time.mes_webapi import post
        cust = {str(r.get("Customer_ID")): r for r in
                post("Customer", "ListCustomer", {"partialKey": "", "active": "1", "langId": "0"})}
        for i, cid in df["mes_customer_id"].items():
            r = cust.get(str(cid))
            if r:
                df.at[i, "mes_customer_name"] = r.get("CustomerName")
                df.at[i, "division"] = r.get("DivisionName")
                df.at[i, "active"] = r.get("Active")
    except Exception as e:  # MES down -> ship id/plant/region only, fill names on next run
        log.warning("workcell_map: MES ListCustomer unavailable (%s) - name/division left blank", e)

    # ── PULL ORDER — smallest workcell first ─────────────────────────────────
    # Every MES pull loops workcells in this order: small ones bank quick wins and
    # surface a broken param in seconds, giants (KEYSIGHT/ARISTA/LAMRESEARCH) go
    # last so an interruption never costs the whole run. model_count = assemblies
    # in mes_assembly_map, the best size proxy we have before pulling anything.
    counts = amap.groupby("customer")["number"].nunique()
    df["model_count"] = df["workcell"].map(counts).fillna(0).astype(int)
    df = df.sort_values(["model_count", "workcell"]).reset_index(drop=True)
    df["pull_order"] = range(1, len(df) + 1)

    df.to_parquet(OUT, index=False)
    log.info("workcell_map.parquet written: %d workcells -> %s", len(df), OUT)
    return df


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
    out = build()
    assert out["mes_customer_id"].notna().all(), "every workcell must have a MES id"
    assert out["workcell"].is_unique, "one row per workcell"
    assert out["pull_order"].is_monotonic_increasing, "pull_order must be 1..N in row order"
    assert out["model_count"].is_monotonic_increasing, "rows must run smallest workcell first"
    print(out.to_string(index=False))
