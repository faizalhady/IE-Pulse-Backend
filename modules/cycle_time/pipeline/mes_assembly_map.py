"""
mes_assembly_map.py  (cycle_time)
─────────────────────────────────
Builds mes_assembly_map.parquet — the name→MES Assembly_ID translator the
completion-status feature needs. IEDB knows a model by NAME; the MES route
endpoints take a numeric Assembly_ID. This mart is the bridge.

All MES Web API, cached once (no per-model live calls at query time):
  1. Customer/ListCustomer   → every MES customer + Customer_ID
  2. match IEDB config workcells → MES Customer_ID by normalised name (+ overrides)
  3. Assembly/ListAssembly(custId) → all assemblies (Number, Revision, Assembly_ID)
  4. write (customer, number, revision, assembly_id, customer_id)

Run:  python -m modules.cycle_time.pipeline.mes_assembly_map          # live build
      python -m modules.cycle_time.pipeline.mes_assembly_map --selftest  # offline logic check

Requires MES_WEBAPI_KEY in .env for the live build.
"""

import logging
import re
import sys

import pandas as pd

from modules.cycle_time.config import CT_CUSTOMERS, CT_MART
from modules.cycle_time.mes_webapi import post, MESWebApiError

log = logging.getLogger(__name__)


def _cnorm(s: str) -> str:
    """Customer-name match key: uppercase, alphanumeric only. Deliberately NOT
    canon() (that strips trailing letters and would erase whole customer names)."""
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


# IEDB config workcell → MES customer name, only where _cnorm won't match on its
# own. Grow this from the "unmatched" WARNING the live build logs.
_MES_NAME_OVERRIDE: dict[str, str] = {
    # "TMO": "THERMO FISHER",   # example — fill in from a real unmatched run
}


def _match_customers(mes_custs: list[dict]) -> dict[str, object]:
    """IEDB config workcell → MES Customer_ID. MES has DUPLICATE customer names
    (e.g. two 'KEYSIGHT', two 'FORTALEZA') distinguished only by division, so we
    match on (name, division) first and fall back to name-only. Logs misses."""
    by_name_div: dict[tuple, object] = {}
    by_name: dict[str, object] = {}
    for c in mes_custs:
        # MES `Customer`/`Division` are encoded ints; human names are the *Name cols.
        name, div, cid = c.get("CustomerName") or "", c.get("DivisionName") or "", c.get("Customer_ID")
        if cid is not None and name:
            by_name_div.setdefault((_cnorm(name), _cnorm(div)), cid)
            by_name.setdefault(_cnorm(name), cid)

    out, missed = {}, []
    for c in CT_CUSTOMERS:
        name = c["customer"]
        target = _MES_NAME_OVERRIDE.get(name, name)
        # config `division` carries a trailing */# → _cnorm strips it.
        cid = by_name_div.get((_cnorm(target), _cnorm(c.get("division", "")))) or by_name.get(_cnorm(target))
        if cid is None:
            missed.append(name)
        else:
            out[name] = cid
    if missed:
        log.warning("No MES Customer_ID for %d workcell(s) - add to _MES_NAME_OVERRIDE: %s",
                    len(missed), ", ".join(missed))
    return out


def _list_customers() -> list[dict]:
    return post("Customer", "ListCustomer", {"partialKey": "", "active": "1", "langId": "0"})


def _list_assemblies(cust_id) -> list[dict]:
    return post("Assembly", "ListAssembly",
                {"custId": str(cust_id), "active": "1", "partialKey": "", "langId": "0"})


def run() -> bool:
    log.info("MES assembly-map build starting")
    try:
        mes_custs = _list_customers()
    except MESWebApiError as e:
        log.error("ListCustomer failed: %s", e)
        return False

    matched = _match_customers(mes_custs)
    log.info("Matched %d/%d workcells to MES Customer_ID", len(matched), len(CT_CUSTOMERS))
    if not matched:
        log.error("No workcells matched a MES customer - map not written.")
        return False

    rows = []
    for name, cid in matched.items():
        try:
            asms = _list_assemblies(cid)
        except MESWebApiError as e:
            log.error("  ListAssembly failed for %s (custId=%s): %s - skipping", name, cid, e)
            continue
        for a in asms:
            rows.append({
                "customer": name,
                "number": a.get("Number"),
                "revision": a.get("Revision"),
                "assembly_id": a.get("Assembly_ID"),
                "customer_id": cid,
            })
        log.info("  %s: %d assemblies", name, len(asms))

    if not rows:
        log.error("No assemblies fetched - map not written.")
        return False

    df = pd.DataFrame(rows)
    df = df[df["assembly_id"].notna() & df["number"].notna()]
    CT_MART["mes_assembly_map"].parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(CT_MART["mes_assembly_map"], index=False)
    log.info("mes_assembly_map.parquet written (%d rows, %d workcells) -> %s",
             len(df), df["customer"].nunique(), CT_MART["mes_assembly_map"])
    return True


def _selftest():
    """Offline check of the matching logic — no network / key needed."""
    assert _cnorm("BECKMAN COULTER") == "BECKMANCOULTER"     # canon() would erase this
    assert _cnorm("K_CTEC") == "KCTEC"
    assert _cnorm("MICRON SIG") == _cnorm("micron  sig")

    # Duplicate-name disambiguation: two 'KEYSIGHT' customers, division decides.
    fake_mes = [
        {"CustomerName": "KEYSIGHT", "DivisionName": "Module",   "Customer_ID": 114},
        {"CustomerName": "KEYSIGHT", "DivisionName": "KEYSIGHT", "Customer_ID": 7},
        {"CustomerName": "BECKMAN_COULTER", "DivisionName": "BECKMAN COULTER", "Customer_ID": 391},
    ]
    by_name_div, by_name = {}, {}
    for c in fake_mes:
        by_name_div.setdefault((_cnorm(c["CustomerName"]), _cnorm(c["DivisionName"])), c["Customer_ID"])
        by_name.setdefault(_cnorm(c["CustomerName"]), c["Customer_ID"])
    # config KEYSIGHT division 'KEYSIGHT*' → picks id=7, NOT the first-seen id=114
    assert by_name_div[(_cnorm("KEYSIGHT"), _cnorm("KEYSIGHT*"))] == 7
    assert by_name.get(_cnorm("KEYSIGHT")) == 114              # name-only would be wrong
    assert by_name_div[(_cnorm("BECKMAN COULTER"), _cnorm("BECKMAN COULTER*"))] == 391
    print("mes_assembly_map self-check OK")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.exit(0 if run() else 1)
